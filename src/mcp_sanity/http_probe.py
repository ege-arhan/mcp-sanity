"""Probe remote MCP servers over Streamable HTTP (plain JSON or SSE frames)."""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from .probe import INITIALIZE, INITIALIZED, TOOLS_LIST, ProbeResult

MAX_BODY = 1_000_000


def _post(url: str, msg: dict, timeout: float) -> tuple[int, str, bytes]:
    data = json.dumps(msg).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BODY + 1)[:MAX_BODY]
            return resp.status, resp.headers.get("Content-Type", ""), raw
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_BODY + 1)[:MAX_BODY]
        except Exception:
            raw = b""
        return exc.code, str(exc.headers.get("Content-Type", "") or ""), raw


def _find_response(body: str, want_id: int) -> dict | None:
    s = body.strip()
    if not s:
        return None
    if s.startswith("{"):
        try:
            msg = json.loads(s)
        except ValueError:
            pass
        else:
            return msg if isinstance(msg, dict) else None
    for line in s.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            msg = json.loads(payload)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == want_id:
            return msg
    return None


def probe_url(url: str, timeout: float = 10.0) -> ProbeResult:
    """Return ProbeResult with one of:
    OK HTTP_UNREACHABLE HTTP_STATUS HTTP_TIMEOUT HTTP_BAD_JSONRPC TOOL_ERROR
    """
    t0 = time.monotonic()

    def ms() -> int:
        return int((time.monotonic() - t0) * 1000)

    def remaining() -> float:
        return max(0.5, timeout - (time.monotonic() - t0))

    def do(msg: dict):
        try:
            return _post(url, msg, remaining())
        except (socket.timeout, TimeoutError):
            raise _Timeout()
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise _Timeout()
            raise _Unreachable(str(exc.reason))
        except (OSError, ValueError) as exc:
            raise _Unreachable(str(exc))

    try:
        st, _ct, raw = do(INITIALIZE)
    except _Timeout:
        return ProbeResult("HTTP_TIMEOUT", f"no HTTP response within {timeout:.0f}s", ms=ms())
    except _Unreachable as exc:
        return ProbeResult("HTTP_UNREACHABLE", f"cannot reach {url}: {exc}", ms=ms())
    if st != 200:
        return ProbeResult("HTTP_STATUS",
                           f"HTTP {st}; {raw.decode(errors='replace')[:150]}", ms=ms())
    init = _find_response(raw.decode(errors="replace"), 1)
    if init is None:
        return ProbeResult("HTTP_BAD_JSONRPC",
                           f"no JSON-RPC id=1 in response: {raw[:120]!r}", ms=ms())
    if "error" in init:
        return ProbeResult("HTTP_BAD_JSONRPC", f"initialize error: {init['error']}", ms=ms())

    try:
        do(INITIALIZED)  # fire-and-forget notification
    except (_Timeout, _Unreachable):
        pass
    try:
        st, _ct, raw = do(TOOLS_LIST)
    except _Timeout:
        return ProbeResult("HTTP_TIMEOUT", f"no tools/list response within {timeout:.0f}s", ms=ms())
    except _Unreachable as exc:
        return ProbeResult("HTTP_UNREACHABLE", f"lost {url}: {exc}", ms=ms())
    if st != 200:
        return ProbeResult("HTTP_STATUS",
                           f"HTTP {st}; {raw.decode(errors='replace')[:150]}", ms=ms())
    tools_resp = _find_response(raw.decode(errors="replace"), 2)
    if tools_resp is None:
        return ProbeResult("HTTP_BAD_JSONRPC",
                           f"no JSON-RPC id=2 in response: {raw[:120]!r}", ms=ms())
    if "error" in tools_resp:
        err = tools_resp["error"]
        detail = err.get("message", err) if isinstance(err, dict) else err
        return ProbeResult("TOOL_ERROR", f"tools/list error: {detail}", ms=ms())
    tools = [t.get("name", "?") for t in (tools_resp.get("result") or {}).get("tools", [])]
    return ProbeResult("OK", f"{len(tools)} tool(s) via HTTP", tools, ms())


class _Timeout(Exception):
    pass


class _Unreachable(Exception):
    pass
