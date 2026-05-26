from __future__ import annotations

INFO_ACCESS_CODE = 0x2F6F4C74B914492E
JAM_ACCESS_CODE = 0x16E8D377151C712D
SOF = 0xA5

CMD_0A01 = 0x0A01
CMD_0A02 = 0x0A02
CMD_0A03 = 0x0A03
CMD_0A04 = 0x0A04
CMD_0A05 = 0x0A05
CMD_0A06 = 0x0A06

INFO_CMD_ORDER = [CMD_0A01, CMD_0A02, CMD_0A03, CMD_0A04, CMD_0A05]
INFO_CMD_LEN = {
    CMD_0A01: 24,
    CMD_0A02: 12,
    CMD_0A03: 10,
    CMD_0A04: 8,
    CMD_0A05: 36,
}


def crc8_maxim(data: bytes, init: int = 0xFF) -> int:
    crc = init & 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x01:
                crc = ((crc >> 1) ^ 0x8C) & 0xFF
            else:
                crc = (crc >> 1) & 0xFF
    return crc


def crc16_ibm(data: bytes, init: int = 0xFFFF) -> int:
    crc = init & 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = ((crc >> 1) ^ 0x8408) & 0xFFFF
            else:
                crc = (crc >> 1) & 0xFFFF
    return crc


def build_referee_frame(cmd_id: int, data: bytes, seq: int) -> bytes:
    data_len = len(data)
    header = bytearray(5)
    header[0] = SOF
    header[1:3] = data_len.to_bytes(2, "little")
    header[3] = seq & 0xFF
    header[4] = crc8_maxim(header[:4])

    body = cmd_id.to_bytes(2, "little") + data
    packet_wo_crc16 = bytes(header) + body
    crc16 = crc16_ibm(packet_wo_crc16)
    return packet_wo_crc16 + crc16.to_bytes(2, "little")


def build_air_packet(payload15: bytes, access_code: int) -> bytes:
    if len(payload15) != 15:
        raise ValueError("payload must be exactly 15 bytes")
    ac = access_code.to_bytes(8, "big")
    hdr = (15).to_bytes(2, "big") + (15).to_bytes(2, "big")
    return ac + hdr + payload15
