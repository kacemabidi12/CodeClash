"""
Phase 1 tests for common/protocol.py and config.py (standard library unittest).

Run from the project root:
    python -m unittest -v tests.test_protocol          (Windows)
    python3 -m unittest -v tests.test_protocol         (macOS)
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest

import config
from common import protocol as P


class FramingTests(unittest.TestCase):
    """TCP is a byte stream: one recv() is NOT one message. The framer must cope."""

    def test_round_trip_single_message(self):
        msg = P.make_request(P.LOGIN, {"username": "kacem", "password": "pw1234"})
        framer = P.LineFramer()
        lines = framer.feed(P.encode_tcp(msg))
        self.assertEqual(len(lines), 1)
        self.assertEqual(P.decode_line(lines[0]), msg)
        self.assertEqual(framer.pending_bytes, 0)

    def test_two_messages_in_one_chunk(self):
        a = P.make_request(P.PING)
        b = P.make_request(P.LIST_ROOMS)
        lines = P.LineFramer().feed(P.encode_tcp(a) + P.encode_tcp(b))
        self.assertEqual([P.decode_line(x)["req_id"] for x in lines], [a["req_id"], b["req_id"]])

    def test_message_split_byte_by_byte(self):
        msg = P.make_request(P.CREATE_ROOM, {"room_name": "Finals"})
        framer = P.LineFramer()
        out = []
        for byte in P.encode_tcp(msg):
            out.extend(framer.feed(bytes([byte])))
        self.assertEqual(len(out), 1)
        self.assertEqual(P.decode_line(out[0]), msg)

    def test_code_snippet_newlines_do_not_break_framing(self):
        code = "def f(x):\n    return x * 2\n\nprint(f(3))\n"
        push = P.make_push(P.QUESTION, {"prompt": "What is printed?", "code": code})
        wire = P.encode_tcp(push)
        self.assertEqual(wire.count(b"\n"), 1, "only the delimiter may be a raw newline")
        lines = P.LineFramer().feed(wire)
        self.assertEqual(P.decode_line(lines[0])["data"]["code"], code)

    def test_utf8_character_split_across_chunks(self):
        msg = P.make_push(P.PLAYER_STATUS, {"username": "قاسم ✅ Éa", "status": P.PlayerStatus.ONLINE})
        wire = P.encode_tcp(msg)
        cut = wire.index("✅".encode()) + 1          # cut inside the 3-byte UTF-8 sequence
        framer = P.LineFramer()
        self.assertEqual(framer.feed(wire[:cut]), [])
        lines = framer.feed(wire[cut:])
        self.assertEqual(P.decode_line(lines[0])["data"]["username"], "قاسم ✅ Éa")

    def test_crlf_and_blank_lines_tolerated(self):
        lines = P.LineFramer().feed(b'\r\n\n{"v":1,"type":"PING","req_id":"x","data":{}}\r\n')
        self.assertEqual(len(lines), 1)
        self.assertEqual(P.decode_line(lines[0])["type"], "PING")

    def test_oversized_frame_rejected(self):
        framer = P.LineFramer()
        with self.assertRaises(P.FrameTooLarge):
            framer.feed(b"x" * (P.MAX_TCP_LINE_BYTES + 1))
        self.assertEqual(framer.pending_bytes, 0)

    def test_invalid_lines_raise_protocol_error(self):
        for bad in (b"not json", b"[1,2,3]", b'{"no_type":1}', b'{"v":99,"type":"PING"}', b"\xff\xfe"):
            with self.subTest(bad=bad), self.assertRaises(P.ProtocolError):
                P.decode_line(bad)

    def test_reassembly_over_a_real_socket_with_tiny_recv(self):
        """Real stream socket pair; the reader uses recv(7) so every message spans many chunks."""
        left, right = socket.socketpair()
        messages = [P.make_request(P.SUBMIT_ANSWER, {"answer": "B", "submission_id": P.new_id(), "i": i})
                    for i in range(50)]

        def writer():
            wire = b"".join(P.encode_tcp(m) for m in messages)
            for i in range(0, len(wire), 13):        # odd-sized writes with tiny pauses
                left.sendall(wire[i:i + 13])
                if i % 130 == 0:
                    time.sleep(0.001)
            left.close()

        t = threading.Thread(target=writer)
        t.start()
        framer, received = P.LineFramer(), []
        while True:
            chunk = right.recv(7)
            if not chunk:
                break
            received.extend(P.decode_line(x) for x in framer.feed(chunk))
        t.join()
        right.close()
        self.assertEqual(received, messages)


class EnvelopeTests(unittest.TestCase):

    def test_request_envelope(self):
        req = P.make_request(P.JOIN_ROOM, {"room_id": "R1042"})
        self.assertEqual(req["v"], P.PROTOCOL_VERSION)
        self.assertEqual(len(req["req_id"]), 32)
        self.assertEqual(P.parse_request(req), (P.JOIN_ROOM, req["req_id"], {"room_id": "R1042"}))

    def test_unknown_request_type_rejected(self):
        with self.assertRaises(ValueError):
            P.make_request("HACK_SCORE")
        with self.assertRaises(P.ProtocolError) as ctx:
            P.parse_request({"type": "HACK_SCORE", "req_id": "1", "data": {}})
        self.assertEqual(ctx.exception.code, P.ErrorCode.UNKNOWN_TYPE)

    def test_bad_request_fields(self):
        with self.assertRaises(P.ProtocolError) as ctx:
            P.parse_request({"type": P.PING, "data": {}})
        self.assertEqual(ctx.exception.code, P.ErrorCode.BAD_REQUEST)
        with self.assertRaises(P.ProtocolError):
            P.parse_request({"type": P.PING, "req_id": "1", "data": [1]})

    def test_response_links_to_request(self):
        req = P.make_request(P.START_GAME, {"room_id": "R1"})
        ok = P.make_response(req["type"], req["req_id"], data={"game_id": "G1"})
        err = P.make_error_response(req["type"], req["req_id"], P.ErrorCode.NOT_HOST, "Only the host can start.")
        self.assertEqual((ok["reply_to"], ok["ok"], ok["data"]["game_id"]), (req["req_id"], True, "G1"))
        self.assertEqual((err["ok"], err["error"]["code"]), (False, P.ErrorCode.NOT_HOST))
        self.assertNotIn("data", err)

    def test_question_push_never_needs_the_answer(self):
        q = P.make_push(P.QUESTION, {"question_id": "PO-03", "options": [{"key": "A", "text": "6"}]})
        self.assertNotIn("answer", json.dumps(q))


class UdpTests(unittest.TestCase):

    def test_udp_round_trip_and_size(self):
        tick = P.make_udp(P.TIMER_TICK, room_id="R1042", game_id="G7", round_id="G7-R3",
                          phase=P.Phase.QUESTION, seconds_left=12, duration=20, seq=118)
        payload = P.encode_udp(tick)
        self.assertLess(len(payload), 300)
        self.assertEqual(P.decode_udp(payload), tick)

    def test_oversized_udp_rejected(self):
        with self.assertRaises(P.ProtocolError):
            P.encode_udp(P.make_udp(P.LIVE_NOTICE, text="x" * 2000))

    def test_sequence_filter_drops_old_and_duplicate_ticks(self):
        last = None
        accepted = []
        for seq in [1, 2, 4, 3, 4, 5]:              # 3 arrives late, 4 is duplicated
            if P.is_newer_seq(seq, last):
                accepted.append(seq)
                last = seq
        self.assertEqual(accepted, [1, 2, 4, 5])


class ValidationTests(unittest.TestCase):

    def test_usernames(self):
        self.assertIsNone(P.validate_username("kacem_01"))
        for bad in ("ab", "x" * 17, "has space", "émoji", None, 42):
            self.assertIsNotNone(P.validate_username(bad))

    def test_passwords_and_room_names(self):
        self.assertIsNone(P.validate_password("1234"))
        self.assertIsNotNone(P.validate_password("123"))
        self.assertIsNone(P.validate_room_name("Finals"))
        self.assertIsNotNone(P.validate_room_name("   "))
        self.assertIsNotNone(P.validate_room_name("x" * 25))

    def test_text_answer_normalisation(self):
        self.assertEqual(P.normalize_text_answer("  [1,  2, 3]\n"), "[1, 2, 3]")
        self.assertEqual(P.normalize_text_answer("TRUE"), P.normalize_text_answer("true"))


class ConfigTests(unittest.TestCase):

    def test_server_binds_all_interfaces_and_ports(self):
        self.assertEqual(config.SERVER_BIND_HOST, "0.0.0.0")
        self.assertEqual((config.TCP_PORT, config.UDP_PORT, config.STREAMLIT_PORT), (8888, 8888, 8501))

    def test_client_host_validation(self):
        self.assertIsNone(config.validate_server_host("192.168.1.25", allow_loopback=False))
        self.assertIsNone(config.validate_server_host("codeclash-mac.local", allow_loopback=False))
        for bad in ("0.0.0.0", "127.0.0.1", "localhost", "", "::1"):
            self.assertIsNotNone(config.validate_server_host(bad, allow_loopback=False), bad)
        self.assertIsNone(config.validate_server_host("127.0.0.1", allow_loopback=True))
        self.assertIsNotNone(config.validate_server_host("0.0.0.0", allow_loopback=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
