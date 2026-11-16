"""
The single shared state of the CodeClash server and the lock that protects it.

Every thread (player handlers, the UDP receiver, game threads, the admin console) reads and
writes the objects below. Rules (see docs/ARCHITECTURE.md §6.1):

  1. Hold `state.lock` while reading or changing sessions_by_player, rooms, player_room, or any
     Room / Game / Round / GamePlayer object.
  2. Never do blocking work while holding it: no socket sendall/recv/sendto, no file I/O, no
     password hashing. Putting a message in a session outbox (session.send) IS allowed: it is a
     non-blocking queue put.
  3. Methods whose name ends in `_locked` must be called with the lock already held. The lock is
     a plain threading.Lock (not re-entrant), so they must never try to take it again.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imported only for type hints, avoids circular imports at runtime
    from server.player_handler import PlayerSession
    from server.room_manager import Room


class RequestError(Exception):
    """Raised by request handlers; turned into an ok:false RESPONSE with this error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ServerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()

        # Logged-in players: player_id -> the ONE active TCP session of that account.
        self.sessions_by_player: dict[str, "PlayerSession"] = {}
        # Every open TCP connection, logged in or not.
        self.all_sessions: set["PlayerSession"] = set()
        # room_id -> Room
        self.rooms: dict[str, "Room"] = {}
        # player_id -> room_id (a player is in at most one room)
        self.player_room: dict[str, str] = {}

        self.shutting_down = threading.Event()
        self.started_at = time.time()
        self._room_ids = itertools.count(1001)
        self._game_ids = itertools.count(1)

    # ---- identifiers (call with the lock held) ------------------------------------------------
    def new_room_id_locked(self) -> str:
        return f"R{next(self._room_ids)}"

    def new_game_id_locked(self) -> str:
        return f"G{next(self._game_ids)}"

    # ---- message delivery helpers (non-blocking: safe under the lock) -------------------------
    def send_to_player_locked(self, player_id: str, msg: dict) -> None:
        session = self.sessions_by_player.get(player_id)
        if session is not None:
            session.send(msg)

    def send_to_players_locked(self, player_ids, msg: dict) -> None:
        for pid in player_ids:
            self.send_to_player_locked(pid, msg)

    def udp_addrs_locked(self, player_ids) -> list[tuple[str, int]]:
        """Registered UDP endpoints of these players (players without one are skipped)."""
        addrs = []
        for pid in player_ids:
            session = self.sessions_by_player.get(pid)
            if session is not None and session.udp_addr is not None:
                addrs.append(session.udp_addr)
        return addrs

    def username_locked(self, player_id: str) -> str | None:
        session = self.sessions_by_player.get(player_id)
        return session.username if session else None

    def summary_locked(self) -> dict[str, Any]:
        return {
            "uptime_s": round(time.time() - self.started_at),
            "connections": len(self.all_sessions),
            "logged_in": len(self.sessions_by_player),
            "rooms": len(self.rooms),
        }
