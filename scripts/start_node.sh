#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# Field Node Process Supervisor & Lifecycle Manager
# Target: Termux background process orchestration
# ==============================================================================

set -euo pipefail

BASE_DIR="${HOME}/station"
CONFIG_FILE="${BASE_DIR}/config/mosquitto_node.conf"
LOG_DIR="${BASE_DIR}/logs"
PID_DIR="${BASE_DIR}/pids"

mkdir -p "${LOG_DIR}" "${PID_DIR}"

echo "[*] [$(date -Iseconds)] Initiating Field Node lifecycle..."

# 1. Acquire Android Partial Wake Lock to prevent CPU sleep during active routines
echo "[*] Requesting Termux Wake Lock..."
termux-wake-lock || echo "[!] Warning: termux-wake-lock command failed or termux-api not present."

# 2. Launch Local Mosquitto Broker
if pgrep -f "mosquitto -c ${CONFIG_FILE}" > /dev/null; then
    echo "[i] Mosquitto broker is already running."
else
    echo "[*] Starting local Mosquitto daemon..."
    mosquitto -c "${CONFIG_FILE}" -d
    sleep 1
fi

# Verify local broker listener using Python socket (Toybox nc does not support -z)
if ! python3 -c "import socket; s = socket.socket(); s.connect(('127.0.0.1', 1883)); s.close()" 2>/dev/null; then
    echo "[!] ERROR: Local Mosquitto failed to respond on port 1883"
    exit 1
fi
echo "[✓] Local broker active on 127.0.0.1:1883"

# 3. Launch Power and Thermal Supervisor in Background
if pgrep -f "power_manager.sh" > /dev/null; then
    echo "[i] Power manager is already running."
else
    echo "[*] Starting Power & Thermal Manager..."
    "${BASE_DIR}/scripts/power_manager.sh" >> "${LOG_DIR}/power.log" 2>&1 &
    echo $! > "${PID_DIR}/power_manager.pid"
fi

# 4. Launch Node Telemetry & DSP Processing Daemon
if pgrep -f "node_processor.py" > /dev/null; then
    echo "[i] Node processor daemon is already running."
else
    echo "[*] Starting Node Telemetry & DSP Processor..."
    python3 "${BASE_DIR}/src/daemon/node_processor.py" >> "${LOG_DIR}/processor.log" 2>&1 &
    echo $! > "${PID_DIR}/node_processor.pid"
fi

# 5. Launch Offline-First Batch Synchronization Worker
if pgrep -f "batch_sync_worker.py" > /dev/null; then
    echo "[i] Batch sync worker is already running."
else
    echo "[*] Starting Offline Batch Sync Worker..."
    python3 "${BASE_DIR}/src/sync/batch_sync_worker.py" >> "${LOG_DIR}/sync_worker.log" 2>&1 &
    echo $! > "${PID_DIR}/batch_sync_worker.pid"
fi

# 6. Launch AgroMesh Decentralized P2P Gossip Engine
if pgrep -f "mesh_p2p_sync.py" > /dev/null; then
    echo "[i] AgroMesh P2P sync daemon is already running."
else
    echo "[*] Starting AgroMesh P2P Sync Daemon..."
    python3 "${BASE_DIR}/src/sync/mesh_p2p_sync.py" >> "${LOG_DIR}/mesh_sync.log" 2>&1 &
    echo $! > "${PID_DIR}/mesh_p2p_sync.pid"
fi

# 7. Process Supervision Loop
echo "[✓] Station node services spawned. Entering watchdog loop..."
while true; do
    # Check Mosquitto
    if ! pgrep -f "mosquitto -c ${CONFIG_FILE}" > /dev/null; then
        echo "[!] [$(date -Iseconds)] Watchdog: Mosquitto dead. Reviving..."
        mosquitto -c "${CONFIG_FILE}" -d
    fi

    # Check Power Manager
    if ! pgrep -f "power_manager.sh" > /dev/null; then
        echo "[!] [$(date -Iseconds)] Watchdog: Power Manager dead. Reviving..."
        "${BASE_DIR}/scripts/power_manager.sh" >> "${LOG_DIR}/power.log" 2>&1 &
        echo $! > "${PID_DIR}/power_manager.pid"
    fi

    # Check Node Processor Daemon
    if ! pgrep -f "node_processor.py" > /dev/null; then
        echo "[!] [$(date -Iseconds)] Watchdog: Node Processor dead. Reviving..."
        python3 "${BASE_DIR}/src/daemon/node_processor.py" >> "${LOG_DIR}/processor.log" 2>&1 &
        echo $! > "${PID_DIR}/node_processor.pid"
    fi

    # Check Batch Sync Worker
    if ! pgrep -f "batch_sync_worker.py" > /dev/null; then
        echo "[!] [$(date -Iseconds)] Watchdog: Batch Sync Worker dead. Reviving..."
        python3 "${BASE_DIR}/src/sync/batch_sync_worker.py" >> "${LOG_DIR}/sync_worker.log" 2>&1 &
        echo $! > "${PID_DIR}/batch_sync_worker.pid"
    fi

    # Check AgroMesh P2P Sync
    if ! pgrep -f "mesh_p2p_sync.py" > /dev/null; then
        echo "[!] [$(date -Iseconds)] Watchdog: AgroMesh Sync dead. Reviving..."
        python3 "${BASE_DIR}/src/sync/mesh_p2p_sync.py" >> "${LOG_DIR}/mesh_sync.log" 2>&1 &
        echo $! > "${PID_DIR}/mesh_p2p_sync.pid"
    fi

    # Sleep between health-checks (low CPU cadence)
    sleep 30
done
