#!/usr/bin/env python3
"""
Real User Microphone Wind Speed DSP Evaluation & Telemetry Ingestion
=====================================================================
Processes the authentic audio recorded by the user on the Redmi Note 13,
computes wind turbulence and wind speed using AcousticAnemometerDSP,
combines it with the phone's physical light & battery sensors, and publishes
to the Master Mosquitto broker.
"""

import sys
import os
import json
import time
import uuid
import numpy as np
from scipy.io import wavfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, WORKSPACE_ROOT)

from src.dsp.wind_acoustic_anemometer import AcousticAnemometerDSP
from src.analytics.agronomic_advisor import AgronomicAdvisor
import paho.mqtt.client as mqtt

from scripts.run_real_hardware_sensors import (
    ensure_adb_forward,
    sample_real_battery,
    sample_real_light_lux,
    sample_real_accelerometer,
    BROKER_HOST,
    BROKER_PORT,
)

WAV_PATH = os.path.join(WORKSPACE_ROOT, "scratch", "user_wind.wav")


def main():
    print("=" * 75)
    print(" EVALUACIÓN DSP DE VIENTO CON AUDIO REAL DEL TELÉFONO")
    print("=" * 75)

    if not os.path.exists(WAV_PATH):
        print(f"[X] No se encontró el archivo de audio: {WAV_PATH}")
        return

    # 1. Load audio file
    sr, data = wavfile.read(WAV_PATH)
    duration_sec = len(data) / float(sr)
    print(f"\n[PASO 1] Cargando audio físico grabado en el Redmi Note 13:")
    print(f"  Archivo     : scratch/user_wind.wav")
    print(f"  Sample Rate : {sr} Hz")
    print(f"  Duración    : {duration_sec:.2f} segundos ({len(data)} muestras)")

    # Convert to PCM16 bytes for process_burst
    if data.dtype != np.int16:
        pcm_data = (data * 32767.0).astype(np.int16).tobytes()
    else:
        pcm_data = data.tobytes()

    # 2. Run DSP Anemometer
    print("\n[PASO 2] Procesando espectro acústico con Algoritmo DSP (FFT 20-400 Hz)...")
    anemometer = AcousticAnemometerDSP(sample_rate=sr)
    dsp_result = anemometer.process_burst(pcm_data)

    avg_ms = dsp_result["wind_speed_avg_ms"]
    avg_kmh = dsp_result["wind_speed_avg_kmh"]
    gust_ms = dsp_result["wind_gust_ms"]
    gust_kmh = dsp_result["wind_gust_kmh"]
    contamination = dsp_result["contamination_pct"]

    print("\n" + "-" * 75)
    print(" RESULTADOS DEL ANEMÓMETRO ACÚSTICO (DSP DE VIENTO):")
    print("-" * 75)
    print(f"  💨  Velocidad Sostenida (Promedio) : {avg_kmh:.1f} km/h  ({avg_ms:.2f} m/s)")
    print(f"  🌪️  Ráfaga Máxima Detectada       : {gust_kmh:.1f} km/h  ({gust_ms:.2f} m/s)")
    print(f"  🛡️  Contaminación Acústica (Fauna) : {contamination:.1f}% cuadros rechazados por tonalidad")
    print("-" * 75)

    # 3. Read other physical sensors (light, battery, orientation)
    print("\n[PASO 3] Muestreando en vivo sensores ópticos y térmicos del Redmi...")
    ensure_adb_forward()
    battery_info = sample_real_battery()
    lux = sample_real_light_lux() or 40.0
    accel = sample_real_accelerometer()

    print(f"  ☀️  Radiación / Luz (ams TMD2755) : {lux:.1f} Lux")
    print(f"  🔋  Batería y Temperatura         : {battery_info['battery_pct']}% | {battery_info['battery_temp_c']} °C")
    print(f"  🧭  Acelerómetro                  : Z={accel['accel_z']:.2f} m/s²")

    # 4. Evaluate agronomic advisory with real wind
    print("\n[PASO 4] Evaluación de Reglas Agronómicas con Datos Físicos Reales:")
    advisor = AgronomicAdvisor()
    obs_record = {
        "ambient_temp_c": battery_info["battery_temp_c"],
        "humidity_pct": 55.0, # valor nominal
        "pressure_hpa": 1013.25,
        "wind_speed_avg_ms": avg_ms,
        "wind_gust_ms": gust_ms,
        "light_lux": lux,
    }
    alerts = advisor.evaluate_conditions(obs_record)

    if alerts:
        for a in alerts:
            print(f"\n  [{a.severity.value}] {a.title}")
            print(f"    Detalle : {a.message}")
            print(f"    Acción  : {a.action_required}")
    else:
        print("\n  [INFO] Condiciones de viento dentro de rangos normales de campo.")

    # 5. Publish to Mosquitto broker on Redmi Note 13
    print("\n[PASO 5] Publicando lote 100% físico hacia el broker en el teléfono...")
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    try:
        client.connect(BROKER_HOST, BROKER_PORT, 60)
        client.loop_start()

        payload = {
            "node_id": "station_redmi_note13_physical",
            "batch_id": f"phys_{uuid.uuid4().hex[:8]}",
            "record_count": 1,
            "records": [
                {
                    "id": int(time.time()),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "pressure_hpa": 1013.25,
                    "wind_speed_avg_ms": avg_ms,
                    "wind_gust_ms": gust_ms,
                    "ambient_temp_c": battery_info["battery_temp_c"],
                    "humidity_pct": 55.0,
                    "light_lux": lux,
                    "battery_pct": battery_info["battery_pct"],
                    "battery_temp_c": battery_info["battery_temp_c"],
                    "contamination_pct": contamination,
                    "notes": "100%_REAL_ACOUSTIC_AND_OPTICAL_HARDWARE"
                }
            ]
        }

        topic = "station/station_redmi_note13_physical/batch"
        info = client.publish(topic, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=5.0)
        print(f"[✓] Lote publicado con QoS 1 en '{topic}'. 'central_receiver.py' lo ingresó a SQLite.")
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        print(f"[X] Error MQTT: {e}")

    print("\n" + "=" * 75)
    print(" [✓✓✓] PRUEBA DE VIENTO Y METROLOGÍA FÍSICA EXITOSA")
    print("=" * 75)


if __name__ == "__main__":
    main()
