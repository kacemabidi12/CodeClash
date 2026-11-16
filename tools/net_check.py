"""
CodeClash network check — run this BEFORE writing/using the game.

It proves, on the real four computers, that:
  * a Windows client can open a TCP connection to the MacBook (AF_INET + SOCK_STREAM)
  * newline-delimited JSON framing survives merged and split TCP segments
  * UDP datagrams travel client -> server AND server -> client (AF_INET + SOCK_DGRAM)
  * TCP and UDP work at the same time on port 8888
  * the server gives every TCP connection its own handler thread (3 clients -> 3 threads)

Usage (from the project root, with the venv activated):

  MacBook (server):   python3 tools/net_check.py server
  Each Windows PC:    python tools/net_check.py client --host <MACBOOK_LAN_IP> --name PC1 --hold 60

Run the three clients at roughly the same time with --hold 60 and watch the server print
"active TCP handler threads: 3".
"""

from __future__ import annotations

import argparse
import errno
import logging
import socket
import sys
import threading
import time
from pathlib import Path

# Allow `python tools/net_check.py ...` from the project root (adds the root to sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from common.protocol import (  # noqa: E402
    PROTOCOL_VERSION, TCP_RECV_BUFFER, UDP_RECV_BUFFER, LineFramer, ProtocolError,
    decode_line, decode_udp, encode_tcp, encode_udp,
)

log = logging.getLogger("net_check")

# Diagnostic-only message types (not part of the game protocol).
NC_WELCOME, NC_PING, NC_PONG, NC_BYE = "NC_WELCOME", "NC_PING", "NC_PONG", "NC_BYE"
NC_UDP_PING, NC_UDP_PONG = "NC_UDP_PING", "NC_UDP_PONG"

EHOSTUNREACH_CODES = {errno.EHOSTUNREACH, 65, 113, 10065}


def guess_lan_ipv4() -> str:
    """Best-effort LAN IPv4 of this machine. connect() on a UDP socket sends no packet;
    it only asks the OS which local interface would route to that destination."""
    for target in (("192.168.255.255", 1), ("10.255.255.255", 1), ("8.8.8.8", 80)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(target)
                ip = s.getsockname()[0]
                if not ip.startswith("127.") and ip != "0.0.0.0":
                    return ip
        except OSError:
            continue
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "unknown"


# ===========================================================================
# SERVER MODE (MacBook)
# ===========================================================================

class NetCheckServer:
    def __init__(self, port: int):
        self.port = port
        self.stop = threading.Event()
        self.lock = threading.Lock()          # protects the counters below
        self.active_handlers = 0
        self.total_connections = 0
        self.udp_packets = 0
        self.tcp_sock: socket.socket | None = None
        self.udp_sock: socket.socket | None = None

    # ---------------- TCP ----------------
    def start_tcp(self) -> None:
        # AF_INET = IPv4, SOCK_STREAM = TCP
        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_sock.bind((config.SERVER_BIND_HOST, self.port))
        self.tcp_sock.listen()
        self.tcp_sock.settimeout(config.SOCKET_POLL_TIMEOUT_S)  # so the loop can notice Ctrl+C
        threading.Thread(target=self._accept_loop, name="tcp-accept", daemon=True).start()

    def _accept_loop(self) -> None:
        log.info("TCP accept loop running on %s:%d", config.SERVER_BIND_HOST, self.port)
        while not self.stop.is_set():
            try:
                conn, addr = self.tcp_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                self.total_connections += 1
                n = self.total_connections
            # One dedicated handler thread per TCP connection: a slow client blocks only itself.
            threading.Thread(target=self._handle_tcp, args=(conn, addr),
                             name=f"tcp-handler-{n}", daemon=True).start()

    def _handle_tcp(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        with self.lock:
            self.active_handlers += 1
            active = self.active_handlers
        log.info("TCP connected from %s:%d -> active TCP handler threads: %d", addr[0], addr[1], active)
        conn.settimeout(config.SOCKET_POLL_TIMEOUT_S)
        framer = LineFramer()
        name = "?"
        try:
            conn.sendall(encode_tcp({"v": PROTOCOL_VERSION, "type": NC_WELCOME,
                                     "server_ip": guess_lan_ipv4(), "tcp_port": self.port,
                                     "udp_port": self.port, "your_addr": f"{addr[0]}:{addr[1]}"}))
            while not self.stop.is_set():
                try:
                    chunk = conn.recv(TCP_RECV_BUFFER)
                except socket.timeout:
                    continue
                if not chunk:                       # orderly close by the client
                    break
                for line in framer.feed(chunk):    # may yield 0, 1 or many complete messages
                    try:
                        msg = decode_line(line)
                    except ProtocolError as exc:
                        log.warning("Bad line from %s: %s", addr[0], exc)
                        continue
                    if msg["type"] == NC_PING:
                        name = str(msg.get("name", name))
                        with self.lock:
                            active = self.active_handlers
                        conn.sendall(encode_tcp({
                            "v": PROTOCOL_VERSION, "type": NC_PONG, "n": msg.get("n"),
                            "server_time": time.time(), "handler_thread": threading.current_thread().name,
                            "active_handlers": active,
                        }))
                    elif msg["type"] == NC_BYE:
                        return
        except (ConnectionError, OSError, ProtocolError) as exc:
            log.warning("TCP connection %s:%d ended with error: %s", addr[0], addr[1], exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self.lock:
                self.active_handlers -= 1
                active = self.active_handlers
            log.info("TCP closed %s (%s:%d) -> active TCP handler threads: %d", name, addr[0], addr[1], active)

    # ---------------- UDP ----------------
    def start_udp(self) -> None:
        # AF_INET = IPv4, SOCK_DGRAM = UDP (connectionless: no accept(), no per-client socket)
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.bind((config.SERVER_BIND_HOST, self.port))
        self.udp_sock.settimeout(config.SOCKET_POLL_TIMEOUT_S)
        threading.Thread(target=self._udp_loop, name="udp-receiver", daemon=True).start()

    def _udp_loop(self) -> None:
        log.info("UDP receiver running on %s:%d", config.SERVER_BIND_HOST, self.port)
        while not self.stop.is_set():
            try:
                payload, sender = self.udp_sock.recvfrom(UDP_RECV_BUFFER)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue   # Windows-style ICMP "port unreachable" echo; harmless for UDP
            except OSError:
                break
            try:
                msg = decode_udp(payload)
            except ProtocolError:
                continue
            if msg.get("type") != NC_UDP_PING:
                continue
            with self.lock:
                self.udp_packets += 1
            log.info("UDP ping seq=%s from %s (%s:%d)", msg.get("seq"), msg.get("name"), *sender)
            reply = {"v": PROTOCOL_VERSION, "type": NC_UDP_PONG, "seq": msg.get("seq"),
                     "server_time": time.time(), "seen_as": f"{sender[0]}:{sender[1]}"}
            try:
                # Reply to the OBSERVED source address: works through client firewalls/NAT.
                self.udp_sock.sendto(encode_udp(reply), sender)
            except OSError as exc:
                log.error("UDP send to %s:%d failed: %s", sender[0], sender[1], exc)
                if getattr(exc, "errno", None) in EHOSTUNREACH_CODES and sys.platform == "darwin":
                    log.error("macOS 'No route to host': if this server runs inside VS Code, allow VS Code in "
                              "System Settings > Privacy & Security > Local Network, or run it from Terminal.app.")

    def run(self) -> None:
        self.start_tcp()
        self.start_udp()   # both services run at the same time, in separate threads
        ip = guess_lan_ipv4()
        print("=" * 68)
        print(" CodeClash net_check SERVER")
        print(f"   TCP listening on {config.SERVER_BIND_HOST}:{self.port}")
        print(f"   UDP listening on {config.SERVER_BIND_HOST}:{self.port}")
        print(f"   Clients must use:  --host {ip}")
        print("   Press Ctrl+C to stop.")
        print("=" * 68)
        try:
            while True:
                time.sleep(10)
                with self.lock:
                    log.info("status: active TCP handler threads=%d, total TCP connections=%d, UDP pings=%d",
                             self.active_handlers, self.total_connections, self.udp_packets)
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            self.stop.set()
            for s in (self.tcp_sock, self.udp_sock):
                if s:
                    s.close()


# ===========================================================================
# CLIENT MODE (each Windows PC)
# ===========================================================================

def explain_os_error(exc: BaseException, host: str, port: int) -> str:
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if isinstance(exc, ConnectionRefusedError) or code in (61, 111, 10061):
        return (f"Connection refused by {host}:{port}. The MacBook is reachable but nothing listens on that "
                f"port: is the server running? Same port number on both sides?")
    if isinstance(exc, (socket.timeout, TimeoutError)) or code in (60, 110, 10060):
        return (f"No answer from {host}:{port}. Check the IP (the server prints it), that all machines are on "
                f"the same Wi-Fi (no guest/campus isolation), and the MacBook firewall allows Python.")
    if code in EHOSTUNREACH_CODES or code in (51, 101, 10051):
        return (f"No route to {host}. Different network or subnet? Compare `ipconfig` here with the "
                f"MacBook's address.")
    if isinstance(exc, socket.gaierror):
        return f"Cannot resolve '{host}'. Use the numeric IPv4 address of the MacBook."
    return f"{type(exc).__name__}: {exc}"


class TcpLineReader:
    """Minimal framed reader for the client side of the check."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.framer = LineFramer()
        self.pending: list[dict] = []

    def read(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while not self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("timed out waiting for a TCP message")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(TCP_RECV_BUFFER)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.pending.extend(decode_line(line) for line in self.framer.feed(chunk))
        return self.pending.pop(0)


def run_client(host: str, port: int, name: str, hold: int, allow_loopback: bool) -> int:
    problem = config.validate_server_host(host, allow_loopback=allow_loopback)
    if problem:
        print(f"[FAIL] {problem}")
        return 2

    ok_tcp = ok_framing = ok_udp = False
    print(f"[....] {name}: TCP connect to {host}:{port}")
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.settimeout(config.CONNECT_TIMEOUT_S)
    try:
        t0 = time.perf_counter()
        tcp.connect((host, port))
        print(f"[ OK ] TCP connected in {(time.perf_counter() - t0) * 1000:.1f} ms "
              f"(local {tcp.getsockname()[0]}:{tcp.getsockname()[1]})")
        ok_tcp = True
        reader = TcpLineReader(tcp)
        welcome = reader.read(5)
        print(f"[ OK ] Server says: you are {welcome.get('your_addr')}, server LAN IP {welcome.get('server_ip')}")

        # --- Framing test: two messages in ONE sendall, then one message split over TWO sendalls ---
        merged = encode_tcp({"v": 1, "type": NC_PING, "n": 1, "name": name}) + \
            encode_tcp({"v": 1, "type": NC_PING, "n": 2, "name": name})
        tcp.sendall(merged)
        split = encode_tcp({"v": 1, "type": NC_PING, "n": 3, "name": name, "note": "split ✂ across segments"})
        half = len(split) // 2
        tcp.sendall(split[:half])
        time.sleep(0.2)                     # force the halves into separate TCP segments
        tcp.sendall(split[half:])
        got = [reader.read(5).get("n") for _ in range(3)]
        ok_framing = got == [1, 2, 3]
        print(f"[{' OK ' if ok_framing else 'FAIL'}] Framing: merged + split messages answered in order {got}")

        # --- RTT ---
        rtts = []
        for n in range(4, 9):
            t0 = time.perf_counter()
            tcp.sendall(encode_tcp({"v": 1, "type": NC_PING, "n": n, "name": name}))
            pong = reader.read(5)
            rtts.append((time.perf_counter() - t0) * 1000)
        print(f"[ OK ] TCP round-trip: min {min(rtts):.1f} ms / avg {sum(rtts) / len(rtts):.1f} ms; "
              f"served by server thread '{pong.get('handler_thread')}'")
    except (OSError, ProtocolError) as exc:
        print(f"[FAIL] TCP: {explain_os_error(exc, host, port)}")
        tcp.close()
        return 1

    # --- UDP: client sends first; the server replies to the address it observes ---
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("0.0.0.0", config.CLIENT_UDP_PORT))
    udp.settimeout(1.5)
    received, udp_rtts, seen_as = 0, [], None
    for seq in range(1, 6):
        t0 = time.perf_counter()
        try:
            udp.sendto(encode_udp({"v": 1, "type": NC_UDP_PING, "seq": seq, "name": name}), (host, port))
            while True:
                payload, _ = udp.recvfrom(UDP_RECV_BUFFER)
                msg = decode_udp(payload)
                if msg.get("type") == NC_UDP_PONG and msg.get("seq") == seq:
                    received += 1
                    udp_rtts.append((time.perf_counter() - t0) * 1000)
                    seen_as = msg.get("seen_as")
                    break
        except (socket.timeout, TimeoutError):
            pass
        except ConnectionResetError:
            pass   # Windows reports an ICMP "port unreachable" as a reset on UDP sockets
        except (OSError, ProtocolError) as exc:
            print(f"[WARN] UDP seq {seq}: {exc}")
    ok_udp = received >= 4
    if received:
        print(f"[{' OK ' if ok_udp else 'WARN'}] UDP: {received}/5 replies, avg RTT "
              f"{sum(udp_rtts) / len(udp_rtts):.1f} ms (server saw this PC as {seen_as})")
    else:
        print("[FAIL] UDP: 0/5 replies while TCP works. Likely causes:\n"
              "       - MacBook firewall blocks incoming UDP for Python (allow Python in Firewall options)\n"
              "       - server runs in VS Code on macOS 15+ without Local Network permission (server log: Errno 65)\n"
              "       - this PC's firewall drops the reply: allow Python on Private networks, or set\n"
              "         CODECLASH_CLIENT_UDP_PORT=8889 and add an inbound UDP 8889 rule")

    # --- Hold the TCP connection open so the server shows concurrent handler threads ---
    try:
        if hold > 0:
            print(f"[....] Holding the TCP connection for {hold} s (start the other clients now)...")
            end = time.monotonic() + hold
            n = 100
            while time.monotonic() < end:
                n += 1
                tcp.sendall(encode_tcp({"v": 1, "type": NC_PING, "n": n, "name": name}))
                pong = reader.read(5)
                print(f"       server reports {pong.get('active_handlers')} active TCP handler thread(s)")
                time.sleep(min(5, max(0, end - time.monotonic())))
        tcp.sendall(encode_tcp({"v": 1, "type": NC_BYE}))
    except (OSError, ProtocolError) as exc:
        print(f"[FAIL] TCP connection lost while holding: {explain_os_error(exc, host, port)}")
        ok_tcp = False
    finally:
        tcp.close()
        udp.close()

    passed = ok_tcp and ok_framing and ok_udp
    print("=" * 68)
    print(f" RESULT for {name}: TCP {'PASS' if ok_tcp else 'FAIL'} | framing {'PASS' if ok_framing else 'FAIL'}"
          f" | UDP {'PASS' if ok_udp else 'FAIL'}  ->  {'READY FOR PHASE 2' if passed else 'FIX BEFORE CONTINUING'}")
    print("=" * 68)
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="CodeClash TCP+UDP network check")
    sub = parser.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("server", help="run on the MacBook")
    s.add_argument("--port", type=int, default=config.TCP_PORT)
    c = sub.add_parser("client", help="run on each Windows PC")
    c.add_argument("--host", default=config.SERVER_HOST, help="MacBook LAN IPv4 (default: config.SERVER_HOST)")
    c.add_argument("--port", type=int, default=config.TCP_PORT)
    c.add_argument("--name", default=socket.gethostname())
    c.add_argument("--hold", type=int, default=0, help="seconds to keep the TCP connection open")
    c.add_argument("--allow-loopback", action="store_true", help="single-computer development only")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT, datefmt=config.LOG_DATEFMT)
    if args.mode == "server":
        try:
            NetCheckServer(args.port).run()
        except OSError as exc:
            if getattr(exc, "errno", None) in (errno.EADDRINUSE, 48, 98, 10048):
                print(f"Port {args.port} is already in use (old server still running? Jupyter uses 8888 too). "
                      f"macOS: lsof -nP -i :{args.port}")
                return 1
            raise
        return 0
    return run_client(args.host, args.port, args.name, args.hold, args.allow_loopback)


if __name__ == "__main__":
    sys.exit(main())
