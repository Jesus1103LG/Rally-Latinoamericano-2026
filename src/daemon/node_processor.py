#!/usr/bin/env python3
"""
Node Telemetry & DSP Processing Daemon
======================================
Subscribes to local loopback Mosquitto (127.0.0.1:1883).
Ingests physical sensor readings and microphone PCM audio bursts, executes
the acoustic anemometer DSP algorithm, and stores the processed observation
in SQLite with WAL mode.

Includes automatic exponential backoff reconnection.
"""

import os
import sys
import time
import json
import base64
import logging
from typing import Dict, Any, Optional

import paho.mqtt.client as mqtt

# Ensure local imports work in both development and Termux environments
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from src.dsp.wind_acoustic_anemometer import AcousticAnemometerDSP
from src.storage.db import StationDatabase

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("NodeProcessor")


class NodeProcessorDaemon:
    def __init__(
        self,
        broker_host: str = "127.0.0.1",
        broker_port: int = 1883,
        db_path: Optional[str] = None,
    ):
        self.broker_host = broker_host
        self.broker_port = broker_port

        self.db = StationDatabase(db_path=db_path)
        self.dsp = AcousticAnemometerDSP()

        # In-memory buffer for the ongoing measurement burst
        self.current_sensor_cache: Dict[str, Any] = {}
        self.audio_buffer = bytearray()
        self.last_audio_received_at: float = 0.0
        self.burst_timeout_seconds: float = 2.0  # Time without chunks indicating burst end

        # Reconnection parameters
        self.reconnect_delay = 1
        self.max_reconnect_delay = 60

        # Initialize MQTT Client
        self.client = mqtt.Client(client_id="termux_dsp_daemon", clean_session=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def _read_battery_telemetry(self) -> Dict[str, Any]:
        """Reads kernel sysfs directly to avoid Termux-API overhead."""
        temp_c = None
        capacity_pct = None

        try:
            if os.path.exists("/sys/class/power_supply/battery/temp"):
                with open("/sys/class/power_supply/battery/temp", "r") as f:
                    raw_temp = float(f.read().strip())
                    temp_c = raw_temp / 10.0 if raw_temp > 100 else raw_temp

            if os.path.exists("/sys/class/power_supply/battery/capacity"):
                with open("/sys/class/power_supply/battery/capacity", "r") as f:
                    capacity_pct = int(f.read().strip())
        except Exception as e:
            logger.debug("Sysfs battery reading fallback: %s", e)

        return {"battery_temp_c": temp_c, "battery_pct": capacity_pct}

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to local Mosquitto loopback broker (127.0.0.1:1883).")
            self.reconnect_delay = 1
            # Subscribe to sensor telemetry and raw audio chunk streams
            client.subscribe("station/sensor/raw", qos=0)
            client.subscribe("station/audio/chunk", qos=0)
            logger.info("Subscribed to station/sensor/raw and station/audio/chunk.")
        else:
            logger.error("Failed to connect to local broker, return code: %d", rc)

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning("Unexpected disconnection from local broker. Will reconnect.")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))

            if msg.topic == "station/sensor/raw":
                self._handle_sensor_telemetry(payload)
            elif msg.topic == "station/audio/chunk":
                self._handle_audio_chunk(payload)

        except Exception as e:
            logger.error("Error processing MQTT message on topic %s: %s", msg.topic, e, exc_info=True)

    def _handle_sensor_telemetry(self, data: Dict[str, Any]):
        """Caches environmental sensor readings."""
        self.current_sensor_cache.update({
            "pressure_hpa": data.get("pressure_hpa"),
            "light_lux": data.get("light_lux"),
            "ambient_temp_c": data.get("ambient_temp_c"),
            "humidity_pct": data.get("humidity_pct"),
            "timestamp": data.get("timestamp_ms"),
        })
        logger.debug("Sensor readings cached: %s", self.current_sensor_cache)

    def _handle_audio_chunk(self, data: Dict[str, Any]):
        """Buffers raw PCM chunks arriving from the native Android service."""
        b64_pcm = data.get("pcm_b64")
        if not b64_pcm:
            return

        raw_bytes = base64.b64decode(b64_pcm)
        self.audio_buffer.extend(raw_bytes)
        self.last_audio_received_at = time.time()

    def check_burst_completion(self):
        """
        Detects when an audio burst has finished arriving (idle gap > burst_timeout_seconds)
        and triggers DSP processing and database persistence.
        """
        if not self.audio_buffer:
            return

        elapsed = time.time() - self.last_audio_received_at
        if elapsed >= self.burst_timeout_seconds:
            logger.info(
                "Audio burst complete: %d bytes received (~%.2f seconds of 16kHz audio). Processing DSP...",
                len(self.audio_buffer),
                len(self.audio_buffer) / (16000 * 2),
            )
            self._finalize_observation()

    def _finalize_observation(self):
        """Runs the DSP anemometer model and atomically saves the final observation."""
        try:
            # Execute DSP analysis
            dsp_result = self.dsp.process_burst(bytes(self.audio_buffer))
            logger.info(
                "DSP Result -> Wind Avg: %.2f m/s (%.1f km/h) | Gust: %.2f m/s | Contamination: %.1f%%",
                dsp_result["wind_speed_avg_ms"],
                dsp_result["wind_speed_avg_kmh"],
                dsp_result["wind_gust_ms"],
                dsp_result["contamination_pct"],
            )

            # Gather battery telemetry
            battery_info = self._read_battery_telemetry()

            # Compile record
            record = {
                "pressure_hpa": self.current_sensor_cache.get("pressure_hpa"),
                "wind_speed_avg_ms": dsp_result["wind_speed_avg_ms"],
                "wind_gust_ms": dsp_result["wind_gust_ms"],
                "ambient_temp_c": self.current_sensor_cache.get("ambient_temp_c"),
                "humidity_pct": self.current_sensor_cache.get("humidity_pct"),
                "light_lux": self.current_sensor_cache.get("light_lux"),
                "battery_pct": battery_info.get("battery_pct"),
                "battery_temp_c": battery_info.get("battery_temp_c"),
                "contamination_pct": dsp_result["contamination_pct"],
            }

            # Persist atomically into SQLite
            row_id = self.db.insert_measurement(record)
            stats = self.db.get_stats()
            logger.info(
                "[✓] Saved observation #%d to SQLite (Pending sync: %d, Total: %d)",
                row_id,
                stats["pending_sync"],
                stats["total_records"],
            )

        except Exception as e:
            logger.error("Error finalizing observation: %s", e, exc_info=True)
        finally:
            # Reset buffers for the next cycle
            self.audio_buffer.clear()
            self.current_sensor_cache.clear()

    def start(self):
        """Starts the daemon with automatic reconnection loop."""
        logger.info("Starting Node Processor Daemon...")

        while True:
            try:
                self.client.connect(self.broker_host, self.broker_port, keepalive=60)
                self.client.loop_start()

                while True:
                    time.sleep(0.5)
                    self.check_burst_completion()

            except (ConnectionRefusedError, OSError) as e:
                logger.warning(
                    "Cannot reach Mosquitto at %s:%d (%s). Retrying in %ds...",
                    self.broker_host,
                    self.broker_port,
                    e,
                    self.reconnect_delay,
                )
                time.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)

            except KeyboardInterrupt:
                logger.info("Daemon interrupted by user. Stopping...")
                self.client.loop_stop()
                self.client.disconnect()
                break

            except Exception as e:
                logger.critical("Unexpected error in daemon main loop: %s", e, exc_info=True)
                time.sleep(5)


if __name__ == "__main__":
    daemon = NodeProcessorDaemon()
    daemon.start()
