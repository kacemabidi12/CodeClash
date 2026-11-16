"""Page 5 — round results, and Page 6 — final results."""

from __future__ import annotations

import streamlit as st

from common.protocol import AnswerKind, GameEndReason, RoomStatus
from ui import theme
from ui.game_arena import clock_fragment, pips

OUTCOME_TEXT = {"no_answer": "No answer", "disconnected": "Disconnected", "left": "Left the game"}
END_TEXT = {
    GameEndReason.COMPLETED: "Final result after {n} rounds.",
    GameEndReason.NOT_ENOUGH_PLAYERS: "The game ended early after {n} rounds: fewer than two players were still connected.",
    GameEndReason.SERVER_SHUTDOWN: "The server was stopped, so the game ended after {n} rounds.",
}


def _result_rows(result: dict, me_id: str) -> str:
    options = {o["key"]: o["text"] for o in result.get("options", [])}
    rows = []
    for r in sorted(result["results"], key=lambda r: (-r["points"], r["response_time_ms"] or 10**9)):
        if r["outcome"] == "answered":
            ans = r["answer"]
            shown = f"{ans} — {options[ans]}" if ans in options else ans
            meta = f"Answered {shown} in {r['response_time_ms'] / 1000:.1f} s"
            mark = '<span class="cc-mark ok">Correct</span>' if r["correct"] else '<span class="cc-mark no">Wrong</span>'
        else:
            meta, mark = OUTCOME_TEXT.get(r["outcome"], r["outcome"]), '<span class="cc-mark no">—</span>'
        you = " (you)" if r["player_id"] == me_id else ""
        detail = f'{r["base_points"]} + {r["bonus_points"]} speed' if r["points"] else ""
        rows.append(f'<div class="cc-result"><div>{theme.person(r["username"], meta, 2, you)}</div>{mark}'
                    f'<div><div class="pts{" zero" if not r["points"] else ""}">+{r["points"]}</div>'
                    f'<div class="detail">{detail}</div></div></div>')
    return "".join(rows)


def _option_breakdown(result: dict) -> str:
    if result["answer_kind"] != AnswerKind.CHOICE:
        return ""
    pickers: dict[str, list[str]] = {}
    for r in result["results"]:
        if r["outcome"] == "answered":
            pickers.setdefault(r["answer"], []).append(r["username"])
    out = []
    for o in result["options"]:
        names = "".join(theme.chip(n) for n in pickers.get(o["key"], []))
        cls = "cc-option correct" if o["key"] == result["correct_answer"] else "cc-option"
        out.append(f'<div class="{cls}"><span>{theme.esc(o["key"])}&nbsp;&nbsp;{theme.esc(o["text"])}</span>'
                   f'<span class="pickers">{names}</span></div>')
    return "".join(out)


def render_round(net, snap: dict) -> None:
    game, me = snap["game"], snap["me"]
    result = game["result"]
    done = {r["round_number"] for r in game["results"]}

    head_left, head_right = st.columns([2.2, 1], vertical_alignment="bottom", gap="large")
    with head_left:
        theme.html_block(f'<span class="cc-cat"><i></i>{theme.esc(result["category"])}</span>'
                         f'<div class="cc-title">Round {result["round_number"]} of {result["total_rounds"]} — results</div>')
        theme.html_block(pips(result["total_rounds"], done, None))
    with head_right:
        if result.get("next_round_in_s"):
            clock_fragment(net, "Next question in", small=True)
        else:
            theme.html_block('<p class="cc-muted">Final results are on their way.</p>')
    st.write("")

    main, side = st.columns([2.2, 1], gap="large")
    with main:
        with theme.panel("reveal"):
            theme.html_block(f'<div class="cc-prompt">{theme.esc(result["prompt"])}</div>')
            if result.get("code"):
                st.code(result["code"], language="python")
            answer = result["correct_answer"]
            shown = f'{answer} — {result["correct_text"]}' if result["answer_kind"] == AnswerKind.CHOICE else answer
            theme.html_block(f'<div class="cc-reveal"><div class="cc-muted">Correct answer</div>'
                             f'<div class="ans">{theme.esc(shown)}</div>'
                             f'<p style="margin:.5rem 0 0">{theme.esc(result["explanation"])}</p></div>')
            breakdown = _option_breakdown(result)
            if breakdown:
                st.write("")
                theme.html_block(breakdown)
        with theme.panel("points"):
            theme.heading("Points this round")
            theme.html_block(_result_rows(result, me["player_id"]))
    with side:
        connected = {p["player_id"]: p["connected"] for p in (snap["room"] or {}).get("players", [])}
        with theme.panel("ranking"):
            theme.heading("Ranking")
            theme.html_block(theme.scoreboard(result["scoreboard"], me["player_id"], connected))
        with theme.panel("activity-results"):
            theme.heading("Activity")
            theme.html_block(theme.feed(snap["notices"], limit=5))


def render_final(net, snap: dict) -> None:
    game, me, room = snap["game"], snap["me"], snap["room"]
    over = game["over"]
    winners = over["winners"]
    names = [w["username"] for w in winners]
    i_won = me["player_id"] in {w["player_id"] for w in winners}
    if not winners:
        headline = "No winner this time"
    elif over["is_tie"]:
        headline = "You share the win" if i_won else f"Tie between {' and '.join(names)}"
    else:
        headline = "You win" if i_won else f"{names[0]} wins"
    sub = END_TEXT.get(over["reason"], "Game over.").format(n=over["rounds_completed"])
    theme.html_block(f'<div class="cc-winner"><div class="crown">🏆</div><div class="headline" role="heading" aria-level="1">{theme.esc(headline)}</div>'
                     f'<p class="cc-sub">{theme.esc(sub)}</p></div>')

    main, side = st.columns([2.2, 1], gap="large")
    with main:
        theme.heading("Final leaderboard")
        rounds = max(1, over["rounds_completed"])
        rows = []
        for r in over["leaderboard"]:
            accuracy = round(100 * r["correct"] / rounds)
            status = "Finished" if r["finished"] else ("Left the game" if r["status"] == "left" else "Disconnected")
            you = " (you)" if r["player_id"] == me["player_id"] else ""
            avg = f'{r["avg_response_ms"] / 1000:.1f} s' if r.get("avg_response_ms") is not None else "—"
            rows.append(
                f'<div class="cc-podium{" first" if r["rank"] == 1 and r["finished"] else ""}">'
                f'<div class="rank">{r["rank"]}</div>'
                f'<div>{theme.person(r["username"], status, 2.3, you)}'
                f'<div class="cc-bar" title="{accuracy}% correct"><div style="width:{accuracy}%"></div></div></div>'
                f'<div class="stat"><b>{r["score"]}</b><span>points</span></div>'
                f'<div class="stat"><b>{r["correct"]}/{over["rounds_completed"]}</b><span>correct</span></div>'
                f'<div class="stat opt"><b>{avg}</b><span>avg answer</span></div></div>')
        theme.html_block("".join(rows))

        st.write("")
        is_host = room is not None and room["host_id"] == me["player_id"]
        a, b = st.columns(2, gap="medium")
        with a:
            if is_host:
                ready = room["status"] == RoomStatus.FINISHED
                if st.button("Play again", type="primary", key="play_again", disabled=not ready,
                             use_container_width=True):
                    response = net.play_again()
                    if response["ok"]:
                        st.rerun()
                    st.error(response["error"]["message"])
            else:
                st.button("Waiting for the host to restart", disabled=True, key="wait_restart",
                          use_container_width=True)
        with b:
            if st.button("Back to lobby", key="back_lobby", use_container_width=True):
                net.leave_room()
                st.rerun()

    with side:
        with theme.panel("history"):
            theme.heading("Round by round")
            lines = []
            for r in game["results"]:
                mine = next((x for x in r["results"] if x["player_id"] == me["player_id"]), None)
                mark = ("+" + str(mine["points"])) if mine and mine["correct"] else "0"
                correct_n = sum(1 for x in r["results"] if x["correct"])
                lines.append(f'<div class="cc-history"><div><div class="t">Round {r["round_number"]}: '
                             f'{theme.esc(r["category"])}</div><div class="s">{correct_n} of {len(r["results"])} '
                             f'players correct, answer {theme.esc(r["correct_answer"])}</div></div>'
                             f'<div class="p">{mark}</div></div>')
            theme.html_block("".join(lines) or '<span class="cc-muted">No completed rounds.</span>')
