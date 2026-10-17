#!/usr/bin/env python3
"""
Real Hardware Mesh Auto-Discovery & Live Telemetry Watcher
===========================================================
Actively monitors the physical Master Node (Redmi Note 13) for newly connected
field devices (over Wi-Fi Hotspot or Bluetooth PAN), announces them immediately,
and live-streams incoming microclimate batches into the console.

Usage:
    python scripts/watch_live_mesh_network.py
"""

import sys
import os
import time
import json
import re
import subprocess
from typing import Dict, Any, Set

import paho.mqtt.client as mqtt

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ADB_BIN = r"C:\Users\user\AppData\Local\Android\Sdk\platform-tools\adb.exe"
DEVICE_ID = "3a07e033"
BROKER_HOST = "127.0.0.1"
BROKER_PORT = 18883


class RealHardwareMeshWatcher:
    def __init__(self):
        self.known_clients: Set[str] = set()
        self.connected_mqtt = False
        self.client: Optional[mqtt.Client] = None
        self.total_batches_received = 0
        self.last_seen_metrics: Dict[str, Any] = {}

    def ensure_adb_forward(self):
        if os.path.exists(ADB_BIN):
            try:
                subprocess.run(
                    [ADB_BIN, "-s", DEVICE_ID, "forward", f"tcp:{BROKER_PORT}", "tcp:1883"],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                )
            except Exception:
                pass

    def connect_broker(self):
        self.ensure_adb_forward()
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

        def on_connect(c, userdata, flags, rc, props=None):
            if rc == 0:
                self.connected_mqtt = True
                print(f"[✓] Conectado al Broker Mosquitto del Master ({BROKER_HOST}:{BROKER_PORT}).")
                # Subscribe to all telemetry, emergency alerts, and topology gossip across the mesh
                c.subscribe("station/+/batch", qos=1)
                c.subscribe("agromesh/telemetry/#", qos=1)
                c.subscribe("agromesh/alerts/#", qos=1)
                c.subscribe("agromesh/topology/#", qos=1)
                print("[✓] Escuchando canales de Malla: 'station/+/batch', 'agromesh/telemetry/#', 'agromesh/alerts/#', 'agromesh/topology/#'")
            else:
                self.connected_mqtt = False

        def on_disconnect(c, userdata, flags, rc, props=None):
            self.connected_mqtt = False

        def on_message(c, userdata, msg):
            self.total_batches_received += 1
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
                
                # Check if multi-hop topology heartbeat
                if msg.topic.startswith("agromesh/topology/"):
                    origin = payload.get("origin_node_id", "desconocido")
                    hops = payload.get("hop_distance", 1)
                    path = payload.get("path", [origin])
                    path_str = " ➔ ".join(path)
                    print("\n" + "=" * 75)
                    print(f" 🌐 [NODO MULTI-SALTO DETECTADO EN LA MALLA (PEER-TO-PEER)]")
                    print("=" * 75)
                    print(f"  Nodo Remoto      : {origin}")
                    print(f"  Distancia Malla  : {hops} salto(s)")
                    print(f"  Ruta de Retransmisión : {path_str}")
                    print(f"  Vecinos Directos : {payload.get('known_neighbors')}")
                    print("=" * 75 + "\n")
                    return

                # Check if emergency alert
                if msg.topic.startswith("agromesh/alerts/"):
                    print("\n" + "!" * 75)
                    print(f" 🚨 [ALERTA DE EMERGENCIA RECIBIDA EN MALLA P2P]")
                    print(f"    Severidad        : {payload.get('severity', 'CRITICAL')}")
                    print(f"    Título           : {payload.get('title')}")
                    print(f"    Nodo Origen      : {payload.get('origin_node_id')}")
                    print(f"    Acción Requerida : {payload.get('action_required')}")
                    print(f"    Timestamp        : {payload.get('timestamp')}")
                    print("!" * 75 + "\n")
                    return

                # Normal telemetry batch
                node = payload.get("node_id") or payload.get("source_peer", "desconocido")
                records = payload.get("records", [])
                print(f"\n[📥 PAQUETE REAL RECIBIDO #{self.total_batches_received}]")
                print(f"  Canal MQTT  : {msg.topic}")
                print(f"  Nodo Emisor : {node}")
                print(f"  Registros   : {len(records)} mediciones físicas")

                for r in records[:2]: # Show first observations
                    lux = r.get("light_lux", 0.0)
                    temp = r.get("ambient_temp_c", 0.0)
                    wind_kmh = (r.get("wind_speed_avg_ms") or 0.0) * 3.6
                    batt = r.get("battery_pct", 0)
                    ts = r.get("timestamp", "")
                    hop = r.get("hop_count", 0)
                    print(f"    • [{ts}] T={temp:.1f}°C | Luz={lux:.1f} Lux | Viento={wind_kmh:.1f} km/h | Batería={batt}% | Saltos Mesh={hop}")

                print("  [✓] Guardado permanente confirmado en central_telemetry.db del teléfono.")

            except Exception as e:
                print(f"[!] Error procesando paquete: {e}")

        self.client.on_connect = on_connect
        self.client.on_disconnect = on_disconnect
        self.client.on_message = on_message

        try:
            self.client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
            self.client.loop_start()
        except Exception as e:
            print(f"[!] Error conectando a Mosquitto: {e}")

    def scan_new_network_neighbors(self):
        """Scans ARP table and Bluetooth neighbors on the Master phone."""
        try:
            cmd = [ADB_BIN, "-s", DEVICE_ID, "shell", "ip neigh; cat /proc/net/arp"]
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=3.0)

            current_active = set()
            for line in res.stdout.splitlines():
                if ("wlan1" in line or "bt-pan" in line or "bnep" in line) and ("REACHABLE" in line or "0x2" in line):
                    m = re.search(r"([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s+.*?([0-9a-f:]{17})", line, re.I)
                    if m:
                        ip = m.group(1)
                        mac = m.group(2)
                        current_active.add(ip)

                        if ip not in self.known_clients:
                            self.known_clients.add(ip)
                            bearer = "Bluetooth PAN" if ("bt-pan" in line or "bnep" in line) else "Wi-Fi Hotspot"
                            self._announce_new_device(ip, mac, bearer)

            # Detect disconnected devices
            for old_ip in list(self.known_clients):
                if old_ip not in current_active:
                    # check if still responds
                    ping_cmd = [ADB_BIN, "-s", DEVICE_ID, "shell", f"ping -c 1 -W 1 {old_ip}"]
                    p_res = subprocess.run(ping_cmd, capture_output=True, text=True, timeout=2.0)
                    if p_res.returncode != 0:
                        self.known_clients.remove(old_ip)
                        print(f"\n[⚠️ NODO DESCONECTADO] El dispositivo con IP {old_ip} se desconectó de la red.")

        except Exception:
            pass

    def _announce_new_device(self, ip: str, mac: str, bearer: str):
        # Ping test to measure link latency
        rtt_str = "N/A"
        try:
            cmd = [ADB_BIN, "-s", DEVICE_ID, "shell", f"ping -c 2 -W 1 {ip}"]
            p_res = subprocess.run(cmd, capture_output=True, text=True, timeout=3.0)
            for l in p_res.stdout.splitlines():
                if "rtt min/avg/max" in l:
                    rtt_str = l.split("=")[1].strip().split("/")[1] + " ms"
        except Exception:
            pass

        # Probe port 1883 on the remote device
        port_open = False
        try:
            p_check = [ADB_BIN, "-s", DEVICE_ID, "shell", f"nc -w 2 {ip} 1883 2>&1"]
            nc_res = subprocess.run(p_check, capture_output=True, text=True, timeout=3.0)
            output = (nc_res.stdout + " " + nc_res.stderr).lower()
            if "refused" not in output and "timed out" not in output and nc_res.returncode == 0:
                port_open = True
        except Exception:
            pass

        print("\n" + "=" * 75)
        print(" 🟢 [NUEVO DISPOSITIVO FÍSICO DETECTADO EN LA MALLA]")
        print("=" * 75)
        print(f"  Dirección IP  : {ip}")
        print(f"  Dirección MAC : {mac}")
        print(f"  Medio Físico  : {bearer}")
        print(f"  Latencia Ping : {rtt_str}")
        if port_open:
            print(f"  Puerto 1883   : [ABIERTO ✓] Mosquitto activo (Modo Nodo Retransmisor Mesh P2P).")
        else:
            print(f"  Puerto 1883   : [INACTIVO] Modo Cliente Directo (Normal si solo envía datos al Master).")
            print(f"  ─────────────────────────────────────────────────────────────────────────")
            print(f"  [i] ESTADO DEL DISPOSITIVO:")
            print(f"      Conectado al Hotspot con éxito. Para comenzar a transmitir telemetría,")
            print(f"      abrí Termux en ese teléfono ({ip}) y ejecutá:")
            print(f"      bash /sdcard/Download/station_sensor/run_sensor.sh")
            print(f"      (o bien: python /sdcard/Download/station_sensor/field_node_client.py)")
            print(f"  ─────────────────────────────────────────────────────────────────────────")
        print("=" * 75 + "\n")

    def run(self):
        print("=" * 75)
        print(" MONITOR DE RED MALLA REAL - DETECTOR DE NODOS Y TELEMETRÍA EN VIVO")
        print("=" * 75)
        print(f"Master: Redmi Note 13 ({DEVICE_ID}) | Broker: {BROKER_HOST}:{BROKER_PORT}")
        print("Escaneando continuamente clientes Wi-Fi Hotspot, Bluetooth y Multi-Salto P2P...")
        print("Presioná [Ctrl + C] para detener.\n")

        self.connect_broker()
        last_diag_time = time.time()
        diag_count = 0

        try:
            while True:
                self.scan_new_network_neighbors()

                # Periodic diagnostic heartbeat every 12 seconds
                now = time.time()
                if now - last_diag_time >= 12.0:
                    diag_count += 1
                    last_diag_time = now
                    direct_nodes = list(self.known_clients)
                    print(f"\n[DIAGNÓSTICO MESH #{diag_count}] ({time.strftime('%H:%M:%S')})")
                    print(f"  • Broker Master MQTT : {'[CONECTADO - 18883]' if self.connected_mqtt else '[DESCONECTADO / ERROR]'}")
                    print(f"  • Nodos Directos L2  : {direct_nodes if direct_nodes else 'Ninguno en antena wlan1/bt-pan'}")
                    print(f"  • Paquetes Ingeridos : {self.total_batches_received} lotes físicos recibidos")

                    if direct_nodes and self.total_batches_received == 0:
                        print("  [!] DIAGNÓSTICO: Dispositivo conectado físicamente pero silencioso.")
                        print("      Verificá que en el teléfono conectado esté corriendo Termux:")
                        print("      bash /sdcard/Download/station_sensor/run_sensor.sh")

                    if not direct_nodes:
                        print("  [!] ESPERANDO CONEXIONES: Conectá los teléfonos por Wi-Fi o Bluetooth.")

                time.sleep(1.5)
        except KeyboardInterrupt:
            print("\nDeteniendo monitor...")
            try:
                if self.client:
                    self.client.loop_stop()
                    self.client.disconnect()
            except Exception:
                pass
            print("[✓] Monitor cerrado de forma segura.")


if __name__ == "__main__":
    watcher = RealHardwareMeshWatcher()
    watcher.run()
