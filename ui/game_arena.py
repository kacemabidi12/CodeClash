"""Page 4 — game arena: round progress, countdown, question, answer, live scoreboard."""

from __future__ import annotations

import inspect

import streamlit as st

from common.protocol import AnswerKind, Phase
from ui import theme

# Newer Streamlit sizes radios to their content by default; stretch the answer cards when supported.
_RADIO_WIDTH = {"width": "stretch"} if "width" in inspect.signature(st.radio).parameters else {}


def pips(total: int, done: set[int], now: int | None) -> str:
    cells = []
    for n in range(1, total + 1):
        cls = "now" if n == now else ("done" if n in done else "")
        cells.append(f'<span class="{cls}" title="Round {n}">{n}</span>')
    return f'<div class="cc-pips" aria-label="Rounds">{"".join(cells)}</div>'


@st.fragment(run_every=0.25)
def clock_fragment(net, label: str, small: bool = False) -> None:
    theme.html_block(theme.clock(net.countdown(), label, small=small))


@st.fragment(run_every=0.5)
def live_side(net) -> None:
    s = net.snapshot()
    game, room, me = s["game"], s["room"], s["me"]
    if not game or not me:
        return
    connected = {p["player_id"]: p["connected"] for p in (room or {}).get("players", [])}
    with theme.panel("scoreboard"):
        theme.heading("Scoreboard")
        theme.html_block(theme.scoreboard(game["scoreboard"], me["player_id"], connected))
    live = game.get("live") or {}
    if game["phase"] == Phase.QUESTION and live:
        active = live.get("active") or sum(1 for v in connected.values() if v) or len(game["players"])
        answered = live.get("answered") or 0
        dots = "".join(f'<i class="{"on" if i < answered else ""}"></i>' for i in range(active))
        if not answered:
            last = "Nobody has answered yet."
        elif live.get("last"):
            last = f'{theme.esc(live["last"])} just locked in.'
        else:
            last = f"{answered} locked in so far."
        with theme.panel("this-round"):
            theme.heading("This round", f"{answered} of {active} answered")
            theme.html_block(f'<div class="cc-seats">{dots}</div><p class="cc-muted" style="margin:.5rem 0 0">{last}</p>')
    with theme.panel("activity-live"):
        theme.heading("Activity")
        theme.html_block(theme.feed(s["notices"], limit=5))


def _get_ready(net, game: dict) -> None:
    with theme.panel("get-ready"):
        theme.title("Get ready", f"{game['total_rounds']} rounds against "
                    + ", ".join(p["username"] for p in game["players"]) + ".")
        clock_fragment(net, "First question opens in")


def _answer_area(net, game: dict, question: dict) -> None:
    sub = game.get("submission")
    sub = sub if sub and sub["round_id"] == question["round_id"] else None
    options = {o["key"]: o["text"] for o in question.get("options", [])}
    if sub and sub.get("accepted"):
        answer = sub.get("answer") or ""
        shown = f"{answer} — {options[answer]}" if answer in options else answer
        when = f" {sub['response_time_ms'] / 1000:.1f} s after the question opened" if sub.get("response_time_ms") is not None else ""
        theme.html_block(f'<div class="cc-locked">Answer locked in: <b>{theme.esc(shown)}</b>.<br>'
                         f'<span class="cc-muted">Received by the server{when}. The correct answer is revealed '
                         f'to everyone when the round closes.</span></div>')
        return
    round_id = question["round_id"]
    if question["answer_kind"] == AnswerKind.CHOICE:
        choice = st.radio("Your answer", list(options), index=None, key=f"choice_{round_id}",
                          format_func=lambda k: f"{k}    {options[k]}", label_visibility="collapsed", **_RADIO_WIDTH)
        if st.button("Lock in answer", type="primary", key=f"submit_{round_id}",
                     disabled=choice is None or bool(sub and sub.get("pending"))):
            response = net.submit_answer(choice)
            if response["ok"]:
                st.rerun()
            st.error(response["error"]["message"])
    else:
        with st.form(f"text_form_{round_id}", border=False):
            text = st.text_input("Type your answer exactly", key=f"text_{round_id}", max_chars=200,
                                 placeholder="Your answer")
            go = st.form_submit_button("Lock in answer", type="primary")
        if go:
            if not text.strip():
                st.error("Type an answer first.")
            else:
                response = net.submit_answer(text.strip())
                if response["ok"]:
                    st.rerun()
                st.error(response["error"]["message"])
    st.caption("Your first locked-in answer is final.")


def render(net, snap: dict) -> None:
    game = snap["game"]
    if game is None:
        theme.title("Joining the game…", "Waiting for the server to send the current round.")
        return
    question = game.get("question") if game["phase"] == Phase.QUESTION else None
    done = {r["round_number"] for r in game["results"]}

    head_left, head_right = st.columns([2.2, 1], vertical_alignment="bottom", gap="large")
    with head_left:
        if question:
            theme.html_block(f'<span class="cc-cat"><i></i>{theme.esc(question["category"])}</span>'
                             f'<div class="cc-title">Round {question["round_number"]} of {game["total_rounds"]}</div>')
        else:
            theme.html_block(f'<div class="cc-title">Game {theme.esc(game["game_id"])}</div>')
        theme.html_block(pips(game["total_rounds"], done, question["round_number"] if question else None))
    with head_right:
        if question:
            clock_fragment(net, "Time left")
    st.write("")

    main, side = st.columns([2.2, 1], gap="large")
    with main:
        if not question:
            _get_ready(net, game)
        else:
            with theme.panel("question"):
                theme.html_block(f'<div class="cc-prompt">{theme.esc(question["prompt"])}</div>')
                if question.get("code"):
                    st.code(question["code"], language="python")
                _answer_area(net, game, question)
    with side:
        live_side(net)
