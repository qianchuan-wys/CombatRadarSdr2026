from __future__ import annotations

import argparse
import collections
import json
import socket
import sys
import time

import numpy as np

try:
    import adi
except ImportError:
    print("ERROR: pyadi-iio not installed. Run: pip install pyadi-iio")
    sys.exit(2)

from ..phy import fm_demod
from ..protocol import CMD_0A01, CMD_0A02, CMD_0A03, CMD_0A04, CMD_0A05, CMD_0A06, SOF, crc8_maxim, crc16_ibm
from ..radio_profiles import INFO_PROFILE_CHOICES, INFO_PROFILES
from ..parser.gnuradio_frame_parser import (
    LiveInfoState,
    OffsetLock,
    ProtocolStreamReassembler,
    StrictInfoCycleFilter,
    decode_cmd,
    select_best_offset_candidate,
    slice_packet_candidates,
)
from ..server_comm import RadarServerComm


def parse_jam_frame(payload15: bytes) -> tuple[int, bytes, bool]:
    """Extract cmd_id and data from a 15-byte JAM protocol frame.
    
    Returns: (cmd_id, data, is_valid)
    
    Frame structure: SOF(1) + len(2) + seq(1) + crc8(1) + cmd(2) + data(6) + crc16(2)
    """
    if len(payload15) < 15:
        return 0, b"", False
    
    try:
        sof = payload15[0]
        if sof != SOF:
            return 0, b"", False
        
        # Verify header CRC
        hdr_crc = crc8_maxim(payload15[0:4])
        if hdr_crc != payload15[4]:
            return 0, b"", False
        
        # Extract cmd_id and data
        cmd_id = int.from_bytes(payload15[5:7], "little")
        data = payload15[7:13]  # 6 bytes for JAM key
        
        # Verify data CRC (optional check)
        expected_crc = crc16_ibm(payload15[0:13])
        actual_crc = int.from_bytes(payload15[13:15], "little")
        is_valid = (expected_crc == actual_crc)
        
        return cmd_id, data, is_valid
    except Exception:
        return 0, b"", False



def main() -> int:
    parser = argparse.ArgumentParser(description="RM2026 2-GFSK RX")
    parser.add_argument("--rx-ip", default="192.168.1.10")
    parser.add_argument("--profile", choices=INFO_PROFILE_CHOICES, default=None)
    parser.add_argument("--center-freq", type=int, default=433_200_000)
    parser.add_argument("--sample-rate", type=int, default=1_000_000)
    parser.add_argument("--sps", type=int, default=52)
    parser.add_argument("--bt", type=float, default=0.35)
    parser.add_argument("--sensitivity", type=float, default=1.5756)
    parser.add_argument("--rf-bandwidth", type=int, default=540_000)
    parser.add_argument("--rx-gain-db", type=float, default=50.0)
    parser.add_argument("--rx-buffer-size", type=int, default=262144)
    parser.add_argument("--agc-mode", choices=["manual", "fast_attack", "slow_attack", "hybrid"], default="manual",
                        help="Gain control mode: manual/fast_attack/slow_attack/hybrid")
    parser.add_argument("--duration", type=int, default=0,
                        help="Test duration in seconds (0 = infinite)")
    parser.add_argument("--access-bit-errors", type=int, default=1)
    parser.add_argument("--multi-offset", type=int, default=3)
    parser.add_argument("--offset-hold-bufs", type=int, default=8)
    parser.add_argument("--offset-refine-span", type=int, default=3)
    parser.add_argument("--allow-jam", action="store_true")
    parser.add_argument("--info-only", action="store_true")
    parser.add_argument("--fuzzy-long-frame", action="store_true")
    parser.add_argument("--fuzzy-long-byte-delta", type=int, default=8)
    parser.add_argument("--strict-cycle", action="store_true")
    parser.add_argument("--json-lines", action="store_true")
    parser.add_argument("--out-proto", choices=["stdout", "udp", "tcp"], default="stdout")
    parser.add_argument("--out-addr", default="127.0.0.1")
    parser.add_argument("--out-port", type=int, default=4000)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--no-packet-warn-bufs", type=int, default=40)
    parser.add_argument("--auto-relax-after-bufs", type=int, default=40)
    parser.add_argument("--auto-relax-access-bit-errors", type=int, default=1)
    parser.add_argument("--panel", action="store_true")
    parser.add_argument("--server-ip", default="127.0.0.1", help="Radar server IP for comm")
    parser.add_argument("--server-port", type=int, default=5000, help="Radar server port")
    parser.add_argument("--no-server-comm", action="store_true", help="Disable server communication")
    args = parser.parse_args()

    if args.profile is not None:
        preset = INFO_PROFILES[args.profile]
        if args.center_freq == parser.get_default("center_freq"):
            args.center_freq = preset.center_freq
        if args.rf_bandwidth == parser.get_default("rf_bandwidth"):
            args.rf_bandwidth = preset.rf_bandwidth
        if args.sensitivity == parser.get_default("sensitivity"):
            args.sensitivity = preset.sensitivity

    print("=" * 68)
    print("RM2026 2-GFSK RX")
    print("=" * 68)
    print(f"RX IP          : {args.rx_ip}")
    print(f"Profile        : {args.profile or 'manual'}")
    print(f"Center Freq    : {args.center_freq} Hz")
    print(f"Sample Rate    : {args.sample_rate} S/s")
    print(f"SPS            : {args.sps}")
    print(f"BT             : {args.bt}")
    print(f"Sensitivity    : {args.sensitivity} rad/sample")
    print(f"RX Gain Mode   : {args.agc_mode.upper()}")
    if args.agc_mode == "manual":
        print(f"RX Gain        : {args.rx_gain_db} dB")
    print(f"Access Bit Err : {args.access_bit_errors}")
    print(f"JSON Lines     : {args.json_lines}")
    print(f"Panel          : {args.panel}")
    print(f"Strict Cycle   : {args.strict_cycle}")
    print(f"Multi Offset   : {args.multi_offset}")
    print(f"Offset Hold    : {args.offset_hold_bufs}")
    print(f"Allow JAM      : {args.allow_jam}")
    print(f"Info Only      : {args.info_only or not args.allow_jam}")
    print(f"Fuzzy Long Frm : {args.fuzzy_long_frame}")
    print(f"Fuzzy Byte Dlt : {args.fuzzy_long_byte_delta}")
    if args.duration > 0:
        print(f"Test Duration  : {args.duration} seconds")

    rx = None
    out_sock = None
    out_addr = None
    server_comm = None
    server_last_send = 0.0
    good = 0
    bad = 0
    recent = collections.deque(maxlen=8)
    stream = ProtocolStreamReassembler(max_buffer=16384)
    live = LiveInfoState()
    cycle_filter = StrictInfoCycleFilter()
    off_lock = OffsetLock(hold_buffers=args.offset_hold_bufs)
    last_group_print = 0.0
    no_packet_streak = 0
    info_only_mode = bool(args.info_only or not args.allow_jam)

    try:
        rx = adi.Pluto(f"ip:{args.rx_ip}")
        rx.sample_rate = int(args.sample_rate)
        rx.rx_lo = int(args.center_freq)
        rx.rx_rf_bandwidth = int(args.rf_bandwidth)
        rx.rx_enabled_channels = [0]
        rx.rx_buffer_size = int(args.rx_buffer_size)
        
        # Configure gain control mode
        if args.agc_mode == "manual":
            rx.gain_control_mode_chan0 = "manual"
            rx.rx_hardwaregain_chan0 = float(args.rx_gain_db)
            print(f"✓ RX configured: Manual mode, Gain={args.rx_gain_db} dB")
        else:
            # AGC mode (fast or slow)
            rx.gain_control_mode_chan0 = args.agc_mode
            print(f"✓ RX configured: AGC mode={args.agc_mode.upper()}")

        print("Receiver started. Press Ctrl+C to stop...")
        if args.json_lines and args.out_proto in ("udp", "tcp"):
            if args.out_proto == "udp":
                out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                out_addr = (args.out_addr, int(args.out_port))
            else:
                out_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                out_sock.settimeout(5.0)
                out_sock.connect((args.out_addr, int(args.out_port)))
                out_addr = (args.out_addr, int(args.out_port))

        # Initialize server communication
        if not args.no_server_comm:
            server_comm = RadarServerComm(server_ip=args.server_ip, server_port=args.server_port)
            if server_comm.connect():
                server_comm.start()
                print(f"✓ Server communication started: {args.server_ip}:{args.server_port}")
            else:
                print(f"✗ Failed to connect to server {args.server_ip}:{args.server_port}")
                server_comm = None

        buf_idx = 0
        start_time = time.time()
        while True:
            # Check duration limit
            if args.duration > 0:
                elapsed = time.time() - start_time
                if elapsed > args.duration:
                    print(f"\n✓ Test duration {args.duration}s reached. Exiting...")
                    break
            
            iq = np.asarray(rx.rx()).astype(np.complex64, copy=False)
            if iq.size < 128:
                continue
            buf_idx += 1

            pwr = 10.0 * np.log10(float(np.mean(np.abs(iq) ** 2)) + 1e-15)
            inst = fm_demod(iq, args.sample_rate)
            effective_access_bit_errors = args.access_bit_errors
            if no_packet_streak >= args.auto_relax_after_bufs:
                effective_access_bit_errors = args.access_bit_errors + max(0, args.auto_relax_access_bit_errors)

            cand = slice_packet_candidates(
                inst,
                args.sps,
                args.bt,
                args.sensitivity,
                max_access_bit_errors=effective_access_bit_errors,
                allow_jam=args.allow_jam,
                info_only=info_only_mode,
                refine_span=max(0, int(args.offset_refine_span)),
                max_candidates=max(1, args.multi_offset),
            )
            if not cand:
                no_packet_streak += 1
                if no_packet_streak == args.no_packet_warn_bufs:
                    print(f"[warn] no packets for {no_packet_streak} buffers")
                continue

            best_cand = max(cand, key=lambda row: (row.get("info_ok", 0), row.get("packet_n", 0)))
            selected = select_best_offset_candidate(
                stream,
                live,
                cand,
                ts=time.time(),
                fuzzy_long_frame=args.fuzzy_long_frame,
                fuzzy_long_byte_delta=args.fuzzy_long_byte_delta,
            )
            selected = off_lock.choose(cand, selected)
            packets = selected["packets"]
            off = str(selected["off"])

            print(
                f"[buf#{buf_idx:05d}] pwr={pwr:+.1f}dBFS packets={len(packets)} off={off} "
                f"best_info_ok={best_cand.get('info_ok', 0)} best_pkt_n={best_cand.get('packet_n', 0)}"
            )

            if len(packets) == 0:
                no_packet_streak += 1
            else:
                no_packet_streak = 0

            for p in packets:
                payload_hex = p["payload"].hex().upper()
                payload_ascii = p["payload"].decode("ascii", errors="replace")
                if p["kind"] == "INFO" and p["valid"]:
                    good += 1
                    recent.appendleft(payload_ascii)
                    stream.append_payload(p["payload"])
                    frames = stream.extract_frames(ts=time.time())
                    for fr in frames:
                        if args.strict_cycle and not cycle_filter.accept(fr):
                            continue
                        fr.decoded = decode_cmd(fr.cmd_id, fr.data)
                        payload = json.dumps({
                            "ts": fr.ts,
                            "seq": fr.seq,
                            "cmd_id": f"0x{fr.cmd_id:04X}",
                            "data_hex": fr.data.hex().upper(),
                            "data_ascii": fr.data.decode("ascii", errors="replace"),
                            "decoded": fr.decoded,
                        }, ensure_ascii=False)
                        try:
                            if args.out_proto == "stdout":
                                print(payload)
                            elif args.out_proto == "udp" and out_sock is not None:
                                out_sock.sendto(payload.encode("utf-8"), out_addr)
                            elif args.out_proto == "tcp" and out_sock is not None:
                                out_sock.sendall((payload + "\n").encode("utf-8"))
                        except Exception as e:
                            print(f"[warn] failed sending JSON output: {e}")
                        if not args.quiet:
                            print(f"    frame cmd=0x{fr.cmd_id:04X} seq={fr.seq:03d} len={len(fr.data):02d} data={fr.data.hex().upper()}")
                        changed = live.update(fr)
                        if changed and not args.quiet:
                            print(f"CMDID:0x{fr.cmd_id:04X} 数据: {live.format_compact_data(fr.cmd_id)}")
                        
                        # Update server communication with new command data
                        if changed and server_comm is not None:
                            if fr.cmd_id == CMD_0A01:
                                server_comm.update_command_data(cmd_0a01=fr.data)
                            elif fr.cmd_id == CMD_0A02:
                                server_comm.update_command_data(cmd_0a02=fr.data)
                            elif fr.cmd_id == CMD_0A03:
                                server_comm.update_command_data(cmd_0a03=fr.data)
                            elif fr.cmd_id == CMD_0A04:
                                server_comm.update_command_data(cmd_0a04=fr.data)
                            elif fr.cmd_id == CMD_0A05:
                                server_comm.update_command_data(cmd_0a05=fr.data)
                elif p["kind"] == "JAM" and p["valid"] and args.allow_jam:
                    # Handle JAM frame (0x0A06)
                    good += 1
                    cmd_id, data, is_valid = parse_jam_frame(p["payload"])
                    if is_valid and cmd_id == CMD_0A06:
                        decoded = decode_cmd(cmd_id, data)
                        payload = json.dumps({
                            "ts": time.time(),
                            "kind": "JAM",
                            "cmd_id": f"0x{cmd_id:04X}",
                            "data_hex": data.hex().upper(),
                            "data_ascii": data.decode("ascii", errors="replace"),
                            "decoded": decoded,
                        }, ensure_ascii=False)
                        try:
                            if args.out_proto == "stdout":
                                print(payload)
                            elif args.out_proto == "udp" and out_sock is not None:
                                out_sock.sendto(payload.encode("utf-8"), out_addr)
                            elif args.out_proto == "tcp" and out_sock is not None:
                                out_sock.sendall((payload + "\n").encode("utf-8"))
                        except Exception as e:
                            print(f"[warn] failed sending JSON output: {e}")
                        if not args.quiet:
                            print(f"    JAM cmd=0x{cmd_id:04X} key={data.decode('ascii', errors='replace')}")
                else:
                    bad += 1
                total = good + bad
                if not args.quiet:
                    print(f"  kind={p['kind']} len=({p['len1']},{p['len2']}) hex={payload_hex} ascii={payload_ascii} pass={(good/total*100 if total else 0):.1f}%")

            if recent and not args.json_lines and not args.quiet:
                print("  recent_info: " + " | ".join(recent))
            now = time.time()
            if now - last_group_print >= 1.0 and live.ts:
                last_group_print = now
                if args.panel:
                    changed = live.changed_panel_lines()
                    if changed and not args.quiet:
                        print("  ===== live_panel =====")
                        for line in changed:
                            print("  " + line)
                else:
                    cur = live.changed_summary()
                    if cur is not None and not args.quiet:
                        print("  group_stats: " + cur)
                
                # Periodically send data to server
                if server_comm is not None and now - server_last_send >= 0.5:
                    server_last_send = now
                    if server_comm.connected:
                        server_comm.send_info_data()
                        if not args.quiet:
                            stats = server_comm.get_stats()
                            print(f"  server: sent={stats['packets_sent']} recv={stats['packets_recv']} jam_lvl={server_comm.get_jam_level()}")

    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"RX ERROR: {exc}")
        return 1
    finally:
        if server_comm is not None:
            server_comm.stop()
        if rx is not None:
            try:
                del rx
            except Exception:
                pass

    print("RX stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
