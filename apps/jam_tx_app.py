from __future__ import annotations

import argparse
import random
import signal
import string
import sys
from dataclasses import dataclass

import numpy as np

try:
    import adi
except ImportError:
    print("ERROR: pyadi-iio not installed. Run: pip install pyadi-iio")
    sys.exit(2)

from ..phy import packet_to_iq
from ..protocol import JAM_ACCESS_CODE, CMD_0A06, build_air_packet, build_referee_frame
from ..radio_profiles import JAM_PROFILES, PROFILE_CHOICES

RUNNING = True


def on_sigint(signum, frame) -> None:
    del signum, frame
    global RUNNING
    RUNNING = False


def random_key6(rng: random.Random) -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(rng.choice(chars) for _ in range(6))


@dataclass
class JamWaveSource:
    update_hz: float
    push_rate_bps: int
    packet_period_s: float
    key_rotate_hz: float
    initial_key: str
    rng: random.Random

    def __post_init__(self) -> None:
        self.seq = 0
        self.tick = 0
        self.stream = b""
        self.cursor = 0
        self.sim_time_s = 0.0
        self.update_period_s = 1.0 / self.update_hz
        self.next_update_time_s = self.update_period_s
        self.current_key = self.initial_key
        self.bytes_per_update = int(round(self.push_rate_bps / self.update_hz))
        # Initialize key rotation tracking (tick-based for precision)
        self.last_key_rotation_tick = -1
        if self.key_rotate_hz > 0:
            # How many update ticks between rotations
            self.rotation_period_ticks = max(1, int(round(self.update_hz / self.key_rotate_hz)))
        else:
            self.rotation_period_ticks = 0
        self._rebuild_snapshot()

    def _build_0a06_frame(self) -> bytes:
        data = self.current_key.encode("ascii")
        return build_referee_frame(CMD_0A06, data, self.seq)

    def _rebuild_snapshot(self) -> None:
        # Check if it's time to rotate the key (tick-based)
        if self.rotation_period_ticks > 0 and self.tick > 0:
            if self.tick - self.last_key_rotation_tick >= self.rotation_period_ticks:
                self.current_key = random_key6(self.rng)
                self.last_key_rotation_tick = self.tick
        
        frame = self._build_0a06_frame()
        self.seq = (self.seq + 1) & 0xFF
        if len(frame) > self.bytes_per_update:
            raise ValueError("0x0A06 frame longer than bytes_per_update")
        pad_len = self.bytes_per_update - len(frame)
        self.stream = frame + self.rng.randbytes(pad_len)
        if self.cursor >= len(self.stream):
            self.cursor %= len(self.stream)

    def next_payload15(self) -> bytes:
        while self.sim_time_s + 1e-12 >= self.next_update_time_s:
            self.tick += 1
            self._rebuild_snapshot()
            self.next_update_time_s += self.update_period_s

        if self.cursor + 15 <= len(self.stream):
            chunk = self.stream[self.cursor: self.cursor + 15]
            self.cursor += 15
            if self.cursor >= len(self.stream):
                self.cursor = 0
        else:
            rem = len(self.stream) - self.cursor
            chunk = self.stream[self.cursor:] + self.stream[: 15 - rem]
            self.cursor = 15 - rem

        self.sim_time_s += self.packet_period_s
        return chunk


def main() -> int:
    parser = argparse.ArgumentParser(description="RM2026 2-GFSK jam TX (0x0A06)")
    parser.add_argument("--tx-ip", default="192.168.2.1")
    parser.add_argument("--profile", choices=PROFILE_CHOICES, default=None)
    parser.add_argument("--center-freq", type=int, default=432_200_000)
    parser.add_argument("--sample-rate", type=int, default=1_000_000)
    parser.add_argument("--sps", type=int, default=52)
    parser.add_argument("--bt", type=float, default=0.35)
    parser.add_argument("--sensitivity", type=float, default=2.8323)
    parser.add_argument("--rf-bandwidth", type=int, default=940_000)
    parser.add_argument("--tx-gain-db", type=float, default=-20.0)
    parser.add_argument("--amplitude", type=float, default=0.8)
    parser.add_argument("--update-hz", type=float, default=10.0)
    parser.add_argument("--push-rate", type=int, default=1350)
    parser.add_argument("--key", default="")
    parser.add_argument("--key-rotate-hz", type=float, default=0.0)
    parser.add_argument("--packets-per-buffer", type=int, default=24)
    args = parser.parse_args()

    if args.profile is not None:
        preset = JAM_PROFILES[args.profile]
        if args.center_freq == parser.get_default("center_freq"):
            args.center_freq = preset.center_freq
        if args.rf_bandwidth == parser.get_default("rf_bandwidth"):
            args.rf_bandwidth = preset.rf_bandwidth
        if args.sensitivity == parser.get_default("sensitivity"):
            args.sensitivity = preset.sensitivity
        if args.tx_gain_db == parser.get_default("tx_gain_db"):
            args.tx_gain_db = preset.tx_gain_db

    rng = random.Random(2026)
    key = args.key.strip().upper() if args.key else random_key6(rng)
    packet_period_s = ((8 + 4 + 15) * 8) / (args.sample_rate / args.sps)
    source = JamWaveSource(
        update_hz=args.update_hz,
        push_rate_bps=args.push_rate,
        packet_period_s=packet_period_s,
        key_rotate_hz=args.key_rotate_hz,
        initial_key=key,
        rng=rng,
    )

    signal.signal(signal.SIGINT, on_sigint)

    print("=" * 68)
    print("RM2026 2-GFSK JAM TX (0x0A06 simulation)")
    print("=" * 68)
    print(f"TX IP          : {args.tx_ip}")
    print(f"Profile        : {args.profile or 'manual'}")
    print(f"Center Freq    : {args.center_freq} Hz")
    print(f"Sample Rate    : {args.sample_rate} S/s")
    print(f"SPS            : {args.sps}")
    print(f"BT             : {args.bt}")
    print(f"Sensitivity    : {args.sensitivity} rad/sample")
    print(f"RF Bandwidth   : {args.rf_bandwidth} Hz")
    print(f"TX Gain        : {args.tx_gain_db} dB")
    print(f"Access Code    : 0x{JAM_ACCESS_CODE:016X}")
    print(f"Update Rate    : {args.update_hz:.2f} Hz")
    print(f"Push Rate      : {args.push_rate} byte/s")
    print(f"Jam Key        : {source.current_key}")
    print("Source Stream  : protocol 0x0A06 frame + random padding")

    tx = None
    try:
        tx = adi.Pluto(f"ip:{args.tx_ip}")
        tx.sample_rate = int(args.sample_rate)
        tx.tx_lo = int(args.center_freq)
        tx.tx_rf_bandwidth = int(args.rf_bandwidth)
        tx.tx_hardwaregain = float(args.tx_gain_db)
        tx.tx_enabled_channels = [0]
        tx.tx_cyclic_buffer = False

        packets = []
        for _ in range(args.packets_per_buffer):
            payload15 = source.next_payload15()
            air = build_air_packet(payload15, JAM_ACCESS_CODE)
            packets.append(
                packet_to_iq(
                    packet=air,
                    sps=args.sps,
                    bt=args.bt,
                    sensitivity=args.sensitivity,
                    amplitude=args.amplitude,
                )
            )

        wave = np.concatenate(packets).astype(np.complex64, copy=False)
        tx.tx(wave)
        print(f"Started TX. Initial buffer samples: {len(wave)}")
        print("Press Ctrl+C to stop...")

        while RUNNING:
            packets = []
            for _ in range(args.packets_per_buffer):
                payload15 = source.next_payload15()
                air = build_air_packet(payload15, JAM_ACCESS_CODE)
                packets.append(
                    packet_to_iq(
                        packet=air,
                        sps=args.sps,
                        bt=args.bt,
                        sensitivity=args.sensitivity,
                        amplitude=args.amplitude,
                    )
                )
            wave = np.concatenate(packets).astype(np.complex64, copy=False)
            tx.tx(wave)

    except Exception as exc:
        print(f"TX ERROR: {exc}")
        return 1
    finally:
        if tx is not None:
            try:
                tx.tx_destroy_buffer()
            except Exception:
                pass
            del tx

    print("TX stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
