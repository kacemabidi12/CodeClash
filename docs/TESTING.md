# CodeClash — test plan

Two kinds of evidence for every requirement:

* **Manual (four computers):** what to do on the real LAN (MacBook server + three Windows PCs), and
  what you must see. Use these during the demo.
* **Automated:** the unit/integration test that proves the same thing on one computer, with real
  sockets and a real server process.

Commands use `python3` on the Mac and `python` on Windows. "Server console" means the terminal on
the MacBook where `bash start_server.sh` runs (type commands at the `codeclash>` prompt).

---

## 1. Requirement checklist

| # | Requirement | Manual check (four computers) | Expected result | Automated proof |
|---|---|---|---|---|
| 1 | The server starts successfully | `bash start_server.sh` on the Mac | Banner shows `TCP 0.0.0.0:8888 (SOCK_STREAM)`, `UDP 0.0.0.0:8888 (SOCK_DGRAM)` and the LAN IP; log lines `UDP server listening` and `TCP server listening` | Every integration suite starts the server as a separate process |
| 2 | One client connects through TCP | Start the app on PC 1, type the Mac's IP, **Connect** | App top bar: green `TCP 192.168.x.x:8888`. Server log: `TCP connection from <PC1 IP>:<port> -> handler thread … started` | `test_network_manager.test_login_udp_and_lobby_mirror` |
| 3 | Three clients connect simultaneously | Start the app on PC 2 and PC 3 too; everyone logs in | Server `players` lists three players with three different IPs; every lobby's "Online now" shows all three | `test_server_integration.test_09_three_bot_processes_play_a_full_match` |
| 4 | Three separate player-handler threads | Server console: `threads` | `=> 3 TCP player-handler thread(s): player-handler-<a>, player-handler-<b>, player-handler-<c>` | `test_09` checks the log for `[player-handler-<name>]` of each of the 3 players |
| 5 | UDP countdown packets reach all three clients | Start a game | On every PC the timer says "Live countdown over UDP" and the top bar shows `UDP live — N ticks` (N grows every second). Server `players` shows a UDP endpoint for each player | `test_03` (each client got QUESTION/STARTING ticks, strictly increasing `seq`), `test_network_manager` (countdown source = `udp`) |
| 6 | Players can register and log in | **Create account and log in** on each PC; later **Log in** | Lobby shows "Welcome, <name>" and a player ID like `P3F9A1C`. Wrong password → "Wrong username or password." `server/data/users.json` holds hashes, no passwords | `test_01_auth_and_protocol_errors`, `test_ui_app` |
| 7 | Players can create and join rooms | PC 1: **Create room**. PC 2, 3: **Join** | All three in the waiting room with three filled seats; PC 1 shows the Host tag; the lobby on any other client shows the room as 3/3 | `test_02_room_rules`, `test_ui_app` |
| 8 | A game starts with exactly three players | Host tries **Start game** with 2 players, then with 3 | With 2: button disabled, "Waiting for 1 more player." With 3 but someone not ready: "Waiting for <name> to get ready." With 3 ready: the game starts on all screens together | `test_02` (`NOT_ENOUGH_PLAYERS`, `PLAYERS_NOT_READY`, `NOT_HOST`, `ROOM_FULL`) |
| 9 | All players receive the same questions | Compare the three screens during a round | Same round number, category, code and options on all three, appearing at the same moment | `test_03` asserts identical `round_id` and `question_id` on all three clients and that no answer field is sent |
| 10 | Players can submit answers simultaneously | All three click **Lock in answer** at the same time | Each sees "Answer locked in … Received by the server X s after the question opened"; the round closes early when all three answered | `test_03`: three threads submit through a barrier at the same instant; plus 5 submissions merged into ONE TCP packet → exactly one accepted |
| 11 | The server calculates correct scores | Look at the round-results page | Correct = 10 + speed bonus (shown as "10 + 4 speed"), wrong/no answer = 0; the correct option is highlighted with who picked what | `test_03` recomputes every score from the round results; `test_questions` executes the code snippets to prove the answer key |
| 12 | The leaderboard updates correctly | Watch the Scoreboard / Ranking after each round | Totals match the sum of each round's points; ties share a rank | `test_03` (scoreboard after every round = running sum); `test_network_manager` |
| 13 | The winner is announced | Finish the game | "<name> wins" (or "You win", or "Tie between …") with the final leaderboard: points, correct answers, average answer time | `test_03`, `test_04` (winner by forfeit), `test_ui_app` |
| 14 | One client disconnects unexpectedly without crashing the server | During a round, close PC 3's app window (or pull its Wi-Fi, or `kick <name>`) | Server log: `Player <name>@<ip> disconnected: …`; the server keeps running; PC 1 and 2 get a "lost connection" toast and keep playing; the round closes once the two connected players answered. PC 3 restarts the app, logs in → back in the same game | `test_05` (TCP RST mid-game → round continues → reconnect with `GAME_SYNC`), `test_06` (2 drop → game ends), `test_07` (silent client dropped after the heartbeat timeout) |
| 15 | Works on four separate computers on the same LAN | Section 2 below, end to end | Everything above, with four different IP addresses in the server's `players` output | `tools/net_check.py` (TCP, framing and UDP across the real LAN) |

Extra checks worth showing:

| Check | How | Expected |
|---|---|---|
| Duplicate submission | Double-click **Lock in answer** quickly | Counted once (the second click reuses the same `submission_id`) |
| Late answer | Wait for 0 s, then try to answer | "Too late: this round is closed." |
| Host leaves before start | Host clicks **Leave room** in the waiting room | Host role passes to the next player; the room stays |
| Same account twice | Log in as the same user on two PCs | The older session is closed with a message; one session survives |
| Server shutdown | `quit` in the server console during a game | Server log: `Game … over (server_shutdown)`, then `Server stopped.`; every app switches to "Connection to the server was lost" |

---

## 2. Four-computer run sheet (demo day)

**Before the demo (once, with internet):** on each Windows PC, double-click `start_client.bat`
once so Streamlit is installed, then close it.

| Step | Where | Action | Show |
|---|---|---|---|
| 0 | all | Same Wi-Fi (not campus/guest Wi-Fi). Mac: charger in, lid open, `caffeinate -dims` in a second tab | — |
| 1 | Mac | `ipconfig getifaddr en0`, write the IP on the board | the IP |
| 2 | Mac | `python3 tools/net_check.py server` | — |
| 3 | PCs | `python tools/net_check.py client --host <IP> --name PC1 --hold 60` (PC2, PC3) | three `READY` lines; Mac: `active TCP handler threads: 3`. Then Ctrl+C on the Mac |
| 4 | Mac | `CODECLASH_TOTAL_ROUNDS=4 bash start_server.sh` | the banner: TCP and UDP both listening |
| 5 | PCs | double-click `start_client.bat`, type the IP, **Connect**, create accounts | server log: three `TCP connection from …` lines with three IPs |
| 6 | Mac | `threads` then `players` | 3 × `player-handler-<name>`; each player's TCP and UDP endpoints |
| 7 | PCs | PC 1 creates a room; PC 2 and 3 join; try **Start game** with 2 players | disabled, with the reason |
| 8 | PCs | everyone ready, host starts | same question on three screens; "Live countdown over UDP" |
| 9 | PCs | all three lock in at the same time | "2 of 3 answered" updates live; round closes early; results |
| 10 | PC 3 | mid-round: close the browser tab **and** the start_client window | server keeps running and logs the disconnect; PC 1/2 toast "lost connection" |
| 11 | PC 3 | start again, log in | "Reconnected — you are back in the game"; PC 1/2 see "reconnected" |
| 12 | PCs | finish the game | winner + leaderboard; host clicks **Play again** |
| 13 | Mac | `rooms`, then `quit` | clean shutdown; every app shows "Connection to the server was lost" |

---

## 3. Automated suites

| Command | Time | What runs |
|---|---|---|
| `python3 -m unittest -v tests.test_protocol` | <1 s | 22 framing/envelope/validation tests, incl. a real socket read with `recv(7)` |
| `python3 -m unittest -v tests.test_questions` | <1 s | answer key proven by executing the snippets |
| `python3 -m unittest -v tests.test_server_integration` | ~1 min | real server process; 9 scenarios incl. 3 bot processes |
| `python3 -m unittest -v tests.test_network_manager` | ~10 s | client networking layer against the real server |
| `python3 -m unittest -v tests.test_ui_app` | ~20 s | the Streamlit app through a whole game (needs `streamlit`) |

All of them at once:
```bash
python3 -m unittest -v tests.test_protocol tests.test_questions tests.test_server_integration tests.test_network_manager tests.test_ui_app
```
The suites start their own server on a free port of the same computer (`127.0.0.1`, loopback allowed
for tests only), so they never touch a server that is running for real.
