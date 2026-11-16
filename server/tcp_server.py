"""
TCP service: one listening socket (AF_INET, SOCK_STREAM) on 0.0.0.0:8888 and ONE accept-loop
thread. Every accepted connection becomes a PlayerSession with its own handler thread, so the
accept loop never blocks on a player and one player never blocks another.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import TYPE_CHECKING

import config
from server.player_handler import PlayerSession

if TYPE_CHECKING:
    from server.main_server import CodeClashServer

log = logging.getLogger("codeclash.tcp")


class TCPServer:
    def __init__(self, server: "CodeClashServer", host: str, port: int):
        self.server = server
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.accepted = 0

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)          # IPv4 + TCP
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)         # quick restart after Ctrl+C
        sock.bind((self.host, self.port))
        sock.listen(16)
        sock.settimeout(config.SOCKET_POLL_TIMEOUT_S)                      # accept() wakes up to check shutdown
        self.sock = sock
        self.thread = threading.Thread(target=self._accept_loop, name="tcp-accept", daemon=True)
        self.thread.start()
        log.info("TCP server listening on %s:%d", self.host, self.port)

    def _accept_loop(self) -> None:
        state = self.server.state
        while not state.shutting_down.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if state.shutting_down.is_set():
                    break
                log.error("accept() failed: %s", exc)
                continue
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # send small JSON lines immediately
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                conn.settimeout(config.SOCKET_POLL_TIMEOUT_S)                # recv() polls so threads can exit
                session = PlayerSession(conn, addr, self.server)
                with state.lock:
                    state.all_sessions.add(session)
                self.accepted += 1
                session.start()          # starts player-handler-N and player-sender-N
            except Exception:  # noqa: BLE001
                log.exception("Could not start a session for %s:%d", *addr)
                conn.close()
        log.info("TCP accept loop stopped")

    def stop(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=2.0)
