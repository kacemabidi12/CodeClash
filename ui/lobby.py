"""Page 2 — dashboard / lobby: profile, rooms, create or join a room, who is online."""

from __future__ import annotations

import streamlit as st

import config
from common.protocol import RoomStatus
from ui import theme

WHERE = {"lobby": "In the lobby", "in_room": "In a room", "in_game": "Playing"}


def _room_row(net, room: dict) -> None:
    with theme.panel(f"room-{room['room_id']}"):
        info, seats, action = st.columns([2.4, 1.3, 1], vertical_alignment="center")
        others = [n for n in room["player_names"] if n != room["host_name"]]
        with_text = f" with {', '.join(others)}" if others else ""
        with info:
            theme.html_block(
                f'<div class="cc-room"><span class="name">{theme.esc(room["name"])}</span>'
                f'<span class="meta">Room {theme.esc(room["room_id"])}, hosted by {theme.esc(room["host_name"] or "—")}'
                f'{theme.esc(with_text)}</span></div>')
        with seats:
            pips = "".join(f'<i class="{"on" if i < room["player_count"] else ""}"></i>' for i in range(room["capacity"]))
            theme.html_block(f'<div class="cc-seats">{pips}<span>{room["player_count"]}/{room["capacity"]}</span>'
                             f'</div><div style="margin-top:.35rem">{theme.room_status_chip(room["status"])}</div>')
        with action:
            full = room["player_count"] >= room["capacity"]
            can_join = room["status"] == RoomStatus.WAITING and not full
            label = "Join" if can_join else ("Full" if room["status"] == RoomStatus.WAITING else "Busy")
            if st.button(label, key=f"join_{room['room_id']}", disabled=not can_join, type="primary" if can_join else "secondary",
                         use_container_width=True):
                response = net.join_room(room["room_id"])
                if not response["ok"]:
                    st.error(response["error"]["message"])
                else:
                    st.rerun()


def render(net, snap: dict) -> None:
    me, lobby = snap["me"], snap["lobby"]
    rooms = lobby["rooms"]
    open_rooms = [r for r in rooms if r["status"] == RoomStatus.WAITING and r["player_count"] < r["capacity"]]
    theme.title(f"Welcome, {me['username']}",
                "Join a room with a free seat, or open your own and wait for two opponents.")

    main, side = st.columns([2, 1], gap="large")
    with main:
        head, refresh = st.columns([4, 1], vertical_alignment="center")
        with head:
            theme.heading("Rooms", f"{len(open_rooms)} with free seats, {len(rooms)} in total")
        with refresh:
            if st.button("Refresh", key="refresh_rooms", use_container_width=True):
                net.refresh_lobby()
                st.rerun()
        if not rooms:
            with theme.panel("empty-rooms"):
                theme.html_block('<p class="cc-muted" style="margin:.4rem 0">No rooms yet. Create one on the right, '
                                 'then ask your two opponents to join it from their computers.</p>')
        for room in rooms:
            _room_row(net, room)

    with side:
        conn, udp = snap["connection"], snap["udp"]
        with theme.panel("profile"):
            theme.html_block(theme.person(me["username"], f"Player ID {me['player_id']}", 2.8))
            local = conn.get("local_addr") or ("?", "?")
            ping = "—" if conn["rtt_ms"] is None else f"{conn['rtt_ms']:.0f} ms"
            theme.html_block(
                '<div class="cc-kv" style="margin-top:.9rem">'
                f'<span>Status</span><span>Online</span>'
                f'<span>Server</span><span>{theme.esc(conn["server_host"])}:{conn["server_port"]}</span>'
                f'<span>Your TCP port</span><span>{theme.esc(local[1])}</span>'
                f'<span>Your UDP port</span><span>{theme.esc(udp["local_port"] or "—")}</span>'
                f'<span>Ping</span><span>{ping}</span>'
                '</div>')

        with theme.panel("create"):
            theme.heading("Create a room")
            with st.form("create_room", border=False, clear_on_submit=True):
                name = st.text_input("Room name", placeholder="Friday finals", max_chars=24, key="new_room_name")
                go = st.form_submit_button("Create room", type="primary", use_container_width=True)
            if go:
                response = net.create_room(name or f"{me['username']}'s room")
                if response["ok"]:
                    st.rerun()
                st.error(response["error"]["message"])
            st.caption(f"Rooms hold exactly {config.ROOM_CAPACITY} players. You become the host.")

        with theme.panel("online"):
            players = lobby["online_players"]
            theme.heading("Online now", str(len(players)))
            rows = "".join(
                f'<div style="margin-bottom:.55rem">{theme.person(p["username"], WHERE.get(p["where"], p["where"]), 1.9)}</div>'
                for p in players)
            theme.html_block(rows or '<span class="cc-muted">Nobody else is online yet.</span>')
