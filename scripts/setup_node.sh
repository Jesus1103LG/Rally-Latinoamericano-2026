#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# Setup Script for Android Field Node (Termux Environment)
# Target Architecture: ARM64 / ARMv7 Recycled Smartphones
# ==============================================================================

set -euo pipefail

echo "[+] Initializing Field Sensor Node bootstrap..."

# 1. Update Core Repositories
echo "[+] Updating Termux package indexes..."
pkg update -y
pkg upgrade -y

# 2. Install Essential System Packages
echo "[+] Installing native runtime dependencies..."
pkg install -y \
    mosquitto \
    python \
    termux-api \
    sqlite \
    clang \
    make \
    libffi \
    openssl \
    curl \
    jq

# 3. Install Pre-compiled Scientific Libraries
# Note: Building numpy/scipy from source on ARM phone CPUs consumes hours and triggers thermal throttling.
# We leverage Termux's pre-compiled binary packages.
echo "[+] Installing pre-compiled scientific Python packages..."
pkg install -y python-numpy python-scipy || {
    echo "[!] Fallback: configuring tur-repo for scientific wheels..."
    pkg install -y tur-repo || true
    pkg install -y python-numpy python-scipy
}

# 4. Setup Python Virtual Environment or Pip Packages
echo "[+] Installing lightweight Python libraries..."
pip install --upgrade pip setuptools wheel
pip install paho-mqtt

# 5. Create Station Directory Hierarchy
STATION_DIR="${HOME}/station"
echo "[+] Creating directory tree in ${STATION_DIR}..."

mkdir -p "${STATION_DIR}/config"
mkdir -p "${STATION_DIR}/data"
mkdir -p "${STATION_DIR}/logs"
mkdir -p "${STATION_DIR}/scripts"
mkdir -p "${HOME}/.termux/boot"

# 6. Copy Configurations
if [ -f "config/node/mosquitto.conf" ]; then
    cp config/node/mosquitto.conf "${STATION_DIR}/config/mosquitto_node.conf"
fi

# 7. Configure Termux:Boot for Automatic Unattended Recovery
# If phone reboots on battery drop and solar panel recharges it, it must resume automatically.
BOOT_SCRIPT="${HOME}/.termux/boot/01_start_station.sh"
cat << 'EOF' > "${BOOT_SCRIPT}"
#!/data/data/com.termux/files/usr/bin/bash
# Auto-start hook executed by Termux:Boot app on device power-on
${HOME}/station/scripts/start_node.sh >> ${HOME}/station/logs/boot.log 2>&1
EOF
chmod +x "${BOOT_SCRIPT}"

echo "[✓] Environment initialized successfully."
echo "[i] Ensure Termux:API and Termux:Boot apps are installed and exempted from Android battery optimizations."
