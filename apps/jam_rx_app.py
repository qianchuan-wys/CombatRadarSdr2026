from __future__ import annotations

import argparse
import json
import string
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

try:
    import adi
except ImportError:
    adi = None

from ..parser.gnuradio_frame_parser import (
    LiveInfoState,
    OffsetLock,
    PAYLOAD_ENDIAN_CHOICES,
    ProtocolStreamReassembler,
    StrictInfoCycleFilter,
    decode_cmd,
    select_best_offset_candidate,
    slice_packet_candidates,
)
from ..phy import fm_demod
from ..protocol import CMD_0A01, CMD_0A06, SOF, crc8_maxim, crc16_ibm
from ..radio_profiles import INFO_PROFILES, JAM_PROFILES, RadioProfile
from ..server_comm import RadarServerComm


ALLOWED_JAM_CHARS = set(string.ascii_uppercase + string.digits)
AIR_PAYLOAD_BYTES = 15
JAM_KEY_DATA_BYTES = 6
JAM_FRAME_BYTES = 15
TEAM_CHOICES = ("red", "blue")
LEVEL_CHOICES = (1, 2, 3)
INFO_MODE_LEVEL = 3
RX_MODE_JAM = "jam"
RX_MODE_INFO = "info"
TEAM_LEVEL_TO_JAM_PROFILE = {
    ("red", 1): "red1",
    ("red", 2): "red2",
    ("red", 3): "red3",
    ("blue", 1): "blue1",
    ("blue", 2): "blue2",
    ("blue", 3): "blue3",
}
TEAM_TO_INFO_PROFILE = {
    "red": "red1",
    "blue": "blue1",
}
INFO_FIELD_BOUNDS = {
    "x": 2800,
    "y": 1500,
}
PARSE_POLICY_CHOICES = ("default", "info_only", "onekey_then_info")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORD_DIR = PROJECT_ROOT / "radio_logs"


@dataclass
class ReceiverState:
    team: str
    level: int
    rx_mode: str
    profile_name: str
    center_freq: int
    rf_bandwidth: int
    sensitivity: float
    jam_frame_count: int = 0
    info_frame_count: int = 0
    last_key: str = "N/A"
    last_seq: int | None = None
    last_info_seq: int | None = None
    last_best_jam_dist: int | None = None
    last_best_info_dist: int | None = None
    last_scan_offset: int | None = None
    last_confidence: float | None = None
    last_frame_hex: str = ""
    last_frame_ts: float | None = None
    last_info_positions: dict[str, dict[str, int]] | None = None
    last_info_frame_hex: str = ""
    last_info_frame_ts: float | None = None
    no_packet_streak: int = 0
    last_buffer_power_dbfs: float | None = None
    last_buffer_packets: int = 0

    def to_status(self, rx_ip: str, server_connected: bool) -> dict[str, Any]:
        return {
            "kind": "jam_status",
            "ts": time.time(),
            "rx_ip": rx_ip,
            "team": self.team,
            "jam_level": self.level,
            "rx_mode": self.rx_mode,
            "profile": self.profile_name,
            "center_freq": self.center_freq,
            "rf_bandwidth": self.rf_bandwidth,
            "sensitivity": self.sensitivity,
            "jam_frame_count": self.jam_frame_count,
            "info_frame_count": self.info_frame_count,
            "last_key": self.last_key,
            "last_seq": self.last_seq,
            "last_info_seq": self.last_info_seq,
            "last_best_jam_dist": self.last_best_jam_dist,
            "last_best_info_dist": self.last_best_info_dist,
            "last_scan_offset": self.last_scan_offset,
            "last_confidence": self.last_confidence,
            "last_frame_hex": self.last_frame_hex,
            "last_frame_ts": self.last_frame_ts,
            "last_info_positions": self.last_info_positions,
            "last_info_frame_hex": self.last_info_frame_hex,
            "last_info_frame_ts": self.last_info_frame_ts,
            "no_packet_streak": self.no_packet_streak,
            "last_buffer_power_dbfs": self.last_buffer_power_dbfs,
            "last_buffer_packets": self.last_buffer_packets,
            "server_connected": server_connected,
        }


def safe_filename_component(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())
    cleaned = cleaned.strip("_")
    return cleaned or "jam_rx"


@dataclass
class WaveRecorder:
    record_dir: Path
    record_tag: str
    enabled: bool = False
    path: Path | None = None
    meta_path: Path | None = None
    handle: BinaryIO | None = None
    bytes_written: int = 0
    buffers_written: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def start(self, metadata: dict[str, Any]) -> tuple[Path, Path]:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        tag = safe_filename_component(self.record_tag)
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.record_dir / f"{tag}_{stamp}.c64"
        self.meta_path = self.record_dir / f"{tag}_{stamp}.json"
        self.handle = self.path.open("wb")
        self.metadata = dict(metadata)
        self.metadata.update(
            {
                "record_path": str(self.path),
                "record_meta_path": str(self.meta_path),
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "format": "raw_iq_complex64",
                "dtype": "complex64",
                "byteorder": sys.byteorder,
            }
        )
        self._write_metadata(status="recording")
        return self.path, self.meta_path

    def write(self, iq: np.ndarray) -> None:
        if self.handle is None:
            return
        chunk = np.asarray(iq, dtype=np.complex64)
        self.handle.write(chunk.tobytes())
        self.bytes_written += chunk.nbytes
        self.buffers_written += 1

    def close(self, status: str, **extra: Any) -> None:
        if extra:
            self.metadata.update(extra)
        if self.handle is not None:
            self.handle.flush()
            self.handle.close()
            self.handle = None
        if self.path is not None and self.meta_path is not None:
            self._write_metadata(status=status)

    def summary(self) -> dict[str, Any]:
        payload = {
            "record_bytes": self.bytes_written,
            "record_buffers": self.buffers_written,
            "record_samples": self.bytes_written // np.dtype(np.complex64).itemsize,
        }
        if self.path is not None:
            payload["record_path"] = str(self.path)
        if self.meta_path is not None:
            payload["record_meta_path"] = str(self.meta_path)
        return payload

    def _write_metadata(self, status: str) -> None:
        if self.meta_path is None:
            return
        payload = dict(self.metadata)
        payload.update(
            {
                "status": status,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "record_bytes": self.bytes_written,
                "record_buffers": self.buffers_written,
                "record_samples": self.bytes_written // np.dtype(np.complex64).itemsize,
            }
        )
        self.meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def should_use_info_mode(level: int, parse_policy: str, info_mode_locked: bool) -> bool:
    if parse_policy == "info_only":
        return True
    if parse_policy == "onekey_then_info":
        return info_mode_locked or level >= 2
    return level >= INFO_MODE_LEVEL


def get_receiver_profile(
    team: str,
    level: int,
    parse_policy: str = "default",
    info_mode_locked: bool = False,
) -> tuple[str, str, RadioProfile]:
    if should_use_info_mode(level, parse_policy, info_mode_locked):
        profile_name = TEAM_TO_INFO_PROFILE[team]
        return RX_MODE_INFO, profile_name, INFO_PROFILES[profile_name]
    profile_name = TEAM_LEVEL_TO_JAM_PROFILE[(team, level)]
    return RX_MODE_JAM, profile_name, JAM_PROFILES[profile_name]


def check_air_packet(packet: dict[str, Any], expected_kind: str) -> tuple[bool, str]:
    payload = packet.get("payload", b"")
    if packet.get("kind") != expected_kind:
        return False, f"not {expected_kind} packet"
    if int(packet.get("len1", -1)) != AIR_PAYLOAD_BYTES:
        return False, f"len1={packet.get('len1')} != 15"
    if int(packet.get("len2", -1)) != AIR_PAYLOAD_BYTES:
        return False, f"len2={packet.get('len2')} != 15"
    if len(payload) != AIR_PAYLOAD_BYTES:
        return False, f"payload_len={len(payload)} != 15"
    return True, "ok"


def extract_jam_0a06_frames(payload30: bytes) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if len(payload30) != AIR_PAYLOAD_BYTES * 2:
        return matches

    max_start = len(payload30) - JAM_FRAME_BYTES
    for start in range(max_start + 1):
        if payload30[start] != SOF:
            continue

        frame = payload30[start:start + JAM_FRAME_BYTES]
        data_len = int.from_bytes(frame[1:3], "little")
        if data_len != JAM_KEY_DATA_BYTES:
            continue

        if crc8_maxim(frame[0:4]) != frame[4]:
            continue

        cmd_id = int.from_bytes(frame[5:7], "little")
        if cmd_id != CMD_0A06:
            continue

        crc16_expected = int.from_bytes(frame[13:15], "little")
        if crc16_ibm(frame[0:13]) != crc16_expected:
            continue

        matches.append(
            {
                "start": start,
                "seq": frame[3],
                "cmd_id": cmd_id,
                "data": bytes(frame[7:13]),
                "frame_hex": frame.hex().upper(),
            }
        )
    return matches


def ascii_score(data: bytes) -> float:
    if len(data) != JAM_KEY_DATA_BYTES:
        return 0.0
    valid = sum(chr(byte_value) in ALLOWED_JAM_CHARS for byte_value in data)
    return valid / JAM_KEY_DATA_BYTES


def jam_confidence(best_jam_dist: int, crc_ok: bool, data: bytes) -> float:
    access_conf = max(0.0, 1.0 - (best_jam_dist / 64.0))
    crc_conf = 1.0 if crc_ok else 0.0
    key_conf = ascii_score(data)
    return 0.45 * access_conf + 0.40 * crc_conf + 0.15 * key_conf


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def emit_error(message: str, **extra: Any) -> None:
    payload = {"kind": "jam_error", "ts": time.time(), "message": message}
    payload.update(extra)
    emit_json(payload)


def configure_receiver(
    rx,
    center_freq: int,
    sample_rate: int,
    rf_bandwidth: int,
    rx_gain_db: float,
    rx_buffer_size: int,
) -> None:
    rx.sample_rate = int(sample_rate)
    rx.rx_lo = int(center_freq)
    rx.rx_rf_bandwidth = int(rf_bandwidth)
    rx.gain_control_mode_chan0 = "manual"
    rx.rx_hardwaregain_chan0 = float(rx_gain_db)
    rx.rx_enabled_channels = [0]
    rx.rx_buffer_size = int(rx_buffer_size)


def build_jam_frame_payload(
    state: ReceiverState,
    data: bytes,
    seq: int,
    confidence: float,
    best_jam_dist: int,
    scan_offset: int,
    frame_hex: str,
) -> dict[str, Any]:
    decoded = decode_cmd(CMD_0A06, data)
    return {
        "kind": "jam_frame",
        "ts": time.time(),
        "team": state.team,
        "jam_level": state.level,
        "rx_mode": state.rx_mode,
        "profile": state.profile_name,
        "cmd_id": "0x0A06",
        "seq": seq,
        "data_hex": data.hex().upper(),
        "data_ascii": data.decode("ascii", errors="replace"),
        "decoded": decoded,
        "confidence": round(confidence, 4),
        "best_jam_dist": best_jam_dist,
        "scan_offset": scan_offset,
        "frame_hex": frame_hex,
        "jam_frame_count": state.jam_frame_count,
    }


def build_info_frame_payload(
    state: ReceiverState,
    seq: int,
    data: bytes,
    decoded: dict[str, Any],
    best_info_dist: int,
    scan_offset: int,
    payload_endian: str,
) -> dict[str, Any]:
    return {
        "kind": "info_frame",
        "ts": time.time(),
        "team": state.team,
        "jam_level": state.level,
        "rx_mode": state.rx_mode,
        "profile": state.profile_name,
        "cmd_id": "0x0A01",
        "seq": seq,
        "data_hex": data.hex().upper(),
        "decoded": decoded,
        "payload_endian": payload_endian,
        "best_info_dist": best_info_dist,
        "scan_offset": scan_offset,
        "info_frame_count": state.info_frame_count,
    }


def info_positions_out_of_bounds(decoded: dict[str, Any]) -> bool:
    for value in decoded.values():
        if not isinstance(value, dict):
            continue
        x_value = value.get("x")
        y_value = value.get("y")
        if isinstance(x_value, int) and x_value > INFO_FIELD_BOUNDS["x"]:
            return True
        if isinstance(y_value, int) and y_value > INFO_FIELD_BOUNDS["y"]:
            return True
    return False


def encode_cmd_0a01_payload(decoded: dict[str, Any], payload_endian: str) -> bytes:
    ordered_names = (
        "enemy_hero",
        "enemy_engineer",
        "enemy_infantry3",
        "enemy_infantry4",
        "enemy_air",
        "enemy_sentinel",
    )
    body = bytearray()
    for name in ordered_names:
        item = decoded.get(name) or {}
        x_value = int(item.get("x", 0))
        y_value = int(item.get("y", 0))
        body.extend(x_value.to_bytes(2, payload_endian, signed=False))
        body.extend(y_value.to_bytes(2, payload_endian, signed=False))
    return bytes(body)


def main() -> int:
    if adi is None:
        print("ERROR: pyadi-iio not installed. Run: pip install pyadi-iio")
        return 2

    parser = argparse.ArgumentParser(description="RM2026 hybrid JAM/INFO RX")
    parser.add_argument("--rx-ip", default="192.168.1.10")
    parser.add_argument("--team", choices=TEAM_CHOICES, default="red")
    parser.add_argument("--initial-level", type=int, choices=LEVEL_CHOICES, default=1)
    parser.add_argument("--sample-rate", type=int, default=1_000_000)
    parser.add_argument("--sps", type=int, default=52)
    parser.add_argument("--bt", type=float, default=0.35)
    parser.add_argument("--rx-gain-db", type=float, default=50.0)
    parser.add_argument("--rx-buffer-size", type=int, default=262144)
    parser.add_argument("--access-bit-errors", type=int, default=1)
    parser.add_argument("--multi-offset", type=int, default=5)
    parser.add_argument("--offset-refine-span", type=int, default=3)
    parser.add_argument("--offset-hold-bufs", type=int, default=8)
    parser.add_argument("--confidence-threshold", type=float, default=0.85)
    parser.add_argument("--server-ip", default="127.0.0.1")
    parser.add_argument("--server-port", type=int, default=5000)
    parser.add_argument("--no-server-comm", action="store_true")
    parser.add_argument("--no-packet-warn-bufs", type=int, default=40)
    parser.add_argument("--auto-relax-after-bufs", type=int, default=40)
    parser.add_argument("--auto-relax-access-bit-errors", type=int, default=1)
    parser.add_argument("--status-interval", type=float, default=0.5)
    parser.add_argument("--strict-cycle", action="store_true")
    parser.add_argument("--parse-policy", choices=PARSE_POLICY_CHOICES, default="default")
    parser.add_argument("--payload-endian", choices=PAYLOAD_ENDIAN_CHOICES, default="little")
    parser.add_argument("--record-wave", action="store_true")
    parser.add_argument("--record-dir", default=str(DEFAULT_RECORD_DIR))
    parser.add_argument("--record-tag", default="jam_rx")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    info_mode_locked = args.parse_policy == "onekey_then_info" and args.initial_level >= 2
    rx_mode, profile_name, profile = get_receiver_profile(
        args.team,
        args.initial_level,
        parse_policy=args.parse_policy,
        info_mode_locked=info_mode_locked,
    )
    state = ReceiverState(
        team=args.team,
        level=args.initial_level,
        rx_mode=rx_mode,
        profile_name=profile_name,
        center_freq=profile.center_freq,
        rf_bandwidth=profile.rf_bandwidth,
        sensitivity=profile.sensitivity,
    )

    rx = None
    server_comm = None
    prev_jam_payload: bytes | None = None
    last_emitted_jam_frame_hex: str | None = None
    last_status_emit = 0.0
    pending_level = state.level
    current_payload_endian = "little" if args.payload_endian == "auto" else args.payload_endian
    wave_recorder = (
        WaveRecorder(record_dir=Path(args.record_dir).expanduser(), record_tag=args.record_tag, enabled=True)
        if args.record_wave
        else None
    )
    record_summary: dict[str, Any] = {}
    final_status = "completed"
    stream = ProtocolStreamReassembler(max_buffer=16384)
    live = LiveInfoState()
    cycle_filter = StrictInfoCycleFilter()
    off_lock = OffsetLock(hold_buffers=args.offset_hold_bufs)

    def reset_info_pipeline() -> tuple[ProtocolStreamReassembler, LiveInfoState, StrictInfoCycleFilter, OffsetLock]:
        return (
            ProtocolStreamReassembler(max_buffer=16384),
            LiveInfoState(),
            StrictInfoCycleFilter(),
            OffsetLock(hold_buffers=args.offset_hold_bufs),
        )

    def handle_jam_level_change(level: int) -> None:
        nonlocal pending_level
        pending_level = level
        if not args.quiet:
            emit_error("jam level change requested", jam_level=level)

    try:
        rx = adi.Pluto(f"ip:{args.rx_ip}")
        configure_receiver(
            rx=rx,
            center_freq=state.center_freq,
            sample_rate=args.sample_rate,
            rf_bandwidth=state.rf_bandwidth,
            rx_gain_db=args.rx_gain_db,
            rx_buffer_size=args.rx_buffer_size,
        )

        if wave_recorder is not None:
            try:
                wave_recorder.start(
                    {
                        "launcher_tag": args.record_tag,
                        "rx_ip": args.rx_ip,
                        "team": args.team,
                        "initial_level": args.initial_level,
                        "parse_policy": args.parse_policy,
                        "payload_endian_mode": args.payload_endian,
                        "sample_rate": args.sample_rate,
                        "rx_buffer_size": args.rx_buffer_size,
                        "rx_gain_db": args.rx_gain_db,
                        "sps": args.sps,
                        "bt": args.bt,
                        "center_freq": state.center_freq,
                        "rf_bandwidth": state.rf_bandwidth,
                        "profile": state.profile_name,
                    }
                )
                record_summary = wave_recorder.summary()
            except Exception as exc:
                emit_error(
                    "failed to start wave recording",
                    detail=str(exc),
                    record_dir=str(Path(args.record_dir).expanduser()),
                )
                try:
                    wave_recorder.close(status="failed", failure_reason=str(exc))
                    record_summary = wave_recorder.summary()
                except Exception:
                    pass
                wave_recorder = None

        if not args.no_server_comm:
            server_comm = RadarServerComm(
                server_ip=args.server_ip,
                server_port=args.server_port,
                on_jam_level_change=handle_jam_level_change,
            )
            if server_comm.connect():
                server_comm.start()
            else:
                emit_error("failed to connect radar server", server_ip=args.server_ip, server_port=args.server_port)
                server_comm = None

        emit_json(
            {
                "kind": "jam_started",
                "ts": time.time(),
                "rx_ip": args.rx_ip,
                "team": state.team,
                "jam_level": state.level,
                "rx_mode": state.rx_mode,
                "profile": state.profile_name,
                "center_freq": state.center_freq,
                "rf_bandwidth": state.rf_bandwidth,
                "sensitivity": state.sensitivity,
                "parse_policy": args.parse_policy,
                "payload_endian_mode": args.payload_endian,
                **record_summary,
            }
        )
        emit_json(state.to_status(args.rx_ip, server_connected=bool(server_comm and server_comm.connected)))

        while True:
            if pending_level != state.level:
                if args.parse_policy == "onekey_then_info" and pending_level >= 2:
                    info_mode_locked = True
                prev_rx_mode = state.rx_mode
                prev_profile_name = state.profile_name
                prev_center_freq = state.center_freq
                prev_rf_bandwidth = state.rf_bandwidth
                rx_mode, profile_name, profile = get_receiver_profile(
                    state.team,
                    pending_level,
                    parse_policy=args.parse_policy,
                    info_mode_locked=info_mode_locked,
                )
                state.level = pending_level
                state.rx_mode = rx_mode
                state.profile_name = profile_name
                state.center_freq = profile.center_freq
                state.rf_bandwidth = profile.rf_bandwidth
                state.sensitivity = profile.sensitivity
                should_reconfigure = (
                    prev_rx_mode != state.rx_mode
                    or prev_profile_name != state.profile_name
                    or prev_center_freq != state.center_freq
                    or prev_rf_bandwidth != state.rf_bandwidth
                )
                if should_reconfigure:
                    state.last_best_jam_dist = None
                    state.last_best_info_dist = None
                    state.last_scan_offset = None
                    state.no_packet_streak = 0
                    current_payload_endian = "little" if args.payload_endian == "auto" else args.payload_endian
                    prev_jam_payload = None
                    last_emitted_jam_frame_hex = None
                    stream, live, cycle_filter, off_lock = reset_info_pipeline()
                    configure_receiver(
                        rx=rx,
                        center_freq=state.center_freq,
                        sample_rate=args.sample_rate,
                        rf_bandwidth=state.rf_bandwidth,
                        rx_gain_db=args.rx_gain_db,
                        rx_buffer_size=args.rx_buffer_size,
                    )
                emit_json(
                    {
                        "kind": "jam_level_change",
                        "ts": time.time(),
                        "team": state.team,
                        "jam_level": state.level,
                        "rx_mode": state.rx_mode,
                        "profile": state.profile_name,
                        "center_freq": state.center_freq,
                        "rf_bandwidth": state.rf_bandwidth,
                        "sensitivity": state.sensitivity,
                    }
                )

            iq = np.asarray(rx.rx()).astype(np.complex64, copy=False)
            if wave_recorder is not None:
                try:
                    wave_recorder.write(iq)
                    record_summary = wave_recorder.summary()
                except Exception as exc:
                    emit_error(
                        "failed writing wave recording",
                        detail=str(exc),
                        **wave_recorder.summary(),
                    )
                    try:
                        wave_recorder.close(
                            status="failed",
                            failure_reason=str(exc),
                            final_level=state.level,
                            final_rx_mode=state.rx_mode,
                        )
                        record_summary = wave_recorder.summary()
                    finally:
                        wave_recorder = None
            if iq.size < 128:
                continue

            pwr = 10.0 * np.log10(float(np.mean(np.abs(iq) ** 2)) + 1e-15)
            state.last_buffer_power_dbfs = round(pwr, 2)
            inst = fm_demod(iq, args.sample_rate)

            effective_access_bit_errors = args.access_bit_errors
            if state.no_packet_streak >= args.auto_relax_after_bufs:
                effective_access_bit_errors = args.access_bit_errors + max(0, args.auto_relax_access_bit_errors)

            now = time.time()
            if state.rx_mode == RX_MODE_JAM:
                cand = slice_packet_candidates(
                    inst,
                    args.sps,
                    args.bt,
                    state.sensitivity,
                    max_access_bit_errors=effective_access_bit_errors,
                    allow_jam=True,
                    info_only=False,
                    refine_span=max(0, int(args.offset_refine_span)),
                    max_candidates=max(1, args.multi_offset),
                )

                if not cand:
                    state.no_packet_streak += 1
                    state.last_buffer_packets = 0
                    prev_jam_payload = None
                    if state.no_packet_streak == args.no_packet_warn_bufs and not args.quiet:
                        emit_error("no jam packets detected", no_packet_streak=state.no_packet_streak)
                    if now - last_status_emit >= args.status_interval:
                        last_status_emit = now
                        emit_json(
                            state.to_status(args.rx_ip, server_connected=bool(server_comm and server_comm.connected))
                        )
                    continue

                jam_counts = []
                for row in cand:
                    jam_count = sum(1 for packet in row["packets"] if packet["kind"] == "JAM" and packet["valid"])
                    jam_counts.append((jam_count, -int(row["best_jam_dist"]), int(row["packet_n"]), row))
                selected = max(jam_counts, key=lambda item: (item[0], item[1], item[2]))[3]
                packets = selected["packets"]
                best_jam_dist = int(selected["best_jam_dist"])

                state.no_packet_streak = 0 if packets else (state.no_packet_streak + 1)
                state.last_buffer_packets = len(packets)
                saw_valid_jam_payload = False

                for packet in packets:
                    air_ok, air_reason = check_air_packet(packet, expected_kind="JAM")
                    if not air_ok:
                        if packet.get("kind") == "JAM":
                            prev_jam_payload = None
                            if not args.quiet:
                                emit_error(
                                    "drop invalid jam air packet",
                                    reason=air_reason,
                                    payload_hex=packet.get("payload", b"").hex().upper(),
                                )
                        continue

                    saw_valid_jam_payload = True
                    matches: list[dict[str, Any]] = []
                    if prev_jam_payload is not None:
                        matches = extract_jam_0a06_frames(prev_jam_payload + packet["payload"])
                    prev_jam_payload = packet["payload"]

                    for match in matches:
                        if match["frame_hex"] == last_emitted_jam_frame_hex:
                            continue

                        data = bytes(match["data"])
                        confidence = jam_confidence(best_jam_dist, True, data)
                        if confidence < args.confidence_threshold:
                            if not args.quiet:
                                emit_error(
                                    "drop low confidence jam frame",
                                    confidence=round(confidence, 4),
                                    frame_hex=match["frame_hex"],
                                )
                            continue

                        last_emitted_jam_frame_hex = match["frame_hex"]
                        state.jam_frame_count += 1
                        state.last_key = data.decode("ascii", errors="replace")
                        state.last_seq = int(match["seq"])
                        state.last_best_jam_dist = best_jam_dist
                        state.last_scan_offset = int(match["start"])
                        state.last_confidence = round(confidence, 4)
                        state.last_frame_hex = match["frame_hex"]
                        state.last_frame_ts = time.time()

                        if server_comm is not None and server_comm.connected:
                            if not server_comm.send_jam_key(data) and not args.quiet:
                                emit_error("failed sending jam key to radar server")

                        emit_json(
                            build_jam_frame_payload(
                                state=state,
                                data=data,
                                seq=int(match["seq"]),
                                confidence=confidence,
                                best_jam_dist=best_jam_dist,
                                scan_offset=int(match["start"]),
                                frame_hex=match["frame_hex"],
                            )
                        )

                if not saw_valid_jam_payload:
                    prev_jam_payload = None
            else:
                cand = slice_packet_candidates(
                    inst,
                    args.sps,
                    args.bt,
                    state.sensitivity,
                    max_access_bit_errors=effective_access_bit_errors,
                    allow_jam=False,
                    info_only=True,
                    refine_span=max(0, int(args.offset_refine_span)),
                    max_candidates=max(1, args.multi_offset),
                )

                if not cand:
                    state.no_packet_streak += 1
                    state.last_buffer_packets = 0
                    if state.no_packet_streak == args.no_packet_warn_bufs and not args.quiet:
                        emit_error("no info packets detected", no_packet_streak=state.no_packet_streak)
                    if now - last_status_emit >= args.status_interval:
                        last_status_emit = now
                        emit_json(
                            state.to_status(args.rx_ip, server_connected=bool(server_comm and server_comm.connected))
                        )
                    continue

                selected = select_best_offset_candidate(
                    stream,
                    live,
                    cand,
                    ts=now,
                    fuzzy_long_frame=False,
                    fuzzy_long_byte_delta=8,
                )
                selected = off_lock.choose(cand, selected)
                packets = selected["packets"]
                best_info_dist = int(selected["best_info_dist"])
                scan_offset = int(selected["off"])

                state.no_packet_streak = 0 if packets else (state.no_packet_streak + 1)
                state.last_buffer_packets = len(packets)
                state.last_best_info_dist = best_info_dist
                state.last_scan_offset = scan_offset

                for packet in packets:
                    air_ok, air_reason = check_air_packet(packet, expected_kind="INFO")
                    if not air_ok:
                        if packet.get("kind") == "INFO" and not args.quiet:
                            emit_error(
                                "drop invalid info air packet",
                                reason=air_reason,
                                payload_hex=packet.get("payload", b"").hex().upper(),
                            )
                        continue
                    stream.append_payload(packet["payload"])

                for frame in stream.extract_frames(ts=now):
                    if args.strict_cycle and not cycle_filter.accept(frame):
                        continue
                    decoded_endian = current_payload_endian
                    frame.decoded = decode_cmd(frame.cmd_id, frame.data, payload_endian=decoded_endian)
                    if (
                        frame.cmd_id == CMD_0A01
                        and args.payload_endian == "auto"
                        and decoded_endian == "little"
                        and info_positions_out_of_bounds(frame.decoded)
                    ):
                        current_payload_endian = "big"
                        decoded_endian = "big"
                        frame.decoded = decode_cmd(frame.cmd_id, frame.data, payload_endian=decoded_endian)
                        if not args.quiet:
                            emit_error(
                                "0x0A01 little-endian out of bounds, switched to big-endian payload parsing",
                                cmd_id="0x0A01",
                                payload_endian=decoded_endian,
                                data_hex=frame.data.hex().upper(),
                            )
                    live.update(frame)
                    if frame.cmd_id != CMD_0A01:
                        continue

                    state.info_frame_count += 1
                    state.last_info_seq = frame.seq
                    state.last_info_positions = frame.decoded
                    state.last_info_frame_hex = frame.data.hex().upper()
                    state.last_info_frame_ts = frame.ts

                    normalized_frame_data = (
                        encode_cmd_0a01_payload(frame.decoded, "little")
                        if decoded_endian == "big"
                        else frame.data
                    )
                    if server_comm is not None and server_comm.connected:
                        if not server_comm.send_command_data(CMD_0A01, normalized_frame_data) and not args.quiet:
                            emit_error("failed sending 0x0A01 to radar server")

                    emit_json(
                        build_info_frame_payload(
                            state=state,
                            seq=frame.seq,
                            data=normalized_frame_data,
                            decoded=frame.decoded,
                            best_info_dist=best_info_dist,
                            scan_offset=scan_offset,
                            payload_endian=decoded_endian,
                        )
                    )

            now = time.time()
            if now - last_status_emit >= args.status_interval:
                last_status_emit = now
                emit_json(state.to_status(args.rx_ip, server_connected=bool(server_comm and server_comm.connected)))

    except KeyboardInterrupt:
        final_status = "interrupted"
        pass
    except Exception as exc:
        final_status = "error"
        emit_error(
            "jam rx exception",
            detail=str(exc),
            error_type=type(exc).__name__,
            traceback=traceback.format_exc(limit=12),
        )
        return 1
    finally:
        if server_comm is not None:
            server_comm.stop()
        if wave_recorder is not None:
            try:
                wave_recorder.close(
                    status=final_status,
                    final_level=state.level,
                    final_rx_mode=state.rx_mode,
                    final_profile=state.profile_name,
                    final_center_freq=state.center_freq,
                )
                record_summary = wave_recorder.summary()
            except Exception as exc:
                emit_error("failed finalizing wave recording", detail=str(exc), **wave_recorder.summary())
        if rx is not None:
            try:
                del rx
            except Exception:
                pass
        emit_json({"kind": "jam_stopped", "ts": time.time(), **record_summary})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
