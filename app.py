"""
CodeClash player app — run on each Windows client computer:

    streamlit run app.py --server.address 127.0.0.1 --server.port 8501

(or double-click start_client.bat). The browser shows http://localhost:8501; the app's sockets
connect to the MacBook server's LAN IP, which you type on the first screen.

How the UI stays in sync with the server
----------------------------------------
* get_network() returns ONE NetworkManager per Streamlit process (st.cache_resource), so the
  TCP/UDP connections and their threads survive reruns and browser refreshes.
* The page shown is chosen from the mirrored SERVER state (room status, game phase), so the three
  players' screens change together when the server says so.
* A small fragment re-runs twice a second; when the NetworkManager's version number changed (a
  new TCP event arrived) it reruns the whole page. Timers re-run on their own faster fragments.
"""

from __future__ import annotations

import os

import streamlit as st

st.set_page_config(page_title="CodeClash", page_icon="⚔️", layout="wide", initial_sidebar_state="collapsed")

from client.network_manager import NetworkManager  # noqa: E402
from common.protocol import Phase, RoomStatus  # noqa: E402
from ui import game_arena, lobby, login, results, theme, waiting_room  # noqa: E402


@st.cache_resource(show_spinner=False)
def get_network() -> NetworkManager:
    return NetworkManager()


def choose_page(snap: dict) -> str:
    conn, me, room, game = snap["connection"], snap["me"], snap["room"], snap["game"]
    if me is None or conn["status"] != "connected":
        return "lost" if (me is not None and conn["lost"]) else "login"
    if room is None:
        return "lobby"
    if game is not None:
        if game["phase"] == Phase.FINISHED and game.get("over"):
            return "final"
        if game["phase"] == Phase.RESULTS and game.get("result"):
            return "round_results"
        return "arena"
    if room["status"] == RoomStatus.IN_PROGRESS:
        return "arena"            # game data is on its way (GAME_STARTED / GAME_SYNC)
    return "waiting"


def top_bar(net: NetworkManager, snap: dict, page: str) -> None:
    signed_in = snap["me"] is not None and page not in ("login", "lost")
    cols = st.columns([1.2, 3, 1.1] if signed_in else [1.2, 4.1], vertical_alignment="center")
    with cols[0]:
        theme.html_block('<div class="cc-brand"><b>⚔</b>CodeClash</div>')
    with cols[1]:
        _status_chips(net)
    if signed_in:
        with cols[2]:
            if st.button(f"Log out {snap['me']['username']}", key="logout", use_container_width=True):
                net.logout()
                st.rerun()


@st.fragment(run_every=1.0)
def _status_chips(net: NetworkManager) -> None:
    """Connection indicators, refreshed every second without rerunning the page."""
    s = net.snapshot()
    conn, udp = s["connection"], s["udp"]
    chips = []
    if conn["status"] == "connected":
        chips.append(theme.chip(f"TCP {conn['server_host']}:{conn['server_port']}", "good"))
        if s["me"] is None:
            chips.append(theme.chip("UDP after login", ""))
        elif udp["live"] or udp["ok"]:
            chips.append(theme.chip(f"UDP live — {udp['ticks']} ticks" if udp["ticks"] else "UDP ready", "good"))
        else:
            chips.append(theme.chip("UDP waiting for server", "warn"))
        if conn["rtt_ms"] is not None:
            chips.append(theme.chip(f"Ping {conn['rtt_ms']:.0f} ms", "volt"))
    elif conn["status"] == "connecting":
        chips.append(theme.chip("Connecting…", "warn"))
    else:
        chips.append(theme.chip("Not connected", "bad"))
    theme.html_block('<div class="cc-chips">' + "".join(chips) + "</div>")


@st.fragment(run_every=0.5)
def _watch_server(net: NetworkManager) -> None:
    """Rerun the whole page as soon as a new server event changed the mirrored state."""
    if net.version != st.session_state.get("_rendered_version"):
        st.rerun()


def render_lost(net: NetworkManager, snap: dict) -> None:
    conn = snap["connection"]
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        reason = (conn["error"] or "the network connection ended").rstrip(".")
        theme.title("Connection to the server was lost",
                    f"{reason[:1].upper()}{reason[1:]}. If a game is running, the server keeps your seat: "
                    "reconnect to continue where you left off.")
        if st.button("Reconnect", type="primary", key="reconnect", use_container_width=True):
            with st.spinner("Reconnecting…"):
                ok, message = net.reconnect()
            if not ok:
                st.error(message)
            else:
                st.rerun()
        if st.button("Back to sign in", key="back_login", use_container_width=True):
            net.disconnect()
            st.rerun()


def main() -> None:
    net = get_network()
    theme.inject()
    snap = net.snapshot()
    page = choose_page(snap)
    if os.environ.get("CODECLASH_DEBUG_RERUNS") == "1":
        print(f"[ui] full run: page={page} version={snap['version']}", flush=True)
    top_bar(net, snap, page)
    theme.show_toasts(snap)
    {
        "login": login.render,
        "lost": render_lost,
        "lobby": lobby.render,
        "waiting": waiting_room.render,
        "arena": game_arena.render,
        "round_results": results.render_round,
        "final": results.render_final,
    }[page](net, snap)
    st.session_state["_rendered_version"] = snap["version"]
    _watch_server(net)


main()
