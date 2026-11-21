"""OTLP sink tests (row 19; D31): every codex.* event name is kept. Stubs only (loopback).

Run: python3 -Es -m unittest discover -s ~/.claude/council/tests -t ~/.claude/council
"""
import gzip
import http.client
import json
import os
import socket
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import otlp_sink  # noqa: E402

CODEX_NAMES = ["codex.sse_event", "codex.websocket.event", "codex.websocket_connect",
               "codex.websocket.request", "codex.api_request", "codex.conversation_starts"]


# ---- tiny protobuf encoder ------------------------------------------------------

def _v(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _ld(field, payload):
    if isinstance(payload, str):
        payload = payload.encode()
    return _v(field << 3 | 2) + _v(len(payload)) + payload


def _kv(key, val):
    return _ld(1, key) + _ld(2, _ld(1, val))                   # KeyValue{key, AnyValue{string_value}}


def _log_record(name, kind=None, trailing_attrs=2):
    rec = _ld(5, _ld(1, "body of " + name))                              # body
    rec += _ld(6, _kv("event.name", name))                              # attribute, tag 0x32 = '2'
    if kind:
        rec += _ld(6, _kv("event.kind", kind))
    for i in range(trailing_attrs):
        rec += _ld(6, _kv("conversation.id", "abc%d" % i))
    return rec


def proto_logs(*records):
    scope = b"".join(_ld(2, r) for r in records)               # ScopeLogs.log_records = 2
    return _ld(1, _ld(2, scope))                               # ResourceLogs.scope_logs = 2


def json_logs(names, kind=None):
    recs = []
    for n in names:
        attrs = [{"key": "event.name", "value": {"stringValue": n}}]
        if kind:
            attrs.append({"key": "event.kind", "value": {"stringValue": kind}})
        recs.append({"body": {"stringValue": "x"}, "attributes": attrs})
    return json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": recs}]}]}).encode()


def post(port, path, body, ctype, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Content-Type": ctype}
    h.update(headers or {})
    c.request("POST", path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


class ExtractNames(unittest.TestCase):
    def test_protobuf_exact_names_no_tag_byte_leak(self):
        body = proto_logs(*[_log_record(n) for n in CODEX_NAMES])
        self.assertEqual(otlp_sink.extract_names(body, "application/x-protobuf"), CODEX_NAMES)

    def test_protobuf_kind(self):
        names = otlp_sink.extract_names(proto_logs(_log_record("codex.sse_event", kind="response.created")),
                                        "application/x-protobuf")
        self.assertEqual(names, ["codex.sse_event", "event.kind=response.created"])

    def test_json_names_and_kind(self):
        names = otlp_sink.extract_names(json_logs(CODEX_NAMES[:3], kind="ping"), "application/json")
        self.assertEqual(names, CODEX_NAMES[:3] + ["event.kind=ping"])

    def test_names_inside_text_and_paths_not_confused(self):
        body = json.dumps({"a": "event.name=codex.websocket.event model=x", "b": "/usr/lib/codex.js",
                           "c": "see codex.api_request."}).encode()
        names = otlp_sink.extract_names(body, "application/json")
        self.assertEqual(names, ["codex.websocket.event", "codex.api_request"])

    def test_unparseable_body_falls_back_to_raw_scan(self):
        names = otlp_sink.extract_names(b"\xff\xff\x00codex.sse_event\x00junk", "application/x-protobuf")
        self.assertEqual(names, ["codex.sse_event"])

    def test_no_names(self):
        self.assertEqual(otlp_sink.extract_names(proto_logs(_log_record("other.thing")), ""), [])
        self.assertEqual(otlp_sink.extract_names(b"", ""), [])

    def test_wrong_content_type_still_decodes(self):
        self.assertEqual(otlp_sink.extract_names(json_logs(["codex.api_request"]), "application/x-protobuf"),
                         ["codex.api_request"])
        self.assertEqual(otlp_sink.extract_names(proto_logs(_log_record("codex.api_request")), "application/json"),
                         ["codex.api_request"])


class SinkServer(unittest.TestCase):
    def setUp(self):
        self.sink = otlp_sink.Sink()
        self.addCleanup(self.sink.close)

    def test_binds_loopback_only_with_ephemeral_port(self):
        host, port = self.sink._server.server_address[:2]
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, self.sink.port)
        self.assertGreater(self.sink.port, 0)
        other = otlp_sink.Sink()
        self.addCleanup(other.close)
        self.assertNotEqual(other.port, self.sink.port)

    def test_receives_and_records_every_codex_name(self):
        body = proto_logs(*[_log_record(n) for n in CODEX_NAMES])
        status, data = post(self.sink.port, "/v1/logs", body, "application/x-protobuf")
        self.assertEqual((status, data), (200, b""))
        status, data = post(self.sink.port, "/v1/traces", json_logs(["codex.websocket.event"]), "application/json")
        self.assertEqual((status, data), (200, b"{}"))
        ev = self.sink.events()
        self.assertEqual([e["path"] for e in ev], ["/v1/logs", "/v1/traces"])
        self.assertEqual(ev[0]["names"], CODEX_NAMES)
        self.assertEqual(ev[0]["bytes"], len(body))
        self.assertEqual(ev[1]["names"], ["codex.websocket.event"])
        self.assertLessEqual(ev[0]["t"], ev[1]["t"])
        self.assertAlmostEqual(ev[1]["t"], time.time(), delta=5)
        ev[0]["names"].append("mutated")
        self.assertNotIn("mutated", self.sink.events()[0]["names"])

    def test_gzip_body(self):
        raw = proto_logs(_log_record("codex.websocket_connect"))
        status, _ = post(self.sink.port, "/v1/logs", gzip.compress(raw), "application/x-protobuf",
                         {"Content-Encoding": "gzip"})
        self.assertEqual(status, 200)
        e = self.sink.events()[0]
        self.assertEqual(e["names"], ["codex.websocket_connect"])
        self.assertLess(e["bytes"], len(raw) + 40)                       # wire size, not inflated size

    def test_chunked_body_and_keepalive(self):
        s = socket.create_connection(("127.0.0.1", self.sink.port), timeout=5)
        body = json_logs(["codex.sse_event"])
        half = len(body) // 2
        req = (b"POST /v1/logs HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n"
               + b"%x\r\n" % half + body[:half] + b"\r\n" + b"%x\r\n" % (len(body) - half) + body[half:]
               + b"\r\n0\r\n\r\n")
        s.sendall(req)
        s.sendall(b"POST /v1/logs HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Content-Length: %d\r\n\r\n" % len(body) + body)
        buf = b""
        deadline = time.time() + 5
        while buf.count(b"HTTP/1.1 200") < 2 and time.time() < deadline:
            buf += s.recv(4096)
        s.close()
        self.assertEqual(buf.count(b"HTTP/1.1 200"), 2)
        self.assertEqual([e["names"] for e in self.sink.events()], [["codex.sse_event"]] * 2)

    def test_garbage_is_recorded_empty_and_does_not_break_the_sink(self):
        status, _ = post(self.sink.port, "/v1/metrics", b"\x00\x01\x02", "application/x-protobuf")
        self.assertEqual(status, 200)
        status, _ = post(self.sink.port, "/v1/logs", b"\x1f\x8bnot gzip", "application/x-protobuf",
                         {"Content-Encoding": "gzip"})
        self.assertEqual(status, 200)
        status, _ = post(self.sink.port, "/v1/logs", proto_logs(_log_record("codex.api_request")),
                         "application/x-protobuf")
        self.assertEqual(status, 200)
        self.assertEqual([e["names"] for e in self.sink.events()], [[], [], ["codex.api_request"]])

    def test_close_stops_listening(self):
        port = self.sink.port
        self.sink.close()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_context_manager(self):
        with otlp_sink.Sink() as s:
            post(s.port, "/v1/logs", json_logs(["codex.sse_event"]), "application/json")
            self.assertEqual(len(s.events()), 1)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", s.port), timeout=1).close()


if __name__ == "__main__":
    unittest.main()
