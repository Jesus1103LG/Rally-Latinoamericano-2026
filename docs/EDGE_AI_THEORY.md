# Theoretical Edge AI Architecture for Agrometeorological Microclimate Forecasting

## 1. Problem Formulation and Constraints in Recycled Hardware

Deploying Artificial Intelligence on upcycled, solar-powered smartphones operating in agricultural fields imposes extreme constraints:
1. **Thermal Envelope**: Under direct sunlight inside a weather-sealed enclosure (IP65+), ambient temperatures reach 45°C–55°C. Heavy matrix multiplication (GEMM) in deep learning frameworks causes thermal throttling, battery swelling, and device shutdown.
2. **RAM & Cache Constraints**: Legacy Android phones often have 1 GB to 2 GB of LPDDR3 RAM, shared between Android OS and the GPU/Modem. Heavy Python frameworks (PyTorch, standard TensorFlow) consume 300+ MB of RAM on startup alone, causing immediate Android Low Memory Killer (LMK) invocations.
3. **Inference vs. Physical Heuristics Trade-off**: The vast majority of microclimate phenomena follow deterministic thermodynamic and aerodynamic laws (e.g., psychrometrics, adiabatic barometric lapse, boundary layer friction). The optimal Edge AI architecture is **Hybrid**: physical equations handle baseline physics, while lightweight ML models learn local non-linear microtopography and soil moisture inertia.

---

## 2. Feature Engineering Pipeline

Raw data is captured every 10 minutes from the physical sensors and the acoustic anemometer. At step $t$, the feature extractor computes an input vector $\mathbf{x}_t \in \mathbb{R}^{d}$:

### A. Raw Temporal Inputs (Lookback Window: $k = 12$ steps = 2 hours)
* Barometric pressure: $P_t$ (hPa)
* Ambient temperature: $T_t$ (°C)
* Relative humidity: $RH_t$ (%)
* Sustained wind speed: $\bar{v}_t$ (m/s)
* Wind gust speed: $v_{\text{gust}, t}$ (m/s)
* Ambient illuminance: $L_t$ (lux)

### B. Derived Thermodynamic & Aerodynamic Features
1. **Barometric Tendency Rate ($\Delta P / \Delta t$)**:
   $$\dot{P}_t = \frac{P_t - P_{t-k}}{\Delta t}$$
   Essential indicator of mesoscale convective fronts and frontal squalls.
2. **Magnus-Tetens Dew Point ($T_{\text{dew}}$) & Dew Point Depression ($\Delta T_{\text{dew}}$)**:
   $$\alpha(T, RH) = \frac{17.27 \cdot T}{237.7 + T} + \ln\left(\frac{RH}{100}\right)$$
   $$T_{\text{dew}} = \frac{237.7 \cdot \alpha(T, RH)}{17.27 - \alpha(T, RH)}$$
   $$\Delta T_{\text{dew}, t} = T_t - T_{\text{dew}, t}$$
3. **Atmospheric Turbulence Intensity ($I_t$)**:
   $$I_t = \frac{v_{\text{gust}, t} - \bar{v}_t}{\bar{v}_t + \epsilon}$$
4. **Delta T (Evaporative Potential for Spraying)**:
   $$\Delta T_{\text{spray}, t} \approx 0.72 \cdot (T_t - T_{\text{dew}, t})$$
5. **Diurnal Cyclic Harmonics**:
   $$\sin\left(\frac{2\pi \cdot \text{hour}}{24}\right), \quad \cos\left(\frac{2\pi \cdot \text{hour}}{24}\right)$$

---

## 3. Candidate Edge AI Models & Architectural Comparison

| Architecture | Model Size | RAM Footprint | Inference Latency (ARMv7/A53) | Edge Suitability |
| :--- | :--- | :--- | :--- | :--- |
| **Vanilla LSTM / GRU (FP32)** | ~1.5 MB | > 80 MB | 45 ms | Poor (thermal heating) |
| **INT8 Quantized 1D-CNN (TFLite Micro)** | < 45 KB | < 2 MB | 1.8 ms | Excellent |
| **LightGBM (C-Inference / Treelite)** | < 120 KB | < 4 MB | 0.4 ms | Outstanding |
| **Auto-Regressive State Space (ARIMA/Kalman)** | < 10 KB | < 1 MB | 0.2 ms | Ideal for Trends |

### Recommended Edge AI Architecture: Multi-Task LightGBM or INT8 Temporal 1D-CNN

Rather than heavy recurrent loops, a **Dilated Temporal 1D-Convolutional Network (TCN)** or **Compiled Gradient Boosted Trees (Treelite/LightGBM)** provides:
* Fixed-memory static buffers (zero heap allocations during inference).
* Sub-millisecond CPU burst, allowing the phone to return immediately to `deep sleep`.

---

## 4. Multi-Horizon Target Objectives

The model maps the feature sequence $\mathbf{X}_{t-12:t}$ to three actionable forecast heads:

1. **Frost Risk Probability ($\hat{y}_{\text{frost}} \in [0, 1]$)**:
   Predicts whether $T_{t+h} \le 0.5^\circ\text{C}$ within a prediction horizon of $h \in \{2\text{h}, 4\text{h}, 6\text{h}\}$.
   Combines nocturnal radiative cooling rates with dew point proximity and low wind conditions ($v < 1.5\text{ m/s}$, zero atmospheric mixing).
2. **Gale / Severe Wind Gust Alert ($\hat{y}_{\text{gust}} \in [0, 1]$)**:
   Predicts whether $v_{\text{gust}, t+h} \ge 35\text{ km/h}$ based on upstream barometric pressure plunge and turbulence intensity buildup.
3. **Fumigation / Spraying Window Score ($\hat{y}_{\text{spray}} \in [0, 100]$)**:
   A composite index of spray drift risk, droplet survival, and canopy penetration.

---

## 5. Offline-First Lifecycle & Cloud-Assisted Retraining

```
 [Field Nodes]                                  [Central Node]                     [Cloud Platform]
       |                                              |                                   |
  Collect Sensors                                     |                                   |
  & Heuristics                                        |                                   |
       |                                              |                                   |
       +---- Batch Sync (Wi-Fi LAN QoS 1) ----------> |                                   |
                                                      +--- Uplink Telemetry (LTE/Sat) --> |
                                                                                          |
                                                                                Ingest Regional Data
                                                                                Train LightGBM / TCN
                                                                                INT8 Quantization
                                                                                          |
                                                      <--- Model Weight Distribution -----+
                                                      |    (OTA Binary Update, < 50 KB)
       <--- Broadcast Update to Field Nodes ---------+
```

1. **Inference on Edge**: Nodes evaluate physical heuristics and (when enabled) pre-compiled tree weights.
2. **Training in Cloud**: Training occurs asynchronously in the Cloud using cross-station regional datasets, satellite imagery ground-truth, and regional radar.
3. **Over-The-Air (OTA) Weight Delivery**: Only compact model weight binaries (< 50 KB) are pushed down to nodes, ensuring zero cellular/satellite bandwidth exhaustion.
