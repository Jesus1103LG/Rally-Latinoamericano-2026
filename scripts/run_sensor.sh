#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# AgroMesh Quick Runner for Sensor Nodes
# Target: Any recycled Android device running Termux
# ==============================================================================

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_SCRIPT="${DIR}/../src/client/field_node_client.py"

# If script is run directly inside station_sensor folder
if [ ! -f "${NODE_SCRIPT}" ]; then
    NODE_SCRIPT="${DIR}/field_node_client.py"
fi
if [ ! -f "${NODE_SCRIPT}" ]; then
    NODE_SCRIPT="$(find /sdcard/Download -name 'field_node_client.py' 2>/dev/null | head -n 1)"
fi

echo "[*] Iniciando Nodo Sensor AgroMesh en Termux..."

# 1. Acquire partial wake lock if Termux API exists to survive screen lock
termux-wake-lock 2>/dev/null || true

# 2. Check if python is available
if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
    echo "[*] Instalando Python ligero en Termux..."
    pkg update -y && pkg install -y python
fi

PYTHON_CMD="python3"
if ! command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python"
fi

# 3. Optional: Install mosquitto for mesh relay if desired
if ! command -v mosquitto >/dev/null 2>&1; then
    echo "[i] mosquitto no detectado. Para activar el modo repetidor Mesh en este nodo:"
    echo "    pkg install -y mosquitto"
fi

echo "[✓] Lanzando cliente de telemetría y sensores..."
exec ${PYTHON_CMD} "${NODE_SCRIPT}"
