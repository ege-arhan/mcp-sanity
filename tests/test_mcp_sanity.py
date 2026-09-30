"""pytest suite for mcp-sanity.
All test cases ported from selfcheck.py into idiomatic standalone pytest functions.
"""
import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp_sanity import __schema_version__, discover, fix, http_probe, probe  # noqa: E402
from mcp_sanity.cli import JSON_SCHEMA, compare_groups, render_compare, select_servers  # noqa: E402
from mcp_sanity.sarif import to_sarif  # noqa: E402

FX = ROOT / "tests" / "fixtures"
PY = sys.executable


def _cfg(name: str, payload: dict) -> Path:
    p = FX / name
    p.write_text(json.dumps(payload))
    return p


def test_discover_json_toml_opencode():
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


def test_probe_real_handshake():
    r = probe.probe_server(PY, [str(FX / "good_server.py")], {}, timeout=10)
    assert r.status == "OK", r
    assert r.tools == ["echo", "add"], r.tools
    assert r.ms >= 0


def test_probe_missing_binary():
    r = probe.probe_server("definitely-not-installed-bin", [], {}, timeout=2)
    assert r.status == "MISSING_BIN", r


def test_probe_stdout_noise():
    r = probe.probe_server(PY, [str(FX / "noise_server.py")], {}, timeout=2)
    assert r.status == "BAD_JSON", r
    assert "non-JSON" in r.detail or "noise" in r.detail.lower(), r.detail


def test_probe_process_exit():
    r = probe.probe_server(PY, ["-c", "import sys; sys.exit(3)"], {}, timeout=3)
    assert r.status == "PROCESS_EXIT", r


def test_fix_hints_and_warnings():
    good = _cfg("good.json", {"mcpServers": {
        "ok": {"command": PY, "args": [str(FX / "good_server.py")], "env": {"TOKEN": "$MY_TOKEN"}},
        "ghost": {"command": "definitely-not-installed-bin", "args": []},
        "noisy": {"command": PY, "args": [str(FX / "noise_server.py")]},
    }})
    servers = discover.load_servers(good, "cursor", "json")
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


def test_cli_end_to_end_and_exit_codes():
    good = _cfg("good.json", {"mcpServers": {
        "ok": {"command": PY, "args": [str(FX / "good_server.py")], "env": {"TOKEN": "$MY_TOKEN"}},
        "ghost": {"command": "definitely-not-installed-bin", "args": []},
        "noisy": {"command": PY, "args": [str(FX / "noise_server.py")]},
    }})
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


def test_http_probe():
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

    try:
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
        assert h and ("auth" in h.lower() or "Authorization" in h), h
        h = fix.fix_hint(discover.Server("n", "c", "cursor", url=base),
                         http_probe.ProbeResult("HTTP_STATUS", "HTTP 404; x"))
        assert h and "/mcp" in h, h
        h = fix.fix_hint(discover.Server("n", "c", "cursor", url=base),
                         http_probe.ProbeResult("HTTP_UNREACHABLE", "x"))
        assert h, h
    finally:
        srv.shutdown()


def test_sarif_export():
    good = _cfg("good.json", {"mcpServers": {
        "ok": {"command": PY, "args": [str(FX / "good_server.py")]},
        "ghost": {"command": "definitely-not-installed-bin", "args": []},
    }})
    only_good = _cfg("only_good.json", {"mcpServers": {
        "ok": {"command": PY, "args": [str(FX / "good_server.py")]}}})

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


def test_only_skip_selectors():
    good = _cfg("good.json", {"mcpServers": {
        "ok": {"command": PY, "args": [str(FX / "good_server.py")]},
        "ghost": {"command": "definitely-not-installed-bin", "args": []},
        "noisy": {"command": PY, "args": [str(FX / "noise_server.py")]},
    }})
    all3 = discover.load_servers(good, "cursor", "json")
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


def test_env_secrets_detection():
    mk = lambda n, **kw: discover.Server(n, "c.json", "cursor",
        command=kw.get("command", "python3"), args=kw.get("args", []),
        env=kw.get("env", {}), url=kw.get("url"))
    assert any("bo\u015f" in w for w in fix.config_warnings([mk("e1", env={"A": ""})]))
    assert any("placeholder" in w for w in fix.config_warnings([mk("e2", env={"A": "YOUR_API_KEY"})]))
    assert any("OpenAI" in w for w in fix.config_warnings([mk("e3", env={"K": "sk-proj-abcdefghij1234567890XYZ"})]))
    assert any("GitHub" in w for w in fix.config_warnings([mk("e4", env={"T": "ghp_abcdefghijklmnopqrstuvwxyz1234567890"})]))
    assert any("args" in w for w in fix.config_warnings([mk("e5", args=["--token", "abc"])]))
    assert any("args" in w for w in fix.config_warnings([mk("e6", args=["--token=***"])]))
    assert any("g\u00f6m\u00fcl\u00fc" in w for w in fix.config_warnings([mk("e7", url="https://user:***@host/mcp")]))
    assert any("query" in w for w in fix.config_warnings([mk("e8", url="https://host/mcp?token=***")]))
    # unresolved $VAR cift uyari vermez
    w = fix.config_warnings([mk("ok", env={"A": "$MY_TOKEN"})])
    assert len(w) == 1 and ("TOKEN" not in w[0] or "A" in w[0]), w


def test_flaky_retry():
    flaky_fx = FX / "flaky_server.py"
    marker = FX / "flaky_marker"
    marker.unlink(missing_ok=True)
    r = probe.retry_flaky(lambda: probe.probe_server(PY, [str(flaky_fx), str(marker)], {}, timeout=5))
    assert r.status == "FLAKY", r
    assert r.attempts == 2 and r.tools == ["echo"], r
    assert "attempt 1 failed with PROCESS_EXIT" in r.detail, r.detail

    calls = []
    def _missing():
        calls.append(1)
        return probe.probe_server("definitely-not-installed-bin", [], {}, timeout=2)
    r = probe.retry_flaky(_missing)
    assert r.status == "MISSING_BIN" and len(calls) == 1, (r, calls)
    s_ok = discover.Server("ok", "c.json", "cursor", command=PY)
    assert fix.fix_hint(s_ok, probe.ProbeResult("FLAKY", "passed on attempt 2")).lower().startswith("arada")

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


def test_schema_stability():
    only_good = _cfg("only_good.json", {"mcpServers": {
        "ok": {"command": PY, "args": [str(FX / "good_server.py")]}}})
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


def test_compare_clients():
    only_good = _cfg("only_good.json", {"mcpServers": {
        "ok": {"command": PY, "args": [str(FX / "good_server.py")]}}})
    rows = [
        {"client": "cursor", "name": "notes", "config": "a", "command": "python3 x.py",
         "status": "OK", "detail": "2 tool(s)", "tools": [], "hint": None, "ms": 5, "attempts": 1},
        {"client": "codex", "name": "notes", "config": "b", "command": "python3 x.py",
         "status": "PROCESS_EXIT", "detail": "died", "tools": [], "hint": "h", "ms": 6, "attempts": 1},
        {"client": "cursor", "name": "solo", "config": "a", "command": "other-bin",
         "status": "OK", "detail": "1 tool(s)", "tools": [], "hint": None, "ms": 3, "attempts": 1},
    ]
    groups = compare_groups(rows)
    assert len(groups) == 2, groups
    shared_g = next(g for g in groups if g["key"] == "python3 x.py")
    assert shared_g["shared"] and shared_g["divergent"], shared_g
    solo_g = next(g for g in groups if g["key"] == "other-bin")
    assert not solo_g["shared"] and not solo_g["divergent"], solo_g
    text = render_compare(groups)
    assert "DIVERGENT" in text and "shared across clients" in text, text

    cmp_a = _cfg("cmp_a.json", {"mcpServers": {
        "ok": {"command": PY, "args": [str(FX / "good_server.py")]}}})
    cmp_b = _cfg("cmp_b.json", {"mcpServers": {
        "ghost": {"command": "definitely-not-installed-bin", "args": []}}})
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(cmp_a),
                           "--config", str(cmp_b), "--timeout", "3", "--compare"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    assert "compare" in proc.stdout, (proc.returncode, proc.stdout)
    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(cmp_a),
                           "--config", str(cmp_b), "--timeout", "3",
                           "--json", "--compare"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    data = json.loads(proc.stdout)
    assert "compare" in data and len(data["compare"]) == 2, data

    proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(only_good),
                           "--timeout", "5", "--json"],
                          capture_output=True, text=True, cwd=ROOT / "src")
    assert "compare" not in json.loads(proc.stdout)


def test_fix_apply_and_dry_run():
    noexec_sh = FX / "noexec.sh"
    noexec_sh.write_text("#!/bin/sh\nexit 0\n")
    noexec_sh.chmod(0o644)
    assert not os.access(noexec_sh, os.X_OK)

    fix_cfg = _cfg("fixable.json", {
        "mcpServers": {
            "noexec": {"command": str(noexec_sh), "args": []},
            "srv1": {"command": PY, "args": [str(FX / "good_server.py")], "env": {"EMPTY": "", "KEEP": "1"}},
            "srv1_dup": {"command": PY, "args": [str(FX / "good_server.py")], "env": {"EMPTY": "", "KEEP": "1"}},
        }
    })

    try:
        proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(fix_cfg),
                               "--timeout", "3", "--fix-dry-run"],
                              capture_output=True, text=True, cwd=ROOT / "src")
        assert "fixes (dry-run)" in proc.stdout, (proc.returncode, proc.stdout)
        assert "candidate" in proc.stdout, proc.stdout
        assert not os.access(noexec_sh, os.X_OK)
        cfg_data_dry = json.loads(fix_cfg.read_text())
        assert "srv1_dup" in cfg_data_dry["mcpServers"]
        assert cfg_data_dry["mcpServers"]["srv1"]["env"]["EMPTY"] == ""

        proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(fix_cfg),
                               "--timeout", "3", "--json", "--fix-dry-run"],
                              capture_output=True, text=True, cwd=ROOT / "src")
        data = json.loads(proc.stdout)
        assert "fixes" in data and len(data["fixes"]) >= 2, data
        assert any(f["kind"] == "chmod_x" and not f["applied"] for f in data["fixes"])
        assert any(f["kind"] == "clean_config" and not f["applied"] for f in data["fixes"])

        proc = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(fix_cfg),
                               "--timeout", "3", "--fix"],
                              capture_output=True, text=True, cwd=ROOT / "src")
        assert "fixes (applied)" in proc.stdout, (proc.returncode, proc.stdout)
        assert "applied" in proc.stdout, proc.stdout
        assert os.access(noexec_sh, os.X_OK)

        cfg_data_fixed = json.loads(fix_cfg.read_text())
        assert "srv1_dup" not in cfg_data_fixed["mcpServers"]
        assert "EMPTY" not in cfg_data_fixed["mcpServers"]["srv1"]["env"]
        assert cfg_data_fixed["mcpServers"]["srv1"]["env"]["KEEP"] == "1"
    finally:
        noexec_sh.unlink(missing_ok=True)
        fix_cfg.unlink(missing_ok=True)


def test_concurrency():
    good = _cfg("good.json", {"mcpServers": {
        "ok": {"command": PY, "args": [str(FX / "good_server.py")]},
        "ghost": {"command": "definitely-not-installed-bin", "args": []},
        "noisy": {"command": PY, "args": [str(FX / "noise_server.py")]},
    }})
    proc_seq = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(good),
                               "--timeout", "5", "-j", "1", "--json"],
                              capture_output=True, text=True, cwd=ROOT / "src")
    assert proc_seq.returncode in (2, 3), proc_seq.returncode
    data_seq = json.loads(proc_seq.stdout)

    proc_par = subprocess.run([PY, "-m", "mcp_sanity", "--config", str(good),
                               "--timeout", "5", "--concurrency", "4", "--json"],
                              capture_output=True, text=True, cwd=ROOT / "src")
    assert proc_par.returncode in (2, 3), proc_par.returncode
    data_par = json.loads(proc_par.stdout)

    assert [s["name"] for s in data_seq["servers"]] == [s["name"] for s in data_par["servers"]]
    assert [s["status"] for s in data_seq["servers"]] == [s["status"] for s in data_par["servers"]]


def test_demo_script():
    demo = subprocess.run(["bash", str(ROOT / "demo" / "run.sh")],
                          capture_output=True, text=True, cwd=ROOT)
    assert demo.returncode == 0, (demo.returncode, demo.stdout, demo.stderr)
    assert "good-notes" in demo.stdout and "ghost-bin" in demo.stdout, demo.stdout
    assert "MISSING_BIN" in demo.stdout and "BAD_JSON" in demo.stdout, demo.stdout
    assert (ROOT / "demo" / "mcp.json.tpl").is_file()
    assert (ROOT / "demo" / "run.sh").is_file()
