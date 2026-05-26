from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..protocol import (
    CMD_0A01,
    CMD_0A02,
    CMD_0A03,
    CMD_0A04,
    CMD_0A05,
    INFO_CMD_ORDER,
    build_referee_frame,
)


@dataclass
class RoboMasterSignalInfo:
    cmd_id_1: int = CMD_0A01
    hero_position: list[int] = field(default_factory=lambda: [0, 0])
    engineer_position: list[int] = field(default_factory=lambda: [0, 0])
    infentry_position_1: list[int] = field(default_factory=lambda: [0, 0])
    infentry_position_2: list[int] = field(default_factory=lambda: [0, 0])
    drone_position: list[int] = field(default_factory=lambda: [0, 0])
    sentinel_position: list[int] = field(default_factory=lambda: [0, 0])

    cmd_id_2: int = CMD_0A02
    hero_blood: int = 0
    engineer_blood: int = 0
    infentry_blood_1: int = 0
    infentry_blood_2: int = 0
    saven_blood: int = 0
    sentinel_blood: int = 0

    cmd_id_3: int = CMD_0A03
    hero_amnunition: int = 0
    infentry_amnunition_1: int = 0
    infentry_amnunition_2: int = 0
    drone_amnunition: int = 0
    sentinel_amnunition: int = 0

    cmd_id_4: int = CMD_0A04
    econmic_remain: int = 0
    economic_total: int = 0
    occupation_status: bytes = b""
    cmd_id_5: int = CMD_0A05
    hero_gain: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])
    engineer_gain: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])
    infentry_gain_1: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])
    infentry_gain_2: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])
    sentinel_gain: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])
    sentinel_posture: int = 0


class MessageValueGenerator:
    def __init__(self, set_mode: str = "random"):
        self.set_mode = set_mode
        self.seq = 0
        self._rng = random.Random(2026)

        self.SOF = 0xA5
        self.cmd_id_1 = CMD_0A01.to_bytes(2, byteorder="little")
        self.cmd_id_2 = CMD_0A02.to_bytes(2, byteorder="little")
        self.cmd_id_3 = CMD_0A03.to_bytes(2, byteorder="little")
        self.cmd_id_4 = CMD_0A04.to_bytes(2, byteorder="little")
        self.cmd_id_5 = CMD_0A05.to_bytes(2, byteorder="little")

        self.hero_position_x = self._pick_u16(0, 1000)
        self.hero_position_y = self._pick_u16(0, 1000)
        self.engineer_position_x = self._pick_u16(0, 1000)
        self.engineer_position_y = self._pick_u16(0, 1000)
        self.infentry_position_1_x = self._pick_u16(0, 1000)
        self.infentry_position_1_y = self._pick_u16(0, 1000)
        self.infentry_position_2_x = self._pick_u16(0, 1000)
        self.infentry_position_2_y = self._pick_u16(0, 1000)
        self.drone_position_x = self._pick_u16(0, 1000)
        self.drone_position_y = self._pick_u16(0, 1000)
        self.sentinel_position_x = self._pick_u16(0, 1000)
        self.sentinel_position_y = self._pick_u16(0, 1000)
        self.hero_blood = self._pick_u16(0, 200)
        self.engineer_blood = self._pick_u16(0, 200)
        self.infentry_blood_1 = self._pick_u16(0, 200)
        self.infentry_blood_2 = self._pick_u16(0, 200)
        self.save_blood = self._pick_u16(0, 200)
        self.sentinel_blood = self._pick_u16(0, 200)
        self.hero_ammunition = self._pick_u16(0, 100)
        self.infentry_ammunition_1 = self._pick_u16(0, 100)
        self.infentry_ammunition_2 = self._pick_u16(0, 100)
        self.drone_ammunition = self._pick_u16(0, 100)
        self.sentinel_ammunition = self._pick_u16(0, 100)
        self.econmic_remain = self._pick_u16(0, 1000)
        self.economic_total = self._pick_u16(0, 1000)
        self.occupation_status = self._pick_u32()
        self.hero_gain = self._pack_gain([self._rng.randint(0, 100) for _ in range(5)])
        self.engineer_gain = self._pack_gain([self._rng.randint(0, 100) for _ in range(5)])
        self.infentry_gain_1 = self._pack_gain([self._rng.randint(0, 100) for _ in range(5)])
        self.infentry_gain_2 = self._pack_gain([self._rng.randint(0, 100) for _ in range(5)])
        self.sentinel_gain = self._pack_gain([self._rng.randint(0, 100) for _ in range(5)])
        self.sentinel_posture = self._rng.randint(0, 255).to_bytes(1, byteorder="little")

    def _pick_u16(self, lo: int, hi: int) -> bytes:
        value = self._rng.randint(lo, hi) if self.set_mode == "random" else lo
        return int(value).to_bytes(2, byteorder="little")

    def _pick_u32(self) -> bytes:
        value = self._rng.randint(0, 0xFFFFFFFF) if self.set_mode == "random" else 0
        return int(value).to_bytes(4, byteorder="little")

    def _pack_gain(self, gain: list[int]) -> bytes:
        if len(gain) != 5:
            raise ValueError("gain must contain [recovery, cooling, defense, vulnerability, attack]")
        recovery, cooling, defense, vulnerability, attack = gain
        body = bytearray()
        body.extend(int(recovery & 0xFF).to_bytes(1, byteorder="little"))
        body.extend(int(cooling & 0xFFFF).to_bytes(2, byteorder="little"))
        body.extend(int(defense & 0xFF).to_bytes(1, byteorder="little"))
        body.extend(int(vulnerability & 0xFF).to_bytes(1, byteorder="little"))
        body.extend(int(attack & 0xFFFF).to_bytes(2, byteorder="little"))
        return bytes(body)

    def _build_cmd_0a01(self) -> bytes:
        return (
            self.hero_position_x
            + self.hero_position_y
            + self.engineer_position_x
            + self.engineer_position_y
            + self.infentry_position_1_x
            + self.infentry_position_1_y
            + self.infentry_position_2_x
            + self.infentry_position_2_y
            + self.drone_position_x
            + self.drone_position_y
            + self.sentinel_position_x
            + self.sentinel_position_y
        )

    def _build_cmd_0a02(self) -> bytes:
        return (
            self.hero_blood
            + self.engineer_blood
            + self.infentry_blood_1
            + self.infentry_blood_2
            + self.save_blood
            + self.sentinel_blood
        )

    def _build_cmd_0a03(self) -> bytes:
        return (
            self.hero_ammunition
            + self.infentry_ammunition_1
            + self.infentry_ammunition_2
            + self.drone_ammunition
            + self.sentinel_ammunition
        )

    def _build_cmd_0a04(self) -> bytes:
        return self.econmic_remain + self.economic_total + self.occupation_status

    def _build_cmd_0a05(self) -> bytes:
        body = bytearray()
        body.extend(self.hero_gain)
        body.extend(self.engineer_gain)
        body.extend(self.infentry_gain_1)
        body.extend(self.infentry_gain_2)
        body.extend(self.sentinel_gain)
        body.extend(self.sentinel_posture)
        return bytes(body)

    def build_snapshot(self) -> bytes:
        frames = []
        for cmd_id, payload in [
            (CMD_0A01, self._build_cmd_0a01()),
            (CMD_0A02, self._build_cmd_0a02()),
            (CMD_0A03, self._build_cmd_0a03()),
            (CMD_0A04, self._build_cmd_0a04()),
            (CMD_0A05, self._build_cmd_0a05()),
        ]:
            frames.append(build_referee_frame(cmd_id, payload, self.seq))
            self.seq = (self.seq + 1) & 0xFF
        return b"".join(frames)


__all__ = ["MessageValueGenerator", "RoboMasterSignalInfo", "INFO_CMD_ORDER"]
