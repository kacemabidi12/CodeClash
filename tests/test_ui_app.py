"""
Phase 4/5 test: the real Streamlit app (app.py) driven by Streamlit's own test runner (AppTest),
connected over real sockets to a real server process. Two BotClients fill the other seats.

It walks the whole player journey: connect -> create account -> lobby -> create room ->
waiting room -> start -> answer every round -> round results -> final results -> play again.

Run from the project root (needs `pip install streamlit`):
    python -m unittest -v tests.test_ui_app
"""

from __future__ import annotations

import time
import unittest
import uuid
from pathlib import Path

import config
from common import protocol as P
from server.questions import QUESTIONS_BY_ID
from tests.bot_client import BotClient
from tests.test_server_integration import ROOT, ServerProcess

try:
    import streamlit as st
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover - the server computer does not need Streamlit
    AppTest = None

ROUNDS = 3


def page_text(at) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [e.value for e in at.error] + [b.label for b in at.button]
    return "\n".join(str(p) for p in parts)


def run_until(at, predicate, timeout: float = 15.0):
    """Re-run the script until the rendered page satisfies predicate (server events are asynchronous)."""
    deadline = time.monotonic() + timeout
    while True:
        at.run()
        if at.exception:
            raise AssertionError(f"app raised: {at.exception[0].value}")
        if predicate(page_text(at)):
            return at
        if time.monotonic() > deadline:
            raise AssertionError("page never matched; last page:\n" + page_text(at)[:1500])
        time.sleep(0.25)


def click(at, label: str) -> None:
    buttons = [b for b in at.button if b.label == label and not b.disabled]
    if not buttons:
        raise AssertionError(f"no enabled button {label!r}; buttons: {[b.label for b in at.button]}")
    buttons[0].click().run()            # AppTest only executes the click on the next run


@unittest.skipIf(AppTest is None, "streamlit is not installed")
class StreamlitAppTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls._old = (config.ALLOW_LOOPBACK, config.CLIENT_SETTINGS_FILE)
        config.ALLOW_LOOPBACK = True
        cls.server = ServerProcess(CODECLASH_TOTAL_ROUNDS=str(ROUNDS), CODECLASH_ROUND_DURATION_S="12",
                                   CODECLASH_RESULTS_DURATION_S="2", CODECLASH_HEARTBEAT_TIMEOUT_S="15")
        config.CLIENT_SETTINGS_FILE = Path(cls.server.tmp.name) / "client_settings.json"
        st.cache_resource.clear()                       # a fresh NetworkManager for this test process

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        config.ALLOW_LOOPBACK, config.CLIENT_SETTINGS_FILE = cls._old

    def test_full_player_journey(self):
        bots: list[BotClient] = []
        try:
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20)
            at.run()
            self.assertIn("Connect to the game server", page_text(at))

            # ---- connect + create account
            at.text_input(key="server_host").set_value("127.0.0.1")
            at.number_input(key="server_port").set_value(self.server.port)
            click(at, "Connect")
            run_until(at, lambda t: "Create account and log in" in t)
            name = "ui" + uuid.uuid4().hex[:6]
            at.text_input(key="reg_user").set_value(name)
            at.text_input(key="reg_pass").set_value("pass1234")
            at.text_input(key="reg_pass2").set_value("pass1234")
            click(at, "Create account and log in")
            run_until(at, lambda t: f"Welcome, {name}" in t)

            # ---- create a room; two bots join and get ready
            at.text_input(key="new_room_name").set_value("UI arena")
            click(at, "Create room")
            run_until(at, lambda t: "Open seat" in t)
            for i in range(2):
                b = BotClient("127.0.0.1", self.server.port, name=f"uibot{i}").connect()
                b.register_and_login("uibot" + uuid.uuid4().hex[:6], "pass1234")
                room_id = next(r["room_id"] for r in b.ok(P.LIST_ROOMS)["rooms"] if r["name"] == "UI arena")
                b.ok(P.JOIN_ROOM, {"room_id": room_id})
                b.ok(P.SET_READY, {"room_id": room_id, "ready": True})
                bots.append(b)
            run_until(at, lambda t: "Everyone is ready." in t)
            click(at, "Start game")

            # ---- play every round: the UI player answers correctly, the bots answer "A"
            for number in range(1, ROUNDS + 1):
                q = bots[0].wait_for(P.QUESTION, timeout=15)["data"]
                bots[1].wait_for(P.QUESTION, timeout=15)
                run_until(at, lambda t: f"Round {number} of {ROUNDS}" in t and "Lock in answer" in t)
                key = QUESTIONS_BY_ID[q["question_id"]]
                if key.kind == "choice":
                    at.radio(key=f"choice_{q['round_id']}").set_value(key.answer)
                else:
                    at.text_input(key=f"text_{q['round_id']}").set_value(key.answer)
                at.run()                        # apply the selection (enables the button)
                click(at, "Lock in answer")
                run_until(at, lambda t: "Answer locked in" in t)
                for b in bots:
                    b.request(P.SUBMIT_ANSWER, {"room_id": q["room_id"], "round_id": q["round_id"],
                                                "question_id": q["question_id"], "answer": "A",
                                                "submission_id": P.new_id()})
                if number < ROUNDS:
                    text = run_until(at, lambda t: "results" in t and "Points this round" in t)
                    self.assertIn("Correct answer", page_text(text))

            # ---- final results, then Play Again back to the waiting room
            run_until(at, lambda t: "Final leaderboard" in t, timeout=20)
            text = page_text(at)
            self.assertTrue("You win" in text or "You share the win" in text, text[:600])
            self.assertIn(f"{ROUNDS}/{ROUNDS}", text)
            run_until(at, lambda t: any(b.label == "Play again" and not b.disabled for b in at.button))
            click(at, "Play again")
            run_until(at, lambda t: "Start game" in t and "Final leaderboard" not in t)
        finally:
            for b in bots:
                b.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
