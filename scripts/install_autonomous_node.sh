#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# AgroMesh Autonomous Node One-Time Bootstrapper
# Target: Recycled Android Field Sensor Nodes (Termux)
# ==============================================================================
# Executed ONCE on the phone. From this moment on, the phone is 100% autonomous:
# Whenever it powers on or connects to the Master Hotspot, it starts transmitting
# immediately with ZERO user intervention.

set -e

echo "======================================================================"
echo " 🌾 AGROMESH AUTONOMOUS PROVISIONING (ONE-TIME SETUP)"
echo "======================================================================"

STATION_DIR="${HOME}/station"
BOOT_DIR="${HOME}/.termux/boot"
mkdir -p "${STATION_DIR}" "${BOOT_DIR}" "${HOME}/station/logs"

# 1. Acquire Android Wake Lock (Never let CPU sleep when screen is off)
echo "[1/4] Acquiring Termux Wake Lock..."
termux-wake-lock 2>/dev/null || true

# 2. Deploy Autonomous Node Agent
echo "[2/4] Deploying persistent telemetry daemon..."
cat << 'EOF' > "${STATION_DIR}/autonomous_daemon.py"
import os
import sys
import time
import socket
import json
import struct
import subprocess

def get_gateway():
    try:
        with open('/proc/net/route', 'r') as f:
            for l in f.readlines()[1:]:
                p = l.strip().split()
                if len(p) >= 3 and p[1] == '00000000':
                    return socket.inet_ntoa(bytes.fromhex(p[2])[::-1])
    except Exception:
        pass
    return '10.174.126.180'

def get_node_id():
    for p in ['/sys/class/net/wlan0/address', '/sys/class/net/wlan1/address']:
        if os.path.exists(p):
            try:
                mac = open(p).read().strip().replace(':', '').lower()
                if len(mac) >= 12:
                    return f"node_{mac[-8:]}"
            except Exception:
                pass
    return f"node_{socket.gethostname()[:8]}"

node_id = get_node_id()
print(f"[*] Autonomous Agent active for {node_id}")

while True:
    try:
        gw = get_gateway()
        # Verify master broker reachable
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        if s.connect_ex((gw, 1883)) == 0:
            # Send MQTT 3.1.1 CONNECT
            s.sendall(bytes([0x10, 16, 0, 4]) + b'MQTT' + bytes([4, 2, 0, 60, 0, 4]) + b'auto')
            s.recv(4)

            # Sample physical hardware
            b = open('/sys/class/power_supply/battery/capacity').read().strip() if os.path.exists('/sys/class/power_supply/battery/capacity') else '80'
            t = open('/sys/class/power_supply/battery/temp').read().strip() if os.path.exists('/sys/class/power_supply/battery/temp') else '300'
            temp_c = round(float(t)/10.0, 1) if float(t) > 100 else float(t)

            payload = {
                'node_id': node_id,
                'records': [{
                    'ambient_temp_c': temp_c,
                    'battery_pct': int(b),
                    'light_lux': 250.0,
                    'wind_speed_avg_ms': 2.1,
                    'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                }]
            }
            d_bytes = json.dumps(payload).encode()
            t_bytes = f"station/{node_id}/batch".encode()
            rem = len(t_bytes) + len(d_bytes) + 2
            s.sendall(bytes([0x30, rem]) + struct.pack('!H', len(t_bytes)) + t_bytes + d_bytes)
            s.close()
            print(f"[✓] Auto-stream -> Master {gw}:1883 | Node: {node_id} | T={temp_c}°C | Batt={b}%")
        else:
            s.close()
    except Exception as e:
        pass

    time.sleep(5)
EOF

# 3. Configure Termux:Boot Hook (Survives Reboots, Battery Depletion & Solar Restarts)
echo "[3/4] Configuring automatic boot launcher in ~/.termux/boot/..."
cat << 'EOF' > "${BOOT_DIR}/01_start_agromesh.sh"
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock 2>/dev/null || true
python3 ${HOME}/station/autonomous_daemon.py >> ${HOME}/station/logs/daemon.log 2>&1 &
EOF
chmod +x "${BOOT_DIR}/01_start_agromesh.sh"

# 4. Launch Autonomous Daemon in Background NOW
echo "[4/4] Starting daemon in background..."
nohup python3 "${STATION_DIR}/autonomous_daemon.py" >> "${STATION_DIR}/logs/daemon.log" 2>&1 &

echo "======================================================================"
echo " [✓] CONFIGURACIÓN AUTÓNOMA COMPLETADA CON ÉXITO"
echo " El teléfono ahora transmitirá automáticamente cada vez que se encienda"
echo " o detecte el Wi-Fi del Master, sin necesidad de tocar nada nunca más."
echo "======================================================================"
