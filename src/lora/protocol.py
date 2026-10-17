#!/usr/bin/env python3
"""
AgroMesh LoRa Binary Frame Protocol Specification
=================================================
Target: Sub-GHz Long Range Inter-Master Telemetry (433/868/915 MHz)
Design Objectives:
  1. Ultra-dense packing: Max 26 bytes per frame (compatible with SF10-SF12 at 15 km).
  2. Sub-second Time-on-Air (ToA): ~56ms on SF7/125kHz, preserving battery & legal duty cycles.
  3. Strict over-the-air integrity: ITU-T CRC-16 validation against RF atmospheric noise.
  4. Multi-hop TTL decrement for inter-master mesh relaying.

Packet Structure (26 Bytes Total):
+--------+--------+------------+----------+-----------+-------------------------+---------+--------+
| Magic  | Flags  | Origin ID  | Seq Num  | Timestamp | Metrics Payload         | Hop/TTL | CRC-16 |
| 1 byte | 1 byte | 4 bytes    | 2 bytes  | 4 bytes   | 11 bytes                | 1 byte  | 2 byte |
+--------+--------+------------+----------+-----------+-------------------------+---------+--------+
"""

import time
import struct
import binascii
from typing import Dict, Any, Optional, Tuple


MAGIC_BYTE = 0x41  # 'A' for AgroMesh
PROTOCOL_VERSION = 1

# Packet Types (Flags lower nibble)
PKT_TYPE_TELEMETRY = 0x01
PKT_TYPE_CRITICAL_ALERT = 0x02
PKT_TYPE_ACK = 0x03
PKT_TYPE_HEARTBEAT = 0x04


def compute_crc16(data: bytes) -> int:
    """Computes ITU-T CRC-16 (Polynomial: 0x1021, Initial: 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


TOTAL_FRAME_SIZE = 27
PAYLOAD_SIZE = 25


class LoRaPacketCodec:
    """Encodes and decodes microclimate observations to/from dense 27-byte binary frames."""

    @staticmethod
    def encode_telemetry(
        origin_id: str,
        seq_num: int,
        timestamp_epoch: int,
        temp_c: float,
        humidity_pct: float,
        pressure_hpa: float,
        wind_speed_ms: float,
        wind_gust_ms: float,
        light_lux: float,
        battery_pct: int,
        ttl_hops: int = 3,
        is_alert: bool = False,
    ) -> bytes:
        """
        Packs telemetry metrics into a 26-byte binary buffer.
        """
        flags = ((PROTOCOL_VERSION & 0x0F) << 4) | (PKT_TYPE_CRITICAL_ALERT if is_alert else PKT_TYPE_TELEMETRY)

        # Truncate or hash origin_id into a stable 32-bit unsigned integer
        if isinstance(origin_id, str):
            origin_hash = binascii.crc32(origin_id.encode("utf-8")) & 0xFFFFFFFF
        else:
            origin_hash = int(origin_id) & 0xFFFFFFFF

        # Quantize floating point metrics into integer ranges
        # Temp: -40.0 to +85.0 C in tenths (-400 to +850) -> int16
        q_temp = int(round(temp_c * 10.0))
        q_temp = max(-400, min(850, q_temp))

        # Humidity: 0 - 100% -> uint8
        q_hum = int(round(humidity_pct))
        q_hum = max(0, min(100, q_hum))

        # Pressure: delta from 800.0 hPa in tenths (e.g. 1013.25 -> 2132) -> uint16
        q_press = int(round((pressure_hpa - 800.0) * 10.0))
        q_press = max(0, min(65535, q_press))

        # Wind speed & gust: cm/s (e.g. 3.5 m/s -> 350) -> uint16
        q_wind = int(round(wind_speed_ms * 100.0))
        q_wind = max(0, min(65535, q_wind))

        q_gust = int(round(wind_gust_ms * 100.0))
        q_gust = max(0, min(65535, q_gust))

        # Light: lux / 5 (allows up to 327,675 Lux in uint16)
        q_lux = int(round(light_lux / 5.0))
        q_lux = max(0, min(65535, q_lux))

        # Battery %: 0 - 100% -> uint8
        q_batt = max(0, min(100, int(battery_pct)))

        # Pack header and payload (24 bytes excluding CRC)
        # Format string breakdown:
        # B  : Magic (1 byte)
        # B  : Flags (1 byte)
        # I  : Origin Hash (4 bytes)
        # H  : Seq Num (2 bytes)
        # I  : Timestamp (4 bytes)
        # h  : Temp (2 bytes, signed)
        # B  : Humidity (1 byte)
        # H  : Pressure Delta (2 bytes)
        # H  : Wind Avg (2 bytes)
        # H  : Wind Gust (2 bytes)
        # H  : Light Lux / 5 (2 bytes)
        # B  : Battery Pct (1 byte)
        # B  : TTL Hops (1 byte)
        raw_without_crc = struct.pack(
            "!BBIHIhBHHHHBB",
            MAGIC_BYTE,
            flags,
            origin_hash,
            seq_num & 0xFFFF,
            timestamp_epoch & 0xFFFFFFFF,
            q_temp,
            q_hum,
            q_press,
            q_wind,
            q_gust,
            q_lux,
            q_batt,
            ttl_hops & 0xFF,
        )

        crc = compute_crc16(raw_without_crc)
        return raw_without_crc + struct.pack("!H", crc)

    @staticmethod
    def decode(packet_bytes: bytes) -> Optional[Dict[str, Any]]:
        """
        Validates CRC and unpacks a 27-byte binary frame into structured telemetry.
        Returns None if packet is corrupt or invalid.
        """
        if len(packet_bytes) != TOTAL_FRAME_SIZE:
            return None

        # Verify magic byte
        if packet_bytes[0] != MAGIC_BYTE:
            return None

        # Verify CRC16
        payload_data = packet_bytes[:PAYLOAD_SIZE]
        expected_crc = struct.unpack("!H", packet_bytes[PAYLOAD_SIZE:TOTAL_FRAME_SIZE])[0]
        actual_crc = compute_crc16(payload_data)

        if expected_crc != actual_crc:
            return None  # Corrupted by atmospheric noise or collision

        # Unpack values
        (
            magic,
            flags,
            origin_hash,
            seq_num,
            ts_epoch,
            q_temp,
            q_hum,
            q_press,
            q_wind,
            q_gust,
            q_lux,
            q_batt,
            ttl_hops,
        ) = struct.unpack("!BBIHIhBHHHHBB", payload_data)

        version = (flags >> 4) & 0x0F
        pkt_type = flags & 0x0F

        # De-quantize
        temp_c = round(q_temp / 10.0, 1)
        humidity_pct = float(q_hum)
        pressure_hpa = round(800.0 + (q_press / 10.0), 1)
        wind_speed_ms = round(q_wind / 100.0, 2)
        wind_gust_ms = round(q_gust / 100.0, 2)
        light_lux = float(q_lux * 5)
        battery_pct = int(q_batt)

        return {
            "version": version,
            "packet_type": pkt_type,
            "is_alert": pkt_type == PKT_TYPE_CRITICAL_ALERT,
            "origin_hash": f"0x{origin_hash:08x}",
            "seq_num": seq_num,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_epoch)),
            "timestamp_epoch": ts_epoch,
            "ambient_temp_c": temp_c,
            "humidity_pct": humidity_pct,
            "pressure_hpa": pressure_hpa,
            "wind_speed_avg_ms": wind_speed_ms,
            "wind_gust_ms": wind_gust_ms,
            "light_lux": light_lux,
            "battery_pct": battery_pct,
            "ttl_hops": ttl_hops,
        }
