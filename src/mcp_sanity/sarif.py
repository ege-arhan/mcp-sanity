"""SARIF 2.1.0 output for CI code scanning. Stdlib only."""
from __future__ import annotations

from . import __version__

SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

SHORT = {
    "MISSING_BIN": "Server command not found on PATH",
    "NOT_EXECUTABLE": "Server file is not executable",
    "PROCESS_EXIT": "Server process exited before handshake",
    "HANDSHAKE_TIMEOUT": "Server did not answer MCP initialize",
    "BAD_JSON": "Server wrote non-JSON to stdout",
    "EMPTY_RESPONSE": "Server closed stdout without responding",
    "TOOL_ERROR": "Server rejected tools/list",
    "HTTP_UNREACHABLE": "Remote MCP URL unreachable",
    "HTTP_STATUS": "Remote MCP URL returned HTTP error",
    "HTTP_TIMEOUT": "Remote MCP URL timed out",
    "HTTP_BAD_JSONRPC": "Remote URL is not speaking JSON-RPC",
    "FLAKY": "Server crashed on first attempt but passed on retry (intermittent)",
    "CONFIG_WARNING": "Suspicious config entry (env/secret/duplicate)",
}


def _level(rule: str) -> str:
    return "warning" if rule in ("TOOL_ERROR", "CONFIG_WARNING", "FLAKY") else "error"


def to_sarif(rows, warnings, version=__version__) -> dict:
    rule_ids = []
    for r in rows:
        if r["status"] != "OK" and r["status"] not in rule_ids:
            rule_ids.append(r["status"])
    if warnings and "CONFIG_WARNING" not in rule_ids:
        rule_ids.append("CONFIG_WARNING")
    rules = [
        {"id": rid, "shortDescription": {"text": SHORT.get(rid, rid)}}
        for rid in sorted(rule_ids)
    ]
    results = []
    for r in rows:
        if r["status"] == "OK":
            continue
        text = f"{r['client']}/{r['name']}: {r['status']} — {r['detail']}"
        if r.get("hint"):
            text += f" Fix: {r['hint']}"
        results.append({
            "ruleId": r["status"],
            "level": _level(r["status"]),
            "message": {"text": text},
            "locations": [{
                "physicalLocation": {"artifactLocation": {"uri": r["config"]}},
            }],
        })
    for w in warnings:
        results.append({
            "ruleId": "CONFIG_WARNING",
            "level": "warning",
            "message": {"text": w},
        })
    return {
        "$schema": SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "mcp-sanity",
                "version": version,
                "informationUri": "https://github.com/ege-arhan/mcp-sanity",
                "rules": rules,
            }},
            "results": results,
        }],
    }
