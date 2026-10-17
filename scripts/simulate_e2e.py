#!/usr/bin/env python3
"""
End-to-End Simulation of the Distributed Agrometeorological System
==================================================================
Simulates the entire data lifecycle:
  1. DSP Wind Anemometer (Turbulence vs. Tonal Voice/Fauna Rejection)
  2. Local Node Persistence (SQLite with WAL mode)
  3. Offline-First Batch Sync & Central Ingestion (Idempotency check)
  4. Agronomic Advisory Engine (Delta T, Magnus-Tetens, Frost, Storm)
  5. Central-to-Cloud Uplink Preparation
"""

import os
import sys
import time
import json
import uuid
import struct
import math
import shutil

# Ensure workspace root is in path
WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, WORKSPACE_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
from src.dsp.wind_acoustic_anemometer import AcousticAnemometerDSP
from src.storage.db import StationDatabase
from src.central.central_receiver import CentralDatabase
from src.analytics.agronomic_advisor import AgronomicAdvisor, AlertSeverity


def generate_synthetic_audio(
    duration_sec: float = 3.0,
    sample_rate: int = 16000,
    wind_intensity: float = 0.25,
    add_bird_chirp: bool = False,
) -> bytes:
    """
    Generates synthetic 16-bit PCM Mono audio simulating:
      - Wind turbulence (low-frequency pinkish noise filtered below 400 Hz)
      - Optional bird chirp (pure tonal sine wave at 4000 Hz)
    """
    num_samples = int(duration_sec * sample_rate)
    
    # 1. Generate broadband noise
    white_noise = np.random.normal(0, 1.0, num_samples)
    
    # Simple recursive 1st order filter to create pink noise (1/f characteristic of turbulence)
    pink_noise = np.zeros(num_samples)
    b0 = 0.05
    for i in range(1, num_samples):
        pink_noise[i] = (1 - b0) * pink_noise[i - 1] + b0 * white_noise[i]

    # Scale by wind intensity
    audio_signal = pink_noise * wind_intensity

    # 2. Add bird chirp tonal contamination if requested
    if add_bird_chirp:
        t = np.linspace(0, duration_sec, num_samples)
        # Chirp in the fauna band (3500 - 4500 Hz)
        bird_chirp = 0.4 * np.sin(2 * np.pi * 4000.0 * t)
        # Apply chirp only in the middle second
        start_idx = int(1.0 * sample_rate)
        end_idx = int(2.0 * sample_rate)
        audio_signal[start_idx:end_idx] += bird_chirp[start_idx:end_idx]

    # Clip to [-1.0, 1.0] and convert to 16-bit PCM Little Endian
    audio_signal = np.clip(audio_signal, -1.0, 1.0)
    pcm16 = (audio_signal * 32767.0).astype(np.int16)
    return pcm16.tobytes()


def run_e2e_simulation():
    print("=" * 75)
    print(" AGROMETEOROLOGICAL SYSTEM: FULL END-TO-END PIPELINE SIMULATION")
    print("=" * 75)

    test_dir = os.path.join(WORKSPACE_ROOT, "scratch", "simulation_test")
    shutil.rmtree(test_dir, ignore_errors=True)
    os.makedirs(test_dir, exist_ok=True)

    node_db_path = os.path.join(test_dir, "node_telemetry.db")
    central_db_path = os.path.join(test_dir, "central_telemetry.db")

    # -------------------------------------------------------------------------
    # STEP 1: DSP Acoustic Anemometer Verification
    # -------------------------------------------------------------------------
    print("\n[STEP 1] Validating Acoustic Anemometer DSP Algorithm...")
    dsp = AcousticAnemometerDSP(sample_rate=16000)

    # 1.1 Pure turbulent wind audio
    clean_wind_audio = generate_synthetic_audio(duration_sec=3.0, wind_intensity=0.30, add_bird_chirp=False)
    result_clean = dsp.process_burst(clean_wind_audio)
    print(f"  -> Clean Wind Burst:   {result_clean['wind_speed_avg_kmh']:.1f} km/h "
          f"(Avg: {result_clean['wind_speed_avg_ms']} m/s, Gust: {result_clean['wind_gust_kmh']} km/h) "
          f"| Contamination: {result_clean['contamination_pct']}%")

    # 1.2 Wind contaminated by bird chirps (fauna filter check)
    noisy_wind_audio = generate_synthetic_audio(duration_sec=3.0, wind_intensity=0.30, add_bird_chirp=True)
    result_noisy = dsp.process_burst(noisy_wind_audio)
    print(f"  -> Contaminated Burst: {result_noisy['wind_speed_avg_kmh']:.1f} km/h "
          f"| Contamination Detected: {result_noisy['contamination_pct']}% (Fauna filter active)")

    assert result_noisy["contamination_pct"] > 0, "Fauna rejection filter should flag bird chirp frames!"
    print("  [OK] DSP Acoustic Anemometer Passed.")

    # -------------------------------------------------------------------------
    # STEP 2: Node Local Ingestion & SQLite WAL Persistence
    # -------------------------------------------------------------------------
    print("\n[STEP 2] Simulating Field Node Telemetry Ingestion into SQLite...")
    node_db = StationDatabase(db_path=node_db_path)

    # Simulate 3 consecutive observations with different agronomic scenarios:
    scenarios = [
        # Scenario 1: Optimal spraying window (Delta T ~ 5°C, wind 8 km/h)
        {
            "timestamp": "2026-10-17T08:00:00Z",
            "pressure_hpa": 1013.2,
            "wind_speed_avg_ms": 2.2,  # ~8 km/h
            "wind_gust_ms": 3.1,
            "ambient_temp_c": 21.5,
            "humidity_pct": 58.0,
            "light_lux": 45000.0,
            "battery_pct": 92,
            "battery_temp_c": 31.4,
            "contamination_pct": 0.0,
        },
        # Scenario 2: Nocturnal Frost Inversion (T=1.8°C, RH=94%, calm wind)
        {
            "timestamp": "2026-10-17T09:00:00Z",
            "pressure_hpa": 1014.0,
            "wind_speed_avg_ms": 0.4,  # Calm air
            "wind_gust_ms": 0.8,
            "ambient_temp_c": 1.8,
            "humidity_pct": 94.0,
            "light_lux": 10.0,
            "battery_pct": 85,
            "battery_temp_c": 24.0,
            "contamination_pct": 0.0,
        },
        # Scenario 3: Imminent Storm (Barometric drop of 3.2 hPa, high wind)
        {
            "timestamp": "2026-10-17T11:00:00Z",
            "pressure_hpa": 1010.0,  # 3.2 hPa drop from 1013.2
            "wind_speed_avg_ms": 7.5,  # 27 km/h
            "wind_gust_ms": 11.2,      # 40 km/h
            "ambient_temp_c": 18.0,
            "humidity_pct": 75.0,
            "light_lux": 12000.0,
            "battery_pct": 78,
            "battery_temp_c": 27.5,
            "contamination_pct": 0.0,
        },
    ]

    for sc in scenarios:
        row_id = node_db.insert_measurement(sc)
        print(f"  -> Inserted observation #{row_id}: P={sc['pressure_hpa']} hPa, T={sc['ambient_temp_c']}°C, v={sc['wind_speed_avg_ms']*3.6:.1f} km/h")

    stats = node_db.get_stats()
    print(f"  [✓] Node SQLite Queue: {stats['total_records']} total records, {stats['pending_sync']} pending synchronization.")
    assert stats["pending_sync"] == 3

    # -------------------------------------------------------------------------
    # STEP 3: Offline-First Batch Synchronization (Two-Phase Commit)
    # -------------------------------------------------------------------------
    print("\n[STEP 3] Simulating Wi-Fi Batch Synchronization & Idempotent Ingestion...")
    central_db = CentralDatabase(db_path=central_db_path)

    # 3.1 Fetch unsynced records
    pending_records = node_db.get_pending_sync_records(limit=50)
    batch_payload = {
        "node_id": "station_parcela_norte_01",
        "batch_id": str(uuid.uuid4()),
        "record_count": len(pending_records),
        "records": pending_records,
    }

    # 3.2 Ingest into Central Node
    inserted = central_db.insert_batch(batch_payload)
    print(f"  -> Central Node ingested batch {batch_payload['batch_id'][:8]}...: {inserted} new records stored.")
    assert inserted == 3

    # 3.3 Confirm Two-Phase commit on Field Node
    record_ids = [r["id"] for r in pending_records]
    marked = node_db.mark_as_synced(record_ids)
    print(f"  -> Field Node marked {marked} records as synced upon confirmation.")
    assert node_db.get_stats()["pending_sync"] == 0

    # 3.4 Test Idempotency: Retransmit same batch (simulate dropped PUBACK)
    print("  -> Testing Idempotency (re-transmitting duplicate batch)...")
    duplicate_inserted = central_db.insert_batch(batch_payload)
    print(f"  -> Retransmission result: {duplicate_inserted} new records (Expected 0 duplicates).")
    assert duplicate_inserted == 0, "Idempotency failed: duplicate records were inserted!"
    print("  [✓] Batch Synchronization & Idempotency Passed.")

    # -------------------------------------------------------------------------
    # STEP 4: Agronomic Decision Engine & Alerts Evaluation
    # -------------------------------------------------------------------------
    print("\n[STEP 4] Evaluating Agronomic Rules & Farmer Recommendations...")
    advisor = AgronomicAdvisor()

    # Query consolidated records from Central DB
    with central_db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM consolidated_telemetry ORDER BY id ASC")
        rows = [dict(r) for r in cursor.fetchall()]

    all_alerts = []
    # Test Scenario 1 (Optimal Spraying)
    alerts_sc1 = advisor.evaluate_conditions(rows[0])
    print(f"\n  [Parcel Observation #1] T={rows[0]['ambient_temp_c']}°C, RH={rows[0]['humidity_pct']}%, Wind={rows[0]['wind_speed_avg_ms']*3.6:.1f} km/h")
    for a in alerts_sc1:
        print(f"    [{a.severity.value}] {a.title}: {a.message}")
        print(f"           Action: {a.action_required}")
    all_alerts.extend(alerts_sc1)

    # Test Scenario 2 (Frost Inversion)
    alerts_sc2 = advisor.evaluate_conditions(rows[1])
    print(f"\n  [Parcel Observation #2] T={rows[1]['ambient_temp_c']}°C, RH={rows[1]['humidity_pct']}%, Wind={rows[1]['wind_speed_avg_ms']*3.6:.1f} km/h")
    for a in alerts_sc2:
        print(f"    [{a.severity.value}] {a.title}: {a.message}")
        print(f"           Action: {a.action_required}")
    all_alerts.extend(alerts_sc2)

    # Test Scenario 3 (Storm with Barometric Drop)
    # Historic pressure 3h ago was 1013.2 (Obs #1), current is 1010.0 (Obs #3) -> delta = -3.2 hPa
    alerts_sc3 = advisor.evaluate_conditions(rows[2], historical_pressure_3h_ago=1013.2)
    print(f"\n  [Parcel Observation #3] P={rows[2]['pressure_hpa']} hPa (Drop: -3.2 hPa), Gust={rows[2]['wind_gust_ms']*3.6:.1f} km/h")
    for a in alerts_sc3:
        print(f"    [{a.severity.value}] {a.title}: {a.message}")
        print(f"           Action: {a.action_required}")
    all_alerts.extend(alerts_sc3)

    # Assertions on alert logic
    has_spray_opt = any(a.code == "SPRAY_OPTIMAL_WINDOW" for a in alerts_sc1)
    has_frost = any(a.code == "FROST_RADIATION_CRITICAL" for a in alerts_sc2)
    has_storm = any(a.code == "STORM_BAROMETRIC_PLUNGE" for a in alerts_sc3)
    has_wind_drift = any(a.code == "SPRAY_WIND_DRIFT_HAZARD" for a in alerts_sc3)

    assert has_spray_opt, "Failed to identify optimal spraying window!"
    assert has_frost, "Failed to identify radiation frost danger!"
    assert has_storm, "Failed to identify severe barometric storm plunge!"
    assert has_wind_drift, "Failed to identify wind drift hazard!"
    print("\n  [✓] Agronomic Decision Engine Passed all validations.")

    # -------------------------------------------------------------------------
    # STEP 5: Cloud Uplink Preparation
    # -------------------------------------------------------------------------
    print("\n[STEP 5] Simulating Central Cloud Uplink Serialization...")
    cloud_payload = {
        "central_station_id": "master_station_valle_central",
        "uplink_time": time.time(),
        "total_records": len(rows),
        "total_alerts": len(all_alerts),
        "alerts_summary": [
            {"severity": a.severity.value, "code": a.code, "action": a.action_required}
            for a in all_alerts
        ],
    }
    print(f"  -> Generated Cloud Uplink Payload ({len(json.dumps(cloud_payload))} bytes):")
    print("     " + json.dumps(cloud_payload, indent=2).replace("\n", "\n     "))

    # Cleanup test files
    shutil.rmtree(test_dir, ignore_errors=True)

    print("\n" + "=" * 75)
    print(" [✓✓✓] ALL SYSTEM COMPONENTS VERIFIED SUCCESSFULLY END-TO-END!")
    print("=" * 75)


if __name__ == "__main__":
    run_e2e_simulation()
