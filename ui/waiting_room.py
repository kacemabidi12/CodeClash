"""Page 3 — waiting room: three seats, ready status, host controls."""

from __future__ import annotations

import streamlit as st

import config
from common.protocol import RoomStatus
from ui import theme


def _seat(player: dict | None, me_id: str) -> str:
    if player is None:
        return '<div class="cc-seat empty"><div class="who">Open seat</div><div>Waiting for a player to join</div></div>'
    tags = []
    if player["is_host"]:
        tags.append(theme.chip("Host", "violet"))
    tags.append(theme.chip("Ready", "good") if player["ready"] else theme.chip("Not ready", "warn"))
    if not player["connected"]:
        tags.append(theme.chip("Offline", "bad"))
    classes = "cc-seat" + (" me" if player["player_id"] == me_id else "") + (" ready" if player["ready"] else "")
    you = " (you)" if player["player_id"] == me_id else ""
    return (f'<div class="{classes}">{theme.avatar(player["username"], 3.4)}'
            f'<div class="who">{theme.esc(player["username"])}{you}</div>'
            f'<div class="tags">{"".join(tags)}</div></div>')


def render(net, snap: dict) -> None:
    room, me = snap["room"], snap["me"]
    players = room["players"]
    me_row = next((p for p in players if p["player_id"] == me["player_id"]), None)
    is_host = room["host_id"] == me["player_id"]
    host = next((p["username"] for p in players if p["is_host"]), "—")
    finished = room["status"] == RoomStatus.FINISHED

    theme.title(room["name"], f"Room {room['room_id']}, hosted by {host}. "
                + ("The last game is over." if finished else
                   f"The game starts when {config.ROOM_CAPACITY} players are seated and everyone is ready."))

    cols = st.columns(config.ROOM_CAPACITY, gap="medium")
    for i, col in enumerate(cols):
        with col:
            theme.html_block(_seat(players[i] if i < len(players) else None, me["player_id"]))

    st.write("")
    main, side = st.columns([2, 1], gap="large")
    with main:
        not_ready = [p["username"] for p in players if not p["is_host"] and not p["ready"]]
        missing = config.ROOM_CAPACITY - len(players)
        if missing > 0:
            reason = f"Waiting for {missing} more player{'s' if missing > 1 else ''}."
        elif not_ready:
            reason = f"Waiting for {', '.join(not_ready)} to get ready."
        else:
            reason = "Everyone is ready."
        a, b = st.columns(2, gap="medium")
        with a:
            if finished:
                if is_host:
                    if st.button("Play again", type="primary", key="play_again_w", use_container_width=True):
                        _report(net.play_again())
                else:
                    st.button("Waiting for the host", disabled=True, key="wait_host", use_container_width=True)
            elif is_host:
                can_start = missing <= 0 and not not_ready
                if st.button("Start game", type="primary", key="start_game", disabled=not can_start,
                             use_container_width=True):
                    _report(net.start_game())
            else:
                ready = bool(me_row and me_row["ready"])
                if st.button("I'm not ready" if ready else "I'm ready", type="secondary" if ready else "primary",
                             key="toggle_ready", use_container_width=True):
                    _report(net.set_ready(not ready))
        with b:
            if st.button("Leave room", key="leave_room", use_container_width=True):
                _report(net.leave_room())
        st.caption(reason if not finished else "The host can start a new game with the same room.")

    with side:
        with theme.panel("rules"):
            theme.heading("How a game works")
            theme.html_block(
                f'<div class="cc-muted" style="line-height:1.6">{config.TOTAL_ROUNDS} rounds, '
                f'{config.ROUND_DURATION_S} seconds each. Everyone gets the same question at the same moment. '
                f'A correct answer earns {config.POINTS_CORRECT} points plus up to {config.SPEED_BONUS_MAX} for speed. '
                'Your first locked-in answer is final. Questions cover code output, logic, '
                'algorithms and debugging.</div>')
        with theme.panel("activity-wait"):
            theme.heading("Room activity")
            theme.html_block(theme.feed(snap["notices"]))


def _report(response: dict) -> None:
    if response["ok"]:
        st.rerun()
    else:
        st.error(response["error"]["message"])
