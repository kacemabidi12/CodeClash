"""
UDP service: one socket (AF_INET, SOCK_DGRAM) on 0.0.0.0:8888, running at the same time as TCP.

* `udp-receiver` thread: recvfrom() loop. A UDP_HELLO with a valid (player_id, token) pair
  registers the SOURCE ADDRESS seen by recvfrom() as that player's UDP endpoint, and is answered
  with UDP_ACK. Replying to the observed address works through the client's firewall (it is a
  reply to traffic the client sent) and through NAT later on.
* `send()` is called by game threads (TIMER_TICK) and handler threads (LIVE_NOTICE). Each
  sendto() is one independent datagram, so concurrent calls from several threads are safe.
  It is never called while holding state.lock.

UDP carries only non-critical, time-sensitive data. Answers and scores always go over TCP.
"""

from __future__ import annotations

import errno
import hmac
import logging
import socket
import sys
import threading
import time

import config
from common import protocol as P
from server.server_state import ServerState

log = logging.getLogger("codeclash.udp")

_EHOSTUNREACH = {errno.EHOSTUNREACH, 65}


class UDPServer:
    def __init__(self, state: ServerState, host: str, port: int):
        self.state = state
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self._stats_lock = threading.Lock()
        self.packets_in = 0
        self.packets_out = 0
        self.rejected = 0
        self.send_errors = 0
        self._mac_hint_shown = False

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)           # IPv4 + UDP
        sock.bind((self.host, self.port))
        sock.settimeout(config.SOCKET_POLL_TIMEOUT_S)
        self.sock = sock
        self.thread = threading.Thread(target=self._receive_loop, name="udp-receiver", daemon=True)
        self.thread.start()
        log.info("UDP server listening on %s:%d", self.host, self.port)

    # ------------------------------------------------------------------ receiving
    def _receive_loop(self) -> None:
        while not self.state.shutting_down.is_set():
            try:
                payload, addr = self.sock.recvfrom(P.UDP_RECV_BUFFER)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue                    # ICMP "port unreachable" echo on some platforms: harmless
            except OSError as exc:
                if self.state.shutting_down.is_set():
                    break
                log.error("recvfrom() failed: %s", exc)
                time.sleep(0.1)
                continue
            with self._stats_lock:
                self.packets_in += 1
            try:
                msg = P.decode_udp(payload)
            except P.ProtocolError:
                self._reject(addr, "malformed datagram")
                continue
            if msg.get("type") == P.UDP_HELLO:
                self._handle_hello(msg, addr)
            else:
                self._reject(addr, f"unexpected type {msg.get('type')!r}")
        log.info("UDP receiver stopped")

    def _handle_hello(self, msg: dict, addr: tuple[str, int]) -> None:
        player_id, token, seq = msg.get("player_id"), msg.get("token"), msg.get("seq")
        if not isinstance(player_id, str) or not isinstance(token, str):
            self._reject(addr, "HELLO without player_id/token")
            return
        with self.state.lock:
            session = self.state.sessions_by_player.get(player_id)
            valid = (session is not None and session.udp_token is not None
                     and hmac.compare_digest(session.udp_token, token))
            if valid:
                first = session.udp_addr != addr
                session.udp_addr = addr                  # the address we SAW, not one the client claims
                session.udp_last_seen = time.monotonic()
                username = session.username
        if not valid:
            self._reject(addr, f"invalid token for {player_id}")
            return
        if first:
            log.info("UDP endpoint of %s registered: %s:%d", username, addr[0], addr[1])
        self.send([addr], P.make_udp(P.UDP_ACK, seq=seq, server_time=round(time.time(), 3)))

    def _reject(self, addr: tuple[str, int], why: str) -> None:
        with self._stats_lock:
            self.rejected += 1
        log.debug("Ignored UDP datagram from %s:%d (%s)", addr[0], addr[1], why)

    # ------------------------------------------------------------------ sending
    def send(self, addrs, msg: dict) -> int:
        """Send one datagram to each address. Returns how many were sent. Never raises."""
        if not addrs or self.sock is None:
            return 0
        try:
            payload = P.encode_udp(msg)
        except P.ProtocolError as exc:
            log.error("UDP message not sent: %s", exc)
            return 0
        sent = 0
        for addr in addrs:
            try:
                self.sock.sendto(payload, addr)
                sent += 1
            except OSError as exc:
                with self._stats_lock:
                    self.send_errors += 1
                log.warning("UDP send to %s:%d failed: %s", addr[0], addr[1], exc)
                if (getattr(exc, "errno", None) in _EHOSTUNREACH and sys.platform == "darwin"
                        and not self._mac_hint_shown):
                    self._mac_hint_shown = True
                    log.error("macOS blocked a LAN send ('No route to host'). If the server runs inside VS Code, "
                              "allow VS Code in System Settings > Privacy & Security > Local Network, "
                              "or start the server from Terminal.app.")
        with self._stats_lock:
            self.packets_out += sent
        return sent

    def stats(self) -> dict:
        with self._stats_lock:
            return {"in": self.packets_in, "out": self.packets_out, "rejected": self.rejected,
                    "send_errors": self.send_errors}

    def stop(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=2.0)
