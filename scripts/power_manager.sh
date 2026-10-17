#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# Extreme Power & Battery Thermal Supervisor
# Target: Solar-powered Android Field Station
# Strategy: Prevent thermal runaway, lithium degradation, and excessive drain.
# ==============================================================================

set -euo pipefail

BASE_DIR="${HOME}/station"
DATA_DIR="${BASE_DIR}/data"
mkdir -p "${DATA_DIR}"

FLAG_THERMAL="${DATA_DIR}/throttle_thermal.flag"
FLAG_LOW_BATTERY="${DATA_DIR}/throttle_battery.flag"

# Thresholds
CRITICAL_TEMP_C=45.0   # Lithium batteries degrade rapidly above 45°C
SAFE_TEMP_C=39.0       # Hysteresis recovery temperature
CRITICAL_BATTERY_PCT=15
CHECK_INTERVAL_SEC=60

get_battery_status() {
    # Method 1: Fast Kernel Sysfs direct read (zero IPC overhead)
    if [ -f "/sys/class/power_supply/battery/temp" ] && [ -f "/sys/class/power_supply/battery/capacity" ]; then
        local raw_temp
        local capacity
        raw_temp=$(cat /sys/class/power_supply/battery/temp 2>/dev/null || echo "0")
        capacity=$(cat /sys/class/power_supply/battery/capacity 2>/dev/null || echo "100")
        
        # Kernel temp is typically in tenths of a degree Celsius (e.g., 385 -> 38.5)
        # Handle devices reporting in millidegrees (e.g. 38500)
        if [ "${raw_temp}" -gt 1000 ]; then
            temp=$(awk "BEGIN {print ${raw_temp}/1000}")
        elif [ "${raw_temp}" -gt 100 ]; then
            temp=$(awk "BEGIN {print ${raw_temp}/10}")
        else
            temp="${raw_temp}"
        fi
        echo "${temp} ${capacity}"
        return 0
    fi

    # Method 2: Termux API fallback
    if command -v termux-battery-status > /dev/null 2>&1; then
        local status_json
        status_json=$(termux-battery-status 2>/dev/null || echo "{}")
        local temp
        local capacity
        temp=$(echo "${status_json}" | jq -r '.temperature // 25.0')
        capacity=$(echo "${status_json}" | jq -r '.percentage // 100')
        echo "${temp} ${capacity}"
        return 0
    fi

    # Default fallback if telemetry is unreadable
    echo "30.0 80"
}

enforce_display_off() {
    # Ensure screen brightness is locked to 0 to prevent accidental OLED/LCD drain
    if command -v termux-brightness > /dev/null 2>&1; then
        termux-brightness 0 2>/dev/null || true
    fi
}

echo "[*] Power Manager started. Supervisory interval: ${CHECK_INTERVAL_SEC}s"
enforce_display_off

while true; do
    read -r TEMP_C BATT_PCT <<< "$(get_battery_status)"

    # 1. Thermal Overheat Protection (Crucial inside solar-exposed enclosure)
    IS_HOT=$(awk "BEGIN {print (${TEMP_C} >= ${CRITICAL_TEMP_C}) ? 1 : 0}")
    IS_SAFE=$(awk "BEGIN {print (${TEMP_C} <= ${SAFE_TEMP_C}) ? 1 : 0}")

    if [ "${IS_HOT}" -eq 1 ]; then
        if [ ! -f "${FLAG_THERMAL}" ]; then
            echo "[!] THERMAL THRESHOLD EXCEEDED: ${TEMP_C}°C. Enforcing system duty-cycle throttle."
            touch "${FLAG_THERMAL}"
        fi
    elif [ "${IS_SAFE}" -eq 1 ] && [ -f "${FLAG_THERMAL}" ]; then
        echo "[✓] Temperature stabilized: ${TEMP_C}°C. Clearing thermal throttle."
        rm -f "${FLAG_THERMAL}"
    fi

    # 2. Battery Depletion Protection
    if [ "${BATT_PCT}" -le "${CRITICAL_BATTERY_PCT}" ]; then
        if [ ! -f "${FLAG_LOW_BATTERY}" ]; then
            echo "[!] LOW BATTERY DETECTED: ${BATT_PCT}%. Entering conservative sampling."
            touch "${FLAG_LOW_BATTERY}"
        fi
    else
        if [ -f "${FLAG_LOW_BATTERY}" ]; then
            echo "[✓] Battery recovered: ${BATT_PCT}%. Restoring standard cycles."
            rm -f "${FLAG_LOW_BATTERY}"
        fi
    fi

    sleep "${CHECK_INTERVAL_SEC}"
done
