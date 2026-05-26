from __future__ import annotations

from dataclasses import dataclass

from .protocol import INFO_ACCESS_CODE, JAM_ACCESS_CODE


@dataclass(frozen=True)
class RadioProfile:
    center_freq: int
    rf_bandwidth: int
    sensitivity: float
    tx_gain_db: float
    access_code: int


INFO_PROFILE_CHOICES = ("red1", "blue1")
JAM_PROFILE_CHOICES = ("red1", "red2", "red3", "blue1", "blue2", "blue3")


INFO_PROFILES = {
    "red1": RadioProfile(433_200_000, 540_000, 1.5756, -50.0, INFO_ACCESS_CODE),
    "blue1": RadioProfile(433_920_000, 540_000, 1.5756, -50.0, INFO_ACCESS_CODE),
}


JAM_PROFILES = {
    "red1": RadioProfile(432_200_000, 940_000, 2.8323, 0.0, JAM_ACCESS_CODE),
    "red2": RadioProfile(432_500_000, 860_000, 2.5809, 0.0, JAM_ACCESS_CODE),
    "red3": RadioProfile(432_800_000, 250_000, 0.6646, 0.0, JAM_ACCESS_CODE),
    "blue1": RadioProfile(434_920_000, 940_000, 2.8323, 0.0, JAM_ACCESS_CODE),
    "blue2": RadioProfile(434_620_000, 860_000, 2.5809, 0.0, JAM_ACCESS_CODE),
    "blue3": RadioProfile(434_320_000, 250_000, 0.6646, 0.0, JAM_ACCESS_CODE),
}

# Backward-compatible alias used by some jam entrypoints.
PROFILE_CHOICES = JAM_PROFILE_CHOICES
