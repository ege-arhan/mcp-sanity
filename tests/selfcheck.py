#!/usr/bin/env python3
"""Self-check: python tests/selfcheck.py  (no frameworks, assert-based)."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp_sanity import discover, fix, probe  # noqa: E402

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

    print("selfcheck: 7/7 groups passed")


if __name__ == "__main__":
    main()
