"""
CodeClash central configuration.

Deployment for the demo
-----------------------
  * Server  : Kacem's MacBook   -> runs `python3 -m server.main_server`
  * Clients : 3 Windows laptops -> each runs `streamlit run app.py` locally

Two different "hosts" exist and must never be confused:

  SERVER_BIND_HOST = "0.0.0.0"
      Used ONLY by the server. Binding to 0.0.0.0 means "listen on every IPv4 interface of
      this MacBook" (Wi-Fi, Ethernet, VPN...). 0.0.0.0 is NOT an address you can connect to.
      (The project brief calls this SERVER_HOST = "0.0.0.0"; it is renamed here so nobody
      accidentally points a client at it.)

  SERVER_HOST
      Used ONLY by clients: the MacBook's real LAN IPv4 address, e.g. "192.168.1.25".
      Resolution order on each client computer:
        1. environment variable CODECLASH_SERVER_HOST
        2. client_settings.json next to this file (written by the Streamlit login page)
        3. DEFAULT_SERVER_HOST below
      localhost / 127.x / 0.0.0.0 are rejected because they would point the client at itself.
      For single-computer development only, set CODECLASH_ALLOW_LOOPBACK=1.
"""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _env_int(name: str, default: int) -> int:
    """Optional override from an environment variable (used by tests and quick demos)."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

SERVER_BIND_HOST = "0.0.0.0"   # server listens on all IPv4 interfaces of the MacBook
TCP_PORT = _env_int("CODECLASH_TCP_PORT", 8888)   # TCP and UDP port spaces are separate,
UDP_PORT = _env_int("CODECLASH_UDP_PORT", 8888)   # so both can use 8888
STREAMLIT_PORT = 8501          # each client's UI, bound to 127.0.0.1 only (not exposed to the LAN)
STREAMLIT_ADDRESS = "127.0.0.1"

# Client UDP socket port. 0 = let the OS pick a free port (recommended: the server replies to
# whatever source address/port it observes). Set CODECLASH_CLIENT_UDP_PORT=8889 only if a
# client's firewall needs a fixed port for an inbound rule.
CLIENT_UDP_PORT = _env_int("CODECLASH_CLIENT_UDP_PORT", 0)

# Placeholder. Replace with the MacBook's LAN IPv4 (the server prints it at startup), or set it
# from the login page / CODECLASH_SERVER_HOST on each Windows client.
DEFAULT_SERVER_HOST = "192.168.1.25"

CLIENT_SETTINGS_FILE = PROJECT_ROOT / "client_settings.json"   # per computer, gitignored
ALLOW_LOOPBACK = os.environ.get("CODECLASH_ALLOW_LOOPBACK", "0") == "1"


def validate_server_host(host: str, allow_loopback: bool | None = None) -> str | None:
    """Return an error message if `host` cannot be used as the remote server address, else None.

    Accepts IPv4 addresses and hostnames (hostnames keep the door open for a VPN/DNS name later).
    """
    allow_loopback = ALLOW_LOOPBACK if allow_loopback is None else allow_loopback
    host = (host or "").strip()
    if not host:
        return "Server address is empty."
    if host.lower() == "localhost" and not allow_loopback:
        return "localhost means 'this computer'. Use the MacBook's LAN IPv4 address."
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None  # a hostname; DNS resolution is checked when connecting
    if ip.version != 4:
        return "Use an IPv4 address (the sockets use AF_INET)."
    if ip.is_unspecified:
        return "0.0.0.0 is a bind address, not a destination. Use the MacBook's LAN IPv4 address."
    if ip.is_loopback and not allow_loopback:
        return f"{host} is this computer (loopback). Use the MacBook's LAN IPv4 address."
    return None


def _read_settings() -> dict:
    try:
        with open(CLIENT_SETTINGS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_server_host() -> str:
    """The server address this client should connect to (see resolution order above)."""
    env = os.environ.get("CODECLASH_SERVER_HOST", "").strip()
    if env:
        return env
    saved = str(_read_settings().get("server_host", "")).strip()
    return saved or DEFAULT_SERVER_HOST


def save_server_host(host: str) -> None:
    """Remember the server address on this client computer (used by the login page)."""
    settings = _read_settings()
    settings["server_host"] = host.strip()
    with open(CLIENT_SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)


SERVER_HOST = get_server_host()

# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

ROOM_CAPACITY = 3               # exactly three players per room
MIN_PLAYERS_TO_START = 3        # START_GAME is refused below this
MIN_CONNECTED_TO_CONTINUE = 2   # a running game ends early if fewer players stay connected

# ---------------------------------------------------------------------------
# Game timing (all timing is decided by the server clock)
# Quick demo: CODECLASH_TOTAL_ROUNDS=4 python3 -m server.main_server
# ---------------------------------------------------------------------------

TOTAL_ROUNDS = _env_int("CODECLASH_TOTAL_ROUNDS", 8)            # 2 questions from each of the 4 categories
ROUND_DURATION_S = _env_int("CODECLASH_ROUND_DURATION_S", 20)   # answer window per question
PRE_GAME_COUNTDOWN_S = _env_int("CODECLASH_PRE_GAME_COUNTDOWN_S", 3)   # "get ready" countdown
RESULTS_DURATION_S = _env_int("CODECLASH_RESULTS_DURATION_S", 6)       # round results before the next question
TIMER_TICK_INTERVAL_S = 1.0     # UDP countdown period
END_ROUND_WHEN_ALL_ANSWERED = True   # close a round early once every connected player answered

# ---------------------------------------------------------------------------
# Scoring (computed only on the server)
# ---------------------------------------------------------------------------

POINTS_CORRECT = 10
POINTS_WRONG = 0
SPEED_BONUS_ENABLED = True
SPEED_BONUS_MAX = 5             # bonus = ceil(SPEED_BONUS_MAX * time_left / ROUND_DURATION_S), correct answers only

# ---------------------------------------------------------------------------
# Liveness / timeouts
# ---------------------------------------------------------------------------

HEARTBEAT_INTERVAL_S = 5        # client sends TCP PING this often
HEARTBEAT_TIMEOUT_S = _env_int("CODECLASH_HEARTBEAT_TIMEOUT_S", 15)  # server drops a TCP session silent this long
UDP_HELLO_INTERVAL_S = 5        # client refreshes its UDP endpoint this often
UDP_ENDPOINT_STALE_S = 15       # server marks a UDP endpoint stale after this long
SOCKET_POLL_TIMEOUT_S = 1.0     # recv/accept timeout so threads can notice shutdown/heartbeat expiry
CONNECT_TIMEOUT_S = 5           # client TCP connect timeout
REQUEST_TIMEOUT_S = 5           # client waits this long for a RESPONSE

# ---------------------------------------------------------------------------
# Authentication / storage (server only)
# ---------------------------------------------------------------------------

USERS_FILE = Path(os.environ.get("CODECLASH_USERS_FILE", PROJECT_ROOT / "server" / "data" / "users.json"))
# hashlib.pbkdf2_hmac("sha256", ...) with a random 16-byte salt per user
PBKDF2_ITERATIONS = _env_int("CODECLASH_PBKDF2_ITERATIONS", 200_000)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("CODECLASH_LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(threadName)s] %(name)s: %(message)s"
LOG_DATEFMT = "%H:%M:%S"
