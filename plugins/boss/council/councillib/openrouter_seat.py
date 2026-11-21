#!/usr/bin/env python3
"""OpenRouter council seat child. STANDALONE: stdlib only, no councillib imports (W11).

Reads the prompt from stdin and the API key from the environment variable NAMED by
--key-env (never argv), POSTs a streaming chat completion and copies the SSE lines to
stdout as they arrive, flushing after every line, so the Supervisor's idle clock sees
every byte. The Supervisor and the reaper own the process group; this child only talks.

Exit codes: 0 stream finished; 1 HTTP error or connect failure (stderr: `openrouter http
<code>` / `openrouter <ExceptionName>`); 2 bad usage or missing key; 3 stream ended
without a finish or [DONE]; 4 the stream carried an error chunk; 5 socket read timeout.
Retry (fallback-chain row 1): BEFORE the first response byte, the same request is sent again on 408, 429, any 5xx and
connect errors (URLError/OSError except a socket timeout), up to MAX_RETRIES (2) more times with a short backoff
(1 s, then 2 s; an integer Retry-After header wins, capped at RETRY_AFTER_CAP). Never on 400/401/402/403/404 (the
request or the account is wrong, a repeat cannot help). Once the 200 response is open nothing is retried: the idle
clock and the partial-output rules of the Supervisor take over. Each retry writes `openrouter retry N: <reason>` to
stderr; the final failure line and the exit code are unchanged.
`--provider-json` (a JSON object, D43) becomes the request's `provider` block; a value that is not an object
exits 2 before any key read or HTTP call.
Only codes and exception names are ever written to stderr, never a response body or key.
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request


MAX_RETRIES = 2                       # extra tries after the first request, before the first response byte
RETRY_BACKOFF_S = (1.0, 2.0)          # wait before retry 1, retry 2 (no Retry-After header)
RETRY_AFTER_CAP = 10.0                # the longest wait a Retry-After header can buy: the idle clock (240 s) is silent meanwhile


def retryable_http(code):
    return code in (408, 429) or 500 <= code <= 599


def retry_wait(n, headers):
    """Seconds to wait before retry number n (1-based): an integer Retry-After (capped), else the backoff table."""
    ra = None
    try:
        ra = headers.get("Retry-After") if headers is not None else None
    except Exception:
        ra = None
    if isinstance(ra, str) and ra.strip().isdigit():
        return min(float(int(ra.strip())), RETRY_AFTER_CAP)
    return RETRY_BACKOFF_S[min(n, len(RETRY_BACKOFF_S)) - 1]


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="openrouter_seat")
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--reasoning-effort", default=None)
    ap.add_argument("--reasoning-max-tokens", type=int, default=None)
    ap.add_argument("--reasoning-off", action="store_true")      # answer-salvage call: reasoning.enabled = false
    ap.add_argument("--socket-timeout", type=float, default=270.0)
    ap.add_argument("--key-env", default="OPENROUTER_API_KEY")
    ap.add_argument("--provider-json", default=None)             # D43: the request's `provider` routing block, a JSON object
    args = ap.parse_args(argv)
    args.provider = None
    if args.provider_json is not None:
        # fail fast, before the key, stdin or any HTTP call; the value is never echoed
        try:
            obj = json.loads(args.provider_json)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            sys.stderr.write("openrouter bad --provider-json (not a JSON object)\n")
            sys.exit(2)
        args.provider = obj
    return args


def build_body(args, prompt):
    body = {"model": args.model, "stream": True,
            "messages": [{"role": "user", "content": prompt}]}
    if args.max_tokens:
        body["max_tokens"] = int(args.max_tokens)
    # OpenRouter's `reasoning` takes effort OR max_tokens, never both: the token cap wins.
    if args.reasoning_off:
        body["reasoning"] = {"enabled": False}
    elif args.reasoning_max_tokens:
        body["reasoning"] = {"max_tokens": int(args.reasoning_max_tokens)}
    elif args.reasoning_effort:
        body["reasoning"] = {"effort": args.reasoning_effort}
    if getattr(args, "provider", None) is not None:
        body["provider"] = args.provider
    return body


def fail(code, msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()
    return code


def retry_note(n, why, wait):
    sys.stderr.write("openrouter retry %d: %s\n" % (n, why))
    sys.stderr.flush()
    time.sleep(wait)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    key = os.environ.get(args.key_env, "")
    if not key:
        return fail(2, "openrouter key missing")
    prompt = sys.stdin.buffer.read().decode("utf-8", "replace")
    if not prompt.strip():
        return fail(2, "openrouter empty prompt")
    data = json.dumps(build_body(args, prompt)).encode("utf-8")
    req = urllib.request.Request(
        args.base_url.rstrip("/") + "/chat/completions", data=data, method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "Accept": "text/event-stream"})
    out = sys.stdout.buffer
    resp = None
    attempt = 0
    while resp is None:
        try:
            resp = urllib.request.urlopen(req, timeout=args.socket_timeout)
        except urllib.error.HTTPError as e:
            code, hdrs = e.code, e.headers
            try:
                e.close()                              # the error body is never read or kept
            except Exception:
                pass
            if attempt < MAX_RETRIES and retryable_http(code):
                attempt += 1
                retry_note(attempt, "http %d" % code, retry_wait(attempt, hdrs))
                continue
            return fail(1, "openrouter http %d" % code)
        except (urllib.error.URLError, OSError) as e:
            name = type(e).__name__
            # a silent connect for the whole socket timeout is not retried: a repeat would outlast the idle clock
            slow = isinstance(e, socket.timeout) or isinstance(getattr(e, "reason", None), socket.timeout)
            if attempt < MAX_RETRIES and not slow:
                attempt += 1
                retry_note(attempt, name, retry_wait(attempt, None))
                continue
            return fail(1, "openrouter %s" % name)
    finished = False
    errored = False
    try:
        with resp:
            if resp.status != 200:
                return fail(1, "openrouter http %d" % resp.status)
            while True:
                line = resp.readline()
                if not line:
                    break
                out.write(line)
                out.flush()
                s = line.strip()
                if not s.startswith(b"data:"):
                    continue                      # blank lines and ": OPENROUTER PROCESSING" comments
                payload = s[5:].strip()
                if payload == b"[DONE]":
                    finished = True
                    continue
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("error"):
                    errored = True
                for ch in obj.get("choices") or []:
                    if isinstance(ch, dict) and ch.get("finish_reason"):
                        finished = True
                        if ch.get("finish_reason") == "error":
                            errored = True
    except socket.timeout:
        return fail(5, "openrouter read timeout")
    except (OSError, ValueError) as e:
        return fail(5, "openrouter %s" % type(e).__name__)
    if errored:
        return fail(4, "openrouter stream error")
    if not finished:
        return fail(3, "openrouter stream ended early")
    return 0


if __name__ == "__main__":
    sys.exit(main())
