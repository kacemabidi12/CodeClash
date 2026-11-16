"""
NetworkManager — the only object the Streamlit UI talks to.

One NetworkManager exists per Streamlit process, i.e. per player computer (app.py creates it with
@st.cache_resource, so it survives script reruns and browser refreshes and its threads start once).

It owns:
  * the persistent TCP connection (TcpClient + its `net-tcp-receiver` thread)
  * the UDP socket (UdpClient + its `net-udp-receiver` thread)
  * a `net-heartbeat` thread: TCP PING (RTT, liveness) + UDP_HELLO (keeps the UDP path open)

Threads write into `self.state`, a MIRROR of what the server told us (lobby, room, game, ticks).
The UI only reads `snapshot()` and calls the request methods below; it never decides anything
itself (the server is authoritative: it says which round is open, what is correct, who won).

request() sends a request with a fresh req_id and blocks the calling (Streamlit) thread until
the RESPONSE with the same reply_to arrives, the timeout passes, or the connection drops.
"""

from __future__ import annotations

import copy
import itertools
import logging
import socket
import sys
import threading
import time

import config
from client.tcp_client import TcpClient
from client.udp_client import UdpClient
from common import protocol as P
from common.protocol import ErrorCode, Phase, PlayerStatus, RoomStatus

log = logging.getLogger("codeclash.client")
if not log.handlers:                                   # show client network logs in the Streamlit terminal
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter(config.LOG_FORMAT, config.LOG_DATEFMT))
    log.addHandler(_h)
    log.setLevel(config.LOG_LEVEL)
    log.propagate = False

NOT_CONNECTED = "NOT_CONNECTED"
TIMEOUT = "TIMEOUT"
MAX_NOTICES = 40


def describe_connect_error(exc: BaseException, host: str, port: int) -> str:
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if isinstance(exc, ConnectionRefusedError) or code in (61, 111, 10061):
        return f"{host}:{port} refused the connection. Is the server running on the MacBook?"
    if isinstance(exc, (socket.timeout, TimeoutError)) or code in (60, 110, 10060):
        return (f"No answer from {host}:{port}. Check the IP, that you are on the same Wi-Fi, "
                f"and that the MacBook firewall allows Python.")
    if code in (65, 113, 10065, 51, 101, 10051):
        return f"No route to {host}. Are both computers on the same network?"
    if isinstance(exc, socket.gaierror):
        return f"Cannot resolve '{host}'. Type the MacBook's IPv4 address, e.g. 192.168.1.25."
    return f"Could not connect: {exc}"


def _local_error(request_type: str, code: str, message: str) -> dict:
    """A RESPONSE-shaped failure produced on the client (no server reply)."""
    return {"type": P.RESPONSE, "request": request_type, "reply_to": None, "ok": False,
            "error": {"code": code, "message": message}}


class NetworkManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._tcp: TcpClient | None = None
        self._udp: UdpClient | None = None
        self._pending: dict[str, dict | None] = {}
        self._ping_sent: dict[str, float] = {}
        self._credentials: tuple[str, str] | None = None
        self._udp_token: str | None = None
        self._hb_stop = threading.Event()
        self._heartbeat: threading.Thread | None = None
        self._user_closing = False
        self._last_rx = 0.0
        self._hello_seq = 0
        self._udp_room: str | None = None
        self._last_udp_seq: int | None = None
        self._notice_ids = itertools.count(1)
        self.version = 0                    # +1 on every server event that can change the page
        self.state = self._fresh_state()

    @staticmethod
    def _fresh_state(server_host: str | None = None, server_port: int | None = None) -> dict:
        return {
            "connection": {"status": "disconnected", "server_host": server_host, "server_port": server_port,
                           "error": None, "lost": False, "rtt_ms": None, "local_addr": None, "welcome": None},
            "udp": {"local_port": None, "last_ack": None, "last_packet": None, "ticks": 0,
                    "notices": 0, "dropped": 0, "hellos": 0},
            "me": None,
            "lobby": {"rooms": [], "online_players": []},
            "room": None,
            "game": None,
            "tick": None,
            "notices": [],
            "kicked": None,
        }

    # ================================================================== connection
    def connect(self, host: str, port: int | None = None) -> tuple[bool, str]:
        host = (host or "").strip()
        port = int(port or config.TCP_PORT)
        problem = config.validate_server_host(host)
        if problem:
            return False, problem
        self._shutdown_transport("switching server")
        with self._cond:
            self.state = self._fresh_state(host, port)
            self.state["connection"]["status"] = "connecting"
            self._user_closing = False
            self.version += 1
        tcp = TcpClient(self._on_tcp_message, self._on_tcp_disconnect)
        try:
            tcp.connect(host, port, config.CONNECT_TIMEOUT_S)
        except OSError as exc:
            message = describe_connect_error(exc, host, port)
            with self._cond:
                self.state["connection"].update(status="disconnected", error=message)
                self.version += 1
            log.warning("Connect to %s:%d failed: %s", host, port, exc)
            return False, message
        self._tcp = tcp
        self._last_rx = time.monotonic()
        with self._cond:                                       # WELCOME arrives first on every connection
            deadline = time.monotonic() + 3.0
            while self.state["connection"]["welcome"] is None and tcp.connected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            welcome = self.state["connection"]["welcome"] or {}
        if not tcp.connected:
            return False, self.state["connection"]["error"] or "The server closed the connection."
        udp = UdpClient(self._on_udp_datagram)
        try:
            udp.open(host, int(welcome.get("udp_port", config.UDP_PORT)), config.CLIENT_UDP_PORT)
            self._udp = udp
        except OSError as exc:
            log.error("Could not open the UDP socket: %s (the countdown will be estimated locally)", exc)
        with self._cond:
            self.state["connection"].update(status="connected", error=None, local_addr=tcp.local_addr)
            self.state["udp"]["local_port"] = udp.local_port if self._udp else None
            self.version += 1
        self._hb_stop.clear()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, name="net-heartbeat", daemon=True)
        self._heartbeat.start()
        try:
            config.save_server_host(host)
        except OSError:
            pass
        return True, f"Connected to {host}:{port}"

    def disconnect(self) -> None:
        """User-initiated: close both sockets and forget the session."""
        self._user_closing = True
        self._credentials = None
        self._shutdown_transport("disconnected by you")
        with self._cond:
            conn = self.state["connection"]
            self.state = self._fresh_state(conn["server_host"], conn["server_port"])
            self.version += 1

    def reconnect(self) -> tuple[bool, str]:
        """After a lost connection: connect again and log back in (the server re-attaches us)."""
        conn = self.state["connection"]
        creds = self._credentials
        ok, message = self.connect(conn["server_host"] or config.get_server_host(), conn["server_port"])
        if not ok or creds is None:
            return ok, message
        response = self.login(*creds)
        if not response["ok"]:
            return False, response["error"]["message"]
        return True, "Reconnected"

    def _shutdown_transport(self, reason: str) -> None:
        self._hb_stop.set()
        tcp, udp = self._tcp, self._udp
        self._tcp = self._udp = None
        if tcp is not None:
            tcp.close(reason)
        if udp is not None:
            udp.close()

    # ================================================================== requests (Streamlit thread)
    def request(self, msg_type: str, data: dict | None = None,
                timeout: float = config.REQUEST_TIMEOUT_S) -> dict:
        tcp = self._tcp
        if tcp is None or not tcp.connected:
            return _local_error(msg_type, NOT_CONNECTED, "Not connected to the server.")
        msg = P.make_request(msg_type, data)
        req_id = msg["req_id"]
        with self._cond:
            self._pending[req_id] = None
        try:
            tcp.send(msg)
        except OSError:
            with self._cond:
                self._pending.pop(req_id, None)
            return _local_error(msg_type, NOT_CONNECTED, "Connection lost while sending.")
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pending.get(req_id) is None:
                if not tcp.connected:
                    self._pending.pop(req_id, None)
                    return _local_error(msg_type, NOT_CONNECTED, "Connection lost.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._pending.pop(req_id, None)
                    return _local_error(msg_type, TIMEOUT, "The server did not answer in time.")
                self._cond.wait(remaining)
            return self._pending.pop(req_id)

    def register(self, username: str, password: str) -> dict:
        return self.request(P.REGISTER, {"username": username.strip(), "password": password})

    def login(self, username: str, password: str) -> dict:
        response = self.request(P.LOGIN, {"username": username.strip(), "password": password})
        if response["ok"]:
            d = response["data"]
            with self._cond:
                self._credentials = (username.strip(), password)   # kept in memory only, for Reconnect
                self._udp_token = d["udp_token"]
                self.state["me"] = {"player_id": d["player_id"], "username": d["username"]}
                self.state["room"] = d.get("room")
                self.state["kicked"] = None
                self.state["connection"]["lost"] = False
                if self._udp is not None:
                    self._udp.set_server_port(int(d.get("udp_port", config.UDP_PORT)))
                self.version += 1
            self._send_hello()                                  # register our UDP endpoint right away
            log.info("Logged in as %s (%s)", d["username"], d["player_id"])
        return response

    def logout(self) -> dict:
        response = self.request(P.LOGOUT)
        with self._cond:
            self._credentials = None
            self._udp_token = None
            self.state.update(me=None, room=None, game=None, tick=None)
            self.version += 1
        return response

    def refresh_lobby(self) -> dict:
        response = self.request(P.LIST_ROOMS)
        if response["ok"]:
            with self._cond:
                self.state["lobby"] = response["data"]
                self.version += 1
        return response

    def _room_request(self, msg_type: str, data: dict) -> dict:
        response = self.request(msg_type, data)
        if response["ok"] and "room" in response.get("data", {}):
            with self._cond:
                self.state["room"] = response["data"]["room"]
                self.version += 1
        return response

    def create_room(self, name: str) -> dict:
        return self._room_request(P.CREATE_ROOM, {"room_name": name.strip()})

    def join_room(self, room_id: str) -> dict:
        return self._room_request(P.JOIN_ROOM, {"room_id": room_id})

    def set_ready(self, ready: bool) -> dict:
        return self._room_request(P.SET_READY, {"room_id": self._room_id(), "ready": ready})

    def start_game(self) -> dict:
        return self.request(P.START_GAME, {"room_id": self._room_id()})

    def play_again(self) -> dict:
        response = self._room_request(P.PLAY_AGAIN, {"room_id": self._room_id()})
        if response["ok"]:
            with self._cond:
                self.state["game"] = None
        return response

    def leave_room(self) -> dict:
        response = self.request(P.LEAVE_ROOM, {"room_id": self._room_id()})
        if response["ok"] or response["error"]["code"] == ErrorCode.NOT_IN_ROOM:
            with self._cond:
                self.state.update(room=None, game=None, tick=None)
                self.version += 1
        return response

    def submit_answer(self, answer: str) -> dict:
        """Submit for the CURRENT round. One submission_id per round, reused on retries (idempotent)."""
        with self._cond:
            game = self.state["game"]
            question = game.get("question") if game else None
            if not game or not question or game["phase"] != Phase.QUESTION:
                return _local_error(P.SUBMIT_ANSWER, ErrorCode.ROUND_CLOSED, "No round is open.")
            sub = game.get("submission")
            if sub and sub["round_id"] == question["round_id"] and sub.get("accepted"):
                return {"type": P.RESPONSE, "ok": True, "data": {"accepted": True, "duplicate": True}}
            if not sub or sub["round_id"] != question["round_id"]:
                sub = {"round_id": question["round_id"], "submission_id": P.new_id()}
                game["submission"] = sub
            sub.update(answer=answer, accepted=False, error=None, pending=True)
            payload = {"room_id": question["room_id"], "round_id": question["round_id"],
                       "question_id": question["question_id"], "answer": answer,
                       "submission_id": sub["submission_id"]}
            self.version += 1
        response = self.request(P.SUBMIT_ANSWER, payload)
        with self._cond:
            game = self.state["game"]
            sub = game.get("submission") if game else None
            if sub and sub["round_id"] == payload["round_id"]:
                sub["pending"] = False
                if response["ok"]:
                    d = response["data"]
                    sub.update(accepted=True, response_time_ms=d.get("response_time_ms"))
                    if d.get("answered_count") is not None:
                        game["live"] = {"answered": d["answered_count"], "active": d["active_count"]}
                else:
                    sub["error"] = response["error"]
            self.version += 1
        return response

    def _room_id(self) -> str:
        room = self.state.get("room")
        return room["room_id"] if room else ""

    # ================================================================== read side (UI)
    def snapshot(self) -> dict:
        """A deep copy of the mirrored state, safe to render without holding any lock."""
        with self._cond:
            snap = copy.deepcopy(self.state)
            snap["version"] = self.version
            now = time.monotonic()
            udp = snap["udp"]
            udp["ok"] = udp["last_ack"] is not None and now - udp["last_ack"] < 3 * config.UDP_HELLO_INTERVAL_S
            udp["live"] = udp["last_packet"] is not None and now - udp["last_packet"] < 2.5
        snap["countdown"] = self.countdown()
        return snap

    def countdown(self) -> dict | None:
        """Seconds left in the current phase. Uses the latest UDP tick when it is fresh,
        otherwise estimates locally from the last authoritative TCP event."""
        with self._cond:
            game = self.state["game"]
            if not game or game["phase"] == Phase.FINISHED:
                return None
            now = time.monotonic()
            phase = game["phase"]
            tick = self.state["tick"]
            if tick is not None and now - tick["at"] < 2.5:
                m = tick["msg"]
                question = game.get("question")
                same_round = phase != Phase.QUESTION or (question and m.get("round_id") == question["round_id"])
                if m.get("game_id") == game["game_id"] and m.get("phase") == phase and same_round:
                    left = max(0.0, m["seconds_left"] - (now - tick["at"]))
                    return {"phase": phase, "seconds_left": left, "duration": m["duration"], "source": "udp"}
            if phase == Phase.QUESTION and game.get("question"):
                duration = game["question"]["duration_s"]
                left = max(0.0, duration - (now - game["question_at"]))
            elif phase == Phase.STARTING:
                duration = game.get("starts_in_s") or config.PRE_GAME_COUNTDOWN_S
                left = max(0.0, duration - (now - game["phase_at"]))
            elif phase == Phase.RESULTS:
                duration = (game.get("result") or {}).get("next_round_in_s") or 0
                left = max(0.0, duration - (now - game["phase_at"]))
            else:
                return None
            return {"phase": phase, "seconds_left": left, "duration": duration, "source": "local"}

    # ================================================================== TCP events (receiver thread)
    def _on_tcp_message(self, msg: dict) -> None:
        msg_type = msg["type"]
        now = time.monotonic()
        with self._cond:
            self._last_rx = now
            if msg_type == P.RESPONSE:
                req_id = msg.get("reply_to")
                sent = self._ping_sent.pop(req_id, None)
                if sent is not None:
                    self.state["connection"]["rtt_ms"] = round((now - sent) * 1000, 1)
                if req_id in self._pending:
                    self._pending[req_id] = msg
                self._cond.notify_all()
                return
            handler = self._push_handlers.get(msg_type)
            if handler is not None:
                handler(self, msg.get("data", {}))
                self.version += 1
            self._cond.notify_all()

    def _on_tcp_disconnect(self, reason: str) -> None:
        with self._cond:
            conn = self.state["connection"]
            was_logged_in = self.state["me"] is not None
            conn.update(status="disconnected", error=reason,
                        lost=was_logged_in and not self._user_closing and self.state["kicked"] is None)
            self._cond.notify_all()
            self.version += 1
        self._hb_stop.set()
        log.warning("TCP connection ended: %s", reason)

    def _notice(self, text: str, kind: str = "info") -> None:
        self.state["notices"].append({"id": next(self._notice_ids), "text": text, "kind": kind,
                                      "time": time.strftime("%H:%M:%S")})
        del self.state["notices"][:-MAX_NOTICES]

    # The handlers below run in the TCP receiver thread with the lock held.
    def _h_welcome(self, d: dict) -> None:
        self.state["connection"]["welcome"] = d

    def _h_lobby(self, d: dict) -> None:
        self.state["lobby"] = {"rooms": d.get("rooms", []), "online_players": d.get("online_players", [])}

    def _h_room(self, d: dict) -> None:
        room = d.get("room")
        me = self.state["me"]
        if room and me and me["player_id"] not in [p["player_id"] for p in room["players"]]:
            return                                              # an update about a room we just left
        self.state["room"] = room
        if room and room["status"] == RoomStatus.WAITING and self.state["game"] is not None:
            self.state["game"] = None                           # Play Again: back to the waiting room
            self.state["tick"] = None
        if d.get("notice"):
            self._notice(d["notice"])

    def _h_player_status(self, d: dict) -> None:
        name, status = d.get("username"), d.get("status")
        text, kind = {
            PlayerStatus.DISCONNECTED: (f"{name} lost connection", "warning"),
            PlayerStatus.RECONNECTED: (f"{name} reconnected", "success"),
            PlayerStatus.LEFT: (f"{name} left the room", "warning"),
            PlayerStatus.OFFLINE: (f"{name} logged out", "warning"),
        }.get(status, (f"{name}: {status}", "info"))
        self._notice(text, kind)

    def _new_game(self, d: dict, phase: str) -> dict:
        return {
            "game_id": d["game_id"], "room_id": d["room_id"], "total_rounds": d["total_rounds"],
            "round_duration_s": d.get("round_duration_s"), "results_duration_s": d.get("results_duration_s"),
            "starts_in_s": d.get("starts_in_s"), "players": d.get("players", []), "scoring": d.get("scoring", {}),
            "phase": phase, "phase_at": time.monotonic(), "question": None, "question_at": None,
            "submission": None, "live": None, "result": None, "results": [], "over": None,
            "scoreboard": [{"rank": 1, "player_id": p["player_id"], "username": p["username"], "score": 0,
                            "correct": 0, "connected": True} for p in d.get("players", [])],
        }

    def _h_game_started(self, d: dict) -> None:
        self.state["game"] = self._new_game(d, Phase.STARTING)
        self.state["tick"] = None

    def _h_question(self, d: dict) -> None:
        game = self.state["game"]
        if game is None or game["game_id"] != d["game_id"]:
            game = self.state["game"] = self._new_game(d, Phase.QUESTION)
        game.update(phase=Phase.QUESTION, phase_at=time.monotonic(), question=d, question_at=time.monotonic(),
                    submission=None, live={"answered": 0, "active": None})

    def _h_round_result(self, d: dict) -> None:
        game = self.state["game"]
        if game is None:
            return
        game.update(phase=Phase.RESULTS, phase_at=time.monotonic(), result=d, scoreboard=d["scoreboard"])
        game["results"].append(d)

    def _h_game_over(self, d: dict) -> None:
        game = self.state["game"]
        if game is None:
            game = self.state["game"] = self._new_game({**d, "total_rounds": d.get("total_rounds", 0)}, Phase.FINISHED)
        game.update(phase=Phase.FINISHED, over=d, scoreboard=d["leaderboard"])
        winners = ", ".join(w["username"] for w in d.get("winners", [])) or "nobody"
        self._notice(f"Game over — winner: {winners}", "success")

    def _h_game_sync(self, d: dict) -> None:
        game = self._new_game(d, d["phase"])
        question = d.get("question")
        now = time.monotonic()
        if question:
            game["question"] = question
            game["question_at"] = now - (question["duration_s"] - d.get("seconds_left", 0))
            if d.get("already_answered"):
                game["submission"] = {"round_id": question["round_id"], "submission_id": None,
                                      "answer": d.get("my_answer"), "accepted": True, "pending": False}
        game["phase_at"] = now - max(0, (game.get("results_duration_s") or 0) - d.get("seconds_left", 0))
        game["result"] = d.get("last_round_result")
        game["scoreboard"] = d.get("scoreboard", game["scoreboard"])
        self.state["game"] = game
        self._notice("Reconnected — you are back in the game", "success")

    def _h_kicked(self, d: dict) -> None:
        self.state["kicked"] = d.get("reason")
        self._notice(d.get("reason", "Disconnected by the server"), "error")

    def _h_error(self, d: dict) -> None:
        self._notice(f"Server error: {d.get('message')}", "error")

    _push_handlers = {
        P.WELCOME: _h_welcome, P.LOBBY_UPDATE: _h_lobby, P.ROOM_UPDATE: _h_room,
        P.PLAYER_STATUS: _h_player_status, P.GAME_STARTED: _h_game_started, P.QUESTION: _h_question,
        P.ROUND_RESULT: _h_round_result, P.GAME_OVER: _h_game_over, P.GAME_SYNC: _h_game_sync,
        P.KICKED: _h_kicked, P.ERROR: _h_error,
    }

    # ================================================================== UDP (receiver thread)
    def _on_udp_datagram(self, msg: dict) -> None:
        now = time.monotonic()
        with self._cond:
            udp = self.state["udp"]
            udp["last_packet"] = now
            msg_type = msg.get("type")
            if msg_type == P.UDP_ACK:
                udp["last_ack"] = now
                return
            room_id = msg.get("room_id")
            if room_id != self._udp_room:                       # a new room starts a new sequence
                self._udp_room, self._last_udp_seq = room_id, None
            if not P.is_newer_seq(msg.get("seq"), self._last_udp_seq):
                udp["dropped"] += 1                             # late or duplicated datagram
                return
            self._last_udp_seq = msg["seq"]
            if msg_type == P.TIMER_TICK:
                self.state["tick"] = {"msg": msg, "at": now}
                udp["ticks"] += 1
            elif msg_type == P.LIVE_NOTICE:
                udp["notices"] += 1
                game = self.state["game"]
                question = game.get("question") if game else None
                if question and msg.get("round_id") == question["round_id"]:
                    game["live"] = {"answered": msg.get("answered"), "active": msg.get("active"),
                                    "last": msg.get("username")}

    def _send_hello(self) -> None:
        me, udp, token = self.state.get("me"), self._udp, self._udp_token
        if me is None or udp is None or token is None:
            return
        self._hello_seq += 1
        if udp.send(P.make_udp(P.UDP_HELLO, player_id=me["player_id"], token=token, seq=self._hello_seq)):
            with self._cond:
                self.state["udp"]["hellos"] += 1

    # ================================================================== heartbeat thread
    def _heartbeat_loop(self) -> None:
        welcome = self.state["connection"].get("welcome") or {}
        timeout = float(welcome.get("heartbeat_timeout_s", config.HEARTBEAT_TIMEOUT_S))
        interval = min(float(welcome.get("heartbeat_interval_s", config.HEARTBEAT_INTERVAL_S)), timeout / 3)
        while not self._hb_stop.wait(interval):
            tcp = self._tcp
            if tcp is None or not tcp.connected:
                return
            ping = P.make_request(P.PING)
            with self._cond:
                self._ping_sent[ping["req_id"]] = time.monotonic()
                if len(self._ping_sent) > 20:                   # forget pings that were never answered
                    for key in list(self._ping_sent)[:-5]:
                        del self._ping_sent[key]
            try:
                tcp.send(ping)
            except OSError:
                return
            self._send_hello()
            if time.monotonic() - self._last_rx > timeout:      # server silent: treat as a lost connection
                log.warning("No message from the server for %.0f s", timeout)
                tcp.close(f"no message from the server for {timeout:.0f} s")
                return
