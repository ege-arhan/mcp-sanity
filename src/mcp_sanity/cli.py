"""mcp-sanity CLI — live doctor for MCP client configs."""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import fnmatch
import json
import sys
from pathlib import Path

from . import __schema_version__, __version__, discover, fix, http_probe, probe
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
    "FLAKY": "⚠",
}
EXIT_FOR = {
    "OK": 0, "MISSING_BIN": 2, "NOT_EXECUTABLE": 2, "PROCESS_EXIT": 2,
    "HANDSHAKE_TIMEOUT": 3, "BAD_JSON": 3, "EMPTY_RESPONSE": 3, "TOOL_ERROR": 3,
    "HTTP_UNREACHABLE": 2, "HTTP_STATUS": 3, "HTTP_TIMEOUT": 3, "HTTP_BAD_JSONRPC": 3,
    "FLAKY": 4,
}

# --json ciktisinin kararli semasi. Breaking alan degisikligi => schema_version artar.
# Ek alan (hint/ms/attempts gibi) minor'dir, schema_version artirmaz.
JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "mcp-sanity --json output",
    "type": "object",
    "required": ["schema_version", "version", "servers", "warnings", "exit_code"],
    "properties": {
        "schema_version": {"const": __schema_version__},
        "version": {"type": "string"},
        "exit_code": {"type": "integer"},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "servers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["client", "name", "config", "command",
                               "status", "detail", "tools", "hint", "ms", "attempts"],
                "properties": {
                    "client": {"type": "string"},
                    "name": {"type": "string"},
                    "config": {"type": "string"},
                    "command": {"type": "string"},
                    "status": {"type": "string"},
                    "detail": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "hint": {"type": ["string", "null"]},
                    "ms": {"type": "integer"},
                    "attempts": {"type": "integer"},
                },
            },
        },
    },
}


def _split_patterns(raw):
    pats = []
    for item in raw or []:
        pats.extend(p.strip() for p in str(item).split(",") if p.strip())
    return pats


def _matches(server, pattern):
    pat = pattern.strip().lower()
    full = f"{server.client}/{server.name}".lower()
    return (fnmatch.fnmatchcase(full, pat) or fnmatch.fnmatchcase(server.name.lower(), pat)
            or fnmatch.fnmatchcase(server.client.lower(), pat))


def select_servers(servers, only=(), skip=()):
    only = _split_patterns(only if isinstance(only, (list, tuple)) else [only])
    skip = _split_patterns(skip if isinstance(skip, (list, tuple)) else [skip])
    sel = [s for s in servers if any(_matches(s, p) for p in only)] if only else list(servers)
    if skip:
        sel = [s for s in sel if not any(_matches(s, p) for p in skip)]
    return sel


def compare_groups(rows):
    """Group result rows by normalized server identity (command+args or url).
    Returns [{key, cells:[row...], divergent:bool}] sorted by key."""
    groups: dict[str, list] = {}
    for r in rows:
        key = (r.get("command") or "").strip() or "(no command/url)"
        groups.setdefault(key, []).append(r)
    out = []
    for key in sorted(groups):
        cells = sorted(groups[key], key=lambda c: (c["client"], c["name"]))
        statuses = {c["status"] for c in cells}
        origins = {(c["client"], c["config"]) for c in cells}
        out.append({"key": key, "cells": cells,
                    "shared": len(origins) > 1, "divergent": len(statuses) > 1})
    return out


def render_compare(groups):
    lines = ["\ncompare"]
    if not groups:
        lines.append("  (no servers)")
        return "\n".join(lines)
    shared = sum(1 for g in groups if g["shared"])
    div = sum(1 for g in groups if g["divergent"])
    for g in groups:
        tag = ""
        if g["shared"]:
            tag = "  [DIVERGENT]" if g["divergent"] else "  [same]"
        lines.append(f"  = {g['key'][:100]}{tag}")
        for c in g["cells"]:
            icon = ICON.get(c["status"], "?")
            lines.append(f"    {icon} {c['client']}/{c['name']:<22} {c['status']}")
    lines.append(f"{len(groups)} unique server(s), {shared} shared across clients"
                 + (f", {div} divergent" if shared else ""))
    return "\n".join(lines)


def run(servers, timeout, use_json, sarif_path=None, retries=3, compare=False):
    results: list[tuple] = []
    def _once(s):
        if not s.command and s.url and s.url.startswith(("http://", "https://")):
            return http_probe.probe_url(s.url, timeout)
        return probe.probe_server(s.command, s.args, s.env, timeout)

    def _run(s):
        if retries <= 1:
            return _once(s)
        return probe.retry_flaky(lambda: _once(s), max_attempts=retries)

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
            "hint": hint, "ms": r.ms, "attempts": r.attempts,
        })

    if sarif_path:
        Path(sarif_path).write_text(json.dumps(to_sarif(rows, warnings),
                                                ensure_ascii=False, indent=2) + "\n")

    groups = compare_groups(rows) if compare else []
    if use_json:
        payload = {"schema_version": __schema_version__, "version": __version__,
                   "servers": rows, "warnings": warnings, "exit_code": exit_code}
        if compare:
            payload["compare"] = [{"key": g["key"], "shared": g["shared"],
                                     "divergent": g["divergent"],
                                     "cells": [{"client": c["client"], "name": c["name"],
                                                  "status": c["status"]} for c in g["cells"]]}
                                    for g in groups]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
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
    if compare:
        print(render_compare(groups))
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
    ap.add_argument("--only", action="append", default=[],
                    help="Yalniz bu server'lari denetle: 'client/name', 'name' veya glob (örn. 'cursor/*'). Tekrarlanabilir, virgulle de ayrilabilir.")
    ap.add_argument("--skip", action="append", default=[],
                    help="Bu server'lari atla: 'client/name', 'name' veya glob. --only'den sonra uygulanir.")
    ap.add_argument("--retries", type=int, default=3, metavar="N",
                    help="Crash/timeout sonrasi deneme sayisi (varsayilan 3). Son denemede gecen server FLAKY. 1 = retry kapali.")
    ap.add_argument("--compare", action="store_true",
                    help="Ayni komut/url'yi paylasan server'lari client'lar arasi karsilastir (ortak + DIVERGENT isaretle).")
    ap.add_argument("--json-schema", action="store_true",
                    help="JSON cikti semasini yazdir (semver: sema degisince schema_version artar).")
    ap.add_argument("--version", action="version", version=f"mcp-sanity {__version__}")
    args = ap.parse_args(argv)
    if args.json_schema:
        print(json.dumps(JSON_SCHEMA, ensure_ascii=False, indent=2))
        return 0

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

    servers = select_servers(servers, _split_patterns(args.only), _split_patterns(args.skip))
    if not servers:
        if args.sarif:
            Path(args.sarif).write_text(json.dumps(to_sarif([], []),
                                                    ensure_ascii=False, indent=2) + "\n")
        if args.json:
            print(json.dumps({"schema_version": __schema_version__, "version": __version__,
                              "servers": [], "warnings": [], "exit_code": 0}))
        else:
            print("Hiç MCP config/server bulunamadı. --config ile dosya göster.")
        return 0
    return run(servers, args.timeout, args.json, args.sarif, max(1, args.retries), args.compare)


if __name__ == "__main__":
    raise SystemExit(main())
