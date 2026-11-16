# CodeClash — Phase 1: Architecture, Protocol & Threading Model

Team reference for the Computer Networks project. Everything later phases build is defined here:
which machine runs what, which messages travel over TCP and which over UDP, the exact JSON of every
message, the game rules, and which thread does what.

**Demo setup:** Kacem's **MacBook = server**, three **Windows laptops = clients**, all four on the same LAN.

**Status:** all seven phases are implemented. Setup and run instructions are in the README; the test plan is in
[TESTING.md](TESTING.md). The names in this document match the code.

---

## 0. Key decisions

| # | Decision | Why |
|---|----------|-----|
| 1 | One server process on the MacBook starts the TCP service and the UDP service together, both on port **8888** | TCP and UDP have separate port spaces; one process = one authoritative game state |
| 2 | Each Windows client runs Streamlit **locally** (`127.0.0.1:8501`) and opens its own sockets to the MacBook | No inbound access to port 8501 is needed; each player is a real, separate network client |
| 3 | TCP messages = **newline-delimited JSON** (`LineFramer`) | TCP is a byte stream; one `recv()` ≠ one message |
| 4 | Every TCP request carries a `req_id`; the server answers with exactly one `RESPONSE` whose `reply_to` = that id | Lets the client match answers to requests while server pushes arrive in between |
| 5 | **TCP decides, UDP decorates.** Round start/end, answers, scores, winner travel over TCP. UDP only carries the countdown and live notices | If a UDP datagram is lost, nothing important is lost |
| 6 | The **client sends UDP first** (`UDP_HELLO` every 5 s) and the server replies to the **observed source address** | Works through the Windows firewall (a reply to outbound traffic) and through NAT later |
| 7 | Per player: one **handler thread** (reads/processes) plus one **sender thread** (writes from an outbox queue) | A slow or frozen client can only block its own threads, never the game loop or other players |
| 8 | One `threading.Lock` (`state.lock`) guards all shared game state; no network I/O while holding it | No lock-ordering deadlocks; the critical sections are microseconds long |
| 9 | Answers are recorded by handler threads; **points are computed once, by the game thread, when the round closes** | Duplicate or simultaneous submissions can never add points twice |
| 10 | The client UI is driven by a **mirror of server state**; `st.session_state` holds only UI details | The server stays authoritative; Streamlit reruns cannot corrupt the game |

---

## 1. Deployment

```
                        Same Wi-Fi router / LAN (e.g. 192.168.1.0/24)
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                                                                              │
 │   COMPUTER 1 — Kacem's MacBook (SERVER)       e.g. 192.168.1.25              │
 │   python3 -m server.main_server                                              │
 │     TCP  0.0.0.0:8888  (SOCK_STREAM) ◄── persistent connection per player    │
 │     UDP  0.0.0.0:8888  (SOCK_DGRAM)  ◄── HELLO in / TIMER_TICK out           │
 │                                                                              │
 │        ▲  TCP + UDP            ▲  TCP + UDP              ▲  TCP + UDP        │
 │        │                       │                         │                   │
 │   COMPUTER 2 — Windows    COMPUTER 3 — Windows    COMPUTER 4 — Windows       │
 │   Player 1                Player 2                Player 3                   │
 │   streamlit run app.py    streamlit run app.py    streamlit run app.py       │
 │   browser → localhost:8501 (UI only, never exposed to the LAN)               │
 │   sockets → 192.168.1.25:8888                                                │
 └──────────────────────────────────────────────────────────────────────────────┘
```

| Item | Value |
|------|-------|
| Server bind address | `SERVER_BIND_HOST = "0.0.0.0"` (all IPv4 interfaces of the MacBook) |
| Address clients connect to | `SERVER_HOST` = the MacBook's LAN IPv4 (printed by the server at startup) |
| TCP port / UDP port | 8888 / 8888 |
| Client UDP port | OS-chosen (`CLIENT_UDP_PORT = 0`); fixed port optional via `CODECLASH_CLIENT_UDP_PORT` |
| Streamlit | `127.0.0.1:8501` on each Windows PC |

`SERVER_HOST` on a client is resolved in this order: env var `CODECLASH_SERVER_HOST` → `client_settings.json`
(written by the login page) → `DEFAULT_SERVER_HOST` in `config.py`. `0.0.0.0`, `localhost` and `127.x` are
rejected (`config.validate_server_host`). `CODECLASH_ALLOW_LOOPBACK=1` exists only for single-computer development.

> The brief's `SERVER_HOST = "0.0.0.0"` is the **bind** address. It is named `SERVER_BIND_HOST` in `config.py`
> so that nobody points a client at 0.0.0.0 by accident.

---

## 2. Components

```
 SERVER (MacBook)                                   CLIENT (each Windows PC)
 ───────────────────────────────────────────        ──────────────────────────────────────────
 main_server.py   starts everything, admin console   app.py          Streamlit entry, page routing
 tcp_server.py    listen + accept loop               ui/*.py         pages (login … final results)
 player_handler.py  PlayerSession, handler thread,   client/network_manager.py
                  sender thread, request dispatch                    request()/snapshot() facade
 udp_server.py    UDP socket, receiver thread,       client/tcp_client.py   persistent TCP + receiver
                  endpoint registry, tick sender     client/udp_client.py   UDP HELLO + tick receiver
 server_state.py  shared state + state.lock
 auth_manager.py  PBKDF2 hashing, users.json                 common/protocol.py (shared by both)
 room_manager.py  rooms, host, ready, leave rules            config.py          (shared by both)
 game_manager.py  game thread, rounds, scoring
 questions.py     question bank WITH answers (server only)
```

Request path for one action (e.g. Submit Answer):

```
Streamlit button → NetworkManager.request(SUBMIT_ANSWER) → TcpClient.send (framed JSON)
   ── TCP ──► player-handler thread → LineFramer → parse_request → game_manager.submit_answer()
              (takes state.lock, records answer, releases) → RESPONSE put in this player's outbox
   ◄── TCP ── player-sender thread sendall → client tcp-receiver thread → wakes the waiting request()
```

---

## 3. TCP vs UDP responsibilities

| Interaction | Protocol | Reason |
|-------------|----------|--------|
| Register / login / logout | TCP | Must arrive once, reliably, in order |
| Lobby list, create / join / leave room, ready, start | TCP | State-changing actions must not be lost |
| `QUESTION` (round start) and `ROUND_RESULT` (round end) | TCP | Authoritative events every client must receive |
| `SUBMIT_ANSWER` and its acknowledgement | TCP | An answer can never be lost or duplicated by the network |
| Scores, rankings, `GAME_OVER`, player status notifications | TCP | Players need the authoritative result |
| Heartbeat `PING` | TCP | Detects a dead TCP session (Wi-Fi drop, laptop sleep) |
| `TIMER_TICK` countdown (1 per second) | UDP | Small and time-sensitive; a lost tick is replaced by the next one 1 s later; retransmitting an old second is useless |
| `LIVE_NOTICE` ("2/3 players locked in") | UDP | Nice-to-have live information |
| `UDP_HELLO` / `UDP_ACK` | UDP | Registers the client's UDP endpoint and proves the UDP path works (shown in the UI) |

If UDP is blocked, the game still works: the client keeps counting down locally from the `duration_s` in
`QUESTION` and the UI shows "UDP lost". The demo, of course, should show UDP working.

---

## 4. Wire protocol

### 4.1 TCP framing

* One message = one JSON object encoded as UTF-8, terminated by `\n` (`encode_tcp`).
* `json.dumps` escapes newlines inside strings, so code snippets with line breaks are safe; the only raw
  `\n` on the wire is the delimiter.
* The receiver appends every `recv()` chunk to a buffer and extracts complete lines (`LineFramer.feed`).
  Splitting happens on the byte `\n` *before* UTF-8 decoding, which is safe for multi-byte characters.
* Limits: 64 KiB per line (`FrameTooLarge` → close the connection). `\r\n` and blank lines are tolerated.
* Malformed line with no usable `req_id` → server pushes `ERROR` and keeps the connection.

### 4.2 Envelopes

Request (client → server):
```json
{"v":1,"type":"JOIN_ROOM","req_id":"6f1c0e2a9b0d4c51a8f3e2d1c0b9a877","data":{"room_id":"R1042"}}
```
Response (server → client), exactly one per request:
```json
{"v":1,"type":"RESPONSE","reply_to":"6f1c0e2a…","request":"JOIN_ROOM","ok":true,"data":{"room":{…}}}
{"v":1,"type":"RESPONSE","reply_to":"6f1c0e2a…","request":"JOIN_ROOM","ok":false,
 "error":{"code":"ROOM_FULL","message":"Room 'Finals' is full (3/3)."}}
```
Push (server → client, not a reply):
```json
{"v":1,"type":"ROOM_UPDATE","ts":1762861234.512,"data":{"room":{…},"notice":"sara joined"}}
```
UDP datagram (one JSON object per datagram, ≤ 1024 bytes):
```json
{"v":1,"type":"TIMER_TICK","room_id":"R1042","game_id":"G7","round_id":"G7-R3","phase":"QUESTION","seconds_left":12,"duration":20,"seq":118}
```

### 4.3 Identifiers

| ID | Format | Created by | Notes |
|----|--------|-----------|-------|
| `player_id` | `P` + 6 hex, e.g. `P3F9A1C` | server at REGISTER | permanent, stored in `users.json` |
| `room_id` | `R` + 4 digits, e.g. `R1042` | server at CREATE_ROOM | |
| `game_id` | `G` + counter, e.g. `G7` | server at START_GAME | new id for every Play Again |
| `round_id` | `<game_id>-R<n>`, e.g. `G7-R3` | server | answers must name the current round |
| `question_id` | category prefix + number, e.g. `PO-03` | question bank | |
| `req_id` | 32 hex (`new_id()`) | client, per request | matches RESPONSE to request |
| `submission_id` | 32 hex | client, **once per round** | reused on retries → idempotent |
| `udp_token` | 32 hex | server at LOGIN | proves a UDP_HELLO belongs to that TCP session |
| `seq` | integer, per room, strictly increasing | server | clients drop UDP packets with `seq ≤ last_seq` |

### 4.4 TCP requests (client → server)

All need a logged-in connection except `REGISTER`, `LOGIN`, `PING` (`UNAUTHENTICATED_REQUESTS`).

| Type | `data` sent | `data` in ok RESPONSE | Possible error codes |
|------|-------------|-----------------------|----------------------|
| `REGISTER` | `username`, `password` | `player_id`, `username` | `INVALID_USERNAME`, `INVALID_PASSWORD`, `USERNAME_TAKEN` |
| `LOGIN` | `username`, `password` | `player_id`, `username`, `udp_token`, `udp_port`, `room` (snapshot or null) | `INVALID_CREDENTIALS`, `ALREADY_AUTHENTICATED` |
| `LOGOUT` | — | — | — |
| `PING` | — | `server_time` (client computes RTT) | — |
| `LIST_ROOMS` | — | `rooms[]`, `online_players[]` | — |
| `CREATE_ROOM` | `room_name` | `room` (creator is host and member) | `INVALID_ROOM_NAME`, `ALREADY_IN_ROOM` |
| `JOIN_ROOM` | `room_id` | `room` | `ROOM_NOT_FOUND`, `ROOM_FULL`, `GAME_IN_PROGRESS`, `ALREADY_IN_ROOM` |
| `LEAVE_ROOM` | `room_id` | — | `NOT_IN_ROOM` |
| `SET_READY` | `room_id`, `ready` (bool) | `room` | `NOT_IN_ROOM`, `GAME_IN_PROGRESS` |
| `START_GAME` | `room_id` | `game_id` | `NOT_HOST`, `NOT_ENOUGH_PLAYERS`, `PLAYERS_NOT_READY`, `GAME_IN_PROGRESS`, `BAD_REQUEST` (room finished: use Play Again) |
| `SUBMIT_ANSWER` | `room_id`, `round_id`, `question_id`, `answer`, `submission_id` | `accepted`, `duplicate`, `response_time_ms`, `answered_count`, `active_count` | `NOT_IN_GAME`, `GAME_NOT_RUNNING`, `WRONG_ROUND`, `ROUND_CLOSED`, `ALREADY_ANSWERED`, `INVALID_ANSWER` |
| `PLAY_AGAIN` | `room_id` | `room` (back to WAITING) | `NOT_HOST`, `GAME_NOT_FINISHED` |

Any request may also fail with `BAD_REQUEST`, `UNKNOWN_TYPE`, `NOT_AUTHENTICATED` or `INTERNAL_ERROR`.

Note that the `SUBMIT_ANSWER` response says only whether the answer was **accepted**, never whether it is
**correct**. Correctness is revealed to everyone at the same time in `ROUND_RESULT`, so a player cannot
shout "I got it!" across the room in the middle of a round.

### 4.5 TCP pushes (server → client)

| Type | When | `data` |
|------|------|--------|
| `WELCOME` | right after the TCP connection is accepted | `server_name`, `protocol_version`, `udp_port`, `heartbeat_interval_s`, `heartbeat_timeout_s`, `room_capacity` |
| `LOBBY_UPDATE` | rooms or online players changed (sent to every logged-in player) | `rooms[]`, `online_players[]` |
| `ROOM_UPDATE` | membership, ready flags, host or status of your room changed | `room`, `notice` (optional text) |
| `PLAYER_STATUS` | a player came online, disconnected, reconnected, left | `player_id`, `username`, `status`, `room_id` |
| `GAME_STARTED` | host started the game | `room_id`, `game_id`, `total_rounds`, `round_duration_s`, `starts_in_s`, `players[]` |
| `QUESTION` | a round opens (**authoritative round start**) | `room_id`, `game_id`, `round_id`, `round_number`, `total_rounds`, `question_id`, `category`, `prompt`, `code` (or null), `answer_kind` (`choice`/`text`), `options[{key,text}]`, `duration_s`, `max_points` — **never the answer** |
| `ROUND_RESULT` | a round closes (**authoritative round end**) | `round_id`, `round_number`, the question again (`prompt`, `code`, `options`), `correct_answer`, `correct_text`, `explanation`, `results[]`, `scoreboard[]`, `is_last_round`, `next_round_in_s` (null after the last round) |
| `GAME_OVER` | last round finished, too few players left, or server shutdown | `game_id`, `reason` (`completed` / `not_enough_players` / `server_shutdown`), `leaderboard[]`, `winners[]`, `is_tie`, `total_rounds`, `rounds_completed` |
| `GAME_SYNC` | a player logs back in during their game | `game_id`, `phase`, `round_number`, `total_rounds`, `seconds_left`, `scoreboard[]`, `question` (or null), `already_answered`, `my_answer`, `last_round_result` |
| `KICKED` | the same account logged in from elsewhere | `reason` |
| `ERROR` | malformed line without a usable `req_id` | `code`, `message` |

Shared objects:
```json
room:            {"room_id":"R1042","name":"Finals","status":"WAITING","host_id":"P3F9A1C","capacity":3,"game_id":null,
                  "players":[{"player_id":"P3F9A1C","username":"kacem","is_host":true,"ready":true,"connected":true}]}
lobby room item: {"room_id":"R1042","name":"Finals","status":"WAITING","player_count":2,"capacity":3,
                  "player_names":["kacem","sara"],"host_name":"kacem"}
results[] item:  {"player_id":"P3F9A1C","username":"kacem","outcome":"answered","answer":"B","correct":true,
                  "base_points":10,"bonus_points":4,"points":14,"response_time_ms":6870}
scoreboard[] and leaderboard[] (same shape):
                 {"rank":1,"player_id":"P3F9A1C","username":"kacem","score":71,"correct":6,"answered":8,"rounds_played":8,
                  "avg_response_ms":7420,"status":"active","connected":true,"finished":true}
```

### 4.6 UDP datagrams

| Type | Direction | Fields | Sent |
|------|-----------|--------|------|
| `UDP_HELLO` | client → server | `player_id`, `token`, `seq` | right after LOGIN, then every 5 s |
| `UDP_ACK` | server → client | `seq` (echo), `server_time` | reply to each HELLO |
| `TIMER_TICK` | server → room players | `room_id`, `game_id`, `round_id`, `phase` (`STARTING`/`QUESTION`/`RESULTS`), `seconds_left`, `duration`, `seq` | every second while a game runs |
| `LIVE_NOTICE` | server → room players | `room_id`, `round_id`, `seq`, `event` (`ANSWER_LOCKED`), `answered`, `active` | when someone's answer is accepted |

* The server accepts a `UDP_HELLO` only if `(player_id, token)` matches a logged-in TCP session; it then stores
  the **source address from `recvfrom()`** as that player's endpoint. Nobody can redirect another player's ticks.
* Every packet carries room/game/round ids and `seq` so a client ignores late, duplicated or foreign packets
  (`is_newer_seq`).

### 4.7 Example: connect and log in

```
C→S TCP  (connect to 192.168.1.25:8888)
S→C TCP  {"type":"WELCOME","data":{"udp_port":8888,"heartbeat_interval_s":5,…}}
C→S TCP  {"type":"LOGIN","req_id":"9b1f…","data":{"username":"kacem","password":"••••"}}
S→C TCP  {"type":"RESPONSE","reply_to":"9b1f…","ok":true,"data":{"player_id":"P3F9A1C","udp_token":"5d2c…","room":null,…}}
C→S UDP  {"type":"UDP_HELLO","player_id":"P3F9A1C","token":"5d2c…","seq":1}
S→C UDP  {"type":"UDP_ACK","seq":1,"server_time":1762861234.12}          → UI shows "UDP ●"
S→C TCP  {"type":"LOBBY_UPDATE","data":{"rooms":[…],"online_players":[…]}} → everyone's lobby refreshes
```

### 4.8 Example: one round

```
game-R1042 thread (server)          TCP (reliable)                        UDP (best effort)
──────────────────────────          ─────────────────────────────         ─────────────────────────────
open round 3, opened_at = now   ──► QUESTION round_id=G7-R3 (×3)
                                                                      ──► TIMER_TICK 20  seq=101 (×3)
                                                                      ──► TIMER_TICK 19  seq=102
handler(sara): SUBMIT_ANSWER     ◄── {"answer":"B","submission_id":…}
  receipt = monotonic()          ──► RESPONSE accepted, 6870 ms
                                                                      ──► LIVE_NOTICE answered 1/3
handler(amine) + handler(kacem) submit at the same moment → serialised by state.lock, each timestamped on receipt
                                                                      ──► LIVE_NOTICE answered 3/3
all connected answered (or deadline) → close round, score ONCE
                                 ──► ROUND_RESULT (correct answer, points, scoreboard) (×3)
                                                                      ──► TIMER_TICK RESULTS 6…1
next round … after round 8:      ──► GAME_OVER (leaderboard, winners)
```

---

## 5. Game rules

### 5.1 Room lifecycle

```
            CREATE_ROOM (creator = host)
                    │
                    ▼
   ┌──────────►  WAITING  ── START_GAME: caller is host, 3 members, the 2 others are ready ──► IN_PROGRESS
   │                ▲                                                                           │
   │                └──────────── PLAY_AGAIN (host) ◄──────── FINISHED ◄── last round done ─────┘
   │                                                              ▲         or < 2 players connected
   └── room deleted when its last member leaves ──────────────────┘
```

* Capacity 3; `JOIN_ROOM` only while `WAITING` and not full. A player is in at most one room.
* The host's Start click counts as the host being ready; the two other members must press Ready.
* Leaving a `WAITING` room: the player is removed; if it was the host, the longest-present member becomes host;
  an empty room is deleted.
* `PLAY_AGAIN` resets scores and ready flags and returns the room to `WAITING` (new `game_id`, new questions).

### 5.2 Rounds

* 8 rounds per game (`TOTAL_ROUNDS`), 2 from each category: **Predict the Output**, **Programming Logic**,
  **Algorithms & Complexity**, **Debugging & Code Analysis**. The server picks them at random at game start;
  all three players get the same questions in the same order.
* 3 s "get ready" countdown → each round 20 s (`ROUND_DURATION_S`) → 6 s results (`RESULTS_DURATION_S`).
  One game ≈ 3.5 minutes. For a quicker demo start the server with `CODECLASH_TOTAL_ROUNDS=4`
  (every timing in `config.py` can be overridden the same way, which is how the tests use 3-second rounds).
* A round closes at its deadline, or earlier once every **connected** player has answered.
* One answer per player per round; the first accepted answer is final.
* Answers arriving after the deadline (server receipt time) are rejected with `ROUND_CLOSED`.
* Questions are mostly multiple choice (`answer_kind = "choice"`, answer = option key). A few are short text
  (`"text"`), compared with `normalize_text_answer` (trimmed, whitespace collapsed, case-insensitive).

### 5.3 Scoring

* Correct = **10** points, wrong or no answer = **0**.
* Speed bonus (correct answers only): `bonus = ceil(5 × time_left / 20)`, i.e. +5 when answered in the first
  4 s, +4 in the next 4 s, …, +1 in the last 4 s. Example: correct with 13 s left → ceil(3.25) = 4 → 14 points.
* **Fairness:** `opened_at` and the receipt time are both taken from the server's `time.monotonic()`; client
  clocks are never used. LAN latency (1–20 ms) is tiny compared with a 4-second bonus band.

### 5.4 Ranking and ties

Sort key: **score** (high first) → **correct answers** (more first) → **total response time of correct
answers** (lower first). Players equal on all three share the rank (competition ranking 1, 1, 3) and are all
announced as winners (`is_tie = true`). Players who were not connected at the end of the game (`finished =
false`) are ranked after everyone who finished (they forfeit), but their scores are still shown.

### 5.5 Disconnection rules

A disconnection is detected when `recv()` returns `b""`, raises `ConnectionResetError`/`OSError`, or when no
message arrives for `HEARTBEAT_TIMEOUT_S = 15` s (the client pings every 5 s). The server then closes the socket,
stops that player's threads, removes their UDP endpoint and token, and keeps running.

| Situation | Rule |
|-----------|------|
| In the lobby | Player removed from the online list; `LOBBY_UPDATE` to everyone |
| In a `WAITING` room | Removed from the room (same as Leave); host passes on; `PLAYER_STATUS` + `ROOM_UPDATE` |
| During a game | Marked `DISCONNECTED`; their score is kept; they get 0 for rounds they miss; the others receive `PLAYER_STATUS`; the current round may close early because only connected players are awaited |
| Fewer than 2 players still connected | The game ends immediately: `GAME_OVER` with `reason = "not_enough_players"` |
| Logs back in before the game ends | Re-attached to the room, receives `GAME_SYNC`, may still answer the current round if it is open; counts as finished if connected at the end |
| Clicks Leave during a game | Same as a disconnection, but permanent (`LEFT`, cannot rejoin that game) |
| Host disconnects during a game | Host role passes to the next connected player (needed for Play Again) |
| Same account logs in again elsewhere | The old session receives `KICKED` and is closed; the new one takes over |

---

## 6. Server threading model

| Thread name | How many | Started by | Job | Waits on |
|-------------|----------|------------|-----|----------|
| `MainThread` | 1 | Python | starts TCP + UDP services, admin console (`status`, `threads`, `players`, `rooms`, `kick <user>`, `quit`), clean shutdown | `input()` |
| `tcp-accept` | 1 | main | `accept()` → creates a `PlayerSession` and starts its two threads | `accept()` (1 s timeout) |
| `player-handler-<name>` | **1 per connected player → 3 in a game** | `tcp-accept` | `recv` → `LineFramer` → `parse_request` → managers → `RESPONSE`; heartbeat timeout; cleanup in `finally` | `recv()` (1 s timeout) |
| `player-sender-<name>` | 1 per connected player | `tcp-accept` | takes messages from the session outbox (`queue.Queue`) and `sendall()`s them | `outbox.get()` |
| `udp-receiver` | 1 | main | `recvfrom` → validate `(player_id, token)` → store endpoint → `UDP_ACK` | `recvfrom()` (1 s timeout) |
| `game-<room_id>` | 1 per running game | `START_GAME` handler | runs the rounds, sends `TIMER_TICK` each second, closes rounds, scores, sends `ROUND_RESULT` / `GAME_OVER` | `Event.wait(timeout)` |

The admin console `threads` command lists these live, which is how we show the three `player-handler-*` threads
during the demo. Thread names are renamed to include the username after LOGIN.

**Why a separate sender thread?** Any thread may need to send to a player (the game thread broadcasts a
question; Sara's handler notifies the room that Sara joined). If those threads called `sendall()` directly, a
stalled client with a full TCP buffer would block the game loop for everybody, and two threads writing to one
socket could interleave bytes. Instead every send is `session.outbox.put(msg)`, which never blocks, and only
that player's sender thread touches the socket's write side.

### 6.1 Locking rules

1. **One lock** — `state.lock` (`threading.Lock`) in `server_state.py` guards sessions, rooms, games, rounds and
   scores. `auth_manager` has its own lock for `users.json`, never held together with `state.lock`.
2. **No blocking work under the lock**: no `sendall`, `recv`, `sendto`, file writes or password hashing
   (PBKDF2 takes ~0.1 s). Putting a message in a session outbox (`session.send`) *is* allowed under the lock:
   it is a non-blocking queue put, and doing it there keeps message order identical to state-change order.
   UDP sends collect the endpoints under the lock and call `sendto()` after releasing it.
3. Public manager methods acquire the lock; helpers named `_..._locked` assume it is already held. This keeps
   a plain (non-reentrant) `Lock` safe.
4. **Only the game thread changes scores**, inside the round-close step. Handler threads only record answers.
5. **Session identity check** on cleanup: a handler removes a player's state only if
   `state.sessions[player_id] is this_session` (after a takeover login the newer session must survive).
6. All game timing uses the server's `time.monotonic()`.

### 6.2 Simultaneous and duplicate submissions

* Each handler thread timestamps a `SUBMIT_ANSWER` **as soon as the line is decoded**, before waiting for the
  lock, so the order in which threads get the lock does not affect fairness or response times.
* Three layers stop double counting:
  1. **Client** — one `submission_id` per round, kept in `st.session_state`; the Submit button is disabled once
     accepted. A Streamlit rerun or double click resends the *same* id.
  2. **Server, per round** — `round.answers[player_id]` is written once. Same `submission_id` again →
     `ok:true, duplicate:true` (idempotent). A different id → `ALREADY_ANSWERED`.
  3. **Scoring** happens once, when the game thread closes the round (`round.closed` set under the lock), so
     no submission path can ever add points.

---

## 7. Client threading model and Streamlit integration

| Thread | Job |
|--------|-----|
| Streamlit script thread | renders pages; calls `net.request(type, data)` (blocks ≤ 5 s for the matching RESPONSE) and `net.snapshot()` |
| `net-tcp-receiver` | `recv` → `LineFramer` → a RESPONSE wakes the waiting `request()` (a `threading.Condition` keyed by `req_id`); pushes update the mirrored state |
| `net-udp-receiver` | `recvfrom` (only from the server's IP) → drop old `seq` → store the latest tick / live notice |
| `net-heartbeat` | every 5 s: TCP `PING` (measures RTT, detects a dead server) and `UDP_HELLO` (keeps the UDP path open) |

* **One `NetworkManager` per Streamlit process**, created with `@st.cache_resource`. It survives script
  reruns and browser refreshes, and its three threads start exactly once (no duplicate threads, no leaked sockets).
  One Streamlit process per Windows PC = one player.
* **Pages are chosen from the mirrored server state**, not from buttons: not connected → Login; logged in, no
  room → Lobby; room `WAITING` → Waiting Room; `QUESTION` received → Game Arena; `ROUND_RESULT` → Round Results;
  `GAME_OVER` → Final Results. So all three screens change together when the server says so.
* **Live refresh:** a `@st.fragment(run_every=0.5)` block redraws the countdown, live scoreboard and
  connection lights twice a second. When the mirrored state's version number changes (a new TCP event), it
  calls `st.rerun(scope="app")` to switch pages. Answer widgets sit outside the fragment and are keyed by
  `round_id`, so the periodic refresh never wipes a selection.
* If UDP ticks stop, the countdown is estimated locally from the last tick (or `duration_s`) and the UI shows
  "UDP lost".
* `st.session_state` holds only UI details (current tab, selected option, `submission_id`).
* A lost connection (not user-initiated) keeps the player's identity and shows a **Reconnect** page;
  Reconnect = connect + LOGIN with the credentials kept in memory, and the server answers with `GAME_SYNC`.
* Development on one computer: run three Streamlit processes on ports 8501/8502/8503 with
  `CODECLASH_ALLOW_LOOPBACK=1`. Each is a separate process with its own sockets, but this does **not**
  replace the four-computer demo.

---

## 8. Folder structure

```
CodeClash/
├── app.py                    # Streamlit entry point (client PCs): theme, NetworkManager, page routing   [Phase 4 ✅]
├── config.py                 # ports, timings, scoring, server-IP resolution                            [Phase 1 ✅]
├── requirements.txt          # streamlit only — the server uses the standard library only               [Phase 1 ✅]
├── .gitignore                                                                                           [Phase 1 ✅]
├── README.md               setup, run, test, troubleshooting                                         [Phase 7 ✅]
├── start_server.sh         MacBook: start the server                                                 [Phase 7 ✅]
├── start_client.bat / .sh  Windows: start the player app (double-click / Git Bash)                    [Phase 7 ✅]
├── .streamlit/config.toml  UI bound to 127.0.0.1:8501, dark theme, static files                      [Phase 4 ✅]
├── static/fonts/           bundled fonts, so the UI looks right without internet                     [Phase 4 ✅]
├── common/
│   ├── __init__.py                                                                                      [Phase 1 ✅]
│   └── protocol.py           # message types, error codes, envelopes, framing, UDP helpers, validation   [Phase 1 ✅]
├── server/                   # needed on the MacBook only
│   ├── __init__.py
│   ├── main_server.py        # entry point: starts TCP + UDP together, admin console, shutdown           [Phase 2 ✅]
│   ├── tcp_server.py         # listening socket + accept loop → PlayerSession per connection             [Phase 2 ✅]
│   ├── udp_server.py         # UDP socket, receiver thread, endpoint registry, send helpers               [Phase 2 ✅]
│   ├── player_handler.py     # PlayerSession, handler + sender threads, request dispatch table           [Phase 2 ✅]
│   ├── server_state.py       # the shared state object and state.lock                                    [Phase 2 ✅]
│   ├── auth_manager.py       # PBKDF2-HMAC-SHA256 hashing, users.json                                    [Phase 2 ✅]
│   ├── room_manager.py       # rooms, host, ready flags, leave/disconnect rules                          [Phase 2 ✅]
│   ├── game_manager.py       # game thread, rounds, validation, scoring, ranking                        [Phase 2 ✅]
│   ├── questions.py          # question bank WITH answers                                               [Phase 2 ✅]
│   └── data/                 # users.json is created here at runtime (gitignored)
├── client/                   # used by the Streamlit app on each Windows PC
│   ├── __init__.py
│   ├── tcp_client.py         # persistent TCP connection, framed send, receiver thread                   [Phase 3 ✅]
│   ├── udp_client.py         # UDP socket, HELLO keep-alive, tick receiver                               [Phase 3 ✅]
│   └── network_manager.py    # request/response matching, mirrored state, heartbeat                     [Phase 3 ✅]
├── ui/
│   ├── __init__.py
│   ├── theme.py              # CSS (dark navy, electric blue/purple) + shared components                 [Phase 4 ✅]
│   ├── login.py · lobby.py · waiting_room.py · game_arena.py · results.py                             [Phase 4 ✅]
├── tools/
│   └── net_check.py          # 4-computer TCP + UDP reachability check                                  [Phase 1 ✅]
├── tests/
│   ├── test_protocol.py      # framing / envelope / validation tests                                    [Phase 1 ✅]
│   ├── test_questions.py     # runs the code snippets to prove the answer key                           [Phase 2 ✅]
│   ├── test_server_integration.py  # real server process + real sockets, 9 scenarios                   [Phase 2 ✅]
│   ├── test_network_manager.py     # client networking against the real server                       [Phase 6 ✅]
│   ├── test_ui_app.py        # the Streamlit app through a whole game (Streamlit AppTest)               [Phase 6 ✅]
│   └── bot_client.py         # headless TEST client (one per process / PC), not the product UI          [Phase 2 ✅]
└── docs/
    ├── ARCHITECTURE.md       # this document                                                            [Phase 1 ✅]
    ├── TESTING.md            # requirement-by-requirement test plan + demo run sheet                    [Phase 6 ✅]
    └── screenshots/
```

`server/questions.py` contains the answer key. The Windows PCs never import it, and you can delete the
`server/` folder from the client copies if you want to remove the temptation.

---

## 9. Platform notes

### 9.1 MacBook (server)

1. **Python 3.11+**: `python3 --version`. The `python3` that comes with Apple's command-line tools may be
   older; if so, install Python 3.12 from python.org or with Homebrew.
2. **venv**: `cd ~/Downloads/CodeClash && python3 -m venv venv && source venv/bin/activate`
   (the server needs no pip packages).
3. **LAN IP**: `ipconfig getifaddr en0` (Wi-Fi). The server also prints it at startup.
4. **Firewall** (System Settings → Network → Firewall). If it is on, click **Allow** when macOS asks whether
   Python may accept incoming connections, and make sure "Block all incoming connections" is off. To
   pre-approve the venv's interpreter from Terminal:
   ```bash
   PY="$(python3 -c 'import os,sys; print(os.path.realpath(sys.executable))')"
   sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$PY"
   sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp "$PY"
   ```
5. **Local Network privacy (macOS 15 Sequoia and later)**: programs started from **Terminal.app are not
   affected**, but apps such as VS Code can be. If the server log shows `[Errno 65] No route to host` when it
   sends UDP to a client, allow VS Code in System Settings → Privacy & Security → Local Network, or start the
   server from Terminal.app.
6. **Port 8888 free?** `lsof -nP -i :8888` (Jupyter also uses 8888 by default).
7. **Do not let it sleep** during the demo: run `caffeinate -dims` in a second Terminal tab, keep the charger
   plugged in and the lid open. If the Mac sleeps, all three players disconnect.

### 9.2 Windows laptops (clients)

1. Python 3.11+: `py --version` (tick "Add Python to PATH" during installation).
2. venv — Git Bash: `python -m venv venv && source venv/Scripts/activate && python -m pip install -r requirements.txt`
   — PowerShell: `.\venv\Scripts\Activate.ps1` (if blocked: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`).
3. Set the Wi-Fi network profile to **Private** (Settings → Network & Internet → Wi-Fi → the network).
4. When Windows Defender Firewall asks about Python the first time, allow it on **Private networks**.
5. Normally no inbound rule is needed: the client sends UDP first and the ticks come back as replies. If
   `net_check` shows TCP OK but UDP failing and the Mac's firewall is fine, fix the client UDP port and open it:
   ```powershell
   # Admin PowerShell, once:
   New-NetFirewallRule -DisplayName "CodeClash client UDP 8889" -Direction Inbound -Protocol UDP -LocalPort 8889 -Action Allow -Profile Private
   # In the terminal that runs the client:
   $env:CODECLASH_CLIENT_UDP_PORT = "8889"          # PowerShell
   export CODECLASH_CLIENT_UDP_PORT=8889            # Git Bash
   ```
6. Server address: type it on the login screen (saved to `client_settings.json`), or
   `export CODECLASH_SERVER_HOST=192.168.1.25` (Git Bash) / `$env:CODECLASH_SERVER_HOST = "192.168.1.25"` (PowerShell).

### 9.3 The network itself

All four machines must be on the same router. Campus and guest Wi-Fi often block device-to-device traffic
(client isolation). A home router or a phone hotspot usually works, but **verify with `net_check`** before
building anything on top of it.

---

## 10. Security notes and cross-network readiness

* Passwords are stored as **PBKDF2-HMAC-SHA256** (200,000 iterations, random 16-byte salt per user) in
  `server/data/users.json`, compared with `hmac.compare_digest`. They travel unencrypted over the LAN
  (no TLS), so players should not reuse real passwords. Wrapping the TCP socket with Python's `ssl` module would
  be a possible extension.
* The server never trusts a client's score, correctness flag or timestamp; it validates every field and size.
* The UDP token stops another machine from claiming a player's UDP endpoint.
* **Cross-network later (bonus)**: nothing in the game logic depends on the LAN. The server binds 0.0.0.0,
  clients accept an IP or a hostname, UDP replies go to the observed source address, and the 5-second
  `UDP_HELLO` keeps NAT mappings alive. With a VPN overlay that carries both TCP and UDP (Tailscale or ZeroTier,
  for example), only `SERVER_HOST` changes. Never expose this development server directly to the internet.

---

## 11. How to test Phase 1

**A. Unit tests (any computer, from the project root):**
```bash
python3 -m unittest -v tests.test_protocol     # MacBook
python -m unittest -v tests.test_protocol      # Windows
```
Pass: `Ran 22 tests … OK`. They prove that framing survives merged messages, byte-by-byte splits, code with
newlines, UTF-8 characters split across chunks and a real socket read with `recv(7)`, and that the client
address guard rejects `0.0.0.0` / `localhost`.

**B. Four-computer network check:**
```bash
# MacBook (Terminal.app), venv active:
python3 tools/net_check.py server
# Each Windows PC, within the same minute:
python tools/net_check.py client --host <MACBOOK_IP> --name PC1 --hold 60    # PC2, PC3 on the others
```
Pass: every PC prints `TCP PASS | framing PASS | UDP PASS -> READY FOR PHASE 2`, and the server log shows
`active TCP handler threads: 3` with UDP pings from three different IP addresses.

**C. Unexpected disconnection:** while the clients are holding, close one PC's terminal window. The server
logs `TCP closed … active TCP handler threads: 2` and keeps serving the other two.

### Phase 2 — the backend

**D. Automated tests (any computer, ~1 minute):**
```bash
python3 -m unittest -v tests.test_protocol tests.test_questions tests.test_server_integration
```
Pass: `Ran 38 tests … OK`. The integration suite starts the real server as a separate process with 3-second rounds
and covers: authentication and protocol errors, room rules (full room, not host, not ready, host transfer,
empty-room deletion), a full game with three simultaneous submissions, five submissions merged into one TCP segment
(only one counted), duplicate `submission_id` retries, late answers, leaving mid-game, a crash (TCP RST) followed by
a reconnect with `GAME_SYNC`, two crashes ending the game, the heartbeat timeout, a same-account takeover, and three
separate bot **processes** playing a full match.

**E. Real four-computer backend test (before the UI exists):**
```bash
# MacBook (Terminal.app), venv active. Quick game: 4 rounds.
CODECLASH_TOTAL_ROUNDS=4 python3 -m server.main_server
# Windows PC 1 (Git Bash or PowerShell, venv active):
python -m tests.bot_client --host <MACBOOK_IP> --user pc1 --create Finals
# Windows PC 2 and PC 3:
python -m tests.bot_client --host <MACBOOK_IP> --user pc2 --join auto
python -m tests.bot_client --host <MACBOOK_IP> --user pc3 --join auto --disconnect-after-round 2
```
While it runs, type `threads` in the server console: it lists `player-handler-pc1`, `-pc2`, `-pc3`. Each bot
prints the UDP ticks it received per round. After round 2, PC 3 crashes on purpose; the other two keep playing
and the server prints `Player pc3 … disconnected: connection reset by peer`. Also try `kick pc2` in the console.

---

## 12. Team split and next phases

| Role | Files | Agree with the others on |
|------|-------|--------------------------|
| TCP / server core | `main_server.py`, `tcp_server.py`, `player_handler.py`, `auth_manager.py` | envelope, error codes, session lifecycle |
| UDP / concurrency | `udp_server.py`, `server_state.py`, locking, disconnect handling | ids, `seq`, token, lock rules (§6.1) |
| Game logic | `room_manager.py`, `game_manager.py`, `questions.py` | payloads of `QUESTION`, `ROUND_RESULT`, `GAME_OVER` |
| GUI / client | `client/*`, `ui/*`, `app.py`, README | mirrored state, page routing (§7) |

* **Phase 2 (backend): done** — the `server/` package, the headless test client and the test suites.
* **Phase 3 (client networking): done** — `client/tcp_client.py`, `udp_client.py`, `network_manager.py`.
* **Phase 4 (Streamlit UI): done** — `app.py`, `ui/` (six pages + theme).
* **Phase 5 (integration): done** — verified in a real browser against the real server and bots.
* **Phase 6 (testing): done** — `tests/test_network_manager.py`, `tests/test_ui_app.py`, [TESTING.md](TESTING.md).
* **Phase 7 (documentation): done** — README, start scripts, VS Code launch configurations.
