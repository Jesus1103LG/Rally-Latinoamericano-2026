#!/usr/bin/env python3
"""
Central Station Cloud Uplink & Agronomic Alert Bridge
=====================================================
Target: Master Android Station (Termux)
Mission: Reads consolidated parcel telemetry from the Master SQLite database,
         evaluates conditions with AgronomicAdvisor for farming alerts, and
         reliably forwards data to the Cloud platform (AWS IoT / ThingsBoard / MQTT Broker)
         whenever cellular 4G/LTE or satellite backhaul is accessible.
"""

import os
import sys
import time
import json
import socket
import logging
from typing import Dict, Any, List, Optional
import sqlite3

import paho.mqtt.client as mqtt

# Ensure local modules can be resolved
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from src.analytics.agronomic_advisor import AgronomicAdvisor, AlertSeverity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("CloudBridge")


class CloudBridgeService:
    def __init__(self, config_path: Optional[str] = None, db_path: Optional[str] = None):
        self.config = self._load_config(config_path)

        if db_path is None:
            base_dir = os.path.expanduser("~/station/central_data")
            self.db_path = os.path.join(base_dir, "central_telemetry.db")
        else:
            self.db_path = db_path

        self.station_id = self.config.get("central_station_id", "master_station_01")
        self.cloud_host = self.config.get("cloud_broker_host", "mqtt.cloud.example.org")
        self.cloud_port = int(self.config.get("cloud_broker_port", 8883))
        self.use_tls = bool(self.config.get("use_tls", False))
        self.username = self.config.get("username")
        self.password = self.config.get("password")
        self.topic_telemetry = self.config.get("cloud_uplink_topic", "agrometeo/telemetry")
        self.topic_alerts = self.config.get("cloud_alerts_topic", "agrometeo/alerts")
        self.batch_size = int(self.config.get("batch_size", 100))
        self.uplink_interval = float(self.config.get("uplink_interval_seconds", 120.0))
        self.max_backoff = float(self.config.get("max_backoff_seconds", 600.0))

        self.advisor = AgronomicAdvisor()
        self.current_backoff = 10.0
        self.client: Optional[mqtt.Client] = None
        self.is_connected = False

    def _load_config(self, path: Optional[str]) -> Dict[str, Any]:
        candidates = [
            path,
            os.path.expanduser("~/station/config/cloud_config.json"),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config/central/cloud_config.json")),
        ]
        for c in candidates:
            if c and os.path.exists(c):
                try:
                    with open(c, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    logger.warning("Failed loading cloud config from %s: %s", c, e)

        return {"cloud_broker_host": "127.0.0.1", "cloud_broker_port": 1883}

    def _get_db_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _is_cloud_reachable(self) -> bool:
        """Probes external cloud IP/DNS to check if cellular/satellite link is active."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(4.0)
        try:
            sock.connect((self.cloud_host, self.cloud_port))
            sock.shutdown(socket.SHUT_RDWR)
            return True
        except (socket.timeout, OSError):
            return False
        finally:
            sock.close()

    def _fetch_pending_cloud_records(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Queries records that haven't been transmitted to the cloud yet."""
        if not os.path.exists(self.db_path):
            return []

        query = """
            SELECT * FROM consolidated_telemetry 
            WHERE cloud_synced = 0 
            ORDER BY id ASC 
            LIMIT ?;
        """
        with self._get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (limit,))
            return [dict(r) for r in cursor.fetchall()]

    def _fetch_pressure_3h_ago(self, node_id: str) -> Optional[float]:
        """Retrieves barometric reading from 3 hours ago for trend evaluation."""
        if not os.path.exists(self.db_path):
            return None

        query = """
            SELECT pressure_hpa FROM consolidated_telemetry 
            WHERE node_id = ? AND timestamp <= datetime('now', '-3 hours')
            ORDER BY timestamp DESC LIMIT 1;
        """
        try:
            with self._get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, (node_id,))
                row = cursor.fetchone()
                return float(row["pressure_hpa"]) if row and row["pressure_hpa"] is not None else None
        except Exception:
            return None

    def _mark_cloud_synced(self, record_ids: List[int]) -> int:
        """Atomically marks records as pushed to cloud."""
        if not record_ids:
            return 0

        placeholders = ",".join("?" for _ in record_ids)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        query = f"""
            UPDATE consolidated_telemetry 
            SET cloud_synced = 1, cloud_synced_at = ? 
            WHERE id IN ({placeholders});
        """
        with self._get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, [now_iso] + record_ids)
            conn.commit()
            return cursor.rowcount

    def _init_client(self) -> mqtt.Client:
        client = mqtt.Client(client_id=f"cloud_bridge_{self.station_id}", clean_session=True)
        if self.username:
            client.username_pw_set(self.username, self.password)
        if self.use_tls:
            client.tls_set()
        return client

    def run_uplink_cycle(self) -> int:
        records = self._fetch_pending_cloud_records(limit=self.batch_size)
        if not records:
            logger.debug("Cloud queue empty. No pending records.")
            return 0

        if not self._is_cloud_reachable():
            logger.warning("Internet backhaul / Cloud endpoint (%s) unavailable.", self.cloud_host)
            return -1

        client = self._init_client()
        try:
            client.connect(self.cloud_host, self.cloud_port, keepalive=60)
            client.loop_start()

            # Group latest record per node to evaluate agronomic alerts
            latest_per_node: Dict[str, Dict[str, Any]] = {}
            for r in records:
                latest_per_node[r["node_id"]] = r

            alerts_payload = []
            for n_id, latest in latest_per_node.items():
                p_3h = self._fetch_pressure_3h_ago(n_id)
                alerts = self.advisor.evaluate_conditions(latest, historical_pressure_3h_ago=p_3h)
                for alert in alerts:
                    alerts_payload.append({
                        "node_id": n_id,
                        "timestamp": latest.get("timestamp"),
                        "severity": alert.severity.value,
                        "code": alert.code,
                        "title": alert.title,
                        "message": alert.message,
                        "action_required": alert.action_required,
                    })

            # Publish Telemetry Batch
            uplink_data = {
                "central_station_id": self.station_id,
                "uplink_time": time.time(),
                "batch_size": len(records),
                "telemetry": records,
            }
            msg_info = client.publish(self.topic_telemetry, json.dumps(uplink_data), qos=1)
            msg_info.wait_for_publish(timeout=15.0)

            if not msg_info.is_published():
                logger.warning("Cloud publish timed out without PUBACK.")
                return -1

            # Publish Alerts (if any)
            if alerts_payload:
                logger.info("Publishing %d agronomic alerts to Cloud...", len(alerts_payload))
                alert_info = client.publish(self.topic_alerts, json.dumps(alerts_payload), qos=1)
                alert_info.wait_for_publish(timeout=10.0)

            # Two-Phase commit: Mark records in consolidated database
            record_ids = [r["id"] for r in records]
            synced_count = self._mark_cloud_synced(record_ids)
            logger.info("[✓] Successfully transmitted %d records to Cloud. Database updated.", synced_count)
            return synced_count

        except Exception as e:
            logger.error("Error during cloud uplink: %s", e, exc_info=True)
            return -1
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

    def start(self):
        logger.info("Starting Cloud Uplink Bridge for station '%s'...", self.station_id)
        while True:
            try:
                result = self.run_uplink_cycle()
                if result >= 0:
                    self.current_backoff = 10.0
                    time.sleep(self.uplink_interval)
                else:
                    logger.info("Retrying Cloud Uplink in %.1f seconds...", self.current_backoff)
                    time.sleep(self.current_backoff)
                    self.current_backoff = min(self.current_backoff * 1.5, self.max_backoff)
            except KeyboardInterrupt:
                logger.info("Cloud Bridge terminated by user.")
                break
            except Exception as e:
                logger.critical("Unexpected error in Cloud Bridge loop: %s", e, exc_info=True)
                time.sleep(15.0)


if __name__ == "__main__":
    bridge = CloudBridgeService()
    bridge.start()
