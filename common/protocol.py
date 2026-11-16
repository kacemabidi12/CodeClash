"""
CodeClash wire protocol — shared by the server (MacBook) and every client (Windows PCs).

This module is the single source of truth for:
  * message type names (TCP requests, TCP server pushes, UDP datagrams)
  * the JSON envelope of every message
  * error codes
  * TCP framing (newline-delimited JSON over a byte stream)
  * UDP packet encoding (one compact JSON object per datagram)
  * shared input-validation rules (usernames, passwords, room names, answers)

It uses only the Python standard library, so the server needs no third-party packages.

TCP FRAMING — why it exists
---------------------------
TCP is a *byte stream*, not a message stream. One sendall() on the sender can arrive as
several recv() chunks on the receiver, and several sendall() calls can arrive merged in a
single recv(). We therefore terminate every JSON message with b"\\n" and reassemble on the
receiving side with LineFramer. json.dumps() never emits a raw newline (newlines inside
strings, e.g. in code snippets, are escaped as the two characters "\\n"), so b"\\n" is a safe
delimiter. Splitting on the byte b"\\n" *before* UTF-8 decoding is also safe: the byte 0x0A
never occurs inside a multi-byte UTF-8 sequence, so a character split across two recv()
calls is reassembled correctly.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Versions and size limits
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = 1
ENCODING = "utf-8"

MAX_TCP_LINE_BYTES = 64 * 1024   # a single framed TCP message may not exceed 64 KiB
MAX_UDP_PACKET_BYTES = 1024      # keep datagrams far below the ~1472-byte Ethernet/IPv4 limit
UDP_RECV_BUFFER = 2048           # recvfrom() buffer size (larger than any valid packet)
TCP_RECV_BUFFER = 4096           # recv() chunk size; framing does NOT depend on it

# ---------------------------------------------------------------------------
# TCP request types (client -> server). Every request gets exactly one RESPONSE.
# ---------------------------------------------------------------------------

REGISTER = "REGISTER"
LOGIN = "LOGIN"
LOGOUT = "LOGOUT"
PING = "PING"
LIST_ROOMS = "LIST_ROOMS"
CREATE_ROOM = "CREATE_ROOM"
JOIN_ROOM = "JOIN_ROOM"
LEAVE_ROOM = "LEAVE_ROOM"
SET_READY = "SET_READY"
START_GAME = "START_GAME"
SUBMIT_ANSWER = "SUBMIT_ANSWER"
PLAY_AGAIN = "PLAY_AGAIN"

REQUEST_TYPES = frozenset({
    REGISTER, LOGIN, LOGOUT, PING, LIST_ROOMS, CREATE_ROOM, JOIN_ROOM,
    LEAVE_ROOM, SET_READY, START_GAME, SUBMIT_ANSWER, PLAY_AGAIN,
})

# Requests that are allowed before the connection has logged in.
UNAUTHENTICATED_REQUESTS = frozenset({REGISTER, LOGIN, PING})

# ---------------------------------------------------------------------------
# TCP server -> client messages
# ---------------------------------------------------------------------------

RESPONSE = "RESPONSE"            # answer to one request, carries reply_to = the request's req_id

WELCOME = "WELCOME"              # sent once, right after the TCP connection is accepted
LOBBY_UPDATE = "LOBBY_UPDATE"    # rooms + online players changed
ROOM_UPDATE = "ROOM_UPDATE"      # membership / ready / host / status of *your* room changed
PLAYER_STATUS = "PLAYER_STATUS"  # a player came online, left, disconnected or reconnected
GAME_STARTED = "GAME_STARTED"    # authoritative game start
QUESTION = "QUESTION"            # authoritative round start: the question (never the answer)
ROUND_RESULT = "ROUND_RESULT"    # authoritative round end: correct answer, points, scoreboard
GAME_OVER = "GAME_OVER"          # final leaderboard, winner(s), statistics
GAME_SYNC = "GAME_SYNC"          # full game snapshot for a player who logs back in mid-game
KICKED = "KICKED"                # this session was replaced by a newer login of the same account
ERROR = "ERROR"                  # unsolicited protocol error (e.g. malformed line without req_id)

PUSH_TYPES = frozenset({
    WELCOME, LOBBY_UPDATE, ROOM_UPDATE, PLAYER_STATUS, GAME_STARTED, QUESTION,
    ROUND_RESULT, GAME_OVER, GAME_SYNC, KICKED, ERROR,
})

# ---------------------------------------------------------------------------
# UDP datagram types
# ---------------------------------------------------------------------------

UDP_HELLO = "UDP_HELLO"          # client -> server: register/refresh my UDP endpoint (every few s)
UDP_ACK = "UDP_ACK"              # server -> client: HELLO received (proves the UDP path works)
TIMER_TICK = "TIMER_TICK"        # server -> clients: countdown, once per second
LIVE_NOTICE = "LIVE_NOTICE"      # server -> clients: non-critical live info ("2/3 locked in")

UDP_CLIENT_TYPES = frozenset({UDP_HELLO})
UDP_SERVER_TYPES = frozenset({UDP_ACK, TIMER_TICK, LIVE_NOTICE})

# ---------------------------------------------------------------------------
# Enumerations used inside payloads
# ---------------------------------------------------------------------------

class RoomStatus:
    WAITING = "WAITING"
    IN_PROGRESS = "IN_PROGRESS"
    FINISHED = "FINISHED"


class PlayerStatus:
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"              # logged out cleanly
    DISCONNECTED = "DISCONNECTED"    # connection lost unexpectedly
    RECONNECTED = "RECONNECTED"
    LEFT = "LEFT"                    # left the room (used in PLAYER_STATUS / round results)


class Phase:
    """Game phase carried by TIMER_TICK and mirrored by the client."""
    STARTING = "STARTING"    # pre-game "get ready" countdown
    QUESTION = "QUESTION"    # a round is open, answers accepted
    RESULTS = "RESULTS"      # round closed, showing results, next round soon
    FINISHED = "FINISHED"


class AnswerKind:
    CHOICE = "choice"        # multiple choice: answer is an option key such as "B"
    TEXT = "text"            # short free text: compared after normalisation on the server


class RoundOutcome:
    ANSWERED = "answered"
    NO_ANSWER = "no_answer"
    DISCONNECTED = "disconnected"
    LEFT = "left"


class GameEndReason:
    COMPLETED = "completed"
    NOT_ENOUGH_PLAYERS = "not_enough_players"
    SERVER_SHUTDOWN = "server_shutdown"


# ---------------------------------------------------------------------------
# Error codes (RESPONSE.error.code and ERROR.data.code)
# ---------------------------------------------------------------------------

class ErrorCode:
    BAD_REQUEST = "BAD_REQUEST"                    # missing/invalid fields
    UNKNOWN_TYPE = "UNKNOWN_TYPE"                  # unsupported message type
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    MALFORMED_MESSAGE = "MALFORMED_MESSAGE"        # not JSON / not an object
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    ALREADY_AUTHENTICATED = "ALREADY_AUTHENTICATED"
    INVALID_USERNAME = "INVALID_USERNAME"
    INVALID_PASSWORD = "INVALID_PASSWORD"
    USERNAME_TAKEN = "USERNAME_TAKEN"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    INVALID_ROOM_NAME = "INVALID_ROOM_NAME"
    ROOM_NOT_FOUND = "ROOM_NOT_FOUND"
    ROOM_FULL = "ROOM_FULL"
    ALREADY_IN_ROOM = "ALREADY_IN_ROOM"
    NOT_IN_ROOM = "NOT_IN_ROOM"
    NOT_HOST = "NOT_HOST"
    NOT_ENOUGH_PLAYERS = "NOT_ENOUGH_PLAYERS"
    PLAYERS_NOT_READY = "PLAYERS_NOT_READY"
    GAME_IN_PROGRESS = "GAME_IN_PROGRESS"
    GAME_NOT_RUNNING = "GAME_NOT_RUNNING"
    GAME_NOT_FINISHED = "GAME_NOT_FINISHED"
    NOT_IN_GAME = "NOT_IN_GAME"
    WRONG_ROUND = "WRONG_ROUND"
    ROUND_CLOSED = "ROUND_CLOSED"
    ALREADY_ANSWERED = "ALREADY_ANSWERED"
    INVALID_ANSWER = "INVALID_ANSWER"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Shared validation rules (the UI may pre-check; the SERVER always re-checks)
# ---------------------------------------------------------------------------

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
PASSWORD_MIN_LEN = 4
PASSWORD_MAX_LEN = 128
ROOM_NAME_MAX_LEN = 24
ANSWER_MAX_LEN = 200
ID_MAX_LEN = 64


def validate_username(username: Any) -> str | None:
    """Return an error message, or None when the username is valid."""
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        return "Username must be 3-16 characters: letters, digits or underscore."
    return None


def validate_password(password: Any) -> str | None:
    if not isinstance(password, str) or not (PASSWORD_MIN_LEN <= len(password) <= PASSWORD_MAX_LEN):
        return f"Password must be {PASSWORD_MIN_LEN}-{PASSWORD_MAX_LEN} characters."
    return None


def validate_room_name(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > ROOM_NAME_MAX_LEN:
        return f"Room name must be 1-{ROOM_NAME_MAX_LEN} characters."
    return None


def normalize_text_answer(answer: str) -> str:
    """Normalisation used by the SERVER to compare free-text answers.

    Strips surrounding whitespace, collapses internal whitespace runs to one space and
    lower-cases. Example: "  [1,  2, 3] " -> "[1, 2, 3]".
    """
    return " ".join(answer.strip().split()).lower()


# ---------------------------------------------------------------------------
# Errors raised by this module
# ---------------------------------------------------------------------------

class ProtocolError(Exception):
    """A message could not be framed, decoded or validated."""

    def __init__(self, message: str, code: str = ErrorCode.MALFORMED_MESSAGE):
        super().__init__(message)
        self.code = code


class FrameTooLarge(ProtocolError):
    """The peer sent more than MAX_TCP_LINE_BYTES without a newline. Close the connection."""

    def __init__(self, size: int):
        super().__init__(
            f"Frame exceeds {MAX_TCP_LINE_BYTES} bytes without a newline ({size} bytes buffered)",
            ErrorCode.MALFORMED_MESSAGE,
        )


# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------

def new_id() -> str:
    """Random 128-bit identifier as 32 hex characters (req_id, submission_id, ...)."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------

def make_request(msg_type: str, data: dict | None = None, req_id: str | None = None) -> dict:
    """Client -> server request: {"v", "type", "req_id", "data"}."""
    if msg_type not in REQUEST_TYPES:
        raise ValueError(f"Unknown request type: {msg_type}")
    return {"v": PROTOCOL_VERSION, "type": msg_type, "req_id": req_id or new_id(), "data": data or {}}


def make_response(request_type: str, reply_to: str | None, ok: bool = True, data: dict | None = None,
                  error_code: str | None = None, error_message: str | None = None) -> dict:
    """Server -> client answer to one request.

    Success: {"v","type":"RESPONSE","reply_to","request","ok":true,"data":{...}}
    Failure: {"v","type":"RESPONSE","reply_to","request","ok":false,"error":{"code","message"}}
    """
    msg: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": RESPONSE,
        "reply_to": reply_to,
        "request": request_type,
        "ok": ok,
    }
    if ok:
        msg["data"] = data or {}
    else:
        msg["error"] = {
            "code": error_code or ErrorCode.INTERNAL_ERROR,
            "message": error_message or "Request failed.",
        }
    return msg


def make_error_response(request_type: str, reply_to: str | None, code: str, message: str) -> dict:
    return make_response(request_type, reply_to, ok=False, error_code=code, error_message=message)


def make_push(msg_type: str, data: dict | None = None) -> dict:
    """Server -> client event that is not a reply: {"v","type","ts","data"}."""
    if msg_type not in PUSH_TYPES:
        raise ValueError(f"Unknown push type: {msg_type}")
    return {"v": PROTOCOL_VERSION, "type": msg_type, "ts": round(time.time(), 3), "data": data or {}}


def make_udp(msg_type: str, **fields: Any) -> dict:
    """One UDP datagram body: {"v","type", ...fields}. Keep it small."""
    if msg_type not in UDP_CLIENT_TYPES and msg_type not in UDP_SERVER_TYPES:
        raise ValueError(f"Unknown UDP type: {msg_type}")
    return {"v": PROTOCOL_VERSION, "type": msg_type, **fields}


# ---------------------------------------------------------------------------
# TCP encoding / framing / decoding
# ---------------------------------------------------------------------------

def _dumps(msg: dict) -> str:
    # Compact separators keep packets small; ensure_ascii=False keeps UTF-8 (Arabic/French names).
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False)


def encode_tcp(msg: dict) -> bytes:
    """Serialise one message as a single newline-terminated UTF-8 JSON line."""
    line = _dumps(msg).encode(ENCODING)
    # json.dumps never produces a raw newline, so the delimiter is unambiguous.
    if len(line) + 1 > MAX_TCP_LINE_BYTES:
        raise ProtocolError(f"Outgoing message too large ({len(line)} bytes)", ErrorCode.BAD_REQUEST)
    return line + b"\n"


class LineFramer:
    """Reassembles newline-delimited messages from an arbitrary sequence of recv() chunks.

    Usage (in a receiver loop):
        framer = LineFramer()
        while True:
            chunk = sock.recv(TCP_RECV_BUFFER)
            if not chunk:            # peer closed the connection
                break
            for line in framer.feed(chunk):
                msg = decode_line(line)  # may raise ProtocolError for this one line

    One LineFramer per connection. Not thread-safe: only the connection's receiver thread uses it.
    """

    __slots__ = ("_buffer",)

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add received bytes; return every complete line (without the trailing newline)."""
        self._buffer.extend(chunk)
        lines: list[bytes] = []
        while True:
            idx = self._buffer.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._buffer[:idx])
            del self._buffer[: idx + 1]
            if line.endswith(b"\r"):        # tolerate CRLF (handy when testing with netcat/telnet)
                line = line[:-1]
            if line.strip():                # ignore blank keep-alive lines
                lines.append(line)
        if len(self._buffer) > MAX_TCP_LINE_BYTES:
            size = len(self._buffer)
            self._buffer.clear()
            raise FrameTooLarge(size)
        return lines

    @property
    def pending_bytes(self) -> int:
        """Bytes of an incomplete message still waiting for its newline."""
        return len(self._buffer)


def decode_line(line: bytes) -> dict:
    """Decode one framed line into a message dict, or raise ProtocolError."""
    try:
        text = line.decode(ENCODING)
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"Invalid UTF-8: {exc}") from exc
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Invalid JSON: {exc.msg} at column {exc.colno}") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("Message must be a JSON object")
    if not isinstance(msg.get("type"), str):
        raise ProtocolError("Message has no string 'type' field")
    version = msg.get("v", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"Unsupported protocol version {version!r}", ErrorCode.UNSUPPORTED_VERSION)
    return msg


def parse_request(msg: dict) -> tuple[str, str, dict]:
    """Validate the envelope of a client request. Returns (type, req_id, data).

    Raises ProtocolError(BAD_REQUEST / UNKNOWN_TYPE). Field-level checks of `data` are done by
    the server handler for each specific request type.
    """
    msg_type = msg.get("type")
    if msg_type not in REQUEST_TYPES:
        raise ProtocolError(f"Unknown request type: {msg_type!r}", ErrorCode.UNKNOWN_TYPE)
    req_id = msg.get("req_id")
    if not isinstance(req_id, str) or not (1 <= len(req_id) <= ID_MAX_LEN):
        raise ProtocolError("Missing or invalid 'req_id'", ErrorCode.BAD_REQUEST)
    data = msg.get("data", {})
    if not isinstance(data, dict):
        raise ProtocolError("'data' must be a JSON object", ErrorCode.BAD_REQUEST)
    return msg_type, req_id, data


# ---------------------------------------------------------------------------
# UDP encoding / decoding (one JSON object per datagram, no framing needed)
# ---------------------------------------------------------------------------

def encode_udp(msg: dict) -> bytes:
    payload = _dumps(msg).encode(ENCODING)
    if len(payload) > MAX_UDP_PACKET_BYTES:
        raise ProtocolError(f"UDP packet too large ({len(payload)} bytes)", ErrorCode.BAD_REQUEST)
    return payload


def decode_udp(payload: bytes) -> dict:
    """Decode one datagram. Raises ProtocolError (callers simply drop bad datagrams)."""
    if len(payload) > MAX_UDP_PACKET_BYTES:
        raise ProtocolError(f"UDP packet too large ({len(payload)} bytes)")
    return decode_line(payload)


def is_newer_seq(seq: Any, last_seq: int | None) -> bool:
    """UDP may reorder or duplicate datagrams: accept only strictly increasing sequence numbers."""
    return isinstance(seq, int) and (last_seq is None or seq > last_seq)
