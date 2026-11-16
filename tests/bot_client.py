"""
Headless CodeClash test client — FOR TESTING ONLY (the real player client is the Streamlit app).

Every BotClient is a genuine network client: its own TCP connection (AF_INET, SOCK_STREAM) and
its own UDP socket (AF_INET, SOCK_DGRAM) to the server's IP and port. Run ONE bot per process,
ideally one per Windows PC, to exercise the backend before the UI exists:

  # Windows PC 1 (creates the room, starts the game when 3 players are ready)
  python -m tests.bot_client --host 192.168.1.25 --user alice --password pass1234 --create Finals
  # Windows PC 2 and PC 3
  python -m tests.bot_client --host 192.168.1.25 --user bob   --password pass1234 --join auto
  python -m tests.bot_client --host 192.168.1.25 --user carol --password pass1234 --join auto

Bots answer with a random option after a random delay (they do not know the answers: only the
server does). Add --disconnect-after-round 2 to one of them to test the disconnection rules.
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from common import protocol as P  # noqa: E402
from common.protocol import AnswerKind, ErrorCode  # noqa: E402


class BotError(Exception):
    def __init__(self, response: dict):
        err = response.get("error", {})
        super().__init__(f"{response.get('request')} failed: {err.get('code')} - {err.get('message')}")
        self.code = err.get("code")
        self.response = response


class BotClient:
    def __init__(self, host: str, port: int = config.TCP_PORT, name: str = "bot", heartbeat: bool = True):
        self.host, self.port, self.name = host, port, name
        self.heartbeat = heartbeat
        self.sock: socket.socket | None = None
        self.udp: socket.socket | None = None
        self.cond = threading.Condition()
        self.send_lock = threading.Lock()
        self.responses: dict[str, dict] = {}
        self.events: list[dict] = []          # every TCP push, in arrival order
        self._taken: set[int] = set()         # indexes already returned by wait_for()
        self.ticks: list[dict] = []
        self.notices: list[dict] = []
        self.udp_acks = 0
        self.last_udp_seq: int | None = None
        self.server_closed = threading.Event()
        self._stop = threading.Event()
        self.player_id: str | None = None
        self.username: str | None = None
        self.udp_token: str | None = None
        self.udp_port = config.UDP_PORT
        self.welcome: dict = {}
        self._hello_seq = 0

    # ------------------------------------------------------------------ TCP
    def connect(self, timeout: float = 5.0) -> "BotClient":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((self.host, self.port))
        sock.settimeout(None)
        self.sock = sock
        threading.Thread(target=self._tcp_loop, name=f"bot-{self.name}-tcp", daemon=True).start()
        self.welcome = self.wait_for(P.WELCOME, timeout=5)["data"]
        self.udp_port = self.welcome.get("udp_port", self.udp_port)
        if self.heartbeat:
            threading.Thread(target=self._heartbeat_loop, name=f"bot-{self.name}-hb", daemon=True).start()
        return self

    def _tcp_loop(self) -> None:
        framer = P.LineFramer()
        try:
            while True:
                chunk = self.sock.recv(P.TCP_RECV_BUFFER)
                if not chunk:
                    break
                for line in framer.feed(chunk):
                    msg = P.decode_line(line)
                    with self.cond:
                        if msg["type"] == P.RESPONSE:
                            self.responses[msg["reply_to"]] = msg
                        else:
                            self.events.append(msg)
                        self.cond.notify_all()
        except (OSError, P.ProtocolError):
            pass
        finally:
            self.server_closed.set()
            with self.cond:
                self.cond.notify_all()

    def send_raw(self, data: bytes) -> None:
        with self.send_lock:
            self.sock.sendall(data)

    def send_request(self, msg_type: str, data: dict | None = None, req_id: str | None = None) -> str:
        msg = P.make_request(msg_type, data, req_id)
        self.send_raw(P.encode_tcp(msg))
        return msg["req_id"]

    def wait_response(self, req_id: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        with self.cond:
            while req_id not in self.responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{self.name}: no RESPONSE for {req_id}")
                if self.server_closed.is_set():
                    raise ConnectionError(f"{self.name}: connection closed by server")
                self.cond.wait(remaining)
            return self.responses.pop(req_id)

    def request(self, msg_type: str, data: dict | None = None, timeout: float = 5.0) -> dict:
        return self.wait_response(self.send_request(msg_type, data), timeout)

    def ok(self, msg_type: str, data: dict | None = None, timeout: float = 5.0) -> dict:
        response = self.request(msg_type, data, timeout)
        if not response["ok"]:
            raise BotError(response)
        return response["data"]

    def wait_for(self, types, pred=None, timeout: float = 10.0) -> dict:
        """Return the oldest not-yet-taken push of one of `types` (matching `pred`)."""
        types = (types,) if isinstance(types, str) else tuple(types)
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for i, msg in enumerate(self.events):
                    if i not in self._taken and msg["type"] in types and (pred is None or pred(msg)):
                        self._taken.add(i)
                        return msg
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{self.name}: no {types} within {timeout} s")
                if self.server_closed.is_set():
                    raise ConnectionError(f"{self.name}: connection closed while waiting for {types}")
                self.cond.wait(min(remaining, 0.5))

    def events_of(self, msg_type: str) -> list[dict]:
        with self.cond:
            return [m for m in self.events if m["type"] == msg_type]

    # ------------------------------------------------------------------ account + UDP
    def register_and_login(self, username: str, password: str) -> dict:
        response = self.request(P.REGISTER, {"username": username, "password": password})
        if not response["ok"] and response["error"]["code"] != ErrorCode.USERNAME_TAKEN:
            raise BotError(response)
        return self.login(username, password)

    def login(self, username: str, password: str) -> dict:
        data = self.ok(P.LOGIN, {"username": username, "password": password})
        self.player_id, self.username, self.udp_token = data["player_id"], data["username"], data["udp_token"]
        self.udp_port = data.get("udp_port", self.udp_port)
        self.start_udp()
        return data

    def start_udp(self) -> None:
        if self.udp is None:
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp.bind(("0.0.0.0", config.CLIENT_UDP_PORT))
            self.udp.settimeout(0.5)
            threading.Thread(target=self._udp_loop, name=f"bot-{self.name}-udp", daemon=True).start()
        self.send_hello()

    def send_hello(self) -> None:
        if self.udp is None or self.player_id is None:
            return
        self._hello_seq += 1
        hello = P.make_udp(P.UDP_HELLO, player_id=self.player_id, token=self.udp_token, seq=self._hello_seq)
        try:
            self.udp.sendto(P.encode_udp(hello), (self.host, self.udp_port))
        except OSError:
            pass

    def _udp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                payload, _ = self.udp.recvfrom(P.UDP_RECV_BUFFER)
                msg = P.decode_udp(payload)
            except (socket.timeout, TimeoutError, ConnectionResetError, P.ProtocolError):
                continue
            except OSError:
                break
            with self.cond:
                if msg["type"] == P.UDP_ACK:
                    self.udp_acks += 1
                elif msg["type"] in (P.TIMER_TICK, P.LIVE_NOTICE):
                    if not P.is_newer_seq(msg.get("seq"), self.last_udp_seq):
                        continue                        # late or duplicated datagram
                    self.last_udp_seq = msg["seq"]
                    (self.ticks if msg["type"] == P.TIMER_TICK else self.notices).append(msg)

    def _heartbeat_loop(self) -> None:
        interval = min(config.HEARTBEAT_INTERVAL_S, max(0.5, self.welcome.get("heartbeat_timeout_s", 15) / 3))
        while not self._stop.wait(interval):
            if self.server_closed.is_set():
                return
            try:
                self.send_request(P.PING)
            except OSError:
                return
            self.send_hello()

    # ------------------------------------------------------------------ closing
    def close(self, hard: bool = False) -> None:
        """Close without LOGOUT. hard=True sends a TCP RST (like a crashed program).

        Another thread is blocked in recv() on this socket. On Linux a bare close() would NOT
        end the connection while that recv() is in progress, so we shutdown() first to wake it.
        """
        self._stop.set()
        if self.sock is not None:
            if hard:
                # linger(on, 0 s): the final close() resets the connection instead of a clean FIN.
                # Windows' struct linger is two unsigned shorts; POSIX uses two ints.
                linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
                try:
                    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                except OSError:
                    pass
            try:
                # SHUT_RD wakes our recv() without sending anything; SHUT_RDWR also sends FIN.
                self.sock.shutdown(socket.SHUT_RD if hard else socket.SHUT_RDWR)
            except OSError:
                pass
            self.server_closed.wait(timeout=1.0)            # receiver thread has left recv()
            try:
                self.sock.close()
            except OSError:
                pass
        if self.udp is not None:
            try:
                self.udp.close()
            except OSError:
                pass


# ============================================================================ CLI bot

def _log(name: str, text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {name}: {text}", flush=True)


def run_bot(args: argparse.Namespace) -> int:
    problem = config.validate_server_host(args.host, allow_loopback=args.allow_loopback)
    if problem:
        print(problem)
        return 2
    rng = random.Random()
    bot = BotClient(args.host, args.port, name=args.user).connect()
    _log(args.user, f"TCP connected to {args.host}:{args.port}")
    bot.register_and_login(args.user, args.password)
    _log(args.user, f"logged in as {bot.player_id}; UDP hello sent from local port {bot.udp.getsockname()[1]}")

    # ---- room ----
    if args.create:
        room = bot.ok(P.CREATE_ROOM, {"room_name": args.create})["room"]
        _log(args.user, f"created room {room['room_id']} '{room['name']}' (host)")
    else:
        deadline = time.monotonic() + args.wait
        room = None
        while room is None:
            if args.join != "auto":
                room = bot.ok(P.JOIN_ROOM, {"room_id": args.join})["room"]
                break
            for r in bot.ok(P.LIST_ROOMS)["rooms"]:
                if (r["status"] == "WAITING" and r["player_count"] < r["capacity"]
                        and (args.room_name is None or r["name"] == args.room_name)):
                    response = bot.request(P.JOIN_ROOM, {"room_id": r["room_id"]})
                    if response["ok"]:
                        room = response["data"]["room"]
                        break
            if room is None:
                if time.monotonic() > deadline:
                    _log(args.user, "no room to join")
                    return 1
                time.sleep(1)
        bot.ok(P.SET_READY, {"room_id": room["room_id"], "ready": True})
        _log(args.user, f"joined room {room['room_id']} '{room['name']}' and pressed Ready")
    room_id = room["room_id"]

    if args.create:
        deadline = time.monotonic() + args.wait
        while True:
            response = bot.request(P.START_GAME, {"room_id": room_id})
            if response["ok"]:
                _log(args.user, f"started game {response['data']['game_id']}")
                break
            if response["error"]["code"] not in (ErrorCode.NOT_ENOUGH_PLAYERS, ErrorCode.PLAYERS_NOT_READY):
                raise BotError(response)
            if time.monotonic() > deadline:
                _log(args.user, "gave up waiting for players")
                return 1
            time.sleep(1)

    # ---- game ----
    started = bot.wait_for(P.GAME_STARTED, timeout=args.wait)["data"]
    _log(args.user, f"game {started['game_id']}: {started['total_rounds']} rounds, players "
                    + ", ".join(p["username"] for p in started["players"]))
    while True:
        msg = bot.wait_for((P.QUESTION, P.ROUND_RESULT, P.GAME_OVER, P.KICKED), timeout=120)
        data = msg["data"]
        if msg["type"] == P.KICKED:
            _log(args.user, f"kicked: {data['reason']}")
            return 1
        if msg["type"] == P.QUESTION:
            _log(args.user, f"round {data['round_number']}/{data['total_rounds']} [{data['category']}] {data['prompt']}")
            if args.answer == "none":
                continue
            time.sleep(min(rng.uniform(args.min_delay, args.max_delay), max(0.0, data["duration_s"] - 1)))
            if data["answer_kind"] == AnswerKind.CHOICE:
                answer = data["options"][0]["key"] if args.answer == "first" else rng.choice(data["options"])["key"]
            else:
                answer = str(rng.randint(0, 20))
            response = bot.request(P.SUBMIT_ANSWER, {
                "room_id": room_id, "round_id": data["round_id"], "question_id": data["question_id"],
                "answer": answer, "submission_id": P.new_id()})
            if response["ok"]:
                _log(args.user, f"  answered {answer!r}: accepted in {response['data']['response_time_ms']} ms "
                                f"({response['data']['answered_count']}/{response['data']['active_count']})")
            else:
                _log(args.user, f"  answer refused: {response['error']['code']}")
        elif msg["type"] == P.ROUND_RESULT:
            mine = next(r for r in data["results"] if r["player_id"] == bot.player_id)
            ticks = sum(1 for t in bot.ticks if t.get("round_id") == data["round_id"] and t["phase"] == "QUESTION")
            board = ", ".join(f"{s['rank']}. {s['username']} {s['score']}" for s in data["scoreboard"])
            _log(args.user, f"  round {data['round_number']} result: correct={data['correct_answer']} | me "
                            f"{'+' + str(mine['points']) if mine['correct'] else mine['outcome'] + ' 0'} | "
                            f"UDP ticks this round: {ticks} | {board}")
            if args.disconnect_after_round and data["round_number"] == args.disconnect_after_round:
                _log(args.user, "simulating a crash: closing the connection abruptly")
                bot.close(hard=True)
                print("BOT_RESULT " + json.dumps({"user": args.user, "disconnected": True}), flush=True)
                return 3
        elif msg["type"] == P.GAME_OVER:
            _log(args.user, f"GAME OVER ({data['reason']}). Winner(s): "
                            + (", ".join(w["username"] for w in data["winners"]) or "none"))
            for row in data["leaderboard"]:
                _log(args.user, f"  {row['rank']}. {row['username']:<12} {row['score']:>3} pts  "
                                f"{row['correct']}/{data['rounds_completed']} correct  {row['status']}")
            print("BOT_RESULT " + json.dumps({
                "user": args.user, "game_id": data["game_id"], "reason": data["reason"],
                "winners": [w["username"] for w in data["winners"]],
                "leaderboard": [(r["username"], r["score"]) for r in data["leaderboard"]],
                "udp_ticks": len(bot.ticks), "udp_acks": bot.udp_acks, "live_notices": len(bot.notices),
            }), flush=True)
            bot.close()
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CodeClash headless test client (one per process)")
    parser.add_argument("--host", default=config.SERVER_HOST)
    parser.add_argument("--port", type=int, default=config.TCP_PORT)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", default="pass1234")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", metavar="ROOM_NAME", help="create a room and start the game when full")
    group.add_argument("--join", metavar="ROOM_ID|auto", help="join a room by id, or the first open one")
    parser.add_argument("--room-name", help="with --join auto: only join rooms with this name")
    parser.add_argument("--answer", choices=("random", "first", "none"), default="random")
    parser.add_argument("--min-delay", type=float, default=1.0)
    parser.add_argument("--max-delay", type=float, default=6.0)
    parser.add_argument("--disconnect-after-round", type=int, default=0)
    parser.add_argument("--wait", type=float, default=180, help="seconds to wait for players / rooms")
    parser.add_argument("--allow-loopback", action="store_true", help="single-computer development only")
    args = parser.parse_args()
    try:
        return run_bot(args)
    except (OSError, BotError, TimeoutError) as exc:
        print(f"{args.user}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
