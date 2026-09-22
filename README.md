# mcp-sanity

**Live doctor for MCP server configs.** Claude Desktop, Cursor, Codex and opencode all point at MCP servers that are quietly broken: binary not on PATH, server prints logs to stdout, process dies on boot. The client says "connected" and lists zero tools. `mcp-sanity` spawns every configured server, runs the real JSON-RPC handshake, and tells you exactly which one is broken and how to fix it.

Not another static config linter — it launches the servers.

```
$ mcp-sanity

cursor
  ✔ brave-search             OK                2 tool(s)
  ✖ filesystem               MISSING_BIN       'npx' not found on PATH
      ↳ fix: Node.js missing or not on PATH: https://nodejs.org
  ✖ my-notes                 PROCESS_EXIT      server exited code 1; stderr: ModuleNotFoundError: anthropic
      ↳ fix: Python module missing — install the server's requirements
  ⏱ weather                  HANDSHAKE_TIMEOUT no initialize response within 10s
      ↳ fix: server started but never answered initialize; check if it serves HTTP/SSE instead

claude-desktop
  ✖ legacy-db                BAD_JSON          non-JSON line on stdout: 'INFO: booting...'
      ↳ fix: server leaked a log line to stdout — stdout must be JSON-RPC only

warnings
  ⚠ cursor/brave-search: env ${BRAVE_API_KEY} not resolved — export it or inline it

4 servers, 1 OK, 3 broken, 1 warning
```

## Install

```bash
pipx install mcp-sanity     # or: pip install mcp-sanity
# zero-install:
uvx mcp-sanity
```

Python ≥ 3.11, standard library only. No dependencies.

## Usage

```bash
mcp-sanity                     # auto-discover known client configs
mcp-sanity --config my.json    # check specific files (repeatable; .json or .toml)
mcp-sanity --timeout 30        # slow startup (npx cold install)
mcp-sanity --json              # machine-readable, for CI
mcp-sanity --only brave-search # tek server (isim, client/name veya glob)
mcp-sanity --only cursor/* --skip "*notes*"  # glob + atlama
```

Exit codes: `0` all healthy · `2` server can't start (missing binary / crashes) · `3` starts but speaks no MCP (timeout, non-JSON stdout) · `4` usage error. CI gate: `mcp-sanity --json && echo all good`.

### GitHub Action

```yaml
- uses: ege-arhan/mcp-sanity@v0.1.0
  with:
    config: .mcp.json
    timeout: "30"
    sarif: mcp-sanity.sarif
    upload-sarif: "true"   # needs security-events: write
```

Exit-code gate without the action: `pipx run mcp-sanity --config .mcp.json --timeout 30`.
SARIF report for code scanning: `mcp-sanity --config .mcp.json --sarif results.sarif`.

## What it detects

| Status | Meaning | Typical fix |
|---|---|---|
| `MISSING_BIN` | `command` not on PATH | install it, or use absolute path in config |
| `NOT_EXECUTABLE` | file exists, no `+x` | `chmod +x` |
| `PROCESS_EXIT` | server died before handshake | run the printed command by hand, read stderr |
| `HANDSHAKE_TIMEOUT` | alive, but `initialize` never answered | wrong transport, or needs more time (`--timeout`) |
| `BAD_JSON` | log/print noise on stdout | stdout must carry JSON-RPC only |
| `EMPTY_RESPONSE` | closed stdout without replying | check args |
| `TOOL_ERROR` | `tools/list` rejected | server-side schema bug |
| `HTTP_UNREACHABLE` | remote `url` not reachable (refused/DNS/TLS) | server down, port/host/VPN |
| `HTTP_STATUS` | non-200 from remote `url` | 401/403 auth, 404 path (usually ends `/mcp`) |
| `HTTP_TIMEOUT` | remote `url` too slow | check endpoint, `--timeout 30` |
| `HTTP_BAD_JSONRPC` | HTTP 200 but no JSON-RPC | endpoint not MCP, check url |

Config warnings: unresolved `$ENV` placeholders, plaintext-looking secrets in `env`, duplicate command+args across servers.

## Supported configs

`~/.cursor/mcp.json` · Claude Desktop `claude_desktop_config.json` (macOS + Linux paths) · `~/.codex/config.toml` `[mcp_servers]` · opencode `mcp` (string commands) · project `.mcp.json`. Anything with the `mcpServers` object shape works via `--config`. Entries with a remote `url` (Streamable HTTP, plain JSON or SSE) are probed over HTTP; stdio `command` entries are spawned locally.

## Development

```bash
python3 tests/selfcheck.py   # 8 assertion groups, no frameworks
```

MIT © Ege Arhan
