"""Spawn one MCP server over stdio and run the real handshake."""
from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "mcp-sanity", "version": "0.1.0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


@dataclass
class ProbeResult:
    status: str
    detail: str = ""
    tools: list[str] = field(default_factory=list)
    ms: int = 0


def _send(proc, msg):
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()


def _stderr_tail(proc, sink, limit=600):
    try:
        for chunk in iter(lambda: proc.stderr.read(2048), b""):
            sink.append(chunk.decode(errors="replace"))
            if sum(map(len, sink)) > limit * 4:
                break
    except Exception:
        pass


def probe_server(command, args, env, timeout=10.0):
    """Return ProbeResult with one of:
    OK MISSING_BIN NOT_EXECUTABLE PROCESS_EXIT HANDSHAKE_TIMEOUT BAD_JSON TOOL_ERROR EMPTY_RESPONSE
    """
    t0 = time.monotonic()
    exe = shutil.which(command, path=env and os.pathsep.join(
        [env.get("PATH", "")]) or None) if command else None
    if command and not exe:
        exe = shutil.which(command)
    if not command:
        return ProbeResult("MISSING_BIN", "no command configured", ms=int((time.monotonic() - t0) * 1000))
    if not exe:
        return ProbeResult("MISSING_BIN", f"'{command}' not found on PATH",
                           ms=int((time.monotonic() - t0) * 1000))
    full_env = dict(os.environ)
    full_env.update(env or {})
    try:
        proc = subprocess.Popen(
            [exe, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=full_env,
        )
    except PermissionError:
        return ProbeResult("NOT_EXECUTABLE", f"'{command}' is not executable",
                           ms=int((time.monotonic() - t0) * 1000))
    except OSError as exc:
        return ProbeResult("PROCESS_EXIT", f"spawn failed: {exc}",
                           ms=int((time.monotonic() - t0) * 1000))

    err_sink: list[str] = []
    threading.Thread(target=_stderr_tail, args=(proc, err_sink), daemon=True).start()

    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    buf = b""
    noise: list[str] = []
    init_resp = None
    tools_resp = None
    deadline = time.monotonic() + timeout

    def finish(result):
        result.ms = int((time.monotonic() - t0) * 1000)
        try:
            proc.kill()
        except Exception:
            pass
        return result

    try:
        _send(proc, INITIALIZE)
        while time.monotonic() < deadline:
            if proc.poll() is not None and not buf.strip():
                tail = "".join(err_sink)[-400:]
                return finish(ProbeResult("PROCESS_EXIT",
                                          f"server exited code {proc.returncode}" + (f"; stderr: {tail}" if tail else "")))
            for key, _ in sel.select(max(0.05, min(0.5, deadline - time.monotonic()))):
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    if init_resp is None:
                        code = proc.poll()
                        tail = "".join(err_sink)[-400:]
                        if code is not None:
                            det = f"server exited code {code}" + (f"; stderr: {tail}" if tail else "")
                            return finish(ProbeResult("PROCESS_EXIT", det))
                        kind = "BAD_JSON" if noise else "EMPTY_RESPONSE"
                        det = f"stdout had non-JSON lines: {noise[0][:120]!r}" if noise else "server closed stdout without responding"
                        return finish(ProbeResult(kind, det))
                    break
                buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line or line.lower().startswith(b"content-length"):
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    noise.append(line.decode(errors="replace")[:120])
                    continue
                if not isinstance(msg, dict):
                    noise.append(line.decode(errors="replace")[:120])
                    continue
                if msg.get("id") == 1:
                    init_resp = msg
                    _send(proc, INITIALIZED)
                    _send(proc, TOOLS_LIST)
                elif msg.get("id") == 2:
                    tools_resp = msg
            if tools_resp is not None:
                break
    finally:
        sel.close()

    if init_resp is None:
        if noise:
            return finish(ProbeResult("BAD_JSON",
                                      f"non-JSON line on stdout, no initialize response: {noise[0][:120]!r}"))
        return finish(ProbeResult("HANDSHAKE_TIMEOUT",
                                  f"no initialize response within {timeout:.0f}s"))
    if "error" in init_resp:
        return finish(ProbeResult("BAD_JSON", f"initialize error: {init_resp['error']}"))
    if tools_resp is None:
        return finish(ProbeResult("HANDSHAKE_TIMEOUT", f"no tools/list response within {timeout:.0f}s"))
    if "error" in tools_resp:
        return finish(ProbeResult("TOOL_ERROR", f"tools/list error: {tools_resp['error'].get('message', tools_resp['error'])}"))
    tools = [t.get("name", "?") for t in (tools_resp.get("result") or {}).get("tools", [])]
    detail = f"{len(tools)} tool(s)"
    if noise:
        detail += f"; warning: {len(noise)} non-JSON stdout line(s) (spec violation)"
    return finish(ProbeResult("OK", detail, tools, 0))
