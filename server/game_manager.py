"""
Games: rounds, server-controlled countdown, answer validation, scoring and ranking.

Threading
---------
* Each running game has ONE dedicated thread, `game-<room_id>`. It opens rounds, sends the
  TCP QUESTION, sends a UDP TIMER_TICK every second, closes rounds, computes points and sends
  ROUND_RESULT / GAME_OVER. It is the ONLY code that changes scores.
* Player-handler threads call `submit_answer()`. They only RECORD an answer (under state.lock),
  using the receipt time the handler measured as soon as the TCP line was decoded.
* The countdown never waits for submissions; it waits on `Round.wake`, an Event that is set
  when every connected player has answered or when the game must stop early.

Scoring (server clock only, time.monotonic())
---------------------------------------------
  correct = POINTS_CORRECT (10) + speed bonus, wrong / no answer = 0
  speed bonus = ceil(SPEED_BONUS_MAX * time_left / ROUND_DURATION_S)  -> +5 in the first 4 s ... +1 in the last 4 s
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

import config
from common import protocol as P
from common.protocol import AnswerKind, ErrorCode, GameEndReason, Phase, RoomStatus, RoundOutcome
from server.questions import Question, pick_questions
from server.server_state import RequestError, ServerState

if TYPE_CHECKING:
    from server.player_handler import PlayerSession
    from server.room_manager import Room, RoomManager
    from server.udp_server import UDPServer

log = logging.getLogger("codeclash.game")

ACTIVE, DISCONNECTED, LEFT = "active", "disconnected", "left"


@dataclass
class GamePlayer:
    player_id: str
    username: str
    status: str = ACTIVE
    score: int = 0
    correct: int = 0
    answered: int = 0
    rounds_played: int = 0          # rounds during which the player was connected when the round opened
    total_correct_ms: int = 0       # tie-breaker: total response time of correct answers
    total_response_ms: int = 0


@dataclass
class AnswerRecord:
    answer: str
    submission_id: str
    received_at: float              # server time.monotonic() when the TCP line was decoded
    response_ms: int


@dataclass
class Round:
    number: int
    round_id: str
    question: Question
    opened_at: float
    deadline: float
    answers: dict[str, AnswerRecord] = field(default_factory=dict)    # player_id -> answer (first one is final)
    by_submission: dict[str, str] = field(default_factory=dict)       # submission_id -> player_id
    closed: bool = False
    wake: threading.Event = field(default_factory=threading.Event)


def speed_bonus(time_left: float, duration: float) -> int:
    if not config.SPEED_BONUS_ENABLED or duration <= 0:
        return 0
    time_left = max(0.0, min(float(duration), time_left))
    return min(config.SPEED_BONUS_MAX, math.ceil(config.SPEED_BONUS_MAX * time_left / duration - 1e-9))


def rank_players(players: Iterable[GamePlayer], final: bool) -> list[dict]:
    """Competition ranking (1, 1, 3). Key: score desc, correct desc, total correct time asc.

    In the FINAL ranking, players who are not connected at the end (disconnected or left)
    are placed after every player who finished: leaving forfeits the game.
    """
    players = list(players)

    def key(p: GamePlayer):
        unfinished = 1 if (final and p.status != ACTIVE) else 0
        return (unfinished, -p.score, -p.correct, p.total_correct_ms)

    ordered = sorted(players, key=lambda p: (key(p), p.username.lower()))
    rows = []
    for p in ordered:
        rank = 1 + sum(1 for q in ordered if key(q) < key(p))
        rows.append({
            "rank": rank,
            "player_id": p.player_id,
            "username": p.username,
            "score": p.score,
            "correct": p.correct,
            "answered": p.answered,
            "rounds_played": p.rounds_played,
            "avg_response_ms": round(p.total_response_ms / p.answered) if p.answered else None,
            "status": p.status,
            "connected": p.status == ACTIVE,
            "finished": p.status == ACTIVE,
        })
    return rows


def _require_str(data: dict, key: str, max_len: int = P.ID_MAX_LEN) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise RequestError(ErrorCode.BAD_REQUEST, f"'{key}' must be a non-empty string (max {max_len}).")
    return value


class Game:
    def __init__(self, manager: "GameManager", room: "Room", game_id: str,
                 members: list[tuple[str, str]], questions: list[Question]):
        self.manager = manager
        self.state: ServerState = manager.state
        self.room = room
        self.game_id = game_id
        self.players: dict[str, GamePlayer] = {pid: GamePlayer(pid, name) for pid, name in members}
        self.questions = questions
        self.total_rounds = len(questions)
        self.duration = int(config.ROUND_DURATION_S)
        self.phase = Phase.STARTING
        self.phase_deadline = time.monotonic() + config.PRE_GAME_COUNTDOWN_S
        self.rounds: dict[str, Round] = {}
        self.current: Round | None = None
        self.rounds_completed = 0
        self.last_round_result: dict | None = None
        self.stop_event = threading.Event()
        self.end_reason = GameEndReason.COMPLETED
        self.thread = threading.Thread(target=self._run, name=f"game-{room.room_id}", daemon=True)

    # ------------------------------------------------------------------ helpers (lock held)
    def active_ids_locked(self) -> list[str]:
        return [pid for pid, p in self.players.items() if p.status == ACTIVE]

    def broadcast_locked(self, msg: dict) -> None:
        self.state.send_to_players_locked(self.active_ids_locked(), msg)

    def all_answered_locked(self, rnd: Round) -> bool:
        active = self.active_ids_locked()
        return bool(active) and all(pid in rnd.answers for pid in active)

    def request_stop_locked(self, reason: str) -> None:
        if self.stop_event.is_set():
            return
        self.end_reason = reason
        self.stop_event.set()
        if self.current is not None:
            self.current.wake.set()
        log.info("Game %s: stop requested (%s)", self.game_id, reason)

    def check_continue_locked(self) -> None:
        """After a disconnect/leave: end the game if too few players remain, or close the round early."""
        if len(self.active_ids_locked()) < config.MIN_CONNECTED_TO_CONTINUE:
            self.request_stop_locked(GameEndReason.NOT_ENOUGH_PLAYERS)
        elif (self.current is not None and not self.current.closed
              and config.END_ROUND_WHEN_ALL_ANSWERED and self.all_answered_locked(self.current)):
            self.current.wake.set()

    def question_payload_locked(self, rnd: Round) -> dict:
        return {
            **rnd.question.public_payload(),          # never contains the answer
            "room_id": self.room.room_id,
            "game_id": self.game_id,
            "round_id": rnd.round_id,
            "round_number": rnd.number,
            "total_rounds": self.total_rounds,
            "duration_s": self.duration,
            "max_points": config.POINTS_CORRECT + (config.SPEED_BONUS_MAX if config.SPEED_BONUS_ENABLED else 0),
        }

    def sync_payload_locked(self, player_id: str) -> dict:
        rnd = self.current if (self.current is not None and not self.current.closed) else None
        record = rnd.answers.get(player_id) if rnd else None
        return {
            "room_id": self.room.room_id,
            "game_id": self.game_id,
            "phase": self.phase,
            "round_number": rnd.number if rnd else self.rounds_completed,
            "total_rounds": self.total_rounds,
            "round_duration_s": self.duration,
            "seconds_left": max(0, math.ceil(self.phase_deadline - time.monotonic())),
            "players": [{"player_id": p.player_id, "username": p.username} for p in self.players.values()],
            "scoreboard": rank_players(self.players.values(), final=False),
            "question": self.question_payload_locked(rnd) if rnd else None,
            "already_answered": record is not None,
            "my_answer": record.answer if record else None,
            "last_round_result": self.last_round_result,
        }

    # ------------------------------------------------------------------ game thread
    def _run(self) -> None:
        log.info("Game %s started in room %s with %s (%d rounds)", self.game_id, self.room.room_id,
                 ", ".join(p.username for p in self.players.values()), self.total_rounds)
        try:
            self._countdown(Phase.STARTING, config.PRE_GAME_COUNTDOWN_S, None, 0)
            for number, question in enumerate(self.questions, 1):
                if self.stop_event.is_set():
                    break
                rnd = self._play_round(number, question)
                if self.stop_event.is_set() or number == self.total_rounds:
                    break
                self._countdown(Phase.RESULTS, config.RESULTS_DURATION_S, rnd.round_id, number)
        except Exception:  # noqa: BLE001 - a bug must not leave the room stuck IN_PROGRESS
            log.exception("Game %s crashed; ending it", self.game_id)
        finally:
            self._finish()

    def _send_tick(self, phase: str, seconds_left: int, round_id: str | None, round_number: int,
                   duration: int) -> None:
        with self.state.lock:
            seq = self.room.next_udp_seq()
            addrs = self.state.udp_addrs_locked(self.active_ids_locked())
        tick = P.make_udp(P.TIMER_TICK, room_id=self.room.room_id, game_id=self.game_id, round_id=round_id,
                          round_number=round_number, total_rounds=self.total_rounds, phase=phase,
                          seconds_left=seconds_left, duration=duration, seq=seq)
        self.manager.udp.send(addrs, tick)        # UDP I/O outside the lock

    def _countdown(self, phase: str, seconds: int, round_id: str | None, round_number: int) -> None:
        """STARTING / RESULTS countdown: one UDP tick per second, interruptible by stop_event."""
        start = time.monotonic()
        with self.state.lock:
            self.phase = phase
            self.phase_deadline = start + seconds
        for i in range(int(seconds)):
            self._send_tick(phase, int(seconds) - i, round_id, round_number, int(seconds))
            # Deadline-based sleep: no drift even if sending took a few milliseconds.
            if self.stop_event.wait(timeout=max(0.0, start + i + 1 - time.monotonic())):
                return

    def _play_round(self, number: int, question: Question) -> Round:
        round_id = f"{self.game_id}-R{number}"
        with self.state.lock:
            now = time.monotonic()
            rnd = Round(number, round_id, question, opened_at=now, deadline=now + self.duration)
            self.rounds[round_id] = rnd
            self.current = rnd
            self.phase = Phase.QUESTION
            self.phase_deadline = rnd.deadline
            for pid in self.active_ids_locked():
                self.players[pid].rounds_played += 1
            # Authoritative round start over TCP (same message, same instant, to every player).
            self.broadcast_locked(P.make_push(P.QUESTION, self.question_payload_locked(rnd)))
        log.info("Game %s round %d/%d opened: %s [%s]", self.game_id, number, self.total_rounds,
                 question.qid, question.category)

        # Countdown over UDP: seconds_left = D, D-1, ..., 0, tick k sent at opened_at + k.
        for k in range(self.duration + 1):
            seconds_left = self.duration - k
            self._send_tick(Phase.QUESTION, seconds_left, round_id, number, self.duration)
            if seconds_left == 0:
                break
            if rnd.wake.wait(timeout=max(0.0, rnd.opened_at + k + 1 - time.monotonic())):
                break                                   # everyone answered, or the game must stop
        self._close_round(rnd)
        return rnd

    def _close_round(self, rnd: Round) -> None:
        with self.state.lock:
            if rnd.closed:
                return
            rnd.closed = True                           # from now on no answer can be recorded
            q = rnd.question
            results = []
            for pid, gp in self.players.items():
                rec = rnd.answers.get(pid)
                if rec is not None:
                    correct = q.is_correct(rec.answer)
                    base = config.POINTS_CORRECT if correct else config.POINTS_WRONG
                    bonus = speed_bonus(rnd.deadline - rec.received_at, self.duration) if correct else 0
                    # The ONLY place where scores change: once per player per round.
                    gp.score += base + bonus
                    gp.answered += 1
                    gp.total_response_ms += rec.response_ms
                    if correct:
                        gp.correct += 1
                        gp.total_correct_ms += rec.response_ms
                    results.append({"player_id": pid, "username": gp.username, "outcome": RoundOutcome.ANSWERED,
                                    "answer": rec.answer, "correct": correct, "base_points": base,
                                    "bonus_points": bonus, "points": base + bonus,
                                    "response_time_ms": rec.response_ms})
                else:
                    outcome = {ACTIVE: RoundOutcome.NO_ANSWER, DISCONNECTED: RoundOutcome.DISCONNECTED,
                               LEFT: RoundOutcome.LEFT}[gp.status]
                    results.append({"player_id": pid, "username": gp.username, "outcome": outcome,
                                    "answer": None, "correct": False, "base_points": 0, "bonus_points": 0,
                                    "points": 0, "response_time_ms": None})
            self.rounds_completed += 1
            self.current = None
            self.phase = Phase.RESULTS
            is_last = rnd.number >= self.total_rounds or self.stop_event.is_set()
            payload = {
                "room_id": self.room.room_id,
                "game_id": self.game_id,
                "round_id": rnd.round_id,
                "round_number": rnd.number,
                "total_rounds": self.total_rounds,
                "question_id": q.qid,
                "category": q.category,
                "prompt": q.prompt,
                "code": q.code,
                "answer_kind": q.kind,
                "options": [{"key": k, "text": t} for k, t in q.options],
                "correct_answer": q.answer,
                "correct_text": q.correct_text(),
                "explanation": q.explanation,
                "results": results,
                "scoreboard": rank_players(self.players.values(), final=False),
                "is_last_round": is_last,
                "next_round_in_s": None if is_last else config.RESULTS_DURATION_S,
            }
            self.last_round_result = payload
            # Authoritative round end over TCP.
            self.broadcast_locked(P.make_push(P.ROUND_RESULT, payload))
        summary = ", ".join(
            f"{r['username']} +{r['points']}" if r["outcome"] == RoundOutcome.ANSWERED else f"{r['username']} {r['outcome']}"
            for r in results)
        log.info("Game %s round %d closed (answer %s): %s", self.game_id, rnd.number, q.answer, summary)

    def _finish(self) -> None:
        with self.state.lock:
            if self.current is not None and not self.current.closed:
                self.current.closed = True             # a crash mid-round: no more answers
            self.phase = Phase.FINISHED
            leaderboard = rank_players(self.players.values(), final=True)
            winners = [{"player_id": r["player_id"], "username": r["username"]}
                       for r in leaderboard if r["rank"] == 1 and r["finished"]]
            payload = {
                "room_id": self.room.room_id,
                "game_id": self.game_id,
                "reason": self.end_reason,
                "total_rounds": self.total_rounds,
                "rounds_completed": self.rounds_completed,
                "leaderboard": leaderboard,
                "winners": winners,
                "is_tie": len(winners) > 1,
            }
            self.broadcast_locked(P.make_push(P.GAME_OVER, payload))
            self.manager.forget_locked(self.game_id)
            if self.room.game is self:
                self.manager.rooms.on_game_over_locked(self.room)
        log.info("Game %s over (%s). Winner(s): %s", self.game_id, self.end_reason,
                 ", ".join(w["username"] for w in winners) or "none")


class GameManager:
    def __init__(self, state: ServerState, udp: "UDPServer"):
        self.state = state
        self.udp = udp
        self.rooms: "RoomManager | None" = None       # set by main_server after both managers exist
        self._games: dict[str, Game] = {}
        self._rng = random.Random()

    # ------------------------------------------------------------------ lifecycle (lock held)
    def start_locked(self, room: "Room") -> str:
        game_id = self.state.new_game_id_locked()
        members = [(pid, m.username) for pid, m in room.members.items() if m.connected]
        game = Game(self, room, game_id, members, pick_questions(config.TOTAL_ROUNDS, self._rng))
        room.game = game
        self._games[game_id] = game
        self.state.send_to_players_locked([pid for pid, _ in members], P.make_push(P.GAME_STARTED, {
            "room_id": room.room_id,
            "game_id": game_id,
            "total_rounds": game.total_rounds,
            "round_duration_s": game.duration,
            "results_duration_s": config.RESULTS_DURATION_S,
            "starts_in_s": config.PRE_GAME_COUNTDOWN_S,
            "players": [{"player_id": pid, "username": name} for pid, name in members],
            "scoring": {"correct": config.POINTS_CORRECT, "speed_bonus_max": config.SPEED_BONUS_MAX
                        if config.SPEED_BONUS_ENABLED else 0},
        }))
        game.thread.start()        # the thread blocks on state.lock until this request releases it
        return game_id

    def forget_locked(self, game_id: str) -> None:
        self._games.pop(game_id, None)

    def running_games_locked(self) -> list[Game]:
        return list(self._games.values())

    def player_disconnected_locked(self, room: "Room", player_id: str) -> None:
        game = room.game
        gp = game.players.get(player_id) if game else None
        if gp is not None and gp.status == ACTIVE:
            gp.status = DISCONNECTED
            log.info("Game %s: %s disconnected (score kept: %d)", game.game_id, gp.username, gp.score)
            game.check_continue_locked()

    def player_left_locked(self, room: "Room", player_id: str) -> None:
        game = room.game
        gp = game.players.get(player_id) if game else None
        if gp is not None and gp.status != LEFT:
            gp.status = LEFT
            log.info("Game %s: %s left the game (forfeit)", game.game_id, gp.username)
            game.check_continue_locked()

    def player_reconnected_locked(self, room: "Room", player_id: str, session: "PlayerSession") -> None:
        game = room.game
        gp = game.players.get(player_id) if game else None
        if gp is None:
            return
        if gp.status == DISCONNECTED:
            gp.status = ACTIVE
        session.send(P.make_push(P.GAME_SYNC, game.sync_payload_locked(player_id)))

    def stop_all(self, reason: str, join_timeout: float = 3.0) -> None:
        with self.state.lock:
            games = list(self._games.values())
            for game in games:
                game.request_stop_locked(reason)
        for game in games:
            game.thread.join(timeout=join_timeout)

    # ------------------------------------------------------------------ SUBMIT_ANSWER (handler thread)
    def submit_answer(self, session: "PlayerSession", data: dict, received_at: float) -> dict:
        room_id = _require_str(data, "room_id")
        round_id = _require_str(data, "round_id")
        question_id = _require_str(data, "question_id")
        submission_id = _require_str(data, "submission_id")
        answer = data.get("answer")
        if not isinstance(answer, str) or len(answer) > P.ANSWER_MAX_LEN:
            raise RequestError(ErrorCode.INVALID_ANSWER, f"answer must be text (max {P.ANSWER_MAX_LEN} characters).")
        pid = session.player_id

        with self.state.lock:
            room = self.state.rooms.get(room_id)
            if room is None or self.state.player_room.get(pid) != room_id:
                raise RequestError(ErrorCode.NOT_IN_GAME, "You are not playing in this room.")
            game = room.game
            if game is None or room.status != RoomStatus.IN_PROGRESS:
                raise RequestError(ErrorCode.GAME_NOT_RUNNING, "No game is running in this room.")
            gp = game.players.get(pid)
            if gp is None or gp.status == LEFT:
                raise RequestError(ErrorCode.NOT_IN_GAME, "You are not a player of this game.")
            rnd = game.rounds.get(round_id)
            if rnd is None:
                raise RequestError(ErrorCode.WRONG_ROUND, f"Round {round_id} is not open.")

            # 1) Same submission_id again (retry / double click): idempotent, never re-scored.
            owner = rnd.by_submission.get(submission_id)
            if owner is not None:
                if owner != pid:
                    raise RequestError(ErrorCode.BAD_REQUEST, "submission_id already used.")
                rec = rnd.answers[pid]
                return {"accepted": True, "duplicate": True, "answer": rec.answer,
                        "response_time_ms": rec.response_ms,
                        "answered_count": sum(1 for a in game.active_ids_locked() if a in rnd.answers),
                        "active_count": len(game.active_ids_locked())}

            # 2) Timing is judged with the receipt time measured by the handler thread.
            if rnd.closed or received_at > rnd.deadline:
                raise RequestError(ErrorCode.ROUND_CLOSED, "Too late: this round is closed.")
            if question_id != rnd.question.qid:
                raise RequestError(ErrorCode.WRONG_ROUND, "question_id does not match the current round.")
            # 3) One answer per player per round: the first accepted one is final.
            if pid in rnd.answers:
                raise RequestError(ErrorCode.ALREADY_ANSWERED, "You already answered this round.")
            if not rnd.question.is_valid_answer(answer):
                raise RequestError(ErrorCode.INVALID_ANSWER, "That is not one of the options.")

            stored = answer.strip().upper() if rnd.question.kind == AnswerKind.CHOICE else answer.strip()
            response_ms = max(0, round((received_at - rnd.opened_at) * 1000))
            rnd.answers[pid] = AnswerRecord(stored, submission_id, received_at, response_ms)
            rnd.by_submission[submission_id] = pid

            active = game.active_ids_locked()
            answered = sum(1 for a in active if a in rnd.answers)
            if config.END_ROUND_WHEN_ALL_ANSWERED and self._all_in(active, rnd):
                rnd.wake.set()                          # lets the game thread close the round now
            notice = P.make_udp(P.LIVE_NOTICE, room_id=room_id, game_id=game.game_id, round_id=round_id,
                                seq=room.next_udp_seq(), event="ANSWER_LOCKED", username=gp.username,
                                answered=answered, active=len(active))
            notice_addrs = self.state.udp_addrs_locked(active)
            result = {"accepted": True, "duplicate": False, "answer": stored, "response_time_ms": response_ms,
                      "answered_count": answered, "active_count": len(active)}

        self.udp.send(notice_addrs, notice)          # UDP I/O outside the lock
        log.info("%s locked an answer for round %d in %d ms (%d/%d answered)",
                 gp.username, rnd.number, response_ms, answered, len(active))
        return result

    @staticmethod
    def _all_in(active: list[str], rnd: Round) -> bool:
        return bool(active) and all(pid in rnd.answers for pid in active)
