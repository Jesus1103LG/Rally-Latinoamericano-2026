#!/usr/bin/env python3
"""
Central Node Aggregator & Batch Receiver
========================================
Target: Master Android Phone / Central Station (Termux)
Mission: Subscribes to 'station/+/batch' on the Central Mosquitto broker (0.0.0.0:1883).
         Ingests telemetric batches from all field sensor nodes, guarantees idempotency
         via unique constraints, and stores consolidated records in SQLite.
"""

import os
import sys
import json
import time
import sqlite3
import logging
from typing import Dict, Any, Optional

import paho.mqtt.client as mqtt

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("CentralReceiver")


class CentralDatabase:
    """Consolidated agricultural telemetry repository."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            base_dir = os.path.expanduser("~/station/central_data")
            os.makedirs(base_dir, exist_ok=True)
            self.db_path = os.path.join(base_dir, "central_telemetry.db")
        else:
            self.db_path = db_path
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.execute("PRAGMA busy_timeout = 5000;")
        cursor.close()
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS consolidated_telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL,
                    source_record_id INTEGER,
                    timestamp TEXT NOT NULL,
                    pressure_hpa REAL,
                    wind_speed_avg_ms REAL,
                    wind_gust_ms REAL,
                    ambient_temp_c REAL,
                    humidity_pct REAL,
                    light_lux REAL,
                    battery_pct INTEGER,
                    battery_temp_c REAL,
                    contamination_pct REAL,
                    batch_id TEXT,
                    received_at TEXT NOT NULL,
                    cloud_synced INTEGER DEFAULT 0,
                    cloud_synced_at TEXT
                );
            """)

            # Unique index ensuring IDEMPOTENCY: prevents duplicate inserts if a node
            # retransmits a batch because it missed the PUBACK.
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_node_timestamp_unique 
                ON consolidated_telemetry (node_id, timestamp);
            """)

            # Index for cloud uplink and queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_cloud_sync 
                ON consolidated_telemetry (cloud_synced, timestamp);
            """)
            conn.commit()
            logger.info("Central database schema initialized at %s", self.db_path)

    def insert_batch(self, batch_payload: Dict[str, Any]) -> int:
        """
        Idempotently inserts a batch of observations from a field node.
        Returns the number of newly inserted records (ignoring duplicates).
        """
        node_id = batch_payload.get("node_id", "unknown_node")
        batch_id = batch_payload.get("batch_id", "")
        records = batch_payload.get("records", [])
        received_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        if not records:
            return 0

        query = """
            INSERT OR IGNORE INTO consolidated_telemetry (
                node_id,
                source_record_id,
                timestamp,
                pressure_hpa,
                wind_speed_avg_ms,
                wind_gust_ms,
                ambient_temp_c,
                humidity_pct,
                light_lux,
                battery_pct,
                battery_temp_c,
                contamination_pct,
                batch_id,
                received_at,
                cloud_synced
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0);
        """

        inserted_count = 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for r in records:
                params = (
                    node_id,
                    r.get("id"),
                    r.get("timestamp"),
                    r.get("pressure_hpa"),
                    r.get("wind_speed_avg_ms"),
                    r.get("wind_gust_ms"),
                    r.get("ambient_temp_c"),
                    r.get("humidity_pct"),
                    r.get("light_lux"),
                    r.get("battery_pct"),
                    r.get("battery_temp_c"),
                    r.get("contamination_pct", 0.0),
                    batch_id,
                    received_at,
                )
                cursor.execute(query, params)
                if cursor.rowcount > 0:
                    inserted_count += 1
            conn.commit()

        return inserted_count


class CentralReceiverDaemon:
    def __init__(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 1883,
        db_path: Optional[str] = None,
    ):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.db = CentralDatabase(db_path=db_path)

        self.client = mqtt.Client(client_id="central_station_receiver", clean_session=False)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Central Receiver connected to Mosquitto (%s:%d)", self.broker_host, self.broker_port)
            # Subscribe to station direct batches and decentralized AgroMesh feeds
            client.subscribe("station/+/batch", qos=1)
            client.subscribe("agromesh/telemetry/#", qos=1)
            client.subscribe("agromesh/alerts/#", qos=1)
            logger.info("Subscribed to 'station/+/batch' and 'agromesh/#' with QoS 1.")
        else:
            logger.error("Connection failed with return code %d", rc)

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning("Disconnected from Central Broker. Reconnecting...")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))

            # 1. Emergency AgroMesh Alert Handler
            if msg.topic.startswith("agromesh/alerts/"):
                logger.warning("🚨 [AGROMESH ALARM] %s from node '%s'", payload.get("title"), payload.get("origin_node_id"))
                logger.warning("   Acción requerida: %s", payload.get("action_required"))
                return

            # 2. AgroMesh Telemetry or Direct Station Batch Handler
            if msg.topic.startswith("agromesh/telemetry/"):
                source_peer = payload.get("source_peer", "unknown_mesh_peer")
                records = payload.get("records", [])
                logger.info("Incoming AgroMesh sync from peer '%s' with %d records.", source_peer, len(records))
                for r in records:
                    single_batch = {
                        "node_id": r.get("origin_node_id") or r.get("node_id") or source_peer,
                        "batch_id": f"mesh_{r.get('timestamp')}",
                        "records": [r]
                    }
                    self.db.insert_batch(single_batch)
                return

            # Direct Station Batch
            node_id = payload.get("node_id", "unknown")
            batch_id = payload.get("batch_id", "unknown")
            count = payload.get("record_count", 0)

            logger.info("Incoming batch from '%s' | Batch ID: %s | Count: %d", node_id, batch_id, count)

            new_inserted = self.db.insert_batch(payload)
            logger.info(
                "[✓] Ingested batch %s from '%s': %d new records stored (%d skipped duplicates).",
                batch_id,
                node_id,
                new_inserted,
                count - new_inserted,
            )

        except Exception as e:
            logger.error("Failed to process payload on %s: %s", msg.topic, e, exc_info=True)

    def start(self):
        logger.info("Starting Central Receiver Daemon on %s:%d...", self.broker_host, self.broker_port)
        while True:
            try:
                self.client.connect(self.broker_host, self.broker_port, keepalive=60)
                self.client.loop_forever()
            except (ConnectionRefusedError, OSError) as e:
                logger.warning("Cannot connect to local broker (%s). Retrying in 5 seconds...", e)
                time.sleep(5)
            except KeyboardInterrupt:
                logger.info("Central receiver stopped by user.")
                self.client.disconnect()
                break


if __name__ == "__main__":
    daemon = CentralReceiverDaemon()
    daemon.start()
