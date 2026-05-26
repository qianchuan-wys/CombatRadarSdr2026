from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..radio_profiles import INFO_PROFILE_CHOICES, JAM_PROFILE_CHOICES


REPO_ROOT = Path(__file__).resolve().parents[3]


def _spawn(module: str, args: list[str]) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-u", "-m", module, *args],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )


def _pump_output(prefix: str, proc: subprocess.Popen[str], lock: threading.Lock) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        with lock:
            print(f"[{prefix}] {line}", end="")


def _build_info_args(args: argparse.Namespace) -> list[str]:
    out = [
        "--profile",
        args.info_profile,
        "--rx-ip",
        args.info_rx_ip,
        "--agc-mode",
        args.info_agc_mode,
        "--server-ip",
        args.server_ip,
        "--server-port",
        str(args.server_port),
    ]
    if args.info_agc_mode == "manual":
        out.extend(["--rx-gain-db", str(args.info_rx_gain_db)])
    if args.info_panel:
        out.append("--panel")
    if args.info_quiet:
        out.append("--quiet")
    if args.duration > 0:
        out.extend(["--duration", str(args.duration)])
    return out


def _build_jam_args(args: argparse.Namespace) -> list[str]:
    out = [
        "--profile",
        args.jam_profile,
        "--rx-ip",
        args.jam_rx_ip,
        "--confidence-threshold",
        str(args.jam_confidence_threshold),
        "--server-ip",
        args.server_ip,
        "--server-port",
        str(args.server_port),
        "--json-lines",
        "--out-proto",
        "stdout",
    ]
    if args.jam_rx_gain_db is not None:
        out.extend(["--rx-gain-db", str(args.jam_rx_gain_db)])
    if args.jam_quiet:
        out.append("--quiet")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="RM2026 dual-SDR RX launcher")
    parser.add_argument("--info-rx-ip", default="192.168.1.10")
    parser.add_argument("--jam-rx-ip", default="192.168.1.9")
    parser.add_argument("--info-profile", choices=INFO_PROFILE_CHOICES, default="red1")
    parser.add_argument("--jam-profile", choices=JAM_PROFILE_CHOICES, default="red1")
    parser.add_argument("--info-agc-mode", choices=["manual", "fast_attack", "slow_attack", "hybrid"], default="slow_attack")
    parser.add_argument("--info-rx-gain-db", type=float, default=50.0)
    parser.add_argument("--jam-rx-gain-db", type=float, default=50.0)
    parser.add_argument("--jam-confidence-threshold", type=float, default=0.40)
    parser.add_argument("--duration", type=int, default=0, help="Run duration in seconds; 0 means keep running")
    parser.add_argument("--info-panel", action="store_true", dest="info_panel")
    parser.add_argument("--no-info-panel", action="store_false", dest="info_panel")
    parser.set_defaults(info_panel=True)
    parser.add_argument("--info-quiet", action="store_true")
    parser.add_argument("--jam-quiet", action="store_true")
    parser.add_argument("--server-ip", default="127.0.0.1", help="Radar server IP")
    parser.add_argument("--server-port", type=int, default=5000, help="Radar server port")
    args = parser.parse_args()

    print("=" * 72)
    print("RM2026 dual SDR RX launcher")
    print("=" * 72)
    print(f"Info RX IP  : {args.info_rx_ip}")
    print(f"Info Profile: {args.info_profile}")
    print(f"Info Mode   : {args.info_agc_mode}")
    print(f"Jam RX IP   : {args.jam_rx_ip}")
    print(f"Jam Profile : {args.jam_profile}")
    print(f"Jam Thr     : {args.jam_confidence_threshold:.2f}")
    print(f"Server      : {args.server_ip}:{args.server_port}")
    if args.duration > 0:
        print(f"Duration    : {args.duration} seconds")

    info_proc = _spawn("copilot.combat_radar_sdr.apps.rx_app", _build_info_args(args))
    jam_proc = _spawn("copilot.combat_radar_sdr.apps.jam_rx_app", _build_jam_args(args))

    lock = threading.Lock()
    threads = [
        threading.Thread(target=_pump_output, args=("INFO", info_proc, lock), daemon=True),
        threading.Thread(target=_pump_output, args=("JAM", jam_proc, lock), daemon=True),
    ]
    for thread in threads:
        thread.start()

    stop_requested = False

    def _handle_sigint(signum, frame) -> None:
        del signum, frame
        nonlocal stop_requested
        stop_requested = True

    previous_handler = signal.signal(signal.SIGINT, _handle_sigint)
    start_time = time.monotonic()

    try:
        while True:
            info_rc = info_proc.poll()
            jam_rc = jam_proc.poll()

            if info_rc is not None and jam_rc is not None:
                break

            if info_rc is not None:
                print(f"[dual] INFO receiver exited with code {info_rc}; stopping JAM receiver")
                break

            if jam_rc is not None:
                print(f"[dual] JAM receiver exited with code {jam_rc}; stopping INFO receiver")
                break

            if args.duration > 0 and (time.monotonic() - start_time) >= args.duration:
                print(f"[dual] duration {args.duration}s reached; stopping both receivers")
                break

            if stop_requested:
                print("[dual] Ctrl+C received; stopping both receivers")
                break

            time.sleep(0.5)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        for proc in (info_proc, jam_proc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        for proc in (info_proc, jam_proc):
            if proc.poll() is None:
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for thread in threads:
            thread.join(timeout=1)

    info_rc = info_proc.returncode
    jam_rc = jam_proc.returncode
    print("=" * 72)
    print(f"INFO return code: {info_rc}")
    print(f"JAM  return code: {jam_rc}")
    print("=" * 72)

    return 0 if (info_rc == 0 and jam_rc == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())