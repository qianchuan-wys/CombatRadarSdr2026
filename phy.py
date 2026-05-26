from __future__ import annotations

import numpy as np


def bytes_to_bits_msb(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    return np.unpackbits(arr, bitorder="big").astype(np.float64)


def gaussian_taps(sps: int, bt: float, span_symbols: int = 4) -> np.ndarray:
    n = np.arange(-span_symbols * sps, span_symbols * sps + 1, dtype=np.float64)
    t = n / float(sps)
    alpha = np.sqrt(np.log(2.0)) / (2.0 * np.pi * bt)
    h = np.exp(-(t * t) / (2.0 * alpha * alpha))
    h /= np.sum(h) + 1e-15
    return h


def packet_to_iq(
    packet: bytes,
    sps: int,
    bt: float,
    sensitivity: float,
    amplitude: float,
) -> np.ndarray:
    bits = bytes_to_bits_msb(packet)
    nrz = 2.0 * bits - 1.0
    up = np.zeros(len(nrz) * sps, dtype=np.float64)
    up[::sps] = nrz
    shaped = np.convolve(up, gaussian_taps(sps=sps, bt=bt), mode="same")
    phase = np.cumsum(sensitivity * shaped)
    iq = (amplitude * 2048.0) * np.exp(1j * phase)
    return iq.astype(np.complex64)


def fm_demod(iq: np.ndarray, sample_rate: int) -> np.ndarray:
    ph = np.angle(iq[1:] * np.conj(iq[:-1]))
    inst_freq = ph * (sample_rate / (2.0 * np.pi))
    return inst_freq - np.mean(inst_freq)
