#!/usr/bin/env python3
"""Minimal compliant MCP server for tests: reads JSON-RPC lines, answers them."""
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture-good", "version": "0"},
        }}), flush=True)
    elif method == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}},
                      {"name": "add", "description": "add", "inputSchema": {"type": "object"}}],
        }}), flush=True)
