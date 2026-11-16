"""
Phase 2 integration tests: a REAL server process and real TCP/UDP sockets.

The server runs as its own process (python -m server.main_server) on a free port, with short
timings. Test clients are BotClient instances (real sockets, test-only knowledge of the answer
key to make scoring deterministic). One test also launches three separate bot PROCESSES.

Run from the project root (takes about a minute; works on macOS, Windows and Linux):
    python3 -m unittest -v tests.test_server_integration
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from common import protocol as P
from common.protocol import AnswerKind, ErrorCode, PlayerStatus, RoomStatus
from server.questions import QUESTIONS_BY_ID
from tests.bot_client import BotClient

ROOT = Path(__file__).resolve().parent.parent
ROUND_S = 3
HEARTBEAT_TIMEOUT_S = 3
TOTAL_ROUNDS = 3


def free_port() -> int:
    """A port number that is free for BOTH TCP and UDP."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as t:
            t.bind(("127.0.0.1", 0))
            port = t.getsockname()[1]
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
    raise RuntimeError("no free port")


class ServerProcess:
    def __init__(self, **overrides: str) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.log_path = Path(self.tmp.name) / "server.log"
        self.env = dict(os.environ,
                        CODECLASH_TCP_PORT=str(self.port), CODECLASH_UDP_PORT=str(self.port),
                        CODECLASH_USERS_FILE=str(Path(self.tmp.name) / "users.json"),
                        CODECLASH_PBKDF2_ITERATIONS="2000", CODECLASH_TOTAL_ROUNDS=str(TOTAL_ROUNDS),
                        CODECLASH_ROUND_DURATION_S=str(ROUND_S), CODECLASH_RESULTS_DURATION_S="1",
                        CODECLASH_PRE_GAME_COUNTDOWN_S="1", CODECLASH_HEARTBEAT_TIMEOUT_S=str(HEARTBEAT_TIMEOUT_S),
                        CODECLASH_LOG_LEVEL="INFO", PYTHONUNBUFFERED="1")
        self.env.update(overrides)                       # e.g. CODECLASH_ROUND_DURATION_S="10" for UI tests
        self._log_file = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-m", "server.main_server", "--no-console"],
                                     cwd=ROOT, env=self.env, stdout=self._log_file, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start:\n" + self.log())

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8")

    def stop(self) -> None:
        if self.proc.poll() is None:
            if os.name == "nt":
                self.proc.terminate()          # Windows cannot send SIGINT to a child process
            else:
                self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._log_file.close()
        self.tmp.cleanup()


class IntegrationTests(unittest.TestCase):
    server: ServerProcess

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ServerProcess()

    @classmethod
    def tearDownClass(cls) -> None:
        log = cls.server.log()
        cls.server.stop()
        if os.environ.get("SHOW_SERVER_LOG"):
            print(log)

    def setUp(self) -> None:
        self.clients: list[BotClient] = []

    def tearDown(self) -> None:
        for c in self.clients:
            c.close()

    # ------------------------------------------------------------------ helpers
    def client(self, heartbeat: bool = True) -> BotClient:
        c = BotClient("127.0.0.1", self.server.port, name=f"c{len(self.clients)}", heartbeat=heartbeat).connect()
        self.clients.append(c)
        return c

    def player(self, prefix: str = "u", heartbeat: bool = True) -> BotClient:
        c = self.client(heartbeat)
        c.register_and_login(f"{prefix}{uuid.uuid4().hex[:8]}", "pass1234")
        return c

    def players(self, n: int = 3) -> list[BotClient]:
        return [self.player() for _ in range(n)]

    def room_with(self, host: BotClient, others: list[BotClient]) -> str:
        room_id = host.ok(P.CREATE_ROOM, {"room_name": "Test arena"})["room"]["room_id"]
        for o in others:
            o.ok(P.JOIN_ROOM, {"room_id": room_id})
            o.ok(P.SET_READY, {"room_id": room_id, "ready": True})
        return room_id

    def start(self, players: list[BotClient]) -> str:
        room_id = self.room_with(players[0], players[1:])
        players[0].ok(P.START_GAME, {"room_id": room_id})
        for p in players:
            p.wait_for(P.GAME_STARTED, timeout=5)
        return room_id

    @staticmethod
    def submit(c: BotClient, q: dict, answer: str, submission_id: str | None = None) -> dict:
        return c.request(P.SUBMIT_ANSWER, {"room_id": q["room_id"], "round_id": q["round_id"],
                                           "question_id": q["question_id"], "answer": answer,
                                           "submission_id": submission_id or P.new_id()})

    @staticmethod
    def right_and_wrong(q: dict) -> tuple[str, str]:
        """Test-only: read the server's answer key to produce a correct and a wrong answer."""
        key = QUESTIONS_BY_ID[q["question_id"]]
        if key.kind == AnswerKind.CHOICE:
            return key.answer, next(k for k in "ABCD" if k != key.answer)
        return key.answer, "definitely wrong"

    # ================================================================== tests
    def test_01_auth_and_protocol_errors(self):
        c = self.client()
        self.assertEqual(c.request(P.LIST_ROOMS)["error"]["code"], ErrorCode.NOT_AUTHENTICATED)
        self.assertEqual(c.request(P.REGISTER, {"username": "a b", "password": "pass1234"})["error"]["code"],
                         ErrorCode.INVALID_USERNAME)
        self.assertEqual(c.request(P.REGISTER, {"username": "okname", "password": "12"})["error"]["code"],
                         ErrorCode.INVALID_PASSWORD)
        name = "kacem" + uuid.uuid4().hex[:6]
        reg = c.ok(P.REGISTER, {"username": name, "password": "secret99"})
        self.assertRegex(reg["player_id"], r"^P[0-9A-F]{6}$")
        self.assertEqual(c.request(P.REGISTER, {"username": name.upper(), "password": "x1234"})["error"]["code"],
                         ErrorCode.USERNAME_TAKEN)
        self.assertEqual(c.request(P.LOGIN, {"username": name, "password": "wrong999"})["error"]["code"],
                         ErrorCode.INVALID_CREDENTIALS)
        login = c.ok(P.LOGIN, {"username": name, "password": "secret99"})
        self.assertEqual(login["player_id"], reg["player_id"])
        c.player_id, c.udp_token = login["player_id"], login["udp_token"]
        c.start_udp()                                          # UDP_HELLO with the token from LOGIN
        self.assertEqual(len(login["udp_token"]), 32)
        self.assertEqual(c.request(P.LOGIN, {"username": name, "password": "secret99"})["error"]["code"],
                         ErrorCode.ALREADY_AUTHENTICATED)

        # Malformed line -> ERROR push, connection stays usable
        c.send_raw(b"this is not json\n")
        self.assertEqual(c.wait_for(P.ERROR, timeout=3)["data"]["code"], ErrorCode.MALFORMED_MESSAGE)
        c.send_raw(P.encode_tcp({"v": 1, "type": "HACK_SCORE", "req_id": "x1", "data": {}}))
        self.assertEqual(c.wait_response("x1")["error"]["code"], ErrorCode.UNKNOWN_TYPE)
        self.assertIn("server_time", c.ok(P.PING))

        # Two requests merged into ONE TCP segment are both answered (framing on the server)
        a, b = P.make_request(P.PING), P.make_request(P.LIST_ROOMS)
        c.send_raw(P.encode_tcp(a) + P.encode_tcp(b))
        self.assertTrue(c.wait_response(a["req_id"])["ok"] and c.wait_response(b["req_id"])["ok"])

        # Passwords are stored hashed, never in plain text
        users = Path(self.server.env["CODECLASH_USERS_FILE"]).read_text(encoding="utf-8")
        self.assertNotIn("secret99", users)
        self.assertIn('"hash"', users)
        # UDP endpoint registered through UDP_HELLO -> UDP_ACK
        deadline = time.monotonic() + 3
        while c.udp_acks == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertGreater(c.udp_acks, 0)

    def test_02_room_rules(self):
        a, b, c, d = self.players(4)
        room_id = a.ok(P.CREATE_ROOM, {"room_name": "Rules"})["room"]["room_id"]
        self.assertEqual(a.request(P.CREATE_ROOM, {"room_name": "Again"})["error"]["code"], ErrorCode.ALREADY_IN_ROOM)
        d.wait_for(P.LOBBY_UPDATE, pred=lambda m: any(r["room_id"] == room_id for r in m["data"]["rooms"]))
        self.assertEqual(a.request(P.START_GAME, {"room_id": room_id})["error"]["code"], ErrorCode.NOT_ENOUGH_PLAYERS)
        b.ok(P.JOIN_ROOM, {"room_id": room_id})
        c.ok(P.JOIN_ROOM, {"room_id": room_id})
        self.assertEqual(d.request(P.JOIN_ROOM, {"room_id": room_id})["error"]["code"], ErrorCode.ROOM_FULL)
        self.assertEqual(d.request(P.JOIN_ROOM, {"room_id": "R0000"})["error"]["code"], ErrorCode.ROOM_NOT_FOUND)
        self.assertEqual(b.request(P.START_GAME, {"room_id": room_id})["error"]["code"], ErrorCode.NOT_HOST)
        self.assertEqual(a.request(P.START_GAME, {"room_id": room_id})["error"]["code"], ErrorCode.PLAYERS_NOT_READY)
        b.ok(P.SET_READY, {"room_id": room_id, "ready": True})
        snap = c.ok(P.SET_READY, {"room_id": room_id, "ready": True})["room"]
        self.assertEqual([p["ready"] for p in snap["players"]], [True, True, True])

        # Host leaves before the game starts -> host passes to the longest-present member (b)
        a.ok(P.LEAVE_ROOM, {"room_id": room_id})
        upd = b.wait_for(P.ROOM_UPDATE, pred=lambda m: m["data"]["room"]["host_id"] == b.player_id, timeout=3)
        self.assertEqual(len(upd["data"]["room"]["players"]), 2)
        self.assertEqual(b.wait_for(P.PLAYER_STATUS)["data"]["status"], PlayerStatus.LEFT)
        self.assertEqual(b.request(P.START_GAME, {"room_id": room_id})["error"]["code"], ErrorCode.NOT_ENOUGH_PLAYERS)
        self.assertEqual(a.request(P.LEAVE_ROOM, {"room_id": room_id})["error"]["code"], ErrorCode.NOT_IN_ROOM)
        b.ok(P.LEAVE_ROOM, {"room_id": room_id})
        c.ok(P.LEAVE_ROOM, {"room_id": room_id})
        self.assertNotIn(room_id, [r["room_id"] for r in d.ok(P.LIST_ROOMS)["rooms"]])   # empty room deleted

    def test_03_full_game_simultaneous_and_duplicate_submissions(self):
        a, b, c = self.players(3)
        room_id = self.start([a, b, c])
        self.assertEqual(a.request(P.JOIN_ROOM, {"room_id": room_id})["ok"], True)     # already inside: idempotent
        expected = {a.player_id: 0, b.player_id: 0, c.player_id: 0}

        for number in range(1, TOTAL_ROUNDS + 1):
            qs = [p.wait_for(P.QUESTION, timeout=8)["data"] for p in (a, b, c)]
            q = qs[0]
            self.assertEqual(q["round_number"], number)
            self.assertEqual({x["round_id"] for x in qs}, {q["round_id"]})            # same round for everyone
            self.assertEqual({x["question_id"] for x in qs}, {q["question_id"]})      # same question
            for secret in ("answer", "correct_answer", "correct_text", "explanation"):
                self.assertNotIn(secret, q)                                         # answer never sent
            right, wrong = self.right_and_wrong(q)
            a_sid = P.new_id()
            opened = time.monotonic()

            # All three submit at the same instant from three threads.
            barrier, results = threading.Barrier(3), {}

            def go(player, answer, key):
                barrier.wait()
                if key == "b" and number == 1:
                    # b sends 5 different submissions in ONE TCP segment: only the first may count.
                    reqs = [P.make_request(P.SUBMIT_ANSWER, {"room_id": room_id, "round_id": q["round_id"],
                                                             "question_id": q["question_id"], "answer": answer,
                                                             "submission_id": P.new_id()}) for _ in range(5)]
                    player.send_raw(b"".join(P.encode_tcp(r) for r in reqs))
                    results[key] = [player.wait_response(r["req_id"]) for r in reqs]
                else:
                    results[key] = [self.submit(player, q, answer, a_sid if key == "a" else None)]

            threads = [threading.Thread(target=go, args=args) for args in
                       ((a, right, "a"), (b, wrong, "b"), (c, right, "c"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertTrue(results["a"][0]["ok"] and results["c"][0]["ok"])
            accepted_b = [r for r in results["b"] if r["ok"]]
            self.assertEqual(len(accepted_b), 1)
            for r in results["b"]:
                if not r["ok"]:   # refused either as a 2nd answer, or because the round already closed
                    self.assertIn(r["error"]["code"], (ErrorCode.ALREADY_ANSWERED, ErrorCode.ROUND_CLOSED))

            if number == 1:
                outsider = self.player()           # logged in, but not in this game
                self.assertEqual(self.submit(outsider, q, right)["error"]["code"], ErrorCode.NOT_IN_GAME)

            res = [p.wait_for(P.ROUND_RESULT, timeout=8)["data"] for p in (a, b, c)]
            self.assertLess(time.monotonic() - opened, ROUND_S, "round should close early once all answered")
            self.assertEqual(res[0], res[1])
            self.assertEqual(res[1], res[2])
            r = {row["player_id"]: row for row in res[0]["results"]}
            self.assertTrue(r[a.player_id]["correct"] and r[c.player_id]["correct"])
            self.assertFalse(r[b.player_id]["correct"])
            self.assertEqual(r[b.player_id]["points"], 0)
            for pid in (a.player_id, c.player_id):
                self.assertEqual(r[pid]["base_points"], 10)
                self.assertTrue(1 <= r[pid]["bonus_points"] <= 5)
            for pid, row in r.items():
                expected[pid] += row["points"]
            self.assertEqual({s["player_id"]: s["score"] for s in res[0]["scoreboard"]}, expected)
            self.assertEqual(res[0]["correct_answer"], QUESTIONS_BY_ID[q["question_id"]].answer)

            # After the round closed: a retry with the SAME submission_id is idempotent (never re-scored),
            # a NEW submission is refused, and a made-up round id is rejected.
            if number == 1:
                retry = self.submit(a, q, right, submission_id=a_sid)
                self.assertTrue(retry["ok"] and retry["data"]["duplicate"])
                self.assertEqual(self.submit(a, q, right)["error"]["code"], ErrorCode.ROUND_CLOSED)
                self.assertEqual(self.submit(b, dict(q, round_id="G999-R1"), right)["error"]["code"],
                                 ErrorCode.WRONG_ROUND)

        over = [p.wait_for(P.GAME_OVER, timeout=8)["data"] for p in (a, b, c)]
        self.assertEqual(over[0], over[1])
        self.assertEqual(over[0]["reason"], "completed")
        board = {row["player_id"]: row for row in over[0]["leaderboard"]}
        self.assertEqual({pid: row["score"] for pid, row in board.items()}, expected)
        self.assertEqual(board[b.player_id]["rank"], 3)
        self.assertEqual(board[a.player_id]["correct"], TOTAL_ROUNDS)
        winners = {w["player_id"] for w in over[0]["winners"]}
        self.assertTrue(winners and winners <= {a.player_id, c.player_id})
        self.assertEqual(over[0]["is_tie"], len(winners) > 1)

        # UDP: countdown ticks + live notices arrived, in strictly increasing seq order
        for p in (a, b, c):
            self.assertTrue(any(t["phase"] == "QUESTION" for t in p.ticks), f"{p.name} got no UDP ticks")
            self.assertTrue(any(t["phase"] == "STARTING" for t in p.ticks))
            seqs = [m["seq"] for m in sorted(p.ticks + p.notices, key=lambda m: m["seq"])]
            self.assertEqual(len(seqs), len(set(seqs)))
            self.assertGreater(len(p.notices), 0)

        # Room is FINISHED; only the host may Play Again; then it is WAITING again.
        a.wait_for(P.ROOM_UPDATE, pred=lambda m: m["data"]["room"]["status"] == RoomStatus.FINISHED, timeout=3)
        self.assertEqual(b.request(P.PLAY_AGAIN, {"room_id": room_id})["error"]["code"], ErrorCode.NOT_HOST)
        snap = a.ok(P.PLAY_AGAIN, {"room_id": room_id})["room"]
        self.assertEqual(snap["status"], RoomStatus.WAITING)
        self.assertEqual([p["ready"] for p in snap["players"]], [True, False, False])

    def test_04_late_answer_and_leaving_mid_game(self):
        a, b, c = self.players(3)
        self.start([a, b, c])
        q = a.wait_for(P.QUESTION, timeout=8)["data"]
        res = a.wait_for(P.ROUND_RESULT, timeout=ROUND_S + 3)["data"]        # nobody answers: timeout
        self.assertEqual({r["outcome"] for r in res["results"]}, {"no_answer"})
        self.assertEqual(self.submit(a, q, self.right_and_wrong(q)[0])["error"]["code"], ErrorCode.ROUND_CLOSED)

        a.ok(P.LEAVE_ROOM, {"room_id": q["room_id"]})          # 2 players left: game continues
        b.wait_for(P.PLAYER_STATUS, pred=lambda m: m["data"]["player_id"] == a.player_id, timeout=3)
        b.ok(P.LEAVE_ROOM, {"room_id": q["room_id"]})          # 1 player left: game ends
        over = c.wait_for(P.GAME_OVER, timeout=8)["data"]
        self.assertEqual(over["reason"], "not_enough_players")
        self.assertEqual([w["player_id"] for w in over["winners"]], [c.player_id])
        board = {r["player_id"]: r for r in over["leaderboard"]}
        self.assertEqual(board[c.player_id]["rank"], 1)
        self.assertFalse(board[a.player_id]["finished"])
        self.assertEqual(board[a.player_id]["status"], "left")

    def test_05_unexpected_disconnect_then_reconnect(self):
        a, b, c = self.players(3)
        self.start([a, b, c])
        q1 = a.wait_for(P.QUESTION, timeout=8)["data"]
        b.wait_for(P.QUESTION, timeout=8)
        c_name = c.username
        c.close(hard=True)                                     # crash: TCP RST, no LOGOUT
        st = a.wait_for(P.PLAYER_STATUS, pred=lambda m: m["data"]["player_id"] == c.player_id, timeout=5)
        self.assertEqual(st["data"]["status"], PlayerStatus.DISCONNECTED)
        for p in (a, b):
            resp = self.submit(p, q1, self.right_and_wrong(q1)[0])
            self.assertTrue(resp["ok"], f"{p.name}: {resp}")
        res = a.wait_for(P.ROUND_RESULT, timeout=8)["data"]
        self.assertLess(len([t for t in a.ticks if t.get("round_id") == q1["round_id"]
                             and t["phase"] == "QUESTION"]), ROUND_S + 1)
        outcome = {r["player_id"]: r["outcome"] for r in res["results"]}
        self.assertEqual(outcome[c.player_id], "disconnected")  # round closed early: connected players answered

        # c logs in again from a new connection and is re-attached to the running game.
        c2 = self.client()
        login = c2.login(c_name, "pass1234")
        self.assertIsNotNone(login["room"])
        sync = c2.wait_for(P.GAME_SYNC, timeout=3)["data"]
        self.assertEqual(sync["game_id"], res["game_id"])
        self.assertEqual(sync["last_round_result"]["round_id"], q1["round_id"])
        self.assertEqual(a.wait_for(P.PLAYER_STATUS, pred=lambda m: m["data"]["status"] == PlayerStatus.RECONNECTED,
                                    timeout=3)["data"]["player_id"], c2.player_id)
        for _ in range(TOTAL_ROUNDS - 1):
            q = a.wait_for(P.QUESTION, timeout=8)["data"]
            c2.wait_for(P.QUESTION, pred=lambda m: m["data"]["round_id"] == q["round_id"], timeout=8)
            b.wait_for(P.QUESTION, timeout=8)
            for p in (a, b, c2):
                self.submit(p, q, self.right_and_wrong(q)[0])
            a.wait_for(P.ROUND_RESULT, timeout=8)
        over = c2.wait_for(P.GAME_OVER, timeout=8)["data"]
        board = {r["player_id"]: r for r in over["leaderboard"]}
        self.assertTrue(board[c2.player_id]["finished"])
        self.assertEqual(board[c2.player_id]["correct"], TOTAL_ROUNDS - 1)

    def test_06_two_disconnects_end_the_game(self):
        a, b, c = self.players(3)
        room_id = self.start([a, b, c])
        a.wait_for(P.QUESTION, timeout=8)
        b.close(hard=True)
        c.close()                                              # graceful FIN, still no LOGOUT
        over = a.wait_for(P.GAME_OVER, timeout=8)["data"]
        self.assertEqual(over["reason"], "not_enough_players")
        self.assertEqual([w["player_id"] for w in over["winners"]], [a.player_id])
        upd = a.wait_for(P.ROOM_UPDATE, pred=lambda m: m["data"]["room"]["status"] == RoomStatus.FINISHED, timeout=3)
        self.assertEqual([p["player_id"] for p in upd["data"]["room"]["players"]], [a.player_id])
        self.assertTrue(a.ok(P.PING))                          # the server is still fine
        self.assertEqual(a.ok(P.PLAY_AGAIN, {"room_id": room_id})["room"]["status"], RoomStatus.WAITING)

    def test_07_heartbeat_timeout_drops_silent_client(self):
        watcher = self.player()
        silent = self.player(prefix="silent", heartbeat=False)
        self.assertTrue(silent.server_closed.wait(timeout=HEARTBEAT_TIMEOUT_S + 3),
                        "server should close a client that stops sending")
        watcher.wait_for(P.LOBBY_UPDATE, pred=lambda m: silent.player_id not in
                         [p["player_id"] for p in m["data"]["online_players"]], timeout=3)
        deadline = time.monotonic() + 3                    # the log line is written just after the close
        while "heartbeat timeout" not in self.server.log() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("heartbeat timeout", self.server.log())

    def test_08_same_account_login_takes_over(self):
        first = self.player()
        second = self.client()
        second.login(first.username, "pass1234")
        self.assertEqual(first.wait_for(P.KICKED, timeout=3)["type"], P.KICKED)
        self.assertTrue(first.server_closed.wait(timeout=3))
        self.assertTrue(second.ok(P.LIST_ROOMS) is not None)
        online = [p["username"] for p in second.ok(P.LIST_ROOMS)["online_players"]]
        self.assertEqual(online.count(first.username), 1)

    def test_09_three_bot_processes_play_a_full_match(self):
        """Three separate client PROCESSES (as on three computers) play one game."""
        tag = uuid.uuid4().hex[:6]
        room = f"Proc{tag}"
        common = [sys.executable, "-m", "tests.bot_client", "--host", "127.0.0.1", "--port", str(self.server.port),
                  "--allow-loopback", "--min-delay", "0.1", "--max-delay", "0.6", "--wait", "30"]
        env = dict(self.server.env)
        users = [f"host{tag}", f"p2{tag}", f"p3{tag}"]
        procs = [subprocess.Popen(common + ["--user", users[0], "--create", room], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)]
        time.sleep(0.5)
        for u in users[1:]:
            procs.append(subprocess.Popen(common + ["--user", u, "--join", "auto", "--room-name", room], cwd=ROOT,
                                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        outputs = [p.communicate(timeout=60)[0] for p in procs]
        for p, out in zip(procs, outputs):
            self.assertEqual(p.returncode, 0, out)
        results = [json.loads(re.search(r"^BOT_RESULT (.*)$", out, re.M).group(1)) for out in outputs]
        self.assertEqual(len({r["game_id"] for r in results}), 1)
        self.assertEqual(len({json.dumps(r["winners"]) for r in results}), 1)
        self.assertTrue(all(r["udp_ticks"] > 0 and r["udp_acks"] > 0 for r in results))
        log = self.server.log()
        for u in users:                                         # one dedicated handler thread per player
            self.assertIn(f"[player-handler-{u}]", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
