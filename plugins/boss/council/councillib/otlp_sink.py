"""Local OTLP/HTTP sink for a probe or supervised codex call (row 19; D16, D31).

A stdlib HTTP server on 127.0.0.1 with an ephemeral port. It accepts any POST (the
exporter posts to /v1/logs, /v1/traces and /v1/metrics), replies 200 with an empty
export response, and records one entry per request:

    {t, path, bytes, names}

`names` lists every event name found in the payload, in first-seen order:
- strings that look like `codex.<name>` (codex.sse_event, codex.websocket.event,
  codex.websocket_connect, codex.websocket.request, codex.api_request, ...), whether
  the body is JSON, protobuf or neither;
- one `event.kind=<kind>` entry per `event.kind` attribute value.

Bodies are never stored: only the names and the size. gzip bodies are inflated.
Each launch gets its own Sink, so every event is attributed to exactly one call.
"""
from __future__ import annotations

import gzip
import json
import re
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 32 * 1024 * 1024
_MAX_DEPTH = 12
_CODEX_RE = re.compile(r"(?<![\w/.\-])codex\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")
_CODEX_RAW_RE = re.compile(rb"(?<![\w/.\-])codex\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")
KIND_KEY = "event.kind"


# ---- payload decoding (pure) --------------------------------------------------

def _varint(b, i):
    val = shift = 0
    while True:
        if i >= len(b) or shift > 63:
            raise ValueError("bad varint")
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7


def _pb_fields(b):
    """Parse a whole buffer as one protobuf message -> [(field, wire, value)], else ValueError."""
    out, i = [], 0
    while i < len(b):
        tag, i = _varint(b, i)
        field, wire = tag >> 3, tag & 7
        if field == 0:
            raise ValueError("field 0")
        if wire == 0:
            val, i = _varint(b, i)
        elif wire == 1:
            val, i = b[i:i + 8], i + 8
        elif wire == 5:
            val, i = b[i:i + 4], i + 4
        elif wire == 2:
            n, i = _varint(b, i)
            val, i = b[i:i + n], i + n
            if len(val) != n:
                raise ValueError("short field")
        else:
            raise ValueError("wire type %d" % wire)
        if i > len(b):
            raise ValueError("short field")
        out.append((field, wire, val))
    return out


def _text(raw):
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if s and all(c.isprintable() or c in "\t\r\n" for c in s):
        return s
    return None


def _pb_collect(b, strings, kinds, depth=0):
    fields = _pb_fields(b)
    if not fields:
        raise ValueError("empty")
    blobs = [(f, v) for f, w, v in fields if w == 2]
    # KeyValue { key = 1; value = AnyValue { string_value = 1 } }
    if any(f == 1 and v == KIND_KEY.encode() for f, v in blobs):
        for f, v in blobs:
            if f == 2:
                try:
                    inner = [x for x in _pb_fields(v) if x[0] == 1 and x[1] == 2]
                except ValueError:
                    continue
                for _f, _w, raw in inner:
                    s = _text(raw)
                    if s:
                        kinds.append(s)
    for _f, v in blobs:
        s = _text(v)
        if s is not None:
            strings.append(s)
        if depth < _MAX_DEPTH and len(v) > 1:
            try:
                _pb_collect(v, strings, kinds, depth + 1)
            except ValueError:
                pass


def _json_collect(node, strings, kinds, depth=0):
    if depth > 64:
        return
    if isinstance(node, dict):
        if node.get("key") == KIND_KEY and isinstance(node.get("value"), dict):
            kind = node["value"].get("stringValue")
            if isinstance(kind, str) and kind:
                kinds.append(kind)
        for k, v in node.items():
            strings.append(str(k))
            _json_collect(v, strings, kinds, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _json_collect(v, strings, kinds, depth + 1)
    elif isinstance(node, str):
        strings.append(node)


def extract_names(body, content_type=""):
    """Every codex.* event name and event.kind=<kind> in an OTLP body, first-seen order."""
    strings, kinds = [], []
    order = ("json", "proto") if "json" in (content_type or "").lower() else ("proto", "json")
    ok = False
    for how in order:
        try:
            if how == "json":
                _json_collect(json.loads(body.decode("utf-8")), strings, kinds)
            else:
                _pb_collect(body, strings, kinds)
            ok = True
            break
        except (ValueError, UnicodeDecodeError):
            strings, kinds = [], []
    names = {}
    if ok:
        for s in strings:
            for m in _CODEX_RE.finditer(s):
                names.setdefault(m.group(0), None)
    if not names:
        # neither format parsed, or it parsed and held no name: scan the raw bytes as a fallback
        for m in _CODEX_RAW_RE.finditer(body):
            names.setdefault(m.group(0).decode("ascii"), None)
    for kind in kinds:
        names.setdefault("%s=%s" % (KIND_KEY, kind), None)
    return list(names)


def _inflate(body, encoding):
    enc = (encoding or "").lower()
    if "gzip" in enc:
        return gzip.decompress(body)
    if "deflate" in enc:
        return zlib.decompress(body)
    return body


# ---- server -------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # silence stderr
        pass

    def _read_body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks, total = [], 0
            while True:
                size = int((self.rfile.readline(64).split(b";")[0].strip() or b"0"), 16)
                if size == 0:
                    while self.rfile.readline(1024).strip():
                        pass
                    break
                total += size
                if total > MAX_BODY:
                    raise ValueError("body too large")
                chunks.append(self.rfile.read(size))
                self.rfile.readline(8)
            return b"".join(chunks)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        ctype = self.headers.get("Content-Type", "")
        try:
            raw = self._read_body()
            wire_bytes = len(raw)
            try:
                body = _inflate(raw, self.headers.get("Content-Encoding"))
            except (OSError, zlib.error, EOFError):
                body = raw
            names = extract_names(body, ctype)
        except (ValueError, OSError):
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        self.server.sink._record(self.path, wire_bytes, names)
        reply = b"{}" if "json" in ctype.lower() else b""
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if reply else "application/x-protobuf")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    do_PUT = do_POST


class Sink:
    """Receiver on 127.0.0.1:<ephemeral>. `.port`, `.events()`, `.close()`."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.sink = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
                                        name="otlp-sink-%d" % self.port, daemon=True)
        self._thread.start()

    def _record(self, path, nbytes, names):
        with self._lock:
            self._events.append({"t": time.time(), "path": path, "bytes": nbytes, "names": names})

    def events(self):
        """Snapshot of [{t, path, bytes, names}] in arrival order (t = wall clock)."""
        with self._lock:
            return [dict(e, names=list(e["names"])) for e in self._events]

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
