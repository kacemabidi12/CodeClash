"""
Phase 3/5 tests: the client networking layer (client/network_manager.py) against a REAL server
process over real TCP and UDP sockets. Two BotClients play the other seats.

Run from the project root:
    python3 -m unittest -v tests.test_network_manager
"""

from __future__ import annotations

import time
import unittest
import uuid
from pathlib import Path

import config
from client.network_manager import NOT_CONNECTED, NetworkManager
from common import protocol as P
from common.protocol import ErrorCode, Phase, RoomStatus
from server.questions import QUESTIONS_BY_ID
from tests.bot_client import BotClient
from tests.test_server_integration import TOTAL_ROUNDS, ServerProcess


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not reached in time")


class NetworkManagerTests(unittest.TestCase):
    server: ServerProcess

    @classmethod
    def setUpClass(cls) -> None:
        cls._old_loopback, cls._old_settings = config.ALLOW_LOOPBACK, config.CLIENT_SETTINGS_FILE
        config.ALLOW_LOOPBACK = True                 # tests run the server on this same machine
        cls.server = ServerProcess()
        config.CLIENT_SETTINGS_FILE = Path(cls.server.tmp.name) / "client_settings.json"   # keep the project clean

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        config.ALLOW_LOOPBACK, config.CLIENT_SETTINGS_FILE = cls._old_loopback, cls._old_settings

    def setUp(self) -> None:
        self.managers: list[NetworkManager] = []
        self.bots: list[BotClient] = []

    def tearDown(self) -> None:
        for nm in self.managers:
            nm.disconnect()
        for b in self.bots:
            b.close()

    # ------------------------------------------------------------------ helpers
    def manager(self) -> NetworkManager:
        nm = NetworkManager()
        ok, message = nm.connect("127.0.0.1", self.server.port)
        self.assertTrue(ok, message)
        self.managers.append(nm)
        return nm

    def login(self, nm: NetworkManager) -> str:
        name = "nm" + uuid.uuid4().hex[:8]
        self.assertTrue(nm.register(name, "pass1234")["ok"])
        self.assertTrue(nm.login(name, "pass1234")["ok"])
        return name

    def bot(self) -> BotClient:
        b = BotClient("127.0.0.1", self.server.port, name=f"bot{len(self.bots)}").connect()
        b.register_and_login("bot" + uuid.uuid4().hex[:8], "pass1234")
        self.bots.append(b)
        return b

    def full_room(self, nm: NetworkManager) -> tuple[str, list[BotClient]]:
        room_id = nm.create_room("NM test")["data"]["room"]["room_id"]
        bots = [self.bot(), self.bot()]
        for b in bots:
            b.ok(P.JOIN_ROOM, {"room_id": room_id})
            b.ok(P.SET_READY, {"room_id": room_id, "ready": True})
        wait_until(lambda: len(nm.snapshot()["room"]["players"]) == 3)
        return room_id, bots

    @staticmethod
    def bots_answer(bots: list[BotClient], room_id: str) -> None:
        for b in bots:
            q = b.wait_for(P.QUESTION, timeout=8)["data"]
            b.request(P.SUBMIT_ANSWER, {"room_id": room_id, "round_id": q["round_id"],
                                        "question_id": q["question_id"],
                                        "answer": "A" if q["answer_kind"] == "choice" else "wrong",
                                        "submission_id": P.new_id()})

    # ================================================================== tests
    def test_connect_errors_are_explained(self):
        nm = NetworkManager()
        self.managers.append(nm)
        ok, message = nm.connect("0.0.0.0", self.server.port)
        self.assertFalse(ok)
        self.assertIn("bind address", message)
        ok, message = nm.connect("127.0.0.1", 1)                 # nothing listens on port 1
        self.assertFalse(ok)
        self.assertIn("refused", message)
        self.assertEqual(nm.request(P.PING)["error"]["code"], NOT_CONNECTED)

    def test_login_udp_and_lobby_mirror(self):
        nm = self.manager()
        snap = nm.snapshot()
        self.assertEqual(snap["connection"]["status"], "connected")
        self.assertIsNotNone(snap["connection"]["welcome"])
        name = self.login(nm)
        wait_until(lambda: nm.snapshot()["udp"]["ok"], timeout=5)         # UDP_HELLO answered by UDP_ACK
        snap = nm.snapshot()
        self.assertEqual(snap["me"]["username"], name)
        self.assertIn(name, [p["username"] for p in snap["lobby"]["online_players"]])
        bad = nm.login(name, "pass1234")
        self.assertEqual(bad["error"]["code"], ErrorCode.ALREADY_AUTHENTICATED)
        other = self.bot()
        other.ok(P.CREATE_ROOM, {"room_name": "Pushed"})
        wait_until(lambda: any(r["name"] == "Pushed" for r in nm.snapshot()["lobby"]["rooms"]))   # LOBBY_UPDATE push

    def test_full_game_through_the_manager(self):
        nm = self.manager()
        me = self.login(nm)
        room_id, bots = self.full_room(nm)
        self.assertEqual(nm.start_game()["data"]["game_id"][0], "G")
        wait_until(lambda: (nm.snapshot()["game"] or {}).get("phase") in (Phase.STARTING, Phase.QUESTION))

        for number in range(1, TOTAL_ROUNDS + 1):
            wait_until(lambda: (nm.snapshot()["game"] or {}).get("phase") == Phase.QUESTION
                       and nm.snapshot()["game"]["question"]["round_number"] == number)
            question = nm.snapshot()["game"]["question"]
            self.assertNotIn("answer", question)
            countdown = wait_until(lambda: (c := nm.countdown()) and c["source"] == "udp" and c, timeout=3)
            self.assertLessEqual(countdown["seconds_left"], question["duration_s"])
            key = QUESTIONS_BY_ID[question["question_id"]].answer
            first = nm.submit_answer(key)
            self.assertTrue(first["ok"] and not first["data"]["duplicate"])
            again = nm.submit_answer(key)                           # double click: answered locally, no re-score
            self.assertTrue(again["data"]["duplicate"])
            self.bots_answer(bots, room_id)
            wait_until(lambda: nm.snapshot()["game"]["phase"] in (Phase.RESULTS, Phase.FINISHED))
            result = nm.snapshot()["game"]["result"]
            mine = next(r for r in result["results"] if r["username"] == me)
            self.assertTrue(mine["correct"])
            self.assertGreaterEqual(mine["points"], 11)

        over = wait_until(lambda: nm.snapshot()["game"].get("over"))
        self.assertEqual(over["reason"], "completed")
        self.assertIn(me, [w["username"] for w in over["winners"]])           # correct every round, fastest or tied
        wait_until(lambda: nm.snapshot()["room"]["status"] == RoomStatus.FINISHED)
        self.assertGreater(nm.snapshot()["udp"]["ticks"], 0)

        self.assertTrue(nm.play_again()["ok"])
        snap = nm.snapshot()
        self.assertEqual(snap["room"]["status"], RoomStatus.WAITING)
        self.assertIsNone(snap["game"])
        self.assertTrue(nm.leave_room()["ok"])
        self.assertIsNone(nm.snapshot()["room"])

    def test_lost_connection_then_reconnect_into_running_game(self):
        nm = self.manager()
        self.login(nm)
        room_id, bots = self.full_room(nm)
        nm.start_game()
        wait_until(lambda: (nm.snapshot()["game"] or {}).get("phase") == Phase.QUESTION)
        round_id = nm.snapshot()["game"]["question"]["round_id"]

        nm._tcp.close("simulated network failure")                  # not user-initiated
        snap = wait_until(lambda: (s := nm.snapshot())["connection"]["status"] == "disconnected" and s)
        self.assertTrue(snap["connection"]["lost"])                   # UI shows the Reconnect page
        self.assertIsNotNone(snap["me"])
        status = bots[0].wait_for(P.PLAYER_STATUS, timeout=5)["data"]
        self.assertEqual(status["status"], "DISCONNECTED")

        ok, message = nm.reconnect()
        self.assertTrue(ok, message)
        snap = wait_until(lambda: (s := nm.snapshot())["game"] and s)  # rebuilt from GAME_SYNC
        self.assertEqual(snap["room"]["room_id"], room_id)
        game = snap["game"]
        self.assertIn(game["phase"], (Phase.QUESTION, Phase.RESULTS, Phase.FINISHED))
        if game["phase"] == Phase.QUESTION and game["question"]["round_id"] == round_id:
            response = nm.submit_answer("A")                       # the same round is still open: we can answer
            self.assertTrue(response["ok"] or response["error"]["code"] == ErrorCode.ROUND_CLOSED, response)
        self.assertFalse(nm.snapshot()["connection"]["lost"])

    def test_user_disconnect_is_not_a_lost_connection(self):
        nm = self.manager()
        self.login(nm)
        nm.disconnect()
        snap = nm.snapshot()
        self.assertEqual(snap["connection"]["status"], "disconnected")
        self.assertFalse(snap["connection"]["lost"])
        self.assertIsNone(snap["me"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
