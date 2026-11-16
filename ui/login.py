"""Page 1 — connect to the server, then log in or create an account."""

from __future__ import annotations

import streamlit as st

import config
from common import protocol as P
from ui import theme


def _hero() -> None:
    theme.html_block(f"""
<div class="cc-hero">
  <div class="cc-wordmark" role="heading" aria-level="1">CodeClash<span class="caret"></span></div>
  <p class="cc-lede">Three players, the same code on every screen, {config.ROUND_DURATION_S} seconds to
  read it. Right answers score; fast right answers score more.</p>
  <div class="cc-facts">
    <div><b>{config.ROOM_CAPACITY}</b><span>players per room</span></div>
    <div><b>{config.TOTAL_ROUNDS}</b><span>rounds per game</span></div>
    <div><b>+{config.POINTS_CORRECT + config.SPEED_BONUS_MAX}</b><span>for a fast right answer</span></div>
  </div>
  <p class="cc-wire">Questions, answers and scores travel over a persistent <em>TCP</em> connection to the
  game server. The countdown arrives every second as <strong>UDP</strong> datagrams.</p>
</div>""")


def _connect_panel(net, snap: dict) -> None:
    conn = snap["connection"]
    with theme.panel("connect"):
        theme.heading("Connect to the game server")
        if snap.get("kicked"):
            st.warning(snap["kicked"])
        with st.form("connect_form", border=False):
            host = st.text_input("Server address", value=conn["server_host"] or config.get_server_host(),
                                 key="server_host",
                                 help="The MacBook's IPv4 address. The server prints it when it starts.")
            port = st.number_input("TCP port", value=int(conn["server_port"] or config.TCP_PORT),
                                   min_value=1, max_value=65535, step=1, key="server_port")
            submitted = st.form_submit_button("Connect", type="primary", use_container_width=True)
        if submitted:
            with st.spinner(f"Connecting to {host}:{int(port)}…"):
                ok, message = net.connect(host, int(port))
            if ok:
                st.rerun()
            st.error(message)
        elif conn["error"] and conn["status"] == "disconnected":
            st.caption(f"Last attempt: {conn['error']}")


def _account_panel(net, snap: dict) -> None:
    conn = snap["connection"]
    with theme.panel("account"):
        theme.heading("Sign in", f"{conn['server_host']}:{conn['server_port']}")
        tab_login, tab_register = st.tabs(["Log in", "Create account"])
        with tab_login:
            with st.form("login_form", border=False):
                username = st.text_input("Username", key="login_user")
                password = st.text_input("Password", type="password", key="login_pass")
                go = st.form_submit_button("Log in", type="primary", use_container_width=True)
            if go:
                response = net.login(username, password)
                if response["ok"]:
                    st.rerun()
                st.error(response["error"]["message"])
        with tab_register:
            with st.form("register_form", border=False):
                username = st.text_input("Username", key="reg_user",
                                         help="3–16 characters: letters, digits or underscore.")
                password = st.text_input("Password", type="password", key="reg_pass",
                                         help=f"At least {P.PASSWORD_MIN_LEN} characters. Stored hashed on the server.")
                confirm = st.text_input("Repeat password", type="password", key="reg_pass2")
                go = st.form_submit_button("Create account and log in", type="primary", use_container_width=True)
            if go:
                problem = P.validate_username(username.strip()) or P.validate_password(password)
                if not problem and password != confirm:
                    problem = "The two passwords are different."
                if problem:
                    st.error(problem)
                else:
                    response = net.register(username, password)
                    if response["ok"]:
                        response = net.login(username, password)
                    if response["ok"]:
                        st.rerun()
                    st.error(response["error"]["message"])
        if st.button("Change server", key="change_server"):
            net.disconnect()
            st.rerun()


def render(net, snap: dict) -> None:
    left, right = st.columns([1.15, 1], gap="large")
    with left:
        _hero()
    with right:
        st.write("")
        if snap["connection"]["status"] == "connected":
            _account_panel(net, snap)
        else:
            _connect_panel(net, snap)
