"""mcp-sanity CLI — live doctor for MCP client configs."""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
from pathlib import Path

from . import __version__, discover, fix, http_probe, probe
from .sarif import to_sarif

ICON = {
    "OK": "✔",
    "MISSING_BIN": "✖",
    "NOT_EXECUTABLE": "✖",
    "PROCESS_EXIT": "✖",
    "HANDSHAKE_TIMEOUT": "⏱",
    "BAD_JSON": "✖",
    "EMPTY_RESPONSE": "✖",
    "TOOL_ERROR": "⚠",
    "HTTP_UNREACHABLE": "✖",
    "HTTP_STATUS": "✖",
    "HTTP_TIMEOUT": "⏱",
    "HTTP_BAD_JSONRPC": "✖",
}
EXIT_FOR = {
    "OK": 0, "MISSING_BIN": 2, "NOT_EXECUTABLE": 2, "PROCESS_EXIT": 2,
    "HANDSHAKE_TIMEOUT": 3, "BAD_JSON": 3, "EMPTY_RESPONSE": 3, "TOOL_ERROR": 3,
    "HTTP_UNREACHABLE": 2, "HTTP_STATUS": 3, "HTTP_TIMEOUT": 3, "HTTP_BAD_JSONRPC": 3,
}


def run(servers, timeout, use_json, sarif_path=None):
    results: list[tuple] = []
    def _run(s):
        if not s.command and s.url and s.url.startswith(("http://", "https://")):
            return http_probe.probe_url(s.url, timeout)
        return probe.probe_server(s.command, s.args, s.env, timeout)

    with cf.ThreadPoolExecutor(max_workers=min(8, max(1, len(servers) or 1))) as pool:
        futs = {pool.submit(_run, s): s for s in servers}
        for fut in cf.as_completed(futs):
            results.append((futs[fut], fut.result()))
    results.sort(key=lambda r: (r[0].client, r[0].name))

    warnings = fix.config_warnings(servers)
    exit_code = 0
    rows = []
    for s, r in results:
        hint = fix.fix_hint(s, r)
        exit_code = max(exit_code, EXIT_FOR.get(r.status, 4))
        rows.append({
            "client": s.client, "name": s.name, "config": s.config,
            "command": " ".join(filter(None, [s.command, *s.args])) or s.url or "",
            "status": r.status, "detail": r.detail, "tools": r.tools,
            "hint": hint, "ms": r.ms,
        })

    if sarif_path:
        Path(sarif_path).write_text(json.dumps(to_sarif(rows, warnings),
                                                ensure_ascii=False, indent=2) + "\n")

    if use_json:
        print(json.dumps({"version": __version__, "servers": rows,
                          "warnings": warnings, "exit_code": exit_code},
                         ensure_ascii=False, indent=2))
        return exit_code

    cur = None
    for row in rows:
        if row["client"] != cur:
            cur = row["client"]
            print(f"\n{cur}")
        icon = ICON.get(row["status"], "?")
        line = f"  {icon} {row['name']:<24} {row['status']:<17} {row['detail'][:90]}"
        print(line.rstrip())
        if row["hint"]:
            print(f"      ↳ fix: {row['hint']}")
    if warnings:
        print("\nwarnings")
        for w in warnings:
            print(f"  ⚠ {w}")
    bad = sum(1 for r in rows if r["status"] != "OK")
    print(f"\n{len(rows)} server, {len(rows) - bad} OK, {bad} sorunlu"
          + (f", {len(warnings)} uyarı" if warnings else ""))
    return exit_code


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mcp-sanity",
                                 description="Live doctor for MCP server configs: spawn, handshake, name the broken one.")
    ap.add_argument("--config", "-c", action="append", default=[],
                    help="Ek config dosyası (tekrarlanabilir). Yoksa otomatik keşif.")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sarif", metavar="FILE", default=None,
                    help="SARIF 2.1.0 raporu yaz (GitHub code scanning).")
    ap.add_argument("--version", action="version", version=f"mcp-sanity {__version__}")
    args = ap.parse_args(argv)

    servers: list[discover.Server] = []
    if args.config:
        for p in args.config:
            path = Path(p)
            if not path.is_file():
                print(f"config yok: {path}", file=sys.stderr)
                return 4
            kind = "toml" if path.suffix == ".toml" else "json"
            servers.extend(discover.load_servers(path, "custom", kind))
    else:
        for path, client, kind in discover.candidate_configs():
            try:
                servers.extend(discover.load_servers(path, client, kind))
            except Exception as exc:
                if not args.json:
                    print(f"⚠ {path} okunamadı: {exc}", file=sys.stderr)

    if not servers:
        if args.sarif:
            Path(sarif_path).write_text(json.dumps(to_sarif([], []),
                                                    ensure_ascii=False, indent=2) + "\n")
        if args.json:
            print(json.dumps({"version": __version__, "servers": [],
                              "warnings": [], "exit_code": 0}))
        else:
            print("Hiç MCP config/server bulunamadı. --config ile dosya göster.")
        return 0
    return run(servers, args.timeout, args.json, args.sarif)


if __name__ == "__main__":
    raise SystemExit(main())
