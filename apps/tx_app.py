from __future__ import annotations

import argparse
import signal
import sys
import time

import numpy as np

try:
    import adi
except ImportError:
    print("ERROR: pyadi-iio not installed. Run: pip install pyadi-iio")
    sys.exit(2)

from ..phy import packet_to_iq
from ..protocol import INFO_ACCESS_CODE, CMD_0A01, CMD_0A02, CMD_0A03, CMD_0A04, CMD_0A05, build_air_packet, build_referee_frame
from ..launch.message_value_generate import MessageValueGenerator
from ..radio_profiles import INFO_PROFILE_CHOICES, INFO_PROFILES

RUNNING = True


def on_sigint(signum, frame) -> None:
    del signum, frame
    global RUNNING
    RUNNING = False


class InfoWaveSource:
    def __init__(self, update_hz: float, packet_period_s: float):
        self.update_period_s = 1.0 / update_hz
        self.packet_period_s = packet_period_s
        self.seq = 0
        self.tick = 0
        self.stream = bytearray()
        self.cursor = 0
        self.sim_time_s = 0.0
        self.next_update_time_s = self.update_period_s
        self._generator = MessageValueGenerator(set_mode="random")
        self.stream += self._build_snapshot_bytes()

    def _build_cmd_payload(self, cmd_id: int) -> bytes:
        if cmd_id == CMD_0A01:
            return self._generator._build_cmd_0a01()
        if cmd_id == CMD_0A02:
            return self._generator._build_cmd_0a02()
        if cmd_id == CMD_0A03:
            return self._generator._build_cmd_0a03()
        if cmd_id == CMD_0A04:
            return self._generator._build_cmd_0a04()
        if cmd_id == CMD_0A05:
            return self._generator._build_cmd_0a05()
        raise ValueError(f"unsupported cmd id: 0x{cmd_id:04X}")

    def _build_snapshot_bytes(self) -> bytes:
        frames = []
        for cmd_id in (CMD_0A01, CMD_0A02, CMD_0A03, CMD_0A04, CMD_0A05):
            payload = self._build_cmd_payload(cmd_id)
            frames.append(build_referee_frame(cmd_id=cmd_id, data=payload, seq=self.seq))
            self.seq = (self.seq + 1) & 0xFF
        return b"".join(frames)

    def next_payload15(self) -> bytes:
        while self.sim_time_s + 1e-12 >= self.next_update_time_s:
            self.tick += 1
            self.stream += self._build_snapshot_bytes()
            self.next_update_time_s += self.update_period_s

        while self.cursor + 15 > len(self.stream):
            self.stream += self._build_snapshot_bytes()

        chunk = bytes(self.stream[self.cursor: self.cursor + 15])
        self.cursor += 15
        if self.cursor > 4096 and self.cursor > (len(self.stream) // 2):
            del self.stream[:self.cursor]
            self.cursor = 0
        self.sim_time_s += self.packet_period_s
        return chunk


def main() -> int:
    parser = argparse.ArgumentParser(description="RM2026 2-GFSK TX")
    parser.add_argument("--tx-ip", default="192.168.2.1")
    parser.add_argument("--profile", choices=INFO_PROFILE_CHOICES, default=None)
    parser.add_argument("--center-freq", type=int, default=433_200_000)
    parser.add_argument("--sample-rate", type=int, default=1_000_000)
    parser.add_argument("--sps", type=int, default=52)
    parser.add_argument("--bt", type=float, default=0.35)
    parser.add_argument("--sensitivity", type=float, default=1.5756)
    parser.add_argument("--rf-bandwidth", type=int, default=540_000)
    parser.add_argument("--tx-gain-db", type=float, default=-25.0)
    parser.add_argument("--amplitude", type=float, default=0.8)
    parser.add_argument("--packets-per-buffer", type=int, default=24)
    parser.add_argument("--update-hz", type=float, default=10.0)
    args = parser.parse_args()

    if args.profile is not None:
        preset = INFO_PROFILES[args.profile]
        if args.center_freq == parser.get_default("center_freq"):
            args.center_freq = preset.center_freq
        if args.rf_bandwidth == parser.get_default("rf_bandwidth"):
            args.rf_bandwidth = preset.rf_bandwidth
        if args.sensitivity == parser.get_default("sensitivity"):
            args.sensitivity = preset.sensitivity
        if args.tx_gain_db == parser.get_default("tx_gain_db"):
            args.tx_gain_db = preset.tx_gain_db

    if args.sps <= 0 or args.sample_rate <= 0 or args.update_hz <= 0:
        print("ERROR: invalid numeric parameters")
        return 1

    bit_rate = args.sample_rate / args.sps
    packet_period_s = ((8 + 4 + 15) * 8) / bit_rate
    source = InfoWaveSource(update_hz=args.update_hz, packet_period_s=packet_period_s)

    signal.signal(signal.SIGINT, on_sigint)

    print("=" * 68)
    print("RM2026 2-GFSK TX")
    print("=" * 68)
    print(f"TX IP          : {args.tx_ip}")
    print(f"Profile        : {args.profile or 'manual'}")
    print(f"Center Freq    : {args.center_freq} Hz")
    print(f"Sample Rate    : {args.sample_rate} S/s")
    print(f"SPS            : {args.sps}")
    print(f"Bit Rate       : {bit_rate:.3f} bps")
    print(f"BT             : {args.bt}")
    print(f"Sensitivity    : {args.sensitivity} rad/sample")
    print(f"RF Bandwidth   : {args.rf_bandwidth} Hz")
    print(f"TX Gain        : {args.tx_gain_db} dB")
    print(f"Access Code    : 0x{INFO_ACCESS_CODE:016X}")
    print(f"Update Rate    : {args.update_hz:.2f} Hz")
    print("Source Stream  : cmd 0x0A01 -> 0x0A05")

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
            air = build_air_packet(payload15, INFO_ACCESS_CODE)
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

        last_tick = source.tick
        last_time = time.time()

        while RUNNING:
            packets = []
            for _ in range(args.packets_per_buffer):
                payload15 = source.next_payload15()
                air = build_air_packet(payload15, INFO_ACCESS_CODE)
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

            # Print snapshot count every second
            now = time.time()
            if now - last_time >= 1.0:
                delta_tick = source.tick - last_tick
                msg = f"[TX] snapshots this sec: {delta_tick} (cumulative: {source.tick}, rate: {delta_tick:.0f}/s)"
                print(msg)
                sys.stdout.flush()
                last_tick = source.tick
                last_time = now

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

    print(f"TX stopped. Total snapshots generated: {source.tick} (= {source.tick * 5} protocol frames)")
    sys.stdout.flush()
    sys.stderr.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
