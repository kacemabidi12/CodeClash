"""
Client side of the persistent TCP connection (AF_INET, SOCK_STREAM).

* connect() opens ONE connection to the server's LAN IP and keeps it for the whole session.
* send() frames a message as one JSON line; a lock makes concurrent senders safe
  (the Streamlit script thread and the heartbeat thread both send).
* A dedicated receiver thread (`net-tcp-receiver`) reads the byte stream, reassembles complete
  messages with LineFramer (one recv() is NOT one message) and hands each one to `on_message`.
* When the connection ends for any reason, `on_disconnect(reason)` is called exactly once.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Callable

from common import protocol as P

log = logging.getLogger("codeclash.client.tcp")


class TcpClient:
    def __init__(self, on_message: Callable[[dict], None], on_disconnect: Callable[[str], None]):
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closing = False
        self._close_reason = "disconnected by you"
        self._notified = False
        self.local_addr: tuple[str, int] | None = None

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closing and not self._notified

    def connect(self, host: str, port: int, timeout: float) -> None:
        """Open the TCP connection. Raises OSError (refused, timeout, unreachable...)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)        # IPv4 + TCP
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        except OSError:
            sock.close()
            raise
        sock.settimeout(None)                                           # receiver blocks in recv()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)     # small messages leave immediately
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._sock = sock
        self.local_addr = sock.getsockname()
        self._thread = threading.Thread(target=self._receive_loop, name="net-tcp-receiver", daemon=True)
        self._thread.start()
        log.info("TCP connected to %s:%d from %s:%d", host, port, *self.local_addr)

    def send(self, msg: dict) -> None:
        """Send one framed message. Raises OSError if the connection is gone."""
        data = P.encode_tcp(msg)
        sock = self._sock
        if sock is None or self._closing:
            raise ConnectionError("not connected")
        with self._send_lock:                       # one complete message at a time on the stream
            sock.sendall(data)

    def close(self, reason: str = "disconnected by you") -> None:
        """Close cleanly. shutdown() first: a bare close() would not wake the blocked recv() on Linux."""
        if not self._closing:
            self._close_reason = reason
        self._closing = True
        sock = self._sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        try:
            sock.close()
        except OSError:
            pass

    def _receive_loop(self) -> None:
        framer = P.LineFramer()
        reason = "server closed the connection"
        try:
            while True:
                chunk = self._sock.recv(P.TCP_RECV_BUFFER)
                if not chunk:
                    break
                for line in framer.feed(chunk):
                    try:
                        msg = P.decode_line(line)
                    except P.ProtocolError as exc:
                        log.warning("Ignoring malformed message from server: %s", exc)
                        continue
                    try:
                        self._on_message(msg)
                    except Exception:  # noqa: BLE001 - a UI-state bug must not kill the connection
                        log.exception("Error while handling %s", msg.get("type"))
        except P.FrameTooLarge as exc:
            reason = str(exc)
        except ConnectionResetError:
            reason = "connection reset by the server"
        except OSError as exc:
            reason = "connection closed" if self._closing else f"network error ({exc})"
        finally:
            if self._closing:
                reason = self._close_reason
            if not self._notified:
                self._notified = True
                self._on_disconnect(reason)
