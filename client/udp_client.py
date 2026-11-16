"""
Client side of UDP (AF_INET, SOCK_DGRAM), running alongside the TCP connection.

* One UDP socket bound to an OS-chosen local port (config.CLIENT_UDP_PORT = 0 by default).
* The client talks first: UDP_HELLO (player_id + token from LOGIN) right after login and then
  every few seconds. The server answers to the address it sees, so the countdown comes back
  through the Windows firewall as a reply, and the NAT/firewall mapping stays open.
* A dedicated receiver thread (`net-udp-receiver`) accepts datagrams ONLY from the server's IP
  and hands them to `on_datagram`. Lost or reordered datagrams are fine: each tick carries a
  sequence number and the next tick replaces the previous one.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Callable

from common import protocol as P

log = logging.getLogger("codeclash.client.udp")


class UdpClient:
    def __init__(self, on_datagram: Callable[[dict], None]):
        self._on_datagram = on_datagram
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._server: tuple[str, int] | None = None
        self.local_port: int | None = None
        self.sent = 0
        self.received = 0

    def open(self, server_host: str, server_port: int, local_port: int = 0) -> None:
        server_ip = socket.gethostbyname(server_host)        # compare source IPs, not hostnames
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)               # IPv4 + UDP
        sock.bind(("0.0.0.0", local_port))
        sock.settimeout(0.5)                                  # lets the thread notice close()
        self._sock = sock
        self._server = (server_ip, server_port)
        self.local_port = sock.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._receive_loop, name="net-udp-receiver", daemon=True)
        self._thread.start()
        log.info("UDP socket bound to local port %d, server %s:%d", self.local_port, server_ip, server_port)

    def set_server_port(self, port: int) -> None:
        if self._server is not None:
            self._server = (self._server[0], port)

    def send(self, msg: dict) -> bool:
        if self._sock is None or self._server is None:
            return False
        try:
            self._sock.sendto(P.encode_udp(msg), self._server)
            self.sent += 1
            return True
        except OSError as exc:
            log.warning("UDP send failed: %s", exc)
            return False

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            try:
                payload, addr = self._sock.recvfrom(P.UDP_RECV_BUFFER)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue          # Windows reports an ICMP "port unreachable" this way; harmless
            except OSError:
                break
            if self._server is None or addr[0] != self._server[0]:
                continue          # only the game server may send us datagrams
            try:
                msg = P.decode_udp(payload)
            except P.ProtocolError:
                continue
            self.received += 1
            try:
                self._on_datagram(msg)
            except Exception:  # noqa: BLE001
                log.exception("Error while handling UDP %s", msg.get("type"))
