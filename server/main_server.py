"""
CodeClash server entry point — run on the MacBook from the project root:

    python3 -m server.main_server

It starts the TCP service and the UDP service together (each in its own thread), prints the
LAN address the Windows clients must use, then offers an admin console:

    status    counters (connections, players, rooms, UDP packets)
    threads   every live thread — shows one player-handler-<name> per connected player
    players   logged-in players with their TCP and UDP endpoints
    rooms     rooms, members, host, ready flags, running game
    kick <username>   close that player's connection (simulates a network drop)
    quit      stop the server cleanly (Ctrl+C works too)
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import signal
import socket
import sys
import threading
import time

import config
from common import protocol as P
from common.protocol import GameEndReason
from server.auth_manager import AuthManager
from server.game_manager import GameManager
from server.player_handler import handle_login, handle_logout, handle_ping, handle_register
from server.room_manager import RoomManager
from server.server_state import ServerState
from server.tcp_server import TCPServer
from server.udp_server import UDPServer

log = logging.getLogger("codeclash.server")


class CodeClashServer:
    """Wires the components together. One instance = the one authoritative game server."""

    def __init__(self, host: str, tcp_port: int, udp_port: int):
        self.state = ServerState()
        self.auth = AuthManager(config.USERS_FILE, config.PBKDF2_ITERATIONS)
        self.udp = UDPServer(self.state, host, udp_port)
        self.games = GameManager(self.state, self.udp)
        self.rooms = RoomManager(self.state, self.games)
        self.games.rooms = self.rooms
        self.tcp = TCPServer(self, host, tcp_port)
        # Request type -> handler(session, data, received_at) -> response data
        self.dispatch = {
            P.REGISTER: handle_register,
            P.LOGIN: handle_login,
            P.LOGOUT: handle_logout,
            P.PING: handle_ping,
            P.LIST_ROOMS: self.rooms.list_rooms,
            P.CREATE_ROOM: self.rooms.create_room,
            P.JOIN_ROOM: self.rooms.join_room,
            P.LEAVE_ROOM: self.rooms.leave_room,
            P.SET_READY: self.rooms.set_ready,
            P.START_GAME: self.rooms.start_game,
            P.PLAY_AGAIN: self.rooms.play_again,
            P.SUBMIT_ANSWER: self.games.submit_answer,
        }
        assert set(self.dispatch) == set(P.REQUEST_TYPES), "every request type needs a handler"
        self._stopped = False

    def start(self) -> None:
        # Both services run simultaneously in the same process, each with its own thread.
        self.udp.start()
        self.tcp.start()

    def shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        log.info("Shutting down...")
        self.state.shutting_down.set()
        self.tcp.stop()                                            # no new connections
        self.games.stop_all(GameEndReason.SERVER_SHUTDOWN)         # GAME_OVER to players in a game
        time.sleep(0.3)                                            # let sender threads flush
        with self.state.lock:
            sessions = list(self.state.all_sessions)
        for session in sessions:
            session.close("server shutting down")
        for session in sessions:
            session.handler_thread.join(timeout=2.0)
        self.udp.stop()
        log.info("Server stopped.")

    # ------------------------------------------------------------------ admin console
    def print_status(self) -> None:
        with self.state.lock:
            summary = self.state.summary_locked()
            games = len(self.games.running_games_locked())
        print(f"  uptime {summary['uptime_s']} s | TCP sessions {summary['connections']} | "
              f"logged in {summary['logged_in']} | rooms {summary['rooms']} | games running {games} | "
              f"accounts {self.auth.account_count()} | UDP {self.udp.stats()}")

    @staticmethod
    def print_threads() -> None:
        names = sorted(t.name for t in threading.enumerate())
        handlers = [n for n in names if n.startswith("player-handler-")]
        print(f"  {len(names)} threads alive:")
        for name in names:
            print(f"    {name}")
        print(f"  => {len(handlers)} TCP player-handler thread(s): {', '.join(handlers) or '-'}")

    def print_players(self) -> None:
        now = time.monotonic()
        with self.state.lock:
            rows = []
            for pid, s in sorted(self.state.sessions_by_player.items(), key=lambda kv: kv[1].username or ""):
                udp = f"{s.udp_addr[0]}:{s.udp_addr[1]} ({now - s.udp_last_seen:.0f}s ago)" if s.udp_addr else "not registered"
                rows.append(f"    {s.username:<16} {pid}  TCP {s.addr[0]}:{s.addr[1]}  UDP {udp}  "
                            f"room {self.state.player_room.get(pid, '-')}  last msg {now - s.last_seen:.0f}s ago")
            guests = len(self.state.all_sessions) - len(self.state.sessions_by_player)
        print("\n".join(rows) if rows else "    (nobody logged in)")
        print(f"  + {guests} connection(s) not logged in")

    def print_rooms(self) -> None:
        with self.state.lock:
            rooms = list(self.state.rooms.values())
            if not rooms:
                print("    (no rooms)")
            for r in rooms:
                members = ", ".join(
                    f"{m.username}{'*' if m.player_id == r.host_id else ''}"
                    f"{'' if m.connected else ' (disconnected)'}{' ready' if m.ready else ''}"
                    for m in r.members.values())
                game = ""
                if r.game is not None:
                    cur = r.game.current
                    game = f" | {r.game.game_id} phase {r.game.phase}, round {cur.number if cur else r.game.rounds_completed}/{r.game.total_rounds}"
                print(f"    {r.room_id} '{r.name}' {r.status} [{members}]{game}")

    def kick(self, username: str) -> None:
        with self.state.lock:
            target = next((s for s in self.state.sessions_by_player.values()
                           if (s.username or "").lower() == username.lower()), None)
        if target is None:
            print(f"  no logged-in player called {username!r}")
            return
        target.close("kicked by the admin console")
        print(f"  closing {target.label}")

    def console(self) -> None:
        print("Admin console ready. Commands: status, threads, players, rooms, kick <user>, quit")
        while not self.state.shutting_down.is_set():
            try:
                line = input("codeclash> ").strip()
            except EOFError:                     # stdin closed: keep serving until Ctrl+C / SIGTERM
                self.state.shutting_down.wait()
                return
            cmd, _, arg = line.partition(" ")
            cmd = cmd.lower()
            if cmd in ("quit", "exit", "stop"):
                return
            elif cmd == "status":
                self.print_status()
            elif cmd == "threads":
                self.print_threads()
            elif cmd == "players":
                self.print_players()
            elif cmd == "rooms":
                self.print_rooms()
            elif cmd == "kick" and arg:
                self.kick(arg.strip())
            elif cmd in ("", "help", "?"):
                print("  status | threads | players | rooms | kick <username> | quit")
            else:
                print(f"  unknown command {line!r} (type help)")


def lan_ipv4_addresses() -> list[str]:
    """Best-effort list of this machine's LAN IPv4 addresses (first = the default route's)."""
    found: list[str] = []
    for target in (("192.168.255.255", 1), ("10.255.255.255", 1), ("172.31.255.255", 1), ("8.8.8.8", 80)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(target)               # no packet is sent; only selects the outgoing interface
                ip = s.getsockname()[0]
                if not ip.startswith("127.") and ip != "0.0.0.0" and ip not in found:
                    found.append(ip)
        except OSError:
            continue
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format=config.LOG_FORMAT,
                        datefmt=config.LOG_DATEFMT, stream=sys.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description="CodeClash game server (TCP + UDP)")
    parser.add_argument("--host", default=config.SERVER_BIND_HOST, help="bind address (default 0.0.0.0)")
    parser.add_argument("--tcp-port", type=int, default=config.TCP_PORT)
    parser.add_argument("--udp-port", type=int, default=config.UDP_PORT)
    parser.add_argument("--no-console", action="store_true", help="no admin prompt (stop with Ctrl+C)")
    parser.add_argument("--log-level", default=config.LOG_LEVEL)
    args = parser.parse_args()

    configure_logging(args.log_level.upper())
    if args.udp_port != config.UDP_PORT:
        config.UDP_PORT = args.udp_port        # WELCOME / LOGIN tell clients the real UDP port
    if args.tcp_port != config.TCP_PORT:
        config.TCP_PORT = args.tcp_port

    server = CodeClashServer(args.host, args.tcp_port, args.udp_port)
    try:
        server.start()
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, 48, 98, 10048):
            print(f"\nPort {args.tcp_port}/{args.udp_port} is already in use: an old server (or Jupyter) is running.\n"
                  f"macOS: lsof -nP -i :{args.tcp_port}   then  kill <PID>\n")
            return 1
        raise

    def _on_sigterm(signum, frame):            # `kill <pid>` behaves like Ctrl+C
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, AttributeError):
        pass

    ips = lan_ipv4_addresses()
    bar = "=" * 72
    print(bar)
    print(" CodeClash server running")
    print(f"   TCP  {args.host}:{args.tcp_port}  (SOCK_STREAM)   UDP  {args.host}:{args.udp_port}  (SOCK_DGRAM)")
    print(f"   Clients connect to: {ips[0] if ips else 'unknown — run: ipconfig getifaddr en0'}")
    if len(ips) > 1:
        print(f"   Other addresses of this machine: {', '.join(ips[1:])}")
    print(f"   Game: {config.TOTAL_ROUNDS} rounds x {config.ROUND_DURATION_S}s, room size {config.ROOM_CAPACITY}")
    if sys.platform == "darwin" and os.environ.get("TERM_PROGRAM") == "vscode":
        print("   Note: running inside VS Code on macOS. If UDP sends fail with 'No route to host', allow VS Code in")
        print("         System Settings > Privacy & Security > Local Network, or run from Terminal.app.")
    print(bar)

    try:
        if args.no_console or not sys.stdin.isatty():
            while True:
                time.sleep(3600)
        else:
            server.console()
    except KeyboardInterrupt:
        print()
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
