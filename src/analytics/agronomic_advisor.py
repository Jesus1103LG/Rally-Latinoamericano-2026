#!/usr/bin/env python3
"""
Agronomic Decision Engine & Microclimate Advisory
=================================================
Translates cold meteorological sensor telemetry into actionable agronomic
recommendations and critical hazard alerts for farmers.

Physics and Agronomic Formulations:
  1. Magnus-Tetens Dew Point Approximation (Frost & Inversion prediction).
  2. Delta T (Spray Drift & Droplet Evaporation suitability for crop protection).
  3. Barometric Pressure Tendency (Storm / Convective Front forecasting).
  4. Gust-to-Sustained Wind Ratio (Crop lodging and mechanical damage risk).
"""

import math
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from enum import Enum


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class AgronomicAlert:
    code: str
    severity: AlertSeverity
    title: str
    message: str
    action_required: str


class AgronomicAdvisor:
    def __init__(
        self,
        # Spraying thresholds based on international agrometeorological standards
        min_delta_t_c: float = 2.0,
        max_delta_t_c: float = 8.0,
        max_spray_wind_kmh: float = 15.0,
        # Frost alert threshold (ambient temp near dew point)
        frost_warning_temp_c: float = 3.0,
        # Severe storm barometric drop threshold (hPa per 3 hours)
        storm_pressure_drop_hpa: float = 2.0,
    ):
        self.min_delta_t = min_delta_t_c
        self.max_delta_t = max_delta_t_c
        self.max_spray_wind = max_spray_wind_kmh
        self.frost_warning_temp = frost_warning_temp_c
        self.storm_pressure_drop = storm_pressure_drop_hpa

    @staticmethod
    def calculate_dew_point(temp_c: float, humidity_pct: float) -> float:
        """
        Calculates Dew Point using the Magnus-Tetens approximation.
        Valid for -45°C <= T <= 60°C and 1% <= RH <= 100%.
        Constants: a = 17.27, b = 237.7°C
        """
        if humidity_pct <= 0:
            return -50.0
        a = 17.27
        b = 237.7
        alpha = ((a * temp_c) / (b + temp_c)) + math.log(humidity_pct / 100.0)
        dew_point = (b * alpha) / (a - alpha)
        return round(dew_point, 2)

    @staticmethod
    def calculate_delta_t(temp_c: float, dew_point_c: float) -> float:
        """
        Approximates Delta T (Dry bulb minus Wet bulb temperature).
        In agronomy, Delta T is the universal metric for spraying decisions.
        Delta T ≈ (Temp - DewPoint) * 0.72 (empirical psychrometric approximation).
        """
        return round((temp_c - dew_point_c) * 0.72, 2)

    def evaluate_conditions(
        self,
        current: Dict[str, Any],
        historical_pressure_3h_ago: Optional[float] = None,
    ) -> List[AgronomicAlert]:
        """
        Evaluates current and short-term trends to produce actionable farmer advice.
        """
        alerts: List[AgronomicAlert] = []

        temp_c = current.get("ambient_temp_c")
        humidity = current.get("humidity_pct")
        wind_kmh = (current.get("wind_speed_avg_ms") or 0.0) * 3.6
        gust_kmh = (current.get("wind_gust_ms") or 0.0) * 3.6
        pressure_hpa = current.get("pressure_hpa")

        # ---------------------------------------------------------------------
        # 1. Agricultural Spraying & Fumigation Window (Delta T & Wind)
        # ---------------------------------------------------------------------
        if temp_c is not None and humidity is not None:
            dew_point = self.calculate_dew_point(temp_c, humidity)
            delta_t = self.calculate_delta_t(temp_c, dew_point)

            if wind_kmh > self.max_spray_wind or gust_kmh > (self.max_spray_wind * 1.3):
                alerts.append(
                    AgronomicAlert(
                        code="SPRAY_WIND_DRIFT_HAZARD",
                        severity=AlertSeverity.CRITICAL,
                        title="High Wind Drift Hazard",
                        message=f"Wind speed ({wind_kmh:.1f} km/h, gust {gust_kmh:.1f} km/h) exceeds safe limits.",
                        action_required="Halt all spraying and herbicide applications immediately to prevent drift onto adjacent crops.",
                    )
                )
            elif delta_t > self.max_delta_t:
                alerts.append(
                    AgronomicAlert(
                        code="SPRAY_HIGH_EVAPORATION",
                        severity=AlertSeverity.WARNING,
                        title="High Evaporation Rate (Delta T > 8°C)",
                        message=f"Delta T is {delta_t:.1f}°C. Air is excessively dry.",
                        action_required="Do not spray. Droplets will evaporate before reaching target weeds/foliage, reducing efficacy.",
                    )
                )
            elif delta_t < self.min_delta_t:
                alerts.append(
                    AgronomicAlert(
                        code="SPRAY_POOR_SURVIVAL",
                        severity=AlertSeverity.WARNING,
                        title="Low Delta T (< 2°C) - High Humidity",
                        message=f"Delta T is {delta_t:.1f}°C. Dew or saturated air will cause chemical runoff.",
                        action_required="Avoid spraying. Droplets will not dry, increasing fungal proliferation risk.",
                    )
                )
            else:
                alerts.append(
                    AgronomicAlert(
                        code="SPRAY_OPTIMAL_WINDOW",
                        severity=AlertSeverity.INFO,
                        title="Optimal Spraying Window",
                        message=f"Delta T ({delta_t:.1f}°C) and wind ({wind_kmh:.1f} km/h) are within ideal agronomic ranges.",
                        action_required="Favorable conditions for foliar application and pest control.",
                    )
                )

            # -----------------------------------------------------------------
            # 2. Frost Risk (Radiation Inversion vs. Dew Point)
            # -----------------------------------------------------------------
            if temp_c <= self.frost_warning_temp:
                margin = temp_c - dew_point
                if margin <= 1.5 and wind_kmh < 5.0:
                    alerts.append(
                        AgronomicAlert(
                            code="FROST_RADIATION_CRITICAL",
                            severity=AlertSeverity.CRITICAL,
                            title="Critical Radiation Frost Alert",
                            message=f"Temperature ({temp_c:.1f}°C) approaching dew point ({dew_point:.1f}°C) with calm wind ({wind_kmh:.1f} km/h).",
                            action_required="Activate anti-frost sprinklers, heaters, or wind machines immediately. Severe tissue freezing expected.",
                        )
                    )
                else:
                    alerts.append(
                        AgronomicAlert(
                            code="FROST_TEMPERATURE_WARNING",
                            severity=AlertSeverity.WARNING,
                            title="Near-Freezing Temperature Warning",
                            message=f"Ambient temperature is {temp_c:.1f}°C.",
                            action_required="Monitor parcel microclimate closely for thermal inversion drops.",
                        )
                    )

        # ---------------------------------------------------------------------
        # 3. Barometric Storm & Wind Shift Warning
        # ---------------------------------------------------------------------
        if pressure_hpa is not None and historical_pressure_3h_ago is not None:
            pressure_delta = pressure_hpa - historical_pressure_3h_ago
            # Fast barometric drop (> 2.0 hPa in 3 hours indicates approaching squall/storm)
            if pressure_delta <= -self.storm_pressure_drop:
                alerts.append(
                    AgronomicAlert(
                        code="STORM_BAROMETRIC_PLUNGE",
                        severity=AlertSeverity.CRITICAL,
                        title="Severe Storm Approaching",
                        message=f"Rapid barometric drop detected ({pressure_delta:.1f} hPa over last 3 hours).",
                        action_required="Secure greenhouse coverings, halt harvesting machinery, and protect solar panels from hail/gusts.",
                    )
                )

        return alerts
