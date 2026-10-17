#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# Central Node Master Supervisor & Lifecycle Manager
# Target: Master Android Smartphone (LAN Hotspot Gateway)
# ==============================================================================

set -euo pipefail

BASE_DIR="${HOME}/station"
CONFIG_FILE="${BASE_DIR}/config/mosquitto_central.conf"
LOG_DIR="${BASE_DIR}/logs"
PID_DIR="${BASE_DIR}/pids"

mkdir -p "${LOG_DIR}" "${PID_DIR}" "${BASE_DIR}/central_data"

echo "[*] [$(date -Iseconds)] Initiating Central Master Node lifecycle..."

# 1. Acquire Android Partial Wake Lock
echo "[*] Requesting Termux Wake Lock..."
termux-wake-lock || echo "[!] Warning: termux-wake-lock command failed or termux-api not present."

# 2. Launch Central Mosquitto Broker (LAN / Hotspot listener)
if pgrep -f "mosquitto -c ${CONFIG_FILE}" > /dev/null; then
    echo "[i] Central Mosquitto broker is already running."
else
    echo "[*] Starting Central Mosquitto daemon..."
    mosquitto -c "${CONFIG_FILE}" -d
    sleep 1
fi

# Verify LAN broker listener using Python socket (Toybox nc does not support -z)
if ! python3 -c "import socket; s = socket.socket(); s.connect(('127.0.0.1', 1883)); s.close()" 2>/dev/null; then
    echo "[!] ERROR: Central Mosquitto failed to respond on port 1883"
    exit 1
fi
echo "[✓] Central broker active on port 1883"

# 3. Launch Central Batch Receiver Daemon
if pgrep -f "central_receiver.py" > /dev/null; then
    echo "[i] Central receiver daemon is already running."
else
    echo "[*] Starting Central Batch Receiver..."
    python3 "${BASE_DIR}/src/central/central_receiver.py" >> "${LOG_DIR}/central_receiver.log" 2>&1 &
    echo $! > "${PID_DIR}/central_receiver.pid"
fi

# 4. Launch Cloud Uplink & Agronomic Alerts Bridge
if pgrep -f "cloud_bridge.py" > /dev/null; then
    echo "[i] Cloud bridge is already running."
else
    echo "[*] Starting Cloud Uplink Bridge..."
    python3 "${BASE_DIR}/src/central/cloud_bridge.py" >> "${LOG_DIR}/cloud_bridge.log" 2>&1 &
    echo $! > "${PID_DIR}/cloud_bridge.pid"
fi

# 5. Process Supervision Loop
echo "[✓] Central Station services spawned. Entering watchdog loop..."
while true; do
    # Check Central Mosquitto
    if ! pgrep -f "mosquitto -c ${CONFIG_FILE}" > /dev/null; then
        echo "[!] [$(date -Iseconds)] Watchdog: Central Mosquitto dead. Reviving..."
        mosquitto -c "${CONFIG_FILE}" -d
    fi

    # Check Central Receiver Daemon
    if ! pgrep -f "central_receiver.py" > /dev/null; then
        echo "[!] [$(date -Iseconds)] Watchdog: Central Receiver dead. Reviving..."
        python3 "${BASE_DIR}/src/central/central_receiver.py" >> "${LOG_DIR}/central_receiver.log" 2>&1 &
        echo $! > "${PID_DIR}/central_receiver.pid"
    fi

    # Check Cloud Bridge Daemon
    if ! pgrep -f "cloud_bridge.py" > /dev/null; then
        echo "[!] [$(date -Iseconds)] Watchdog: Cloud Bridge dead. Reviving..."
        python3 "${BASE_DIR}/src/central/cloud_bridge.py" >> "${LOG_DIR}/cloud_bridge.log" 2>&1 &
        echo $! > "${PID_DIR}/cloud_bridge.pid"
    fi

    sleep 30
done
