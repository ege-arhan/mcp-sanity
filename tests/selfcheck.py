#!/usr/bin/env python3
"""Self-check: python tests/selfcheck.py  (no frameworks, assert-based)."""
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp_sanity import discover, fix, http_probe, probe  # noqa: E402
from mcp_sanity.cli import select_servers  # noqa: E402
from mcp_sanity.sarif import to_sarif  # noqa: E402

FX = ROOT / "tests" / "fixtures"
PY = sys.executable


def _cfg(name: str, payload: dict) -> Path:
    p = FX / name
    p.write_text(json.dumps(payload))
    return p


def main():
    # 1) discover: json + toml + opencode loaders
    good = _cfg("good.json", {"mcpServers": {
        "ok": {"command": PY, "args": [str(FX / "good_server.py")], "env": {"TOKEN": "$MY_TOKEN"}},
        "ghost": {"command": "definitely-not-installed-bin", "args": []},
        "noisy": {"command": PY, "args": [str(FX / "noise_server.py")]},
    }})
    servers = discover.load_servers(good, "cursor", "json")
    assert [s.name for s in servers] == ["ok", "ghost", "noisy"], servers
    assert servers[0].env["TOKEN"] == "$MY_TOKEN"
    assert servers[0].command == PY

    toml_cfg = FX / "codex.toml"
    toml_cfg.write_text('[mcp_servers.demo]\ncommand = "python3"\nargs = ["-c", "pass"]\n')
    tservers = discover.load_servers(toml_cfg, "codex", "toml")
    assert tservers[0].name == "demo" and tservers[0].args == ["-c", "pass"], tservers

    oc = _cfg("oc.json", {"mcp": {"brave": {"command": "npx -y @scope/server-brave"}}})
    oservers = discover.load_servers(oc, "opencode", "opencode")
    assert oservers[0].command == "npx" and oservers[0].args[0] == "-y", oservers

    # 2) probe: real handshake against fixture server
    r = probe.probe_server(PY, [str(FX / "good_server.py")], {}, timeout=10)
    assert r.status == "OK", r
    assert r.tools == ["echo", "add"], r.tools
    assert r.ms >= 0

    # 3) probe: missing binary is classified, not crashed
    r = probe.probe_server("definitely-not-installed-bin", [], {}, timeout=2)
    assert r.status == "MISSING_BIN", r

    # 4) probe: stdout noise -> BAD_JSON (spec violation caught)
    r = probe.probe_server(PY, [str(FX / "noise_server.py")], {}, timeout=2)
    assert r.status == "BAD_JSON", r
    assert "non-JSON" in r.detail or "noise" in r.detail.lower(), r.detail

    # 5) probe: process that exits immediately
    r = probe.probe_server(PY, ["-c", "import sys; sys.exit(3)"], {}, timeout=3)
    assert r.status == "PROCESS_EXIT", r

    # 6) fix hints are actionable and hint-bearing
    s_ok, s_ghost = servers[0], servers[1]
    assert fix.fix_hint(s_ok, probe.ProbeResult("OK")) is None
    hint = fix.fix_hint(s_ghost, probe.ProbeResult("MISSING_BIN", "x"))
    assert hint and "definitely-not-installed-bin" in hint, hint
    assert "PATH" in fix.fix_hint(
        discover.Server("n", "c", "cursor", command="npx"),
        probe.ProbeResult("MISSING_BIN", ""))
    warn = fix.config_warnings([s_ok])
    assert any("TOKEN" in w for w in warn), warn
    dup = discover.Server("dup", str(good), "cursor", command=PY, args=s_ok.args)
    assert any("duplicate" in w.lower() or "aynı" in w for w in fix.config_warnings([s_ok, dup]))

    # 7) CLI end-to-end + exit codes + --json shape
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(good), "--timeout", "3"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    assert proc.returncode in (2, 3), (proc.returncode, proc.stdout, proc.stderr)
    assert "ghost" in proc.stdout and "fix:" in proc.stdout, proc.stdout

    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(good), "--timeout", "3", "--json"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    data = json.loads(proc.stdout)
    assert {row["status"] for row in data["servers"]} == {"OK", "MISSING_BIN", "BAD_JSON"}, data
    assert data["exit_code"] == proc.returncode != 0
    assert len(data["warnings"]) >= 1

    only_good = _cfg("only_good.json", {"mcpServers": {
        "ok": {"command": PY, "args": [str(FX / "good_server.py")]}}})
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(only_good), "--timeout", "5"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)

    # 8) http_probe: fixture MCP endpoint (JSON, SSE, 404, slow)
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _body(self):
            ln = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(ln) or b"{}")

        def do_POST(self):
            if self.path not in ("/mcp-json", "/mcp-sse", "/slow"):
                self.send_response(404); self.end_headers(); return
            msg = self._body()
            if self.path == "/slow":
                import time as _t
                _t.sleep(3)
            mid = msg.get("id")
            if "notifications/" in (msg.get("method") or ""):
                raw = b""; ct = "application/json"; code = 202
            elif mid == 1:
                raw = json.dumps({"jsonrpc": "2.0", "id": 1,
                                  "result": {"protocolVersion": "2024-11-05",
                                               "capabilities": {},
                                               "serverInfo": {"name": "fx", "version": "0"}}}).encode()
                ct = "application/json"
            elif mid == 2:
                raw = json.dumps({"jsonrpc": "2.0", "id": 2,
                                  "result": {"tools": [{"name": "echo"}, {"name": "add"}]}}).encode()
                ct = "application/json"
            else:
                self.send_response(404); self.end_headers(); return
            if self.path == "/mcp-sse":
                raw = b"data: " + raw + b"\n\n"
                ct = "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    r = http_probe.probe_url(base + "/mcp-json", timeout=10)
    assert r.status == "OK", r
    assert r.tools == ["echo", "add"], r.tools

    r = http_probe.probe_url(base + "/mcp-sse", timeout=10)
    assert r.status == "OK", r
    assert r.tools == ["echo", "add"], r.tools

    r = http_probe.probe_url(base + "/nope", timeout=5)
    assert r.status == "HTTP_STATUS", r
    assert "404" in r.detail, r.detail

    r = http_probe.probe_url(base + "/slow", timeout=1)
    assert r.status == "HTTP_TIMEOUT", r

    r = http_probe.probe_url("http://127.0.0.1:1/mcp", timeout=2)
    assert r.status == "HTTP_UNREACHABLE", r

    h = fix.fix_hint(discover.Server("n", "c", "cursor", url=base),
                     http_probe.ProbeResult("HTTP_STATUS", "HTTP 401; x"))
    assert h and "auth" in h.lower() or "Authorization" in h, h
    h = fix.fix_hint(discover.Server("n", "c", "cursor", url=base),
                     http_probe.ProbeResult("HTTP_STATUS", "HTTP 404; x"))
    assert h and "/mcp" in h, h
    h = fix.fix_hint(discover.Server("n", "c", "cursor", url=base),
                     http_probe.ProbeResult("HTTP_UNREACHABLE", "x"))
    assert h, h
    srv.shutdown()

    # 9) sarif: rules + results, CLI --sarif writes file
    rows = [
        {"client": "cursor", "name": "ghost", "config": str(good),
         "command": "x", "status": "MISSING_BIN", "detail": "'x' not found",
         "tools": [], "hint": "kur", "ms": 1},
        {"client": "cursor", "name": "ok", "config": str(good),
         "command": "y", "status": "OK", "detail": "2 tool(s)",
         "tools": ["a"], "hint": None, "ms": 2},
    ]
    doc = to_sarif(rows, ["cursor/ok: env uyarisi"])
    assert doc["version"] == "2.1.0", doc
    assert doc["runs"][0]["tool"]["driver"]["name"] == "mcp-sanity"
    ids = {r["ruleId"] for r in doc["runs"][0]["results"]}
    assert ids == {"MISSING_BIN", "CONFIG_WARNING"}, ids
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(good),
                           "--timeout", "3", "--sarif", str(FX / "out.sarif")],
                          capture_output=True, text=True, cwd=ROOT / "src")
    assert proc.returncode in (2, 3), (proc.returncode, proc.stdout)
    sarif_doc = json.loads((FX / "out.sarif").read_text())
    assert sarif_doc["runs"][0]["results"], sarif_doc
    (FX / "out.sarif").unlink(missing_ok=True)
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(only_good),
                           "--timeout", "5", "--sarif", str(FX / "ok.sarif")],
                          capture_output=True, text=True, cwd=ROOT / "src")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert json.loads((FX / "ok.sarif").read_text())["runs"][0]["results"] == []
    (FX / "ok.sarif").unlink(missing_ok=True)

    # 10) --only/--skip selectors: name, client/name, glob, skip-after-only
    all3 = servers  # cursor: ok, ghost, noisy
    assert [s.name for s in select_servers(all3, ["ok"])] == ["ok"]
    assert [s.name for s in select_servers(all3, ["cursor/ok"])] == ["ok"]
    assert [s.name for s in select_servers(all3, ["cursor/*"])] == ["ok", "ghost", "noisy"]
    assert [s.name for s in select_servers(all3, ["ok,noisy"])] == ["ok", "noisy"]
    assert [s.name for s in select_servers(all3, [], ["ghost"])] == ["ok", "noisy"]
    assert [s.name for s in select_servers(all3, ["cursor/*"], ["*ghost*"])] == ["ok", "noisy"]
    assert select_servers(all3, ["nope"]) == []
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(good),
                           "--timeout", "5", "--only", "ok", "--json"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    data = json.loads(proc.stdout)
    assert [r["name"] for r in data["servers"]] == ["ok"], data
    assert proc.returncode == 0, (proc.returncode, proc.stdout)
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(good),
                           "--timeout", "5", "--skip", "ghost,noisy", "--json"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    data = json.loads(proc.stdout)
    assert [r["name"] for r in data["servers"]] == ["ok"], data

    # 11) G5: env eksigi + secrets acik-key taramasi
    mk = lambda n, **kw: discover.Server(n, "c.json", "cursor",
        command=kw.get("command", "python3"), args=kw.get("args", []),
        env=kw.get("env", {}), url=kw.get("url"))
    assert any("bo\u015f" in w for w in fix.config_warnings([mk("e1", env={"A": ""})]))
    assert any("placeholder" in w for w in fix.config_warnings([mk("e2", env={"A": "YOUR_API_KEY"})]))
    assert any("OpenAI" in w for w in fix.config_warnings([mk("e3", env={"K": "sk-proj-abcdefghij1234567890XYZ"})]))
    assert any("GitHub" in w for w in fix.config_warnings([mk("e4", env={"T": "ghp_abcdefghij1234567890"})]))
    assert any("args" in w for w in fix.config_warnings([mk("e5", args=["--token", "abc"])]))
    assert any("args" in w for w in fix.config_warnings([mk("e6", args=["--token=abc"])]))
    assert any("g\u00f6m\u00fcl\u00fc" in w for w in fix.config_warnings([mk("e7", url="https://user:pass@host/mcp")]))
    assert any("query" in w for w in fix.config_warnings([mk("e8", url="https://host/mcp?token=abc123")]))
    # unresolved $VAR cift uyari vermez
    w = fix.config_warnings([mk("ok", env={"A": "$MY_TOKEN"})])
    assert len(w) == 1 and "TOKEN" not in w[0] or "A" in w[0], w

    # 12) G6: retry + FLAKY (transient crashes caught, deterministic failures not retried)
    flaky_fx = FX / "flaky_server.py"
    marker = FX / "flaky_marker"
    marker.unlink(missing_ok=True)
    r = probe.retry_flaky(lambda: probe.probe_server(PY, [str(flaky_fx), str(marker)], {}, timeout=5))
    assert r.status == "FLAKY", r
    assert r.attempts == 2 and r.tools == ["echo"], r
    assert "attempt 1 failed with PROCESS_EXIT" in r.detail, r.detail
    # deterministic MISSING_BIN: one attempt, unchanged status
    calls = []
    def _missing():
        calls.append(1)
        return probe.probe_server("definitely-not-installed-bin", [], {}, timeout=2)
    r = probe.retry_flaky(_missing)
    assert r.status == "MISSING_BIN" and len(calls) == 1, (r, calls)
    assert fix.fix_hint(s_ok, probe.ProbeResult("FLAKY", "passed on attempt 2")).lower().startswith("arada")
    # SARIF: FLAKY = warning-level rule
    doc = to_sarif([{"client": "cursor", "name": "f", "config": "c.json", "command": "x",
                     "status": "FLAKY", "detail": "d", "tools": [], "hint": None, "ms": 5}], [])
    res = doc["runs"][0]["results"]
    assert len(res) == 1 and res[0]["ruleId"] == "FLAKY" and res[0]["level"] == "warning", doc

    flaky_cfg = _cfg("flaky.json", {"mcpServers": {
        "f": {"command": PY, "args": [str(flaky_fx), str(FX / "flaky_marker2")]}}})
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(flaky_cfg), "--timeout", "5", "--json"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    data = json.loads(proc.stdout)
    assert data["servers"][0]["status"] == "FLAKY", data
    assert data["servers"][0]["attempts"] == 2, data
    assert proc.returncode == 4 == data["exit_code"], (proc.returncode, data)
    # --retries 1 disables: first crash stays PROCESS_EXIT (exit 2)
    (FX / "flaky_marker3").unlink(missing_ok=True)
    flaky_cfg3 = _cfg("flaky3.json", {"mcpServers": {
        "f": {"command": PY, "args": [str(flaky_fx), str(FX / "flaky_marker3")]}}})
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(flaky_cfg3), "--timeout", "5",
                           "--retries", "1", "--json"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    data = json.loads(proc.stdout)
    assert data["servers"][0]["status"] == "PROCESS_EXIT" and proc.returncode == 2, data
    for m in ("flaky_marker", "flaky_marker2", "flaky_marker3"):
        (FX / m).unlink(missing_ok=True)

    # 14) G8: JSON cikista semver + sema stabil
    from mcp_sanity import __schema_version__  # noqa: E402
    from mcp_sanity.cli import JSON_SCHEMA  # noqa: E402
    assert __schema_version__ == "1", __schema_version__
    assert JSON_SCHEMA["properties"]["schema_version"] == {"const": "1"}
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--json-schema"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    schema = json.loads(proc.stdout)
    assert schema["required"] == ["schema_version", "version", "servers", "warnings", "exit_code"], schema
    want_cols = {"client", "name", "config", "command", "status", "detail",
                 "tools", "hint", "ms", "attempts"}
    assert set(schema["properties"]["servers"]["items"]["required"]) == want_cols, schema
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(only_good),
                           "--timeout", "5", "--json"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    data = json.loads(proc.stdout)
    assert data["schema_version"] == "1", data
    assert set(data["servers"][0]) == want_cols, data["servers"][0]

    # 13) G7: demo senaryosu calisir durumda (OK + MISSING_BIN + BAD_JSON)
    demo = subprocess.run(["bash", str(ROOT / "demo" / "run.sh")],
                          capture_output=True, text=True, cwd=ROOT)
    assert demo.returncode == 0, (demo.returncode, demo.stdout, demo.stderr)
    assert "good-notes" in demo.stdout and "ghost-bin" in demo.stdout, demo.stdout
    assert "MISSING_BIN" in demo.stdout and "BAD_JSON" in demo.stdout, demo.stdout
    assert (ROOT / "demo" / "mcp.json.tpl").is_file()
    assert (ROOT / "demo" / "run.sh").is_file()

    print("selfcheck: 14/14 groups passed")


if __name__ == "__main__":
    main()
