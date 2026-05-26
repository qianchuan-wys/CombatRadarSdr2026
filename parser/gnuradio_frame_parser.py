from __future__ import annotations

import collections
import dataclasses
import time
from dataclasses import dataclass

import numpy as np

from ..phy import gaussian_taps
from ..protocol import (
    CMD_0A01,
    CMD_0A02,
    CMD_0A03,
    CMD_0A04,
    CMD_0A05,
    CMD_0A06,
    INFO_ACCESS_CODE,
    JAM_ACCESS_CODE,
    SOF,
    crc16_ibm,
    crc8_maxim,
)

INFO_BITS = np.unpackbits(np.frombuffer(INFO_ACCESS_CODE.to_bytes(8, "big"), dtype=np.uint8), bitorder="big")
JAM_BITS = np.unpackbits(np.frombuffer(JAM_ACCESS_CODE.to_bytes(8, "big"), dtype=np.uint8), bitorder="big")
PKT_BITS = (8 + 4 + 15) * 8


@dataclass
class ParsedFrame:
    ts: float
    seq: int
    cmd_id: int
    data: bytes
    decoded: dict | None = None


PAYLOAD_ENDIAN_CHOICES = ("little", "big", "auto")


class StrictInfoCycleFilter:
    def __init__(self) -> None:
        self.last_seq: int | None = None

    def accept(self, frame: ParsedFrame) -> bool:
        if self.last_seq is None:
            self.last_seq = frame.seq
            return True
        self.last_seq = frame.seq
        return True


def bits_to_u16(bits: np.ndarray) -> int:
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def bits_to_bytes(bits: np.ndarray) -> bytes:
    if len(bits) % 8 != 0:
        return b""
    return np.packbits(bits.astype(np.uint8), bitorder="big").tobytes()


def u16_le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset: offset + 2], "little", signed=False)


def u16_be(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset: offset + 2], "big", signed=False)


def u32_value(data: bytes, offset: int, payload_endian: str) -> int:
    return int.from_bytes(data[offset: offset + 4], payload_endian, signed=False)


def u16_value(data: bytes, offset: int, payload_endian: str) -> int:
    if payload_endian == "big":
        return u16_be(data, offset)
    return u16_le(data, offset)


def decode_cmd_0a01(data: bytes, payload_endian: str = "little") -> dict:
    names = [
        "enemy_hero",
        "enemy_engineer",
        "enemy_infantry3",
        "enemy_infantry4",
        "enemy_air",
        "enemy_sentinel",
    ]
    return {
        name: {
            "x": u16_value(data, idx * 4, payload_endian),
            "y": u16_value(data, idx * 4 + 2, payload_endian),
        }
        for idx, name in enumerate(names)
    }


def decode_cmd_0a02(data: bytes, payload_endian: str = "little") -> dict:
    names = ["enemy_hero_hp", "enemy_engineer_hp", "enemy_infantry3_hp", "enemy_infantry4_hp", "reserved", "enemy_sentinel_hp"]
    return {name: u16_value(data, idx * 2, payload_endian) for idx, name in enumerate(names)}


def decode_cmd_0a03(data: bytes, payload_endian: str = "little") -> dict:
    names = ["enemy_hero_ammo", "enemy_infantry3_ammo", "enemy_infantry4_ammo", "enemy_air_ammo", "enemy_sentinel_ammo"]
    return {name: u16_value(data, idx * 2, payload_endian) for idx, name in enumerate(names)}


def decode_cmd_0a04(data: bytes, payload_endian: str = "little") -> dict:
    return {
        "left_coins": u16_value(data, 0, payload_endian),
        "total_coins": u16_value(data, 2, payload_endian),
        "occupation_status": u32_value(data, 4, payload_endian) if len(data) >= 8 else 0,
    }


def decode_cmd_0a05(data: bytes) -> dict:
    return {"len": len(data), "hex": data.hex().upper()}


def decode_cmd_0a06(data: bytes) -> dict:
    return {"key": data.decode("ascii", errors="replace"), "len": len(data)}


def decode_cmd(cmd_id: int, data: bytes, payload_endian: str = "little") -> dict:
    if cmd_id == CMD_0A01:
        return decode_cmd_0a01(data, payload_endian=payload_endian)
    if cmd_id == CMD_0A02:
        return decode_cmd_0a02(data, payload_endian=payload_endian)
    if cmd_id == CMD_0A03:
        return decode_cmd_0a03(data, payload_endian=payload_endian)
    if cmd_id == CMD_0A04:
        return decode_cmd_0a04(data, payload_endian=payload_endian)
    if cmd_id == CMD_0A05:
        return decode_cmd_0a05(data)
    if cmd_id == CMD_0A06:
        return decode_cmd_0a06(data)
    return {"len": len(data), "hex": data.hex().upper()}


def popcount_mismatch(a: np.ndarray, b: np.ndarray) -> int:
    return int(np.count_nonzero(a ^ b))


def access_code_distance_stats(bits: np.ndarray) -> tuple[int, int]:
    if len(bits) < 64:
        return 64, 64
    best_info = 64
    best_jam = 64
    for i in range(0, len(bits) - 63):
        win = bits[i:i + 64]
        d_info = popcount_mismatch(win, INFO_BITS)
        d_jam = popcount_mismatch(win, JAM_BITS)
        best_info = min(best_info, d_info)
        best_jam = min(best_jam, d_jam)
        if best_info == 0 and best_jam == 0:
            break
    return best_info, best_jam


def parse_air_packets(
    bits: np.ndarray,
    max_access_bit_errors: int = 0,
    allow_jam: bool = False,
    info_only: bool = False,
) -> list[dict]:
    out: list[dict] = []
    i = 0
    n = len(bits)

    while i + PKT_BITS <= n:
        win = bits[i:i + 64]
        d_info = popcount_mismatch(win, INFO_BITS)
        d_jam = popcount_mismatch(win, JAM_BITS)

        if d_info <= max_access_bit_errors:
            kind = "INFO"
        elif (not info_only) and allow_jam and d_jam <= max_access_bit_errors and d_jam < d_info:
            kind = "JAM"
        else:
            i += 1
            continue

        pkt = bits[i:i + PKT_BITS]
        hdr = pkt[64:96]
        l1 = bits_to_u16(hdr[:16])
        l2 = bits_to_u16(hdr[16:32])
        payload_bits = pkt[96:216]
        payload = bits_to_bytes(payload_bits)

        out.append({
            "pos": i,
            "kind": kind,
            "len1": l1,
            "len2": l2,
            "payload": payload,
            "valid": (l1 == 15 and l2 == 15),
        })
        i += PKT_BITS

    return out


def slice_packet_candidates(
    inst_freq_hz: np.ndarray,
    sps: int,
    bt: float,
    sensitivity: float,
    max_access_bit_errors: int = 0,
    allow_jam: bool = False,
    info_only: bool = False,
    refine_span: int = 3,
    max_candidates: int = 1,
) -> list[dict]:
    norm = inst_freq_hz * (2.0 * np.pi / 1.0) / (sensitivity * 1e6)
    mf = np.convolve(norm, gaussian_taps(sps=sps, bt=bt), mode="same")

    best_off = 0
    best_score = -1.0
    for off in range(sps):
        sym = mf[off::sps]
        if len(sym) < 32:
            continue
        score = float(np.mean(np.abs(sym)))
        if score > best_score:
            best_score = score
            best_off = off

    cand = [((best_off + d) % sps) for d in range(-refine_span, refine_span + 1)]
    rows: list[tuple[int, int, int, int, int, np.ndarray, list[dict]]] = []
    seen = set()
    for off in cand:
        if off in seen:
            continue
        seen.add(off)
        bits = (mf[off::sps] >= 0.0).astype(np.uint8)
        pkts = parse_air_packets(
            bits,
            max_access_bit_errors=max_access_bit_errors,
            allow_jam=allow_jam,
            info_only=info_only,
        )
        best_info_dist, best_jam_dist = access_code_distance_stats(bits)
        info_ok = sum(1 for p in pkts if p["kind"] == "INFO" and p["valid"])
        rows.append((info_ok, len(pkts), best_info_dist, best_jam_dist, off, bits, pkts))

    rows.sort(key=lambda x: (x[0], x[1], -x[2], -x[3]), reverse=True)
    k = max(1, min(max_candidates, len(rows)))
    out: list[dict] = []
    for info_ok, pkt_n, best_info_dist, best_jam_dist, off, bits, pkts in rows[:k]:
        out.append({
            "off": off,
            "bits": bits,
            "packets": pkts,
            "info_ok": info_ok,
            "packet_n": pkt_n,
            "best_info_dist": best_info_dist,
            "best_jam_dist": best_jam_dist,
        })
    return out


@dataclass
class ProtocolStreamReassembler:
    max_buffer: int = 16384

    def __post_init__(self) -> None:
        self.buf = bytearray()

    def clone(self) -> "ProtocolStreamReassembler":
        cp = ProtocolStreamReassembler(max_buffer=self.max_buffer)
        cp.buf = bytearray(self.buf)
        return cp

    def append_payload(self, payload15: bytes) -> None:
        self.buf.extend(payload15)
        if len(self.buf) > self.max_buffer:
            del self.buf[: len(self.buf) - self.max_buffer]

    def extract_frames(self, ts: float) -> list[ParsedFrame]:
        out: list[ParsedFrame] = []
        i = 0
        n = len(self.buf)

        while i + 5 <= n:
            sof_pos = self.buf.find(bytes([SOF]), i)
            if sof_pos < 0:
                break
            if sof_pos + 5 > n:
                i = sof_pos
                break

            hdr = self.buf[sof_pos: sof_pos + 5]
            if crc8_maxim(hdr[:4]) != hdr[4]:
                i = sof_pos + 1
                continue

            data_len = int.from_bytes(hdr[1:3], "little")
            if data_len > 256:
                i = sof_pos + 1
                continue

            frame_len = 5 + 2 + data_len + 2
            if sof_pos + frame_len > n:
                i = sof_pos
                break

            frame = self.buf[sof_pos: sof_pos + frame_len]
            if int.from_bytes(frame[-2:], "little") != crc16_ibm(frame[:-2]):
                i = sof_pos + 1
                continue

            seq = frame[3]
            cmd_id = int.from_bytes(frame[5:7], "little")
            data = bytes(frame[7:-2])
            out.append(ParsedFrame(ts=ts, seq=seq, cmd_id=cmd_id, data=data, decoded=decode_cmd(cmd_id, data)))
            i = sof_pos + frame_len

        if i > 0:
            del self.buf[:i]
        return out


def evaluate_offset_candidate(
    stream: ProtocolStreamReassembler,
    packets: list[dict],
    live: "LiveInfoState",
    ts: float,
    fuzzy_long_frame: bool = False,
    fuzzy_long_byte_delta: int = 8,
) -> tuple[int, int, int, int]:
    trial = stream.clone()
    appended = 0
    for p in packets:
        if p["kind"] == "INFO" and p["valid"]:
            trial.append_payload(p["payload"])
            appended += 1
    frames = trial.extract_frames(ts=ts)
    priority_gain = sum(1 for fr in frames if fr.cmd_id in (CMD_0A01, CMD_0A05))

    if not fuzzy_long_frame:
        return priority_gain, len(frames), 0, appended

    fuzzy_score = 0
    for fr in frames:
        if fr.cmd_id not in (CMD_0A01, CMD_0A05):
            continue
        prev = live.data.get(fr.cmd_id)
        if not prev:
            continue
        diff = sum(a != b for a, b in zip(prev, fr.data)) + abs(len(prev) - len(fr.data))
        if diff <= fuzzy_long_byte_delta:
            fuzzy_score += (fuzzy_long_byte_delta - diff + len(fr.data))
        else:
            fuzzy_score -= diff

    return priority_gain, len(frames), fuzzy_score, appended


def select_best_offset_candidate(
    stream: ProtocolStreamReassembler,
    live: "LiveInfoState",
    candidates: list[dict],
    ts: float,
    fuzzy_long_frame: bool = False,
    fuzzy_long_byte_delta: int = 8,
) -> dict:
    best = candidates[0]
    best_score = (-1, -1, -10**9, -1, -1)
    for row in candidates:
        priority_gain, frame_gain, fuzzy_score, appended = evaluate_offset_candidate(
            stream,
            row["packets"],
            live,
            ts,
            fuzzy_long_frame=fuzzy_long_frame,
            fuzzy_long_byte_delta=fuzzy_long_byte_delta,
        )
        score = (priority_gain, frame_gain, fuzzy_score, appended, row["packet_n"])
        if score > best_score:
            best_score = score
            best = row
    return best


class OffsetLock:
    def __init__(self, hold_buffers: int = 8) -> None:
        self.hold_buffers = max(0, int(hold_buffers))
        self.current_off: int | None = None
        self.weak_count = 0

    def choose(self, candidates: list[dict], preferred: dict) -> dict:
        if not candidates:
            return preferred
        if self.current_off is None:
            self.current_off = int(preferred["off"])
            self.weak_count = 0
            return preferred

        same = None
        for row in candidates:
            if int(row["off"]) == self.current_off:
                same = row
                break

        if same is None:
            self.current_off = int(preferred["off"])
            self.weak_count = 0
            return preferred

        if same.get("info_ok", 0) > 0:
            self.weak_count = 0
            return same

        self.weak_count += 1
        if self.weak_count <= self.hold_buffers:
            return same

        self.current_off = int(preferred["off"])
        self.weak_count = 0
        return preferred


class LiveInfoState:
    def __init__(self):
        self.data: dict[int, bytes] = {}
        self.ts: dict[int, float] = {}
        self.seq: dict[int, int] = {}
        self.counts: collections.Counter[int] = collections.Counter()
        self.decoded: dict[int, dict] = {}
        self.last_snapshot: dict[int, str] = {}
        self.last_summary: str | None = None

    def update(self, frame: ParsedFrame) -> None:
        prev = self.data.get(frame.cmd_id)
        changed = prev != frame.data
        self.data[frame.cmd_id] = frame.data
        self.ts[frame.cmd_id] = frame.ts
        self.seq[frame.cmd_id] = frame.seq
        self.counts[frame.cmd_id] += 1
        self.decoded[frame.cmd_id] = frame.decoded if frame.decoded is not None else decode_cmd(frame.cmd_id, frame.data)
        return changed

    def snapshot(self) -> dict:
        return {
            "0x0A01": (self.data.get(CMD_0A01).hex().upper() if CMD_0A01 in self.data else None),
            "0x0A02": (self.data.get(CMD_0A02).hex().upper() if CMD_0A02 in self.data else None),
            "0x0A03": (self.data.get(CMD_0A03).hex().upper() if CMD_0A03 in self.data else None),
            "0x0A04": (self.data.get(CMD_0A04).hex().upper() if CMD_0A04 in self.data else None),
            "0x0A05": (self.data.get(CMD_0A05).hex().upper() if CMD_0A05 in self.data else None),
            "0x0A06": (self.data.get(CMD_0A06).hex().upper() if CMD_0A06 in self.data else None),
        }

    def format_summary(self) -> str:
        parts = []
        now = time.time()
        for cmd in (CMD_0A01, CMD_0A02, CMD_0A03, CMD_0A04, CMD_0A05, CMD_0A06):
            if cmd in self.data:
                age_ms = int((now - self.ts[cmd]) * 1000)
                decoded = self.decoded.get(cmd, {})
                if cmd == CMD_0A01:
                    one = decoded.get("enemy_hero", {})
                    short = f"hero=({one.get('x', 0)},{one.get('y', 0)})"
                elif cmd == CMD_0A02:
                    short = f"hero_hp={decoded.get('enemy_hero_hp', 0)} eng_hp={decoded.get('enemy_engineer_hp', 0)} inf3_hp={decoded.get('enemy_infantry3_hp', 0)} inf4_hp={decoded.get('enemy_infantry4_hp', 0)} sent_hp={decoded.get('enemy_sentinel_hp', 0)}"
                elif cmd == CMD_0A03:
                    short = f"hero_ammo={decoded.get('enemy_hero_ammo', 0)} inf3_ammo={decoded.get('enemy_infantry3_ammo', 0)} inf4_ammo={decoded.get('enemy_infantry4_ammo', 0)} air_ammo={decoded.get('enemy_air_ammo', 0)} sent_ammo={decoded.get('enemy_sentinel_ammo', 0)}"
                elif cmd == CMD_0A04:
                    short = (
                        f"left={decoded.get('left_coins', 0)} total={decoded.get('total_coins', 0)} "
                        f"occ=0x{decoded.get('occupation_status', 0):08X}"
                    )
                elif cmd == CMD_0A05:
                    short = f"len={decoded.get('len', 0)}"
                else:
                    short = f"key={decoded.get('key', '')}"
                parts.append(f"0x{cmd:04X}:n={self.counts[cmd]} age={age_ms}ms seq={self.seq[cmd]:03d} {short}")
            else:
                parts.append(f"0x{cmd:04X}:n=0 age=NA seq=NA")
        return " | ".join(parts)

    def format_compact_data(self, cmd_id: int) -> str:
        decoded = self.decoded.get(cmd_id, {})
        if cmd_id == CMD_0A01:
            d = decoded.get("enemy_hero", {})
            return f"hero=({d.get('x', 0)},{d.get('y', 0)})"
        if cmd_id == CMD_0A02:
            return f"hero_hp={decoded.get('enemy_hero_hp', 0)} eng_hp={decoded.get('enemy_engineer_hp', 0)} inf3_hp={decoded.get('enemy_infantry3_hp', 0)} inf4_hp={decoded.get('enemy_infantry4_hp', 0)} sent_hp={decoded.get('enemy_sentinel_hp', 0)}"
        if cmd_id == CMD_0A03:
            return f"hero_ammo={decoded.get('enemy_hero_ammo', 0)} inf3_ammo={decoded.get('enemy_infantry3_ammo', 0)} inf4_ammo={decoded.get('enemy_infantry4_ammo', 0)} air_ammo={decoded.get('enemy_air_ammo', 0)} sent_ammo={decoded.get('enemy_sentinel_ammo', 0)}"
        if cmd_id == CMD_0A04:
            return (
                f"left_coins={decoded.get('left_coins', 0)} total_coins={decoded.get('total_coins', 0)} "
                f"occupation_status=0x{decoded.get('occupation_status', 0):08X}"
            )
        if cmd_id == CMD_0A05:
            return f"len={decoded.get('len', 0)} hex={decoded.get('hex', '')}"
        if cmd_id == CMD_0A06:
            return f"key={decoded.get('key', '')} len={decoded.get('len', 0)}"
        return str(decoded)

    def changed_panel_lines(self) -> list[str]:
        lines = self.format_summary().split(" | ")
        out: list[str] = []
        cmds = (CMD_0A01, CMD_0A02, CMD_0A03, CMD_0A04, CMD_0A05, CMD_0A06)
        for idx, cmd in enumerate(cmds):
            prev = self.last_snapshot.get(cmd)
            if prev != lines[idx]:
                out.append(lines[idx])
                self.last_snapshot[cmd] = lines[idx]
        return out

    def changed_summary(self) -> str | None:
        cur = self.format_summary()
        if self.last_summary != cur:
            self.last_summary = cur
            return cur
        return None
