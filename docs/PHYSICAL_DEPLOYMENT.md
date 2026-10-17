# Physical Deployment Guide: Recycled Smartphone Weather Stations

This guide provides the exact configuration and setup procedures to deploy the system onto physical Android devices using Termux.

---

## 1. Device Roles & Network Topology

```
+-------------------------------------------------------------+
| FIELD SENSOR NODE (Phone A)                                 |
| - Connects to Hotspot Wi-Fi                                 |
| - IP: 192.168.43.X (DHCP)                                   |
| - Local Broker: 127.0.0.1:1883                              |
| - Target Master Broker: 192.168.43.1:1883                   |
+-------------------------------------------------------------+
                              |
                     (Agricultural Wi-Fi / Hotspot)
                              v
+-------------------------------------------------------------+
| CENTRAL MASTER NODE (Phone B)                               |
| - Runs Android Wi-Fi Hotspot (AP)                           |
| - Gateway IP: 192.168.43.1                                  |
| - Central Broker: 0.0.0.0:1883                              |
| - Backhaul: Cellular 4G/LTE or Satellite                    |
+-------------------------------------------------------------+
                              |
                     (Internet / Cloud Uplink)
                              v
+-------------------------------------------------------------+
| CLOUD PLATFORM (AWS IoT, ThingsBoard, EMQX, etc.)           |
+-------------------------------------------------------------+
```

---

## 2. Phone A: Field Sensor Node Configuration

### Directory Tree in Termux:
```
/data/data/com.termux/files/home/station/
├── config/
│   ├── mosquitto_node.conf    <- (config/node/mosquitto.conf)
│   └── sync_config.json       <- (config/node/sync_config.json)
├── data/
│   └── telemetry.db           <- SQLite Store & Forward
├── logs/
│   ├── mosquitto.log
│   ├── power.log
│   ├── processor.log
│   └── sync_worker.log
├── scripts/
│   ├── setup_node.sh
│   ├── start_node.sh
│   └── power_manager.sh
└── src/
    ├── dsp/
    ├── storage/
    └── sync/
```

### Configuration Files for Phone A:

#### 1. `~/station/config/mosquitto_node.conf`
```conf
bind_address 127.0.0.1
port 1883
allow_anonymous true
persistence false
max_queued_messages 1000
sys_interval 0
set_tcp_nodelay true
log_dest file /data/data/com.termux/files/home/station/logs/mosquitto.log
log_type error
log_type warning
```

#### 2. `~/station/config/sync_config.json`
```json
{
  "node_id": "station_parcela_01",
  "central_broker_host": "192.168.43.1",
  "central_broker_port": 1883,
  "batch_topic_template": "station/{node_id}/batch",
  "batch_size": 50,
  "sync_interval_seconds": 60,
  "connection_timeout_seconds": 10.0,
  "publish_timeout_seconds": 15.0,
  "max_backoff_seconds": 300
}
```
* **Parameters to change per physical device**:
  * `node_id`: Assign a unique string (e.g. `station_lote_norte`, `station_vinedo_02`).
  * `central_broker_host`: The IP of Phone B. By default, Android Hotspot sets the gateway to `192.168.43.1`.

---

## 3. Phone B: Central Master Node Configuration

### Directory Tree in Termux:
```
/data/data/com.termux/files/home/station/
├── central_data/
│   ├── central_telemetry.db   <- Consolidated SQLite
│   └── mosquitto.db           <- Mosquitto persistence
├── config/
│   ├── mosquitto_central.conf <- (config/central/mosquitto.conf)
│   └── cloud_config.json      <- (config/central/cloud_config.json)
├── logs/
│   ├── mosquitto_central.log
│   ├── central_receiver.log
│   └── cloud_bridge.log
├── scripts/
│   └── start_central.sh
└── src/
    ├── analytics/
    └── central/
```

### Configuration Files for Phone B:

#### 1. `~/station/config/mosquitto_central.conf`
```conf
listener 1883 0.0.0.0
allow_anonymous true
max_connections 100
persistence true
persistence_location /data/data/com.termux/files/home/station/central_data/
persistence_file mosquitto.db
autosave_interval 300
autosave_on_changes false
max_queued_messages 50000
set_tcp_nodelay true
log_dest file /data/data/com.termux/files/home/station/logs/mosquitto_central.log
log_type error
log_type warning
```

#### 2. `~/station/config/cloud_config.json`
```json
{
  "central_station_id": "master_station_valle_central",
  "cloud_broker_host": "mqtt.cloud.example.org",
  "cloud_broker_port": 8883,
  "use_tls": true,
  "username": "station_master",
  "password": "YOUR_SECRET_DEVICE_TOKEN",
  "cloud_uplink_topic": "agrometeo/valle/telemetry",
  "cloud_alerts_topic": "agrometeo/valle/alerts",
  "batch_size": 100,
  "uplink_interval_seconds": 120,
  "max_backoff_seconds": 600
}
```
* **Parameters to change**:
  * `cloud_broker_host` and `cloud_broker_port`: Endpoint of your external cloud server.
  * `username` and `password`: Auth token / credentials for your IoT cloud broker.

---

## 4. Critical Android OS Configuration (Both Devices)

To guarantee 24/7 unattended execution without Android terminating the processes:

1. **Exempt Termux from Battery Optimizations**:
   * Android Settings -> Apps -> Special App Access -> Battery Optimization -> Select `All Apps` -> `Termux` -> Set to **Don't Optimize** (or "Unrestricted").
   * Repeat for `Termux:API` and `Termux:Boot`.
2. **Display & Timeout**:
   * Set Display Timeout to 15 seconds.
   * Enable "Stay Awake while charging" in Android Developer Options (useful for solar charging setups).
3. **Microphone Permissions (Phone A)**:
   * Termux & Native APK: Grant `Record Audio` permission ("Allow all the time" or "Allow while in use").
4. **Termux:Boot Setup**:
   * Install the `Termux:Boot` companion app from F-Droid.
   * Open `Termux:Boot` once to register its broadcast receiver with Android OS.
