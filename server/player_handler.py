"""
One PlayerSession per accepted TCP connection, served by two dedicated threads:

  player-handler-<name>   THE per-player handler thread required by the brief.
                          recv() -> LineFramer -> parse -> dispatch to managers -> RESPONSE.
                          Detects disconnection (b"", reset, heartbeat timeout) and cleans up.
  player-sender-<name>    The only thread that writes to this socket. It drains the session's
                          outbox (queue.Queue) with sendall(). Any thread can call
                          session.send(msg): it is a non-blocking queue put, so a slow or frozen
                          client can never block the game thread or other players.

The handler thread owns the socket's lifetime: only it calls sock.close(), after the sender
thread has finished. The sender only calls shutdown(), which wakes a blocked recv().
"""

from __future__ import annotations

import itertools
import logging
import queue
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

import config
from common import protocol as P
from common.protocol import ErrorCode, PlayerStatus
from server.server_state import RequestError

if TYPE_CHECKING:
    from server.main_server import CodeClashServer

log = logging.getLogger("codeclash.player")

_STOP = object()                        # sentinel that tells the sender thread to finish
_session_ids = itertools.count(1)

Handler = Callable[["PlayerSession", dict, float], dict]


class PlayerSession:
    def __init__(self, sock: socket.socket, addr: tuple[str, int], server: "CodeClashServer"):
        self.sid = next(_session_ids)
        self.sock = sock
        self.addr = addr
        self.server = server
        self.outbox: "queue.Queue[Any]" = queue.Queue()

        # Set by LOGIN (under state.lock), cleared by LOGOUT.
        self.player_id: str | None = None
        self.username: str | None = None
        self.udp_token: str | None = None
        # Set by the UDP receiver when a valid UDP_HELLO arrives (under state.lock).
        self.udp_addr: tuple[str, int] | None = None
        self.udp_last_seen: float | None = None

        self.connected_at = time.time()
        self.last_seen = time.monotonic()        # any TCP bytes received (heartbeat)
        self.closed = threading.Event()
        self.close_reason: str | None = None
        self.messages_in = 0
        self.messages_out = 0

        self._cleaned = False
        self._holding = False                    # see _process_line: RESPONSE goes before own pushes
        self._held: list[dict] = []

        self.handler_thread = threading.Thread(target=self._handler_loop, name=f"player-handler-{self.sid}",
                                               daemon=True)
        self.sender_thread = threading.Thread(target=self._sender_loop, name=f"player-sender-{self.sid}",
                                              daemon=True)

    # ------------------------------------------------------------------ public API (any thread)
    @property
    def label(self) -> str:
        who = self.username or f"guest#{self.sid}"
        return f"{who}@{self.addr[0]}:{self.addr[1]}"

    def start(self) -> None:
        self.sender_thread.start()
        self.handler_thread.start()

    def send(self, msg: dict) -> None:
        """Queue a message for this client. Never blocks; safe while holding state.lock."""
        if self.closed.is_set():
            return
        if self._holding and threading.current_thread() is self.handler_thread:
            self._held.append(msg)
            return
        self.outbox.put(msg)

    def close(self, reason: str) -> None:
        """Ask both threads to finish. Messages already queued (e.g. KICKED) are still sent first."""
        if self.closed.is_set():
            return
        self.close_reason = reason
        self.closed.set()
        self.outbox.put(_STOP)

    def rename_threads(self) -> None:
        name = self.username or str(self.sid)
        self.handler_thread.name = f"player-handler-{name}"
        self.sender_thread.name = f"player-sender-{name}"

    # ------------------------------------------------------------------ sender thread
    def _sender_loop(self) -> None:
        try:
            while True:
                msg = self.outbox.get()
                if msg is _STOP:
                    break
                try:
                    data = P.encode_tcp(msg)
                except P.ProtocolError as exc:
                    log.error("Dropping unencodable message to %s: %s", self.label, exc)
                    continue
                self.sock.sendall(data)          # blocks only THIS thread (timeout: SOCKET_POLL_TIMEOUT_S)
                self.messages_out += 1
        except OSError as exc:
            if not self.closed.is_set():
                log.warning("Sending to %s failed: %s", self.label, exc)
                self.close(f"send failed ({exc})")
        finally:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)   # wakes the handler's recv()
            except OSError:
                pass

    # ------------------------------------------------------------------ handler thread
    def _handler_loop(self) -> None:
        reason = "connection closed by client"
        log.info("TCP connection from %s:%d -> handler thread %s started (open sessions: %d)",
                 self.addr[0], self.addr[1], self.handler_thread.name, self._open_sessions())
        self.send(P.make_push(P.WELCOME, {
            "server_name": "CodeClash",
            "protocol_version": P.PROTOCOL_VERSION,
            "udp_port": config.UDP_PORT,
            "heartbeat_interval_s": config.HEARTBEAT_INTERVAL_S,
            "heartbeat_timeout_s": config.HEARTBEAT_TIMEOUT_S,
            "room_capacity": config.ROOM_CAPACITY,
            "your_addr": f"{self.addr[0]}:{self.addr[1]}",
        }))
        framer = P.LineFramer()
        try:
            while not self.closed.is_set():
                try:
                    chunk = self.sock.recv(P.TCP_RECV_BUFFER)
                except socket.timeout:
                    if time.monotonic() - self.last_seen > config.HEARTBEAT_TIMEOUT_S:
                        reason = f"no message for {config.HEARTBEAT_TIMEOUT_S:g} s (heartbeat timeout)"
                        break
                    continue
                if not chunk:
                    reason = self.close_reason or "connection closed by client"
                    break
                self.last_seen = time.monotonic()
                # One recv() may hold part of a message, one message, or several: framing decides.
                for line in framer.feed(chunk):
                    self._process_line(line, received_at=time.monotonic())
                    if self.closed.is_set():
                        break
            else:
                reason = self.close_reason or reason
        except P.FrameTooLarge as exc:
            reason = str(exc)
        except ConnectionResetError:
            reason = "connection reset by peer (unexpected disconnect)"
        except OSError as exc:
            reason = self.close_reason or f"socket error ({exc})"
        except Exception:  # noqa: BLE001 - never let one client's bug kill the server
            log.exception("Handler for %s crashed", self.label)
            reason = "internal error"
        finally:
            self._cleanup(reason)

    def _process_line(self, line: bytes, received_at: float) -> None:
        self.messages_in += 1
        try:
            msg = P.decode_line(line)
        except P.ProtocolError as exc:
            log.warning("Malformed message from %s: %s", self.label, exc)
            self.send(P.make_push(P.ERROR, {"code": exc.code, "message": str(exc)}))
            return
        raw_req_id = msg.get("req_id") if isinstance(msg.get("req_id"), str) else None
        try:
            msg_type, req_id, data = P.parse_request(msg)
        except P.ProtocolError as exc:
            if raw_req_id:
                self.send(P.make_error_response(str(msg.get("type")), raw_req_id, exc.code, str(exc)))
            else:
                self.send(P.make_push(P.ERROR, {"code": exc.code, "message": str(exc)}))
            return

        if msg_type not in P.UNAUTHENTICATED_REQUESTS and self.player_id is None:
            self.send(P.make_error_response(msg_type, req_id, ErrorCode.NOT_AUTHENTICATED, "Log in first."))
            return

        handler: Handler = self.server.dispatch[msg_type]
        # Pushes this thread generates while handling the request (e.g. ROOM_UPDATE) are held back
        # so the client always receives the RESPONSE first, then the resulting pushes.
        self._holding = True
        try:
            try:
                result = handler(self, data, received_at)
                response = P.make_response(msg_type, req_id, ok=True, data=result)
            except RequestError as exc:
                log.info("%s: %s refused (%s: %s)", self.label, msg_type, exc.code, exc.message)
                response = P.make_error_response(msg_type, req_id, exc.code, exc.message)
            except Exception:  # noqa: BLE001
                log.exception("Error while handling %s from %s", msg_type, self.label)
                response = P.make_error_response(msg_type, req_id, ErrorCode.INTERNAL_ERROR, "Server error.")
        finally:
            self._holding = False
            held, self._held = self._held, []
        self.send(response)
        for pushed in held:
            self.send(pushed)
        if msg_type != P.PING:
            log.debug("%s -> %s handled", self.label, msg_type)

    def _cleanup(self, reason: str) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        state = self.server.state
        pid, username = self.player_id, self.username
        with state.lock:
            state.all_sessions.discard(self)
            # Only the CURRENT session of an account may change its state (a newer login may exist).
            was_current = pid is not None and state.sessions_by_player.get(pid) is self
            if was_current:
                del state.sessions_by_player[pid]
                self.server.rooms.on_player_gone_locked(pid, username, PlayerStatus.DISCONNECTED)
            open_sessions = len(state.all_sessions)
        self.close(reason)
        self.sender_thread.join(timeout=2.0)
        try:
            self.sock.close()
        except OSError:
            pass
        if was_current:
            log.warning("Player %s disconnected: %s", self.label, reason)
        log.info("TCP session %s closed (%s). Open TCP sessions: %d", self.label, reason, open_sessions)

    def _open_sessions(self) -> int:
        with self.server.state.lock:
            return len(self.server.state.all_sessions)


# ============================================================================ account requests

def handle_register(session: PlayerSession, data: dict, received_at: float) -> dict:
    player_id, username = session.server.auth.register(data.get("username"), data.get("password"))
    return {"player_id": player_id, "username": username}


def handle_login(session: PlayerSession, data: dict, received_at: float) -> dict:
    server = session.server
    if session.player_id is not None:
        raise RequestError(ErrorCode.ALREADY_AUTHENTICATED, "This connection is already logged in.")
    player_id, username = server.auth.verify(data.get("username"), data.get("password"))  # slow: no lock held
    with server.state.lock:
        old = server.state.sessions_by_player.get(player_id)
        session.player_id, session.username = player_id, username
        session.udp_token = P.new_id()           # proves future UDP_HELLOs come from this player
        session.udp_addr = None
        server.state.sessions_by_player[player_id] = session
        if old is not None and old is not session:
            # Same account logged in elsewhere: the newest connection wins.
            old.send(P.make_push(P.KICKED, {"reason": "Your account logged in from another connection."}))
            old.close("replaced by a newer login of the same account")
        room = server.rooms.on_login_locked(session)
        server.rooms.broadcast_lobby_locked()
    session.rename_threads()
    log.info("%s logged in as %s (%s)%s", session.label, username, player_id,
             " — replaced an older session" if old is not None and old is not session else "")
    return {"player_id": player_id, "username": username, "udp_token": session.udp_token,
            "udp_port": config.UDP_PORT, "room": room, "server_time": time.time()}


def handle_logout(session: PlayerSession, data: dict, received_at: float) -> dict:
    server = session.server
    pid, username = session.player_id, session.username
    with server.state.lock:
        if server.state.sessions_by_player.get(pid) is session:
            del server.state.sessions_by_player[pid]
            server.rooms.on_player_gone_locked(pid, username, PlayerStatus.OFFLINE)
        session.player_id = session.username = session.udp_token = None
        session.udp_addr = None
    session.rename_threads()
    log.info("%s logged out", username)
    return {}


def handle_ping(session: PlayerSession, data: dict, received_at: float) -> dict:
    return {"server_time": time.time()}
