#!/usr/bin/env python3
"""
RM2026 一级干扰波密钥接收 GUI

与 gui_launcher 保持一致，但仅在一级阶段解析干扰波密钥；
干扰波等级升到二级后切换为信息波解析，不再继续解析干扰波。
信息波 payload 固定按小端序解析。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

try:
    from .gui_launcher import JamRxGUI
except ImportError:
    this_dir = Path(__file__).resolve().parent
    if str(this_dir) not in sys.path:
        sys.path.insert(0, str(this_dir))
    from gui_launcher import JamRxGUI


class JamRxOneKeyGUI(JamRxGUI):
    LAUNCHER_NAME = "gui_launcher_onekey"
    WINDOW_TITLE = "RM2026 一级干扰波密钥接收系统"
    PARSE_POLICY = "onekey_then_info"
    PAYLOAD_ENDIAN = "little"


def main():
    root = tk.Tk()
    gui = JamRxOneKeyGUI(root)
    root.protocol("WM_DELETE_WINDOW", gui.on_closing)
    try:
        root.mainloop()
    finally:
        if gui.active_session is not None:
            gui._persist_log_session(gui.active_session, "GUI退出")


if __name__ == "__main__":
    main()
