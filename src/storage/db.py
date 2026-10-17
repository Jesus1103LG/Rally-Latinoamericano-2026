#!/usr/bin/env python3
"""
Local SQLite Store-and-Forward Engine
=====================================
Provides transactional persistence with Write-Ahead Logging (WAL) mode
to allow concurrent, lock-free reads by synchronization workers while the
DSP daemon writes new measurements.
"""

import sqlite3
import os
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger("StationDB")


class StationDatabase:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            # Default to Termux station path, fallback to local path if testing
            base_dir = os.path.expanduser("~/station/data")
            os.makedirs(base_dir, exist_ok=True)
            self.db_path = os.path.join(base_dir, "telemetry.db")
        else:
            self.db_path = db_path
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """
        Creates connection configured for concurrency, resilience and low wear.
        """
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row

        # Performance and resilience pragmas
        cursor = conn.cursor()
        # WAL (Write-Ahead Log) allows simultaneous readers and writers
        cursor.execute("PRAGMA journal_mode = WAL;")
        # NORMAL synchronous guarantees zero database corruption on power cut
        # while eliminating expensive synchronous disk fsync flushes
        cursor.execute("PRAGMA synchronous = NORMAL;")
        # Wait up to 5000ms if table is locked before throwing an exception
        cursor.execute("PRAGMA busy_timeout = 5000;")
        cursor.close()
        return conn

    def _init_db(self) -> None:
        """Creates table and indices if they do not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS measurements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                    synced INTEGER DEFAULT 0,
                    synced_at TEXT
                );
            """)

            # Fast index for offline store-and-forward queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_pending_sync 
                ON measurements (synced, timestamp);
            """)
            conn.commit()
            logger.info("Database schema initialized at %s", self.db_path)

    def insert_measurement(self, record: Dict[str, Any]) -> int:
        """
        Inserts an agrometeorological observation into the queue.
        Returns the inserted row ID.
        """
        now_iso = record.get("timestamp") or datetime.utcnow().isoformat() + "Z"
        query = """
            INSERT INTO measurements (
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
                synced
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0);
        """
        params = (
            now_iso,
            record.get("pressure_hpa"),
            record.get("wind_speed_avg_ms"),
            record.get("wind_gust_ms"),
            record.get("ambient_temp_c"),
            record.get("humidity_pct"),
            record.get("light_lux"),
            record.get("battery_pct"),
            record.get("battery_temp_c"),
            record.get("contamination_pct", 0.0),
        )

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            return cursor.lastrowid

    def get_pending_sync_records(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieves pending offline records ordered chronologically.
        """
        query = """
            SELECT * FROM measurements 
            WHERE synced = 0 
            ORDER BY id ASC 
            LIMIT ?;
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def mark_as_synced(self, record_ids: List[int]) -> int:
        """
        Marks records as acknowledged by the Central Master Broker.
        Executed in an atomic transaction.
        """
        if not record_ids:
            return 0

        now_iso = datetime.utcnow().isoformat() + "Z"
        placeholders = ",".join("?" for _ in record_ids)
        query = f"""
            UPDATE measurements 
            SET synced = 1, synced_at = ? 
            WHERE id IN ({placeholders});
        """
        params = [now_iso] + record_ids

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            return cursor.rowcount

    def get_stats(self) -> Dict[str, int]:
        """Returns total records and un-synced queue size."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM measurements;")
            total = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM measurements WHERE synced = 0;")
            pending = cursor.fetchone()[0]

            return {"total_records": total, "pending_sync": pending}
