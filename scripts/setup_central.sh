#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# Setup Script for Central Master Node (Termux Environment)
# Target: Master Android Phone / Hotspot Gateway
# ==============================================================================

set -euo pipefail

echo "[+] Initializing Central Master Node bootstrap..."

# 1. Update Termux Repositories
echo "[+] Updating Termux package indexes..."
pkg update -y
pkg upgrade -y

# 2. Install Master Dependencies
echo "[+] Installing native runtime packages..."
pkg install -y \
    mosquitto \
    python \
    termux-api \
    sqlite \
    curl \
    jq

# 3. Install Python Libraries
echo "[+] Installing Python MQTT and runtime dependencies..."
pip install --upgrade pip setuptools wheel
pip install paho-mqtt

# 4. Create Master Station Hierarchy
STATION_DIR="${HOME}/station"
echo "[+] Ensuring directory tree in ${STATION_DIR}..."

mkdir -p "${STATION_DIR}/config"
mkdir -p "${STATION_DIR}/central_data"
mkdir -p "${STATION_DIR}/logs"
mkdir -p "${STATION_DIR}/pids"
mkdir -p "${STATION_DIR}/scripts"
mkdir -p "${STATION_DIR}/src/central"
mkdir -p "${STATION_DIR}/src/analytics"
mkdir -p "${HOME}/.termux/boot"

# 5. Fix Permissions
chmod +x "${STATION_DIR}/scripts/"*.sh 2>/dev/null || true

# 6. Configure Termux:Boot for Automatic Unattended Recovery
BOOT_SCRIPT="${HOME}/.termux/boot/01_start_master.sh"
cat << 'EOF' > "${BOOT_SCRIPT}"
#!/data/data/com.termux/files/usr/bin/bash
# Auto-start hook executed by Termux:Boot on phone power-on
${HOME}/station/scripts/start_central.sh >> ${HOME}/station/logs/boot_master.log 2>&1
EOF
chmod +x "${BOOT_SCRIPT}"

echo "[✓] Master Station environment initialized successfully."
echo "[i] Run: bash ~/station/scripts/start_central.sh to start services."
