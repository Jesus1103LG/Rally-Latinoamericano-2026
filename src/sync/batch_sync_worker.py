#!/usr/bin/env python3
"""
Offline-First Store-and-Forward Batch Synchronization Worker
============================================================
Target: Field Sensor Node (Termux)
Mission: Reliably flushes un-synced SQLite records to the Master Central Node
         over agricultural local Wi-Fi / Hotspot using MQTT QoS 1.

Resilience Guarantees:
  1. Atomic Store-and-Forward: Records are ONLY flagged as synced in SQLite
     AFTER the Central Broker sends a cryptographic/TCP MQTT PUBACK.
  2. Zero Packet Loss: If Wi-Fi breaks mid-stream, pending records remain
     untouched in SQLite and will be transmitted upon reconnection.
  3. Energy-Aware Backoff: Exponential backoff with random jitter prevents
     continuous radio wakeups and battery drain when out of Wi-Fi range.
"""

import os
import sys
import time
import json
import uuid
import socket
import random
import logging
from typing import Dict, Any, List, Optional

import paho.mqtt.client as mqtt

# Ensure local modules can be resolved
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from src.storage.db import StationDatabase

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("BatchSyncWorker")


class BatchSyncWorker:
    def __init__(self, config_path: Optional[str] = None, db_path: Optional[str] = None):
        self.config = self._load_config(config_path)
        self.db = StationDatabase(db_path=db_path)

        self.node_id = self.config.get("node_id", "station_unknown")
        self.broker_host = self.config.get("central_broker_host", "192.168.43.1")
        self.broker_port = int(self.config.get("central_broker_port", 1883))
        self.batch_size = int(self.config.get("batch_size", 50))
        self.sync_interval = float(self.config.get("sync_interval_seconds", 60.0))
        self.conn_timeout = float(self.config.get("connection_timeout_seconds", 10.0))
        self.pub_timeout = float(self.config.get("publish_timeout_seconds", 15.0))
        self.max_backoff = float(self.config.get("max_backoff_seconds", 300.0))

        # MQTT Topic
        topic_template = self.config.get("batch_topic_template", "station/{node_id}/batch")
        self.topic = topic_template.format(node_id=self.node_id)

        # Backoff state
        self.current_backoff = 5.0
        self.is_connected = False
        self.client: Optional[mqtt.Client] = None

    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        """Loads configuration from JSON file or provides robust defaults."""
        search_paths = [
            config_path,
            os.path.expanduser("~/station/config/sync_config.json"),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config/node/sync_config.json")),
        ]

        for path in search_paths:
            if path and os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                        logger.info("Loaded synchronization config from %s", path)
                        return cfg
                except Exception as e:
                    logger.warning("Failed parsing config %s: %s", path, e)

        logger.info("Using default internal synchronization configuration.")
        return {
            "node_id": f"station_{socket.gethostname()}",
            "central_broker_host": "192.168.43.1",
            "central_broker_port": 1883,
            "batch_size": 50,
            "sync_interval_seconds": 60,
        }

    def _resolve_broker_candidates(self) -> List[str]:
        """
        Dynamically discovers all candidate broker IPs:
        1. Configured host in sync_config.json
        2. Dynamic Default Gateway from active network (Bluetooth PAN 192.168.44.1, Hotspot 10.174.126.180)
        3. Common fallback subnets (10.174.126.180, 192.168.44.1, 192.168.43.1, 192.168.49.1)
        """
        candidates = []
        configured = self.config.get("central_broker_host")
        if configured and configured != "auto" and configured not in candidates:
            candidates.append(configured)

        # Dynamic Default Gateway via /proc/net/route or ip route
        try:
            with open("/proc/net/route", "r") as f:
                for line in f.readlines()[1:]:
                    fields = line.strip().split()
                    if len(fields) >= 3 and fields[1] == "00000000":
                        gw_hex = fields[2]
                        gw_ip = socket.inet_ntoa(bytes.fromhex(gw_hex)[::-1])
                        if gw_ip != "0.0.0.0" and gw_ip not in candidates:
                            candidates.append(gw_ip)
        except Exception:
            pass

        # Standard agricultural Master fallbacks (Wi-Fi Hotspot, Bluetooth PAN, Wi-Fi Direct)
        for standard_gw in ["10.174.126.180", "192.168.44.1", "192.168.43.1", "192.168.49.1"]:
            if standard_gw not in candidates:
                candidates.append(standard_gw)

        return candidates

    def _is_broker_reachable(self) -> bool:
        """
        Tests reachability across candidate bearer interfaces (Wi-Fi and Bluetooth PAN).
        Automatically locks onto whichever connection is active (Zero-Touch Plug & Play).
        """
        for host in self._resolve_broker_candidates():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            try:
                sock.connect((host, self.broker_port))
                sock.shutdown(socket.SHUT_RDWR)
                if self.broker_host != host:
                    logger.info("Auto-discovered Central Master at %s:%d", host, self.broker_port)
                    self.broker_host = host
                return True
            except (socket.timeout, OSError):
                continue
            finally:
                sock.close()
        return False

    def _init_mqtt_client(self) -> mqtt.Client:
        """Instantiates and configures Paho MQTT client."""
        client_id = f"sync_{self.node_id}_{uuid.uuid4().hex[:6]}"
        client = mqtt.Client(client_id=client_id, clean_session=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        return client

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.is_connected = True
            logger.info("Connected to Central Master Broker (%s:%d)", self.broker_host, self.broker_port)
        else:
            self.is_connected = False
            logger.error("Connection to Central Broker refused with code %d", rc)

    def _on_disconnect(self, client, userdata, rc):
        self.is_connected = False
        if rc != 0:
            logger.warning("Disconnected unexpectedly from Central Broker.")

    def sync_batch(self, records: List[Dict[str, Any]]) -> bool:
        """
        Transmits one batch of records to the Central Broker using MQTT QoS 1.
        Returns True ONLY if PUBACK is received and local SQLite is updated.
        """
        if not records:
            return True

        record_ids = [r["id"] for r in records]
        batch_id = str(uuid.uuid4())

        payload = {
            "node_id": self.node_id,
            "batch_id": batch_id,
            "timestamp": time.time(),
            "record_count": len(records),
            "records": records,
        }

        json_bytes = json.dumps(payload).encode("utf-8")
        logger.info(
            "Transmitting batch %s (%d records, %d bytes) with QoS 1...",
            batch_id,
            len(records),
            len(json_bytes),
        )

        try:
            # Publish with QoS 1 (At Least Once delivery guarantee)
            msg_info = self.client.publish(self.topic, json_bytes, qos=1)

            # Block until Central Broker acknowledges with PUBACK
            msg_info.wait_for_publish(timeout=self.pub_timeout)

            if msg_info.is_published():
                # TWO-PHASE COMMIT: Broker has the batch. Now atomically flag in SQLite.
                updated_count = self.db.mark_as_synced(record_ids)
                logger.info(
                    "[✓] Batch %s confirmed by Central Broker. Marked %d records as synced in SQLite.",
                    batch_id,
                    updated_count,
                )
                return True
            else:
                logger.warning(
                    "[!] Publish timeout: PUBACK not received for batch %s. Will retry.",
                    batch_id,
                )
                return False

        except Exception as e:
            logger.error("Transmission error for batch %s: %s", batch_id, e)
            return False

    def run_sync_cycle(self) -> int:
        """
        Executes a drain cycle: flushes all pending batches while connection holds.
        Returns the number of synchronized records.
        """
        stats = self.db.get_stats()
        pending = stats["pending_sync"]
        if pending == 0:
            logger.debug("Local queue is empty. Zero pending records.")
            return 0

        logger.info("Found %d pending records awaiting synchronization.", pending)

        # 1. Check network connectivity
        if not self._is_broker_reachable():
            logger.warning(
                "Central Broker (%s:%d) is unreachable via Wi-Fi LAN.",
                self.broker_host,
                self.broker_port,
            )
            return -1

        # 2. Connect to Central Mosquitto
        self.client = self._init_mqtt_client()
        total_synced = 0

        try:
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()

            # Wait briefly for connection handshake
            t_start = time.time()
            while not self.is_connected and (time.time() - t_start) < self.conn_timeout:
                time.sleep(0.1)

            if not self.is_connected:
                logger.error("Timed out waiting for MQTT handshake with Central Broker.")
                return -1

            # Reset backoff upon successful connection
            self.current_backoff = 5.0

            # Drain loop: transmit chunks until queue is empty or error occurs
            while True:
                records = self.db.get_pending_sync_records(limit=self.batch_size)
                if not records:
                    logger.info("All pending batches successfully drained.")
                    break

                success = self.sync_batch(records)
                if not success:
                    logger.warning("Batch sync failed. Aborting drain loop to preserve atomic order.")
                    break

                total_synced += len(records)
                time.sleep(0.1)  # Minimal pacing to prevent socket buffer congestion

        except Exception as e:
            logger.error("Error during sync cycle: %s", e, exc_info=True)
            return -1
        finally:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.is_connected = False

        return total_synced

    def start(self):
        """Main service loop with jittered exponential backoff."""
        logger.info(
            "Starting Batch Synchronization Worker for node '%s' -> target '%s:%d'...",
            self.node_id,
            self.broker_host,
            self.broker_port,
        )

        while True:
            try:
                result = self.run_sync_cycle()

                if result >= 0:
                    # Successful cycle or empty queue -> sleep normal interval
                    sleep_time = self.sync_interval
                else:
                    # Network down / connection failure -> apply exponential backoff + jitter
                    jitter = random.uniform(0.8, 1.2)
                    sleep_time = min(self.current_backoff * jitter, self.max_backoff)
                    logger.info(
                        "Wi-Fi LAN unavailable. Backing off for %.1f seconds...",
                        sleep_time,
                    )
                    self.current_backoff = min(self.current_backoff * 2.0, self.max_backoff)

                time.sleep(sleep_time)

            except KeyboardInterrupt:
                logger.info("Worker stopped by user.")
                break
            except Exception as e:
                logger.critical("Unexpected error in main sync loop: %s", e, exc_info=True)
                time.sleep(10.0)


if __name__ == "__main__":
    worker = BatchSyncWorker()
    worker.start()
