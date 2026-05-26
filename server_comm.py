"""
TCP Server Communication Module for RM2026 Radar System

Handles bidirectional communication with the radar main program server:
- Receives jam level control packets (interference level 1-3)
- Sends radar wireless link protocol frames (0x0A01-0x0A06)
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Optional, Callable
import logging

from .protocol import CMD_0A01, CMD_0A02, CMD_0A03, CMD_0A04, CMD_0A05, CMD_0A06, build_referee_frame


logger = logging.getLogger(__name__)


class RadarServerComm:
    """Manages TCP connection to radar main program server (127.0.0.1:5000)
    
    Protocol:
    - RX from server (jam level control): 0xFF + level(1-3) + 0xFE (3 bytes)
    - TX to server (radar wireless): official frames 0xA5 + len + seq + crc8 + cmd + data + crc16
    """
    
    def __init__(
        self,
        server_ip: str = "127.0.0.1",
        server_port: int = 5000,
        on_jam_level_change: Optional[Callable[[int], None]] = None,
    ):
        """
        Initialize server communication.
        
        Args:
            server_ip: Radar server IP address
            server_port: Radar server TCP port
            on_jam_level_change: Callback when jam level changes (called with new level 1-3)
        """
        self.server_ip = server_ip
        self.server_port = server_port
        self.on_jam_level_change = on_jam_level_change
        
        self.sock: Optional[socket.socket] = None
        self.connected = False
        self.running = False
        
        # RX thread for receiving jam level commands
        self.rx_thread: Optional[threading.Thread] = None
        
        # Latest frame payloads keyed by cmd id; each process only updates the commands it owns.
        self.cmd_frames: dict[int, bytes] = {}
        self.seq_by_cmd: dict[int, int] = {
            CMD_0A01: 0,
            CMD_0A02: 0,
            CMD_0A03: 0,
            CMD_0A04: 0,
            CMD_0A05: 0,
            CMD_0A06: 0,
        }
        self.cmd_data_lock = threading.Lock()
        
        # Current jam level
        self.jam_level = 1  # Default to level 1
        self.jam_level_lock = threading.Lock()
        
        # Stats
        self.stats = {
            "packets_sent": 0,
            "packets_recv": 0,
            "jam_level_changes": 0,
            "send_errors": 0,
            "recv_errors": 0,
        }
        self.stats_lock = threading.Lock()
    
    def connect(self) -> bool:
        """Connect to radar server. Returns True if successful."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10.0)
            self.sock.connect((self.server_ip, self.server_port))
            self.connected = True
            logger.info(f"✓ Connected to radar server {self.server_ip}:{self.server_port}")
            return True
        except Exception as e:
            logger.error(f"✗ Failed to connect to server: {e}")
            self.connected = False
            if self.sock:
                self.sock.close()
                self.sock = None
            return False
    
    def start(self) -> None:
        """Start RX thread to listen for jam level commands."""
        if not self.connected:
            logger.error("Not connected to server")
            return
        
        self.running = True
        self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self.rx_thread.start()
        logger.info("✓ RX thread started")
    
    def stop(self) -> None:
        """Stop communication and close connection."""
        self.running = False
        if self.rx_thread:
            self.rx_thread.join(timeout=2.0)
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        self.connected = False
        logger.info("✓ Server communication stopped")
    
    def _rx_loop(self) -> None:
        """RX thread: Listen for jam level commands from server."""
        rx_buffer = bytearray()
        while self.running and self.connected:
            try:
                data = self.sock.recv(1024)
                if not data:
                    logger.warning("Server closed connection")
                    self.connected = False
                    break

                rx_buffer.extend(data)
                while True:
                    start = rx_buffer.find(b"\xFF")
                    if start < 0:
                        rx_buffer.clear()
                        break
                    if len(rx_buffer) - start < 3:
                        if start > 0:
                            del rx_buffer[:start]
                        break

                    candidate = rx_buffer[start:start + 3]
                    if candidate[2] == 0xFE and 1 <= candidate[1] <= 3:
                        self._handle_jam_level(candidate[1])
                        with self.stats_lock:
                            self.stats["packets_recv"] += 1
                        del rx_buffer[:start + 3]
                    else:
                        del rx_buffer[:start + 1]
            except socket.timeout:
                continue
            except Exception as e:
                logger.error(f"RX error: {e}")
                with self.stats_lock:
                    self.stats["recv_errors"] += 1
                break
    
    def _handle_jam_level(self, level: int) -> None:
        """Process jam level command."""
        with self.jam_level_lock:
            old_level = self.jam_level
            self.jam_level = level
        
        if level != old_level:
            logger.info(f"Jam level changed: {old_level} → {level}")
            with self.stats_lock:
                self.stats["jam_level_changes"] += 1
            if self.on_jam_level_change:
                self.on_jam_level_change(level)
    
    def get_jam_level(self) -> int:
        """Get current jam level."""
        with self.jam_level_lock:
            return self.jam_level
    
    def update_command_data(
        self,
        cmd_0a01: Optional[bytes] = None,
        cmd_0a02: Optional[bytes] = None,
        cmd_0a03: Optional[bytes] = None,
        cmd_0a04: Optional[bytes] = None,
        cmd_0a05: Optional[bytes] = None,
        cmd_0a06: Optional[bytes] = None,
    ) -> None:
        """
        Update command payloads from demodulated frames.
        
        New data overwrites old data for that cmd id only; missing data leaves previous values unchanged.
        
        Args:
            cmd_0a01: 24 bytes or None
            cmd_0a02: 12 bytes or None
            cmd_0a03: 10 bytes or None
            cmd_0a04: 8 bytes or None
            cmd_0a05: 36 bytes or None
            cmd_0a06: 6 bytes (ASCII) or None
        """
        with self.cmd_data_lock:
            if cmd_0a01 and len(cmd_0a01) == 24:
                self.cmd_frames[CMD_0A01] = bytes(cmd_0a01)
            if cmd_0a02 and len(cmd_0a02) == 12:
                self.cmd_frames[CMD_0A02] = bytes(cmd_0a02)
            if cmd_0a03 and len(cmd_0a03) == 10:
                self.cmd_frames[CMD_0A03] = bytes(cmd_0a03)
            if cmd_0a04 and len(cmd_0a04) == 8:
                self.cmd_frames[CMD_0A04] = bytes(cmd_0a04)
            if cmd_0a05 and len(cmd_0a05) == 36:
                self.cmd_frames[CMD_0A05] = bytes(cmd_0a05)
            if cmd_0a06 and len(cmd_0a06) == 6:
                self.cmd_frames[CMD_0A06] = bytes(cmd_0a06)

    def send_command_data(self, cmd_id: int, data: bytes) -> bool:
        """
        Immediately send one decoded radar command frame to the server.

        The server side listens on loopback TCP and expects the standard
        referee frame format, so the decoded payload is re-packed here.
        """
        if not self.connected or not self.sock:
            return False

        expected_len = {
            CMD_0A01: 24,
            CMD_0A02: 12,
            CMD_0A03: 10,
            CMD_0A04: 8,
            CMD_0A05: 36,
            CMD_0A06: 6,
        }.get(cmd_id)
        if expected_len is None or len(data) != expected_len:
            logger.error(f"Invalid command payload: cmd=0x{cmd_id:04X} len={len(data)}")
            return False

        try:
            with self.cmd_data_lock:
                seq = self.seq_by_cmd[cmd_id]
                self.seq_by_cmd[cmd_id] = (seq + 1) & 0xFF

            frame = build_referee_frame(cmd_id, bytes(data), seq)
            self.sock.sendall(frame)
            with self.stats_lock:
                self.stats["packets_sent"] += 1
            return True
        except Exception as e:
            logger.error(f"Send error: {e}")
            self.connected = False
            with self.stats_lock:
                self.stats["send_errors"] += 1
            return False

    def send_jam_key(self, key: bytes) -> bool:
        """Immediately send one decoded 0x0A06 key frame to the server."""
        return self.send_command_data(CMD_0A06, key)
    
    def send_info_data(self) -> bool:
        """
        Send all currently available radar wireless protocol frames to server.
        """
        if not self.connected or not self.sock:
            return False
        
        try:
            with self.cmd_data_lock:
                frames_to_send = []
                for cmd_id in (CMD_0A01, CMD_0A02, CMD_0A03, CMD_0A04, CMD_0A05, CMD_0A06):
                    payload = self.cmd_frames.get(cmd_id)
                    if payload is None:
                        continue
                    seq = self.seq_by_cmd[cmd_id]
                    frames_to_send.append(build_referee_frame(cmd_id, payload, seq))
                    self.seq_by_cmd[cmd_id] = (seq + 1) & 0xFF

            if not frames_to_send:
                return True

            sent_count = 0
            for frame in frames_to_send:
                self.sock.sendall(frame)
                sent_count += 1
            with self.stats_lock:
                self.stats["packets_sent"] += sent_count
            return True
        except Exception as e:
            logger.error(f"Send error: {e}")
            self.connected = False
            with self.stats_lock:
                self.stats["send_errors"] += 1
            return False
    
    def get_stats(self) -> dict:
        """Get communication statistics."""
        with self.stats_lock:
            return self.stats.copy()
    
    def print_status(self) -> None:
        """Print connection status and statistics."""
        jam_level = self.get_jam_level()
        stats = self.get_stats()
        
        print("=" * 60)
        print("Server Communication Status")
        print("=" * 60)
        print(f"Server Address : {self.server_ip}:{self.server_port}")
        print(f"Connected      : {'Yes' if self.connected else 'No'}")
        print(f"Running        : {'Yes' if self.running else 'No'}")
        print(f"Current Jam Lvl: {jam_level}")
        print("-" * 60)
        print(f"Packets Sent   : {stats['packets_sent']}")
        print(f"Packets Recv   : {stats['packets_recv']}")
        print(f"Jam Lvl Changes: {stats['jam_level_changes']}")
        print(f"Send Errors    : {stats['send_errors']}")
        print(f"Recv Errors    : {stats['recv_errors']}")
        print("=" * 60)
