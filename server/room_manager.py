"""
Rooms: create / join / leave / ready / start / play again, host management, and what happens to
a room when a player disconnects, logs back in, or a game ends.

Every public method takes `state.lock` itself. Methods ending in `_locked` expect the caller to
hold it already (they are called from the player handler, the game thread, or each other).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import config
from common import protocol as P
from common.protocol import ErrorCode, PlayerStatus, RoomStatus
from server.server_state import RequestError, ServerState

if TYPE_CHECKING:
    from server.game_manager import Game, GameManager
    from server.player_handler import PlayerSession

log = logging.getLogger("codeclash.rooms")


@dataclass
class RoomMember:
    player_id: str
    username: str
    ready: bool = False
    connected: bool = True


class Room:
    def __init__(self, room_id: str, name: str, host_id: str):
        self.room_id = room_id
        self.name = name
        self.host_id = host_id
        self.status = RoomStatus.WAITING
        self.members: dict[str, RoomMember] = {}   # insertion order = join order
        self.game: "Game | None" = None
        self.last_game_id: str | None = None
        self.created_at = time.time()
        self._udp_seq = 0

    def next_udp_seq(self) -> int:
        """Strictly increasing sequence number for every UDP datagram about this room."""
        self._udp_seq += 1
        return self._udp_seq

    def connected_member_ids(self) -> list[str]:
        return [pid for pid, m in self.members.items() if m.connected]


def _require_room_id(data: dict) -> str:
    room_id = data.get("room_id")
    if not isinstance(room_id, str) or not room_id or len(room_id) > P.ID_MAX_LEN:
        raise RequestError(ErrorCode.BAD_REQUEST, "room_id is required.")
    return room_id


class RoomManager:
    def __init__(self, state: ServerState, games: "GameManager"):
        self.state = state
        self.games = games

    # =================================================================== payload builders
    def room_snapshot_locked(self, room: Room) -> dict:
        return {
            "room_id": room.room_id,
            "name": room.name,
            "status": room.status,
            "host_id": room.host_id,
            "capacity": config.ROOM_CAPACITY,
            "game_id": room.game.game_id if room.game else room.last_game_id,
            "players": [
                {"player_id": m.player_id, "username": m.username, "is_host": m.player_id == room.host_id,
                 "ready": m.ready, "connected": m.connected}
                for m in room.members.values()
            ],
        }

    def lobby_rooms_locked(self) -> list[dict]:
        rooms = sorted(self.state.rooms.values(), key=lambda r: r.created_at)
        out = []
        for r in rooms:
            host = r.members.get(r.host_id)
            out.append({
                "room_id": r.room_id, "name": r.name, "status": r.status,
                "player_count": len(r.members), "capacity": config.ROOM_CAPACITY,
                "player_names": [m.username for m in r.members.values()],
                "host_name": host.username if host else None,
            })
        return out

    def online_players_locked(self) -> list[dict]:
        out = []
        for pid, session in sorted(self.state.sessions_by_player.items(), key=lambda kv: kv[1].username or ""):
            room_id = self.state.player_room.get(pid)
            room = self.state.rooms.get(room_id) if room_id else None
            where = "lobby" if room is None else ("in_game" if room.status == RoomStatus.IN_PROGRESS else "in_room")
            out.append({"player_id": pid, "username": session.username, "room_id": room_id, "where": where})
        return out

    def lobby_payload_locked(self) -> dict:
        return {"rooms": self.lobby_rooms_locked(), "online_players": self.online_players_locked()}

    # =================================================================== broadcasts (lock held)
    def broadcast_lobby_locked(self) -> None:
        msg = P.make_push(P.LOBBY_UPDATE, self.lobby_payload_locked())
        for session in self.state.sessions_by_player.values():
            session.send(msg)

    def broadcast_room_locked(self, room: Room, notice: str | None = None) -> None:
        msg = P.make_push(P.ROOM_UPDATE, {"room": self.room_snapshot_locked(room), "notice": notice})
        self.state.send_to_players_locked(room.connected_member_ids(), msg)

    def notify_player_status_locked(self, room: Room, player_id: str, username: str, status: str) -> None:
        msg = P.make_push(P.PLAYER_STATUS, {"player_id": player_id, "username": username,
                                            "status": status, "room_id": room.room_id})
        others = [pid for pid in room.connected_member_ids() if pid != player_id]
        self.state.send_to_players_locked(others, msg)

    # =================================================================== helpers
    def _room_of_locked(self, player_id: str, room_id: str) -> Room:
        """The room this player is in, which must be `room_id`."""
        current = self.state.player_room.get(player_id)
        room = self.state.rooms.get(room_id)
        if room is None:
            raise RequestError(ErrorCode.ROOM_NOT_FOUND, f"Room {room_id} does not exist.")
        if current != room_id or player_id not in room.members:
            raise RequestError(ErrorCode.NOT_IN_ROOM, f"You are not in room {room_id}.")
        return room

    def _ensure_host_locked(self, room: Room) -> str | None:
        """Make sure the host is a connected member; returns a notice if the host changed."""
        host = room.members.get(room.host_id)
        if host is not None and host.connected:
            return None
        candidates = [m for m in room.members.values() if m.connected] or list(room.members.values())
        if not candidates:
            return None
        new_host = candidates[0]                      # longest-present member
        room.host_id = new_host.player_id
        new_host.ready = True                         # the host is implicitly ready
        log.info("Room %s: host is now %s", room.room_id, new_host.username)
        return f"{new_host.username} is now the host"

    def _remove_member_locked(self, room: Room, player_id: str) -> bool:
        """Remove a member; transfer host or delete the empty room. Returns True if the room was deleted."""
        room.members.pop(player_id, None)
        if self.state.player_room.get(player_id) == room.room_id:
            del self.state.player_room[player_id]
        if not room.members:
            self.state.rooms.pop(room.room_id, None)
            log.info("Room %s (%s) deleted: no players left", room.room_id, room.name)
            return True
        return False

    # =================================================================== requests
    def list_rooms(self, session: "PlayerSession", data: dict, received_at: float) -> dict:
        with self.state.lock:
            return self.lobby_payload_locked()

    def create_room(self, session: "PlayerSession", data: dict, received_at: float) -> dict:
        name = data.get("room_name")
        problem = P.validate_room_name(name)
        if problem:
            raise RequestError(ErrorCode.INVALID_ROOM_NAME, problem)
        pid, username = session.player_id, session.username
        with self.state.lock:
            if pid in self.state.player_room:
                raise RequestError(ErrorCode.ALREADY_IN_ROOM, "Leave your current room first.")
            room = Room(self.state.new_room_id_locked(), name.strip(), pid)
            room.members[pid] = RoomMember(pid, username, ready=True)
            self.state.rooms[room.room_id] = room
            self.state.player_room[pid] = room.room_id
            log.info("%s created room %s (%s)", username, room.room_id, room.name)
            self.broadcast_room_locked(room, notice=f"{username} created the room")
            self.broadcast_lobby_locked()
            return {"room": self.room_snapshot_locked(room)}

    def join_room(self, session: "PlayerSession", data: dict, received_at: float) -> dict:
        room_id = _require_room_id(data)
        pid, username = session.player_id, session.username
        with self.state.lock:
            room = self.state.rooms.get(room_id)
            if room is None:
                raise RequestError(ErrorCode.ROOM_NOT_FOUND, f"Room {room_id} does not exist.")
            current = self.state.player_room.get(pid)
            if current == room_id:
                return {"room": self.room_snapshot_locked(room)}        # already there: idempotent
            if current is not None:
                raise RequestError(ErrorCode.ALREADY_IN_ROOM, "Leave your current room first.")
            if room.status != RoomStatus.WAITING:
                raise RequestError(ErrorCode.GAME_IN_PROGRESS, f"Room '{room.name}' is not accepting players now.")
            if len(room.members) >= config.ROOM_CAPACITY:
                raise RequestError(ErrorCode.ROOM_FULL,
                                   f"Room '{room.name}' is full ({len(room.members)}/{config.ROOM_CAPACITY}).")
            room.members[pid] = RoomMember(pid, username)
            self.state.player_room[pid] = room_id
            log.info("%s joined room %s (%d/%d)", username, room_id, len(room.members), config.ROOM_CAPACITY)
            self.broadcast_room_locked(room, notice=f"{username} joined")
            self.broadcast_lobby_locked()
            return {"room": self.room_snapshot_locked(room)}

    def leave_room(self, session: "PlayerSession", data: dict, received_at: float) -> dict:
        room_id = _require_room_id(data)
        pid, username = session.player_id, session.username
        with self.state.lock:
            room = self._room_of_locked(pid, room_id)
            self._leave_locked(room, pid, username, PlayerStatus.LEFT)
            self.broadcast_lobby_locked()
        log.info("%s left room %s", username, room_id)
        return {}

    def _leave_locked(self, room: Room, pid: str, username: str, status: str) -> None:
        """Remove a member for good (Leave button, logout, or disconnect outside a game)."""
        if room.status == RoomStatus.IN_PROGRESS and room.game is not None:
            self.games.player_left_locked(room, pid)          # forfeit: stays on the leaderboard as LEFT
        self.notify_player_status_locked(room, pid, username, status)
        deleted = self._remove_member_locked(room, pid)
        if not deleted:
            host_notice = self._ensure_host_locked(room)
            verb = "disconnected" if status == PlayerStatus.DISCONNECTED else "left"
            notice = f"{username} {verb}" + (f" — {host_notice}" if host_notice else "")
            self.broadcast_room_locked(room, notice=notice)

    def set_ready(self, session: "PlayerSession", data: dict, received_at: float) -> dict:
        room_id = _require_room_id(data)
        ready = data.get("ready")
        if not isinstance(ready, bool):
            raise RequestError(ErrorCode.BAD_REQUEST, "ready must be true or false.")
        pid, username = session.player_id, session.username
        with self.state.lock:
            room = self._room_of_locked(pid, room_id)
            if room.status != RoomStatus.WAITING:
                raise RequestError(ErrorCode.GAME_IN_PROGRESS, "Ready can only change while waiting.")
            member = room.members[pid]
            if pid != room.host_id and member.ready != ready:     # the host is always ready
                member.ready = ready
                self.broadcast_room_locked(room, notice=f"{username} is {'ready' if ready else 'not ready'}")
            return {"room": self.room_snapshot_locked(room)}

    def start_game(self, session: "PlayerSession", data: dict, received_at: float) -> dict:
        room_id = _require_room_id(data)
        pid = session.player_id
        with self.state.lock:
            room = self._room_of_locked(pid, room_id)
            if room.host_id != pid:
                raise RequestError(ErrorCode.NOT_HOST, "Only the host can start the game.")
            if room.status == RoomStatus.IN_PROGRESS:
                raise RequestError(ErrorCode.GAME_IN_PROGRESS, "The game is already running.")
            if room.status == RoomStatus.FINISHED:
                raise RequestError(ErrorCode.BAD_REQUEST, "This game is over. Use Play Again first.")
            connected = room.connected_member_ids()
            if len(connected) < config.MIN_PLAYERS_TO_START:
                raise RequestError(ErrorCode.NOT_ENOUGH_PLAYERS,
                                   f"{config.MIN_PLAYERS_TO_START} players are needed ({len(connected)} in the room).")
            not_ready = [m.username for m in room.members.values() if m.player_id != room.host_id and not m.ready]
            if not_ready:
                raise RequestError(ErrorCode.PLAYERS_NOT_READY, "Not ready yet: " + ", ".join(not_ready))
            game_id = self.games.start_locked(room)        # creates the game thread
            room.status = RoomStatus.IN_PROGRESS
            self.broadcast_room_locked(room, notice="Game started!")
            self.broadcast_lobby_locked()
            return {"game_id": game_id}

    def play_again(self, session: "PlayerSession", data: dict, received_at: float) -> dict:
        room_id = _require_room_id(data)
        pid = session.player_id
        with self.state.lock:
            room = self._room_of_locked(pid, room_id)
            if room.host_id != pid:
                raise RequestError(ErrorCode.NOT_HOST, "Only the host can restart the room.")
            if room.status != RoomStatus.FINISHED:
                raise RequestError(ErrorCode.GAME_NOT_FINISHED, "The room is not in the finished state.")
            room.status = RoomStatus.WAITING
            for m in room.members.values():
                m.ready = m.player_id == room.host_id
            self.broadcast_room_locked(room, notice="New game: press Ready!")
            self.broadcast_lobby_locked()
            return {"room": self.room_snapshot_locked(room)}

    # =================================================================== lifecycle hooks (lock held)
    def on_login_locked(self, session: "PlayerSession") -> dict | None:
        """Called after a successful LOGIN. Re-attaches the player to their room, if any."""
        pid = session.player_id
        room_id = self.state.player_room.get(pid)
        room = self.state.rooms.get(room_id) if room_id else None
        if room is None:
            return None
        member = room.members[pid]
        if room.status == RoomStatus.IN_PROGRESS and room.game is not None:
            was_disconnected = not member.connected
            member.connected = True
            self.games.player_reconnected_locked(room, pid, session)     # sends GAME_SYNC
            if was_disconnected:
                self.notify_player_status_locked(room, pid, member.username, PlayerStatus.RECONNECTED)
                self.broadcast_room_locked(room, notice=f"{member.username} reconnected")
                log.info("%s reconnected to running game in room %s", member.username, room.room_id)
            else:
                session.send(P.make_push(P.ROOM_UPDATE, {"room": self.room_snapshot_locked(room), "notice": None}))
        else:
            member.connected = True
            session.send(P.make_push(P.ROOM_UPDATE, {"room": self.room_snapshot_locked(room), "notice": None}))
        return self.room_snapshot_locked(room)

    def on_player_gone_locked(self, player_id: str, username: str, status: str) -> None:
        """The player's session ended: DISCONNECTED (network) or OFFLINE (logout)."""
        room_id = self.state.player_room.get(player_id)
        room = self.state.rooms.get(room_id) if room_id else None
        if room is not None:
            in_game = room.status == RoomStatus.IN_PROGRESS and room.game is not None
            if in_game and status == PlayerStatus.DISCONNECTED:
                # Keep the seat so the player can log back in and continue.
                room.members[player_id].connected = False
                self.games.player_disconnected_locked(room, player_id)
                self.notify_player_status_locked(room, player_id, username, PlayerStatus.DISCONNECTED)
                host_notice = self._ensure_host_locked(room)
                self.broadcast_room_locked(room, notice=f"{username} disconnected"
                                           + (f" — {host_notice}" if host_notice else ""))
            else:
                self._leave_locked(room, player_id, username, status)
        self.broadcast_lobby_locked()

    def on_game_over_locked(self, room: Room) -> None:
        """Called by the game thread after it has sent GAME_OVER."""
        room.status = RoomStatus.FINISHED
        room.last_game_id = room.game.game_id if room.game else room.last_game_id
        room.game = None
        if self.state.rooms.get(room.room_id) is not room:   # everybody left: room already deleted
            self.broadcast_lobby_locked()
            return
        for pid in [pid for pid, m in room.members.items() if not m.connected]:
            if self._remove_member_locked(room, pid):        # disconnected players leave the room now
                self.broadcast_lobby_locked()
                return
        self._ensure_host_locked(room)
        for m in room.members.values():
            m.ready = m.player_id == room.host_id
        self.broadcast_room_locked(room, notice=None)       # clients announce the winner themselves
        self.broadcast_lobby_locked()
