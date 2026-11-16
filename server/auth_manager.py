"""
Player accounts: registration and login with salted PBKDF2-HMAC-SHA256 password hashes.

Storage: config.USERS_FILE (server/data/users.json), created automatically. Format:
  {"users": {"kacem": {"username": "Kacem", "player_id": "P3F9A1C", "salt": "<hex>",
                       "hash": "<hex>", "iterations": 200000, "created_at": 1762861234.5}}}
Keys are lower-cased usernames, so "Kacem" and "kacem" are the same account.

Thread safety: this manager has its own lock (never held together with state.lock). The slow
part, PBKDF2 (~0.1 s), runs OUTSIDE every lock so one login never stalls other players.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from common.protocol import ErrorCode, validate_password, validate_username
from server.server_state import RequestError

log = logging.getLogger("codeclash.auth")


def hash_password(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


class AuthManager:
    def __init__(self, users_file: Path, iterations: int):
        self.path = Path(users_file)
        self.iterations = iterations
        self.lock = threading.Lock()
        self.users: dict[str, dict] = self._load()
        # Used to spend the same time on unknown usernames (no username-existence timing leak).
        self._dummy_salt = os.urandom(16)
        log.info("Loaded %d account(s) from %s", len(self.users), self.path)

    # ------------------------------------------------------------------ persistence
    def _load(self) -> dict[str, dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            users = data.get("users", {})
            return users if isinstance(users, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.error("Could not read %s (%s); starting with no accounts", self.path, exc)
            return {}

    def _save_locked(self) -> None:
        """Atomic write: write a temp file, then rename over the old one."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"users": self.users}, fh, indent=2)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ operations
    def register(self, username: object, password: object) -> tuple[str, str]:
        """Create an account. Returns (player_id, username). Raises RequestError."""
        problem = validate_username(username)
        if problem:
            raise RequestError(ErrorCode.INVALID_USERNAME, problem)
        problem = validate_password(password)
        if problem:
            raise RequestError(ErrorCode.INVALID_PASSWORD, problem)
        assert isinstance(username, str) and isinstance(password, str)

        salt = os.urandom(16)
        digest = hash_password(password, salt, self.iterations)        # slow part, no lock held
        key = username.lower()
        with self.lock:
            if key in self.users:
                raise RequestError(ErrorCode.USERNAME_TAKEN, f"Username '{username}' is already taken.")
            existing_ids = {u["player_id"] for u in self.users.values()}
            player_id = "P" + secrets.token_hex(3).upper()
            while player_id in existing_ids:
                player_id = "P" + secrets.token_hex(3).upper()
            self.users[key] = {
                "username": username,
                "player_id": player_id,
                "salt": salt.hex(),
                "hash": digest.hex(),
                "iterations": self.iterations,
                "created_at": round(time.time(), 3),
            }
            self._save_locked()
        log.info("Registered new account %s (%s)", username, player_id)
        return player_id, username

    def verify(self, username: object, password: object) -> tuple[str, str]:
        """Check credentials. Returns (player_id, username) or raises INVALID_CREDENTIALS."""
        if not isinstance(username, str) or not isinstance(password, str):
            raise RequestError(ErrorCode.BAD_REQUEST, "username and password must be strings.")
        with self.lock:
            record = dict(self.users.get(username.lower(), {}))
        if not record:
            hash_password(password, self._dummy_salt, self.iterations)   # equalise timing
            raise RequestError(ErrorCode.INVALID_CREDENTIALS, "Wrong username or password.")
        digest = hash_password(password, bytes.fromhex(record["salt"]), int(record["iterations"]))
        if not hmac.compare_digest(digest.hex(), record["hash"]):          # constant-time compare
            raise RequestError(ErrorCode.INVALID_CREDENTIALS, "Wrong username or password.")
        return record["player_id"], record["username"]

    def account_count(self) -> int:
        with self.lock:
            return len(self.users)
