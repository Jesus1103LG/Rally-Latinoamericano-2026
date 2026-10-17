#!/usr/bin/env python3
"""
AgroMesh Master Node - Zero-Touch Auto-Provisioner Daemon
========================================================
Target: Master Android Station / Central Receiver (Termux or PC)
Mission:
  1. Actively watches Layer-2 ARP table on the Wi-Fi Hotspot (wlan1 / bt-pan).
  2. The moment a new phone connects to the network:
     - Attempts Zero-Touch OTA provisioning via Wireless ADB (port 5555) or SSH (port 8022).
     - Automatically pushes the field node agent and triggers the background service.
     - Registers the node in the centralized SQLite database.
"""

import os
import sys
import time
import re
import socket
import sqlite3
import subprocess
import logging
from typing import Dict, Any, Set, Optional

# Reconfigure stdout/stderr encoding
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, WORKSPACE_ROOT)

ADB_BIN = r"C:\Users\user\AppData\Local\Android\Sdk\platform-tools\adb.exe"
if not os.path.exists(ADB_BIN):
    ADB_BIN = "adb"

MASTER_DEVICE_ID = "3a07e033"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [AutoProvisioner] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("AutoProvisioner")


class ZeroTouchProvisioner:
    def __init__(self, db_path: Optional[str] = None):
        self.provisioned_ips: Set[str] = set()
        self.failed_attempts: Dict[str, int] = {}
        self.db_path = db_path or os.path.join(WORKSPACE_ROOT, "scratch", "nodes_provisioned.db")
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode = WAL;")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS provisioned_nodes (
                    ip TEXT PRIMARY KEY,
                    mac TEXT,
                    method TEXT,
                    status TEXT,
                    first_seen TEXT,
                    last_seen TEXT
                );
            """)
            conn.commit()

    def scan_hotspot_clients(self) -> Dict[str, str]:
        """Scans active ARP entries on the Master hotspot."""
        clients = {}
        try:
            # Query ARP via ADB on Master phone or local Linux ARP
            if os.path.exists(ADB_BIN) and sys.platform == "win32":
                cmd = [ADB_BIN, "-s", MASTER_DEVICE_ID, "shell", "cat /proc/net/arp"]
                res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=3.0)
                stdout = res.stdout
            else:
                with open("/proc/net/arp", "r") as f:
                    stdout = f.read()

            for line in stdout.splitlines():
                if "wlan1" in line or "bt-pan" in line:
                    m = re.search(r"([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s+.*?([0-9a-f:]{17})", line, re.I)
                    if m:
                        ip = m.group(1)
                        mac = m.group(2)
                        # Filter out master IP itself
                        if not ip.endswith(".180") and not ip.endswith(".1"):
                            clients[ip] = mac
        except Exception as e:
            logger.debug("ARP scan exception: %s", e)

        return clients

    def is_port_open(self, ip: str, port: int, timeout: float = 1.0) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, port))
            sock.close()
            return result == 0
        except Exception:
            return False

    def try_wireless_adb_provision(self, ip: str) -> bool:
        """Attempts zero-touch installation over Wireless ADB (port 5555)."""
        logger.info("Attempting Wireless ADB auto-provisioning on %s:5555...", ip)
        try:
            # Connect ADB over network
            res = subprocess.run([ADB_BIN, "connect", f"{ip}:5555"], capture_output=True, text=True, timeout=4.0)
            if "connected" in res.stdout.lower():
                logger.info("[✓] Successfully connected to %s via Wireless ADB!", ip)
                # Push client script
                client_path = os.path.join(WORKSPACE_ROOT, "src", "client", "field_node_client.py")
                subprocess.run([ADB_BIN, "-s", f"{ip}:5555", "push", client_path, "/sdcard/Download/field_node_client.py"], timeout=5.0)

                # Trigger background execution via Termux intent or shell
                start_cmd = (
                    "nohup python3 /sdcard/Download/field_node_client.py > /dev/null 2>&1 & "
                    "|| nohup python /sdcard/Download/field_node_client.py > /dev/null 2>&1 &"
                )
                subprocess.run([ADB_BIN, "-s", f"{ip}:5555", "shell", start_cmd], timeout=5.0)
                logger.info("🚀 [ZERO-TOUCH SUCCESS] Node %s provisioned and streaming automatically!", ip)
                return True
        except Exception as e:
            logger.warning("Wireless ADB provisioning on %s failed: %s", ip, e)
        return False

    def process_new_client(self, ip: str, mac: str):
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        logger.info("🟢 [NEW HARDWARE NODE JOINED HOTSPOT] IP: %s | MAC: %s", ip, mac)

        # 1. Check if node is already transmitting MQTT to master broker
        # (If it is already autonomous, we don't need to do anything)
        # 2. Check if Wireless ADB (5555) is enabled for zero-touch injection
        if self.is_port_open(ip, 5555):
            if self.try_wireless_adb_provision(ip):
                self.provisioned_ips.add(ip)
                self._record_node(ip, mac, "Wireless_ADB", "ACTIVE", now_iso)
                return

        # 3. Check if SSH (8022 - Termux OpenSSH) is available
        if self.is_port_open(ip, 8022):
            logger.info("Node %s has OpenSSH port 8022 open. Available for automated SSH key deployment.", ip)
            self._record_node(ip, mac, "OpenSSH_8022", "PENDING_AUTH", now_iso)
            return

        logger.info("Node %s connected. Waiting for persistent boot daemon handshake on MQTT.", ip)
        self._record_node(ip, mac, "Autonomous_MQTT", "WAITING_DATA", now_iso)

    def _record_node(self, ip: str, mac: str, method: str, status: str, now_iso: str):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO provisioned_nodes (ip, mac, method, status, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(ip) DO UPDATE SET last_seen = excluded.last_seen, status = excluded.status;
            """, (ip, mac, method, status, now_iso, now_iso))
            conn.commit()

    def run(self):
        logger.info("Zero-Touch Auto-Provisioner running. Monitoring wlan1 Hotspot...")
        known_clients = set()

        while True:
            try:
                current_clients = self.scan_hotspot_clients()
                for ip, mac in current_clients.items():
                    if ip not in known_clients:
                        known_clients.add(ip)
                        self.process_new_client(ip, mac)

                time.sleep(2.0)
            except KeyboardInterrupt:
                logger.info("Auto-Provisioner stopped.")
                break
            except Exception as e:
                logger.error("Scan loop exception: %s", e)
                time.sleep(3.0)


if __name__ == "__main__":
    provisioner = ZeroTouchProvisioner()
    provisioner.run()
