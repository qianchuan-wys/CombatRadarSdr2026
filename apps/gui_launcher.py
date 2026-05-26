#!/usr/bin/env python3
"""
RM2026 干扰波接收系统 GUI

负责干扰波/信息波接收与显示：
- 配置红/蓝方
- 配置 PlutoSDR 接收器 IP
- 配置主程序服务器地址
- 实时显示干扰波帧数、干扰等级、干扰波密钥
- 干扰等级 3 时显示信息波 0x0A01 敌方机器人坐标
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk, font as tkfont


PYTHONPATH_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"

UI_FONT_CANDIDATES = (
    "Noto Sans CJK SC",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "WenQuanYi Micro Hei",
    "Droid Sans Fallback",
    "SimHei",
)

MONO_FONT_CANDIDATES = (
    "Noto Sans Mono CJK SC",
    "Sarasa Mono SC",
    "WenQuanYi Zen Hei Mono",
    "Droid Sans Mono",
    "DejaVu Sans Mono",
)


@dataclass
class RunSession:
    run_id: int
    started_at: datetime
    log_path: Path
    process: subprocess.Popen[str] | None = None
    read_thread: threading.Thread | None = None
    stop_requested: bool = False
    log_saved: bool = False
    log_lines: list[str] = field(default_factory=list)


class JamRxGUI:
    LAUNCHER_NAME = "gui_launcher"
    WINDOW_TITLE = "RM2026 干扰波接收系统"
    INFO_POSITION_NAMES = (
        ("enemy_hero", "英雄"),
        ("enemy_engineer", "工程"),
        ("enemy_infantry3", "步兵3"),
        ("enemy_infantry4", "步兵4"),
        ("enemy_air", "空中6"),
        ("enemy_sentinel", "哨兵"),
    )
    INITIAL_LEVEL = 1
    PARSE_POLICY = "default"
    PAYLOAD_ENDIAN = "little"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(self.WINDOW_TITLE)
        self.root.geometry("1180x860")
        self.root.resizable(True, True)

        self.active_session: RunSession | None = None
        self.next_run_id = 0
        self.ui_session_id: int | None = None
        self.running = False
        self.log_lock = threading.Lock()

        self.last_jam_key = "N/A"
        self.last_jam_level = "N/A"
        self.last_rx_mode = "N/A"
        self.jam_frame_count = 0
        self.info_frame_count = 0
        self.last_profile = "N/A"
        self.last_center_freq = "N/A"
        self.last_power = "N/A"
        self.last_status = "未运行"
        self.last_info_positions: dict[str, dict[str, int]] = {}

        self.show_realtime = tk.BooleanVar(value=True)

        self._configure_fonts()
        self._create_ui()

    def _pick_font_family(self, candidates, fallback):
        available_fonts = set(tkfont.families(self.root))
        for family in candidates:
            if family in available_fonts:
                return family
        return fallback

    def _configure_fonts(self):
        default_ui_family = tkfont.nametofont("TkDefaultFont").actual("family")
        default_mono_family = tkfont.nametofont("TkFixedFont").actual("family")
        self.ui_font_family = self._pick_font_family(UI_FONT_CANDIDATES, default_ui_family)
        self.mono_font_family = self._pick_font_family(
            MONO_FONT_CANDIDATES,
            default_mono_family or self.ui_font_family,
        )

        named_fonts = {
            "TkDefaultFont": (self.ui_font_family, 10),
            "TkTextFont": (self.ui_font_family, 10),
            "TkMenuFont": (self.ui_font_family, 10),
            "TkHeadingFont": (self.ui_font_family, 10),
            "TkCaptionFont": (self.ui_font_family, 10),
            "TkSmallCaptionFont": (self.ui_font_family, 9),
            "TkIconFont": (self.ui_font_family, 10),
            "TkTooltipFont": (self.ui_font_family, 9),
            "TkFixedFont": (self.mono_font_family, 10),
        }
        for font_name, (family, size) in named_fonts.items():
            try:
                tkfont.nametofont(font_name).configure(family=family, size=size)
            except tk.TclError:
                continue

    def _create_ui(self):
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        config_frame = ttk.LabelFrame(main_frame, text="配置参数", padding=10)
        config_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(config_frame, text="PlutoSDR 接收器 IP:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.jam_ip_var = tk.StringVar(value="192.168.1.10")
        ttk.Entry(config_frame, textvariable=self.jam_ip_var, width=16).grid(row=0, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="阵营:").grid(row=0, column=2, sticky=tk.W, padx=20, pady=5)
        self.team_var = tk.StringVar(value="red")
        ttk.Radiobutton(config_frame, text="红方", variable=self.team_var, value="red").grid(row=0, column=3, sticky=tk.W, padx=5)
        ttk.Radiobutton(config_frame, text="蓝方", variable=self.team_var, value="blue").grid(row=0, column=4, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="服务器 IP:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.server_ip_var = tk.StringVar(value="127.0.0.1")
        ttk.Entry(config_frame, textvariable=self.server_ip_var, width=16).grid(row=1, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="服务器端口:").grid(row=1, column=2, sticky=tk.W, padx=20, pady=5)
        self.server_port_var = tk.StringVar(value="5000")
        ttk.Entry(config_frame, textvariable=self.server_port_var, width=10).grid(row=1, column=3, sticky=tk.W, padx=5)

        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill=tk.X, pady=(0, 10))

        self.start_btn = ttk.Button(control_frame, text="启动接收", command=self._start_receiver)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(control_frame, text="停止接收", command=self._stop_receiver, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        ttk.Separator(control_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Checkbutton(control_frame, text="显示实时日志", variable=self.show_realtime).pack(side=tk.LEFT, padx=5)

        self.status_label = ttk.Label(control_frame, text="未运行", foreground="gray")
        self.status_label.pack(side=tk.RIGHT, padx=10)

        display_frame = ttk.LabelFrame(main_frame, text="接收状态", padding=10)
        display_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(display_frame, text="干扰波密钥:", font=(self.ui_font_family, 10, "bold")).grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.jam_key_label = ttk.Label(display_frame, text="N/A", font=(self.mono_font_family, 14), foreground="blue")
        self.jam_key_label.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(display_frame, text="干扰等级:", font=(self.ui_font_family, 10, "bold")).grid(row=0, column=2, sticky=tk.W, padx=20, pady=5)
        self.jam_level_label = ttk.Label(display_frame, text="N/A", font=(self.ui_font_family, 14, "bold"), foreground="gray")
        self.jam_level_label.grid(row=0, column=3, sticky=tk.W, padx=5, pady=5)

        ttk.Label(display_frame, text="接收模式:", font=(self.ui_font_family, 10, "bold")).grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.rx_mode_label = ttk.Label(display_frame, text="N/A", font=(self.ui_font_family, 12, "bold"), foreground="purple")
        self.rx_mode_label.grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(display_frame, text="干扰波帧数:", font=(self.ui_font_family, 10, "bold")).grid(row=1, column=2, sticky=tk.W, padx=20, pady=5)
        self.jam_count_label = ttk.Label(display_frame, text="0", font=(self.ui_font_family, 12), foreground="darkgreen")
        self.jam_count_label.grid(row=1, column=3, sticky=tk.W, padx=5, pady=5)

        ttk.Label(display_frame, text="信息波帧数:", font=(self.ui_font_family, 10, "bold")).grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        self.info_count_label = ttk.Label(display_frame, text="0", font=(self.ui_font_family, 12), foreground="darkblue")
        self.info_count_label.grid(row=2, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(display_frame, text="当前频段:", font=(self.ui_font_family, 10, "bold")).grid(row=2, column=2, sticky=tk.W, padx=20, pady=5)
        self.profile_label = ttk.Label(display_frame, text="N/A", font=(self.mono_font_family, 12))
        self.profile_label.grid(row=2, column=3, sticky=tk.W, padx=5, pady=5)

        ttk.Label(display_frame, text="中心频率:", font=(self.ui_font_family, 10, "bold")).grid(row=3, column=0, sticky=tk.W, padx=5, pady=5)
        self.freq_label = ttk.Label(display_frame, text="N/A", font=(self.mono_font_family, 12))
        self.freq_label.grid(row=3, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(display_frame, text="接收功率:", font=(self.ui_font_family, 10, "bold")).grid(row=3, column=2, sticky=tk.W, padx=20, pady=5)
        self.power_label = ttk.Label(display_frame, text="N/A", font=(self.mono_font_family, 12))
        self.power_label.grid(row=3, column=3, sticky=tk.W, padx=5, pady=5)

        info_frame = ttk.LabelFrame(main_frame, text="信息波 0x0A01 敌方机器人坐标", padding=10)
        info_frame.pack(fill=tk.X, pady=(0, 10))

        self.info_position_labels: dict[str, ttk.Label] = {}
        for index, (field_name, title) in enumerate(self.INFO_POSITION_NAMES):
            row = index // 2
            col = (index % 2) * 2
            ttk.Label(info_frame, text=f"{title}:", font=(self.ui_font_family, 10, "bold")).grid(
                row=row,
                column=col,
                sticky=tk.W,
                padx=5,
                pady=4,
            )
            value_label = ttk.Label(info_frame, text="N/A", font=(self.mono_font_family, 11), foreground="navy")
            value_label.grid(row=row, column=col + 1, sticky=tk.W, padx=5, pady=4)
            self.info_position_labels[field_name] = value_label

        log_frame = ttk.LabelFrame(main_frame, text="日志输出", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=14,
            width=120,
            font=(self.mono_font_family, 9),
            state=tk.DISABLED,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.tag_config("info", foreground="black")
        self.log_text.tag_config("success", foreground="green")
        self.log_text.tag_config("warn", foreground="orange")
        self.log_text.tag_config("error", foreground="red")
        self.log_text.tag_config("jam", foreground="blue")

    def _new_run_session(self) -> RunSession:
        self.next_run_id += 1
        started_at = datetime.now()
        stamp = started_at.strftime("%Y%m%d_%H%M%S_%f")
        return RunSession(
            run_id=self.next_run_id,
            started_at=started_at,
            log_path=LOG_DIR / f"{self.LAUNCHER_NAME}_{stamp}.log",
        )

    def _record_log_entry(self, session: RunSession, entry: str):
        if session.log_saved:
            return
        with self.log_lock:
            if session.log_saved:
                return
            session.log_lines.append(entry)

    def _append_log(self, entry: str, tag: str = "info"):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{entry}\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _log(
        self,
        message: str,
        tag: str = "info",
        show: bool = True,
        session: RunSession | None = None,
    ):
        entry = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        target_session = session or self.active_session
        if target_session is not None:
            self._record_log_entry(target_session, entry)
        if not show:
            return
        if session is not None and session.run_id != self.ui_session_id:
            return
        if threading.current_thread() is threading.main_thread():
            self._append_log(entry, tag)
            return
        self.root.after(0, self._append_log, entry, tag)

    def _persist_log_session(self, session: RunSession, exit_reason: str):
        with self.log_lock:
            if session.log_saved:
                return
            lines = list(session.log_lines)
        footer_time = datetime.now().strftime("%H:%M:%S")
        started_at = session.started_at.strftime("%Y-%m-%d %H:%M:%S")
        payload = [
            f"# gui_launcher run started at {started_at}",
            *lines,
            f"[{footer_time}] 会话结束: {exit_reason}",
        ]
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            session.log_path.write_text("\n".join(payload) + "\n", encoding="utf-8")
        except Exception as exc:
            self._log(f"写入日志文件失败: {exc}", "error")
            return
        session.log_saved = True

    def _reset_runtime_state(self):
        self.last_jam_key = "N/A"
        self.last_jam_level = "N/A"
        self.last_rx_mode = "N/A"
        self.jam_frame_count = 0
        self.info_frame_count = 0
        self.last_profile = "N/A"
        self.last_center_freq = "N/A"
        self.last_power = "N/A"
        self.last_status = "未运行"
        self.last_info_positions = {}
        self._update_display()

    def _build_command(self) -> list[str]:
        team = self.team_var.get()
        jam_ip = self.jam_ip_var.get().strip()
        server_ip = self.server_ip_var.get().strip()
        server_port = int(self.server_port_var.get().strip())
        runner: list[str]
        if shutil.which("conda"):
            runner = ["conda", "run", "--no-capture-output", "-n", "radio", "python3"]
        else:
            runner = [sys.executable]
        return runner + [
            "-u",
            "-m",
            "radar.CombatRadarSdr.apps.jam_rx_app",
            "--rx-ip",
            jam_ip,
            "--team",
            team,
            "--initial-level",
            str(self.INITIAL_LEVEL),
            "--server-ip",
            server_ip,
            "--server-port",
            str(server_port),
            "--parse-policy",
            self.PARSE_POLICY,
            "--payload-endian",
            self.PAYLOAD_ENDIAN,
            "--record-wave",
            "--record-tag",
            self.LAUNCHER_NAME,
        ]

    def _signal_process_group(self, process: subprocess.Popen[str], sig: int) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            else:
                process.send_signal(sig)
        except ProcessLookupError:
            pass

    def _start_receiver(self):
        try:
            int(self.server_port_var.get().strip())
        except ValueError:
            messagebox.showerror("错误", "服务器端口必须是整数")
            return

        self._reset_runtime_state()
        session = self._new_run_session()
        self.active_session = session
        self.ui_session_id = session.run_id
        cmd = self._build_command()

        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["PYTHONPATH"] = str(PYTHONPATH_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(PYTHONPATH_ROOT),
                env=env,
                start_new_session=True,
            )
            session.process = process
            self.running = True
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            self.status_label.config(text="运行中", foreground="green")
            self.last_status = "运行中"
            self._log("接收程序已启动", "success", session=session)
            self._log(f"阵营={self.team_var.get()} 接收器IP={self.jam_ip_var.get().strip()}", "info", session=session)
            self._log(
                f"服务器={self.server_ip_var.get().strip()}:{self.server_port_var.get().strip()}",
                "info",
                session=session,
            )
            self._log(f"本次运行日志将在结束后写入 {session.log_path}", "info", session=session)

            session.read_thread = threading.Thread(target=self._read_output_for_session, args=(session,), daemon=True)
            session.read_thread.start()
        except Exception as exc:
            self._log(f"启动失败: {exc}", "error", session=session)
            self.running = False
            self.active_session = None
            self.ui_session_id = None
            self._persist_log_session(session, "启动失败")

    def _stop_receiver(self):
        session = self.active_session
        if session is None:
            return

        session.stop_requested = True
        self.ui_session_id = None

        if session.process is not None:
            try:
                self._signal_process_group(session.process, signal.SIGTERM)
                session.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._signal_process_group(session.process, signal.SIGKILL)
                session.process.wait(timeout=5)
            except Exception as exc:
                self._log(f"停止失败: {exc}", "error")

        self.running = False
        if session.read_thread is not None and session.read_thread.is_alive():
            session.read_thread.join(timeout=2)
        if session.read_thread is not None and session.read_thread.is_alive():
            self._log("读取线程未及时退出，旧会话输出将被忽略", "warn")
            self._persist_log_session(session, "手动停止")
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.status_label.config(text="已停止", foreground="red")
        self.last_status = "已停止"
        self._update_display()

    def _handle_json_message(self, data: dict, session: RunSession):
        if session.run_id != self.ui_session_id:
            return
        kind = data.get("kind")
        if kind == "jam_started":
            self.last_jam_level = str(data.get("jam_level", "N/A"))
            self.last_rx_mode = str(data.get("rx_mode", self.last_rx_mode))
            self.last_profile = str(data.get("profile", "N/A"))
            freq = data.get("center_freq")
            self.last_center_freq = f"{freq} Hz" if freq is not None else "N/A"
            self._log(
                f"接收已启动: level={self.last_jam_level} mode={self.last_rx_mode} profile={self.last_profile} freq={self.last_center_freq}",
                "success",
            )
            record_path = data.get("record_path")
            if record_path:
                self._log(f"录波文件: {record_path}", "info")
        elif kind == "jam_level_change":
            self.last_jam_level = str(data.get("jam_level", "N/A"))
            self.last_rx_mode = str(data.get("rx_mode", self.last_rx_mode))
            self.last_profile = str(data.get("profile", "N/A"))
            freq = data.get("center_freq")
            self.last_center_freq = f"{freq} Hz" if freq is not None else "N/A"
            self._log(
                f"干扰等级切换到 {self.last_jam_level}，模式={self.last_rx_mode}，当前频段 {self.last_profile} @ {self.last_center_freq}",
                "jam",
            )
        elif kind == "jam_status":
            self.last_jam_level = str(data.get("jam_level", self.last_jam_level))
            self.last_rx_mode = str(data.get("rx_mode", self.last_rx_mode))
            self.jam_frame_count = int(data.get("jam_frame_count", self.jam_frame_count))
            self.info_frame_count = int(data.get("info_frame_count", self.info_frame_count))
            self.last_jam_key = str(data.get("last_key", self.last_jam_key))
            self.last_profile = str(data.get("profile", self.last_profile))
            positions = data.get("last_info_positions")
            if isinstance(positions, dict):
                self.last_info_positions = positions
            freq = data.get("center_freq")
            if freq is not None:
                self.last_center_freq = f"{freq} Hz"
            power = data.get("last_buffer_power_dbfs")
            self.last_power = f"{power} dBFS" if power is not None else "N/A"
        elif kind == "jam_frame":
            self.last_jam_level = str(data.get("jam_level", self.last_jam_level))
            self.last_rx_mode = str(data.get("rx_mode", self.last_rx_mode))
            self.last_jam_key = str(data.get("data_ascii", self.last_jam_key))
            self.jam_frame_count = int(data.get("jam_frame_count", self.jam_frame_count))
            self.last_profile = str(data.get("profile", self.last_profile))
            self._log(
                f"收到干扰波: level={self.last_jam_level} key={self.last_jam_key} seq={data.get('seq', 'N/A')}",
                "jam",
                show=self.show_realtime.get(),
            )
        elif kind == "info_frame":
            self.last_jam_level = str(data.get("jam_level", self.last_jam_level))
            self.last_rx_mode = str(data.get("rx_mode", self.last_rx_mode))
            self.info_frame_count = int(data.get("info_frame_count", self.info_frame_count))
            self.last_profile = str(data.get("profile", self.last_profile))
            decoded = data.get("decoded")
            if isinstance(decoded, dict):
                self.last_info_positions = decoded
            payload_endian = str(data.get("payload_endian", self.PAYLOAD_ENDIAN))
            self._log(
                f"收到信息波 0x0A01: endian={payload_endian} seq={data.get('seq', 'N/A')} hero={self._format_position(decoded, 'enemy_hero')}",
                "success",
                show=self.show_realtime.get(),
            )
        elif kind == "jam_error":
            message = str(data.get("message", "unknown jam error"))
            detail = data.get("detail")
            error_type = data.get("error_type")
            if detail:
                if error_type:
                    message = f"{message}: {error_type}: {detail}"
                else:
                    message = f"{message}: {detail}"
            tag = "warn" if "no jam packets" in message else "error"
            self._log(message, tag)
        elif kind == "jam_stopped":
            self._log("接收进程已退出", "info")
            record_path = data.get("record_path")
            if record_path:
                record_bytes = data.get("record_bytes")
                if record_bytes is not None:
                    self._log(f"录波完成: {record_path} ({record_bytes} bytes)", "info")
                else:
                    self._log(f"录波完成: {record_path}", "info")

        self.root.after(0, self._update_display)

    def _read_output_for_session(self, session: RunSession):
        try:
            process = session.process
            assert process is not None
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    self._log(line, "info", show=self.show_realtime.get(), session=session)
                    continue

                self._handle_json_message(data, session)

            rc = process.wait()
            if session.stop_requested and session.run_id == self.ui_session_id:
                self._log("接收程序已停止", "success", session=session)
            exit_reason = "手动停止" if session.stop_requested else ("已停止" if rc == 0 else f"异常退出({rc})")
            self._persist_log_session(session, exit_reason)
            if self.active_session is session:
                self.running = False
                self.active_session = None
                self.root.after(0, self.start_btn.config, {"state": tk.NORMAL})
                self.root.after(0, self.stop_btn.config, {"state": tk.DISABLED})
                if rc == 0:
                    self.last_status = "已停止"
                    self.root.after(0, self.status_label.config, {"text": "已停止", "foreground": "red"})
                else:
                    self.last_status = f"异常退出({rc})"
                    self.root.after(0, self.status_label.config, {"text": f"异常退出({rc})", "foreground": "red"})
            session.read_thread = None
        except Exception as exc:
            self._log(f"读取输出错误: {exc}", "error", session=session)
            self._persist_log_session(session, "读取输出错误")

    def _update_display(self):
        self.jam_key_label.config(text=self.last_jam_key)
        level_text = self.last_jam_level
        level_color = "gray"
        if self.last_jam_level == "1":
            level_text = "1 级"
            level_color = "green"
        elif self.last_jam_level == "2":
            level_text = "2 级"
            level_color = "orange"
        elif self.last_jam_level == "3":
            level_text = "3 级"
            level_color = "red"
        self.jam_level_label.config(text=level_text, foreground=level_color)
        if self.last_rx_mode == "jam":
            rx_mode_text = "干扰波"
            rx_mode_color = "darkgreen"
        elif self.last_rx_mode == "info":
            rx_mode_text = "信息波"
            rx_mode_color = "darkblue"
        else:
            rx_mode_text = self.last_rx_mode
            rx_mode_color = "gray"
        self.rx_mode_label.config(text=rx_mode_text, foreground=rx_mode_color)
        self.jam_count_label.config(text=str(self.jam_frame_count))
        self.info_count_label.config(text=str(self.info_frame_count))
        self.profile_label.config(text=self.last_profile)
        self.freq_label.config(text=self.last_center_freq)
        self.power_label.config(text=self.last_power)
        for field_name, _title in self.INFO_POSITION_NAMES:
            self.info_position_labels[field_name].config(text=self._format_position(self.last_info_positions, field_name))

    def _format_position(self, decoded: object, field_name: str) -> str:
        if not isinstance(decoded, dict):
            return "N/A"
        entry = decoded.get(field_name)
        if not isinstance(entry, dict):
            return "N/A"
        x_value = entry.get("x")
        y_value = entry.get("y")
        if x_value is None or y_value is None:
            return "N/A"
        return f"({x_value}, {y_value}) cm"

    def on_closing(self):
        if self.running:
            if messagebox.askokcancel("确认", "接收程序仍在运行，确定关闭？"):
                session = self.active_session
                self._stop_receiver()
                if session is not None:
                    self._persist_log_session(session, "GUI关闭")
                self.root.destroy()
        else:
            if self.active_session is not None:
                self._persist_log_session(self.active_session, "GUI关闭")
            self.root.destroy()


def main():
    root = tk.Tk()
    gui = JamRxGUI(root)
    root.protocol("WM_DELETE_WINDOW", gui.on_closing)
    try:
        root.mainloop()
    finally:
        if gui.active_session is not None:
            gui._persist_log_session(gui.active_session, "GUI退出")


if __name__ == "__main__":
    main()
