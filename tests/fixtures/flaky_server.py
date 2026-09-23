#!/usr/bin/env python3
"""Crashes on the first spawn, answers correctly on later spawns (simulates a flaky server).
Attempt tracking via a marker file passed as argv[1]."""
import json
import os
import sys

marker = sys.argv[1]
attempt = 0
if os.path.exists(marker):
    attempt = int(open(marker).read().strip() or "0")
attempt += 1
open(marker, "w").write(str(attempt))

if attempt == 1:
    sys.stderr.write("simulated startup race\n")
    sys.exit(1)

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
            "serverInfo": {"name": "fixture-flaky", "version": "0"},
        }}), flush=True)
    elif method == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}],
        }}), flush=True)
