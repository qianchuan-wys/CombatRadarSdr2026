#!/usr/bin/env python3
"""
RM2026 信息波接收 GUI

与 gui_launcher 保持一致，但启动后直接按信息波模式解析，
不再解析干扰波密钥；信息波 payload 固定按小端序解析。
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


class JamRxInfoOnlyGUI(JamRxGUI):
    LAUNCHER_NAME = "gui_launcher_info"
    WINDOW_TITLE = "RM2026 信息波接收系统"
    INITIAL_LEVEL = 3
    PARSE_POLICY = "info_only"
    PAYLOAD_ENDIAN = "little"


def main():
    root = tk.Tk()
    gui = JamRxInfoOnlyGUI(root)
    root.protocol("WM_DELETE_WINDOW", gui.on_closing)
    try:
        root.mainloop()
    finally:
        if gui.active_session is not None:
            gui._persist_log_session(gui.active_session, "GUI退出")


if __name__ == "__main__":
    main()
