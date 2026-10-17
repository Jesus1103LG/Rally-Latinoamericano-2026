#!/usr/bin/env python3
"""
Acoustic Wind Anemometer (DSP Engine)
=====================================
Translates microphone acoustic noise caused by aerodynamic turbulence into
calibrated wind speed (m/s and km/h), while rejecting acoustic contamination
from fauna (birds, cicadas), human speech, and engine noise.

Optimized for ARM Cortex-A CPU in Termux using NumPy & SciPy.
"""

import numpy as np
from scipy import signal
from typing import Dict, Tuple, Optional
import logging

logger = logging.getLogger("WindDSP")


class AcousticAnemometerDSP:
    def __init__(
        self,
        sample_rate: int = 16000,
        frame_size: int = 2048,
        hop_size: int = 1024,
        # Aerodynamic calibration coefficients: v = a * (RMS_wind)^b
        # Calibrated for smartphone mic covered with high-density acoustic windscreen foam.
        calib_a: float = 42.5,
        calib_b: float = 0.65,
        min_wind_speed_ms: float = 0.5,
        max_wind_speed_ms: float = 45.0,  # ~160 km/h hurricane cutoff
    ):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.hop_size = hop_size
        self.calib_a = calib_a
        self.calib_b = calib_b
        self.min_wind_speed_ms = min_wind_speed_ms
        self.max_wind_speed_ms = max_wind_speed_ms

        # Pre-compute Hann window for spectral leakage mitigation
        self.window = np.hanning(self.frame_size).astype(np.float32)

        # Pre-compute FFT frequency bins (for 2048 points @ 16 kHz, bin width = 7.81 Hz)
        self.freq_bins = np.fft.rfftfreq(self.frame_size, d=1.0 / self.sample_rate)

        # Frequency band masks
        # Wind turbulence vortex shedding concentrates in 20 Hz - 400 Hz
        self.mask_wind = (self.freq_bins >= 20.0) & (self.freq_bins <= 400.0)
        # Human speech fundamental and formants
        self.mask_voice = (self.freq_bins >= 500.0) & (self.freq_bins <= 2500.0)
        # Bird chirps, crickets, cicadas
        self.mask_fauna = (self.freq_bins >= 3000.0) & (self.freq_bins <= 7500.0)

        # Design lightweight 4th-order Butterworth bandpass filter (20 Hz - 400 Hz)
        nyquist = 0.5 * self.sample_rate
        low = 20.0 / nyquist
        high = 400.0 / nyquist
        self.b_filter, self.a_filter = signal.butter(4, [low, high], btype="band")

    def _compute_spectral_flatness(self, psd: np.ndarray) -> float:
        """
        Spectral Flatness Measure (Wiener entropy).
        SFM = geometric_mean(psd) / arithmetic_mean(psd)
        
        Wind turbulence is broadband stochastic noise -> SFM is high (> 0.25).
        Speech and bird chirps have sharp harmonic tonal peaks -> SFM is low (< 0.15).
        """
        psd_safe = np.maximum(psd, 1e-12)
        arithmetic_mean = np.mean(psd_safe)
        if arithmetic_mean == 0:
            return 0.0
        geometric_mean = np.exp(np.mean(np.log(psd_safe)))
        return float(geometric_mean / arithmetic_mean)

    def process_burst(self, pcm_data: bytes) -> Dict[str, float]:
        """
        Processes a multi-second raw PCM byte stream (16-bit Little-Endian Mono).
        Returns estimated wind speed metrics:
            - wind_speed_avg_ms: Sustained average wind speed in m/s
            - wind_speed_avg_kmh: Sustained average wind speed in km/h
            - wind_gust_ms: Maximum 3-second gust speed in m/s
            - wind_gust_kmh: Maximum 3-second gust speed in km/h
            - contamination_pct: Percentage of discarded frames (voices, fauna)
        """
        # Convert raw PCM16 bytes to float32 [-1.0, 1.0]
        raw_samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0

        if len(raw_samples) < self.frame_size:
            logger.warning("Audio burst too short for FFT analysis.")
            return {
                "wind_speed_avg_ms": 0.0,
                "wind_speed_avg_kmh": 0.0,
                "wind_gust_ms": 0.0,
                "wind_gust_kmh": 0.0,
                "contamination_pct": 100.0,
            }

        # Apply digital Bandpass to isolate turbulent mechanical airflow
        filtered_audio = signal.filtfilt(self.b_filter, self.a_filter, raw_samples)

        frame_speeds = []
        contaminated_count = 0
        total_frames = 0

        # Slide analysis window across the audio burst
        num_frames = (len(raw_samples) - self.frame_size) // self.hop_size + 1

        for i in range(num_frames):
            start = i * self.hop_size
            end = start + self.frame_size
            total_frames += 1

            chunk_raw = raw_samples[start:end] * self.window
            chunk_filtered = filtered_audio[start:end]

            # Fast Fourier Transform (Real FFT)
            spectrum = np.fft.rfft(chunk_raw)
            psd = (np.abs(spectrum) ** 2) / self.frame_size

            # Energy per frequency band
            e_wind = np.sum(psd[self.mask_wind])
            e_voice = np.sum(psd[self.mask_voice])
            e_fauna = np.sum(psd[self.mask_fauna])

            # Measure tonal vs stochastic nature
            sfm_voice_band = self._compute_spectral_flatness(psd[self.mask_voice])

            # Contamination Rule 1: Voice or Fauna energy exceeds wind turbulent energy
            interference_ratio = (e_voice + e_fauna) / (e_wind + 1e-9)

            # Contamination Rule 2: Strong tonal harmonics in speech range
            is_tonal_voice = (sfm_voice_band < 0.12) and (e_voice > (0.3 * e_wind))

            if interference_ratio > 0.85 or is_tonal_voice:
                contaminated_count += 1
                continue  # Discard frame contaminated by human voice, bird or vehicle

            # RMS of the turbulence band (20 - 400 Hz)
            rms_wind = np.sqrt(np.mean(chunk_filtered ** 2))

            # Calibration Transfer Function: v = a * (RMS)^b
            speed_ms = self.calib_a * (rms_wind ** self.calib_b)

            # Floor and clamp
            if speed_ms < self.min_wind_speed_ms:
                speed_ms = 0.0
            elif speed_ms > self.max_wind_speed_ms:
                speed_ms = self.max_wind_speed_ms

            frame_speeds.append(speed_ms)

        contamination_pct = (contaminated_count / total_frames * 100.0) if total_frames > 0 else 0.0

        if not frame_speeds:
            logger.warning("All audio frames were rejected due to acoustic contamination.")
            return {
                "wind_speed_avg_ms": 0.0,
                "wind_speed_avg_kmh": 0.0,
                "wind_gust_ms": 0.0,
                "wind_gust_kmh": 0.0,
                "contamination_pct": contamination_pct,
            }

        # Average wind speed (sustained)
        v_avg_ms = float(np.mean(frame_speeds))

        # Gust speed: Maximum 3-second moving average window
        # 3 seconds @ hop_size = 1024 samples (64 ms) is ~47 hops
        gust_window_size = max(1, int(3.0 * self.sample_rate / self.hop_size))
        if len(frame_speeds) >= gust_window_size:
            rolling_avg = np.convolve(
                frame_speeds, np.ones(gust_window_size) / gust_window_size, mode="valid"
            )
            v_gust_ms = float(np.max(rolling_avg))
        else:
            v_gust_ms = float(np.max(frame_speeds))

        return {
            "wind_speed_avg_ms": round(v_avg_ms, 2),
            "wind_speed_avg_kmh": round(v_avg_ms * 3.6, 2),
            "wind_gust_ms": round(v_gust_ms, 2),
            "wind_gust_kmh": round(v_gust_ms * 3.6, 2),
            "contamination_pct": round(contamination_pct, 1),
        }
