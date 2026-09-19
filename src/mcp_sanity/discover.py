"""Find MCP client config files and normalize their server entries."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Server:
    name: str
    config: str
    client: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None


def _expand(v: object) -> str:
    return os.path.expandvars(str(v))


def load_json_mcp(path: Path, client: str) -> list[Server]:
    raw = json.loads(path.read_text())
    out = []
    for name, s in (raw.get("mcpServers") or {}).items():
        if not isinstance(s, dict):
            out.append(Server(name, str(path), client))
            continue
        out.append(
            Server(
                name,
                str(path),
                client,
                command=s.get("command"),
                args=[str(a) for a in s.get("args") or []],
                env={k: _expand(v) for k, v in (s.get("env") or {}).items()},
                url=s.get("url"),
            )
        )
    return out


def load_opencode(path: Path) -> list[Server]:
    # opencode keeps "command" as a single shell-ish string: "npx -y @modelcontextprotocol/server-brave-search"
    raw = json.loads(path.read_text())
    out = []
    for name, s in (raw.get("mcp") or {}).items():
        if not isinstance(s, dict):
            continue
        cmd = s.get("command")
        if isinstance(cmd, str):
            parts = cmd.split()
            out.append(Server(name, str(path), "opencode",
                              command=parts[0] if parts else None, args=parts[1:]))
        elif isinstance(cmd, list) and cmd:
            out.append(Server(name, str(path), "opencode",
                              command=str(cmd[0]), args=[str(x) for x in cmd[1:]]))
    return out


def load_codex(path: Path) -> list[Server]:
    raw = tomllib.loads(path.read_text())
    out = []
    for name, s in (raw.get("mcp_servers") or {}).items():
        if not isinstance(s, dict):
            continue
        out.append(
            Server(
                name,
                str(path),
                "codex",
                command=s.get("command"),
                args=[str(a) for a in s.get("args") or []],
                env={k: _expand(v) for k, v in (s.get("env") or {}).items()},
                url=s.get("url"),
            )
        )
    return out


def candidate_configs() -> list[tuple[Path, str, str]]:
    """(path, client, kind) for every MCP client config that exists here."""
    home = Path.home()
    cands = [
        (home / ".cursor" / "mcp.json", "cursor", "json"),
        (home / ".codex" / "config.toml", "codex", "toml"),
        (home / ".config" / "Claude" / "claude_desktop_config.json", "claude-desktop", "json"),
        (home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json", "claude-desktop", "json"),
        (home / ".config" / "opencode" / "opencode.json", "opencode", "opencode"),
        (Path.cwd() / ".mcp.json", "claude-code", "json"),
    ]
    return [(p, c, k) for p, c, k in cands if p.is_file()]


LOADERS = {
    "json": lambda p, c: load_json_mcp(p, c),
    "toml": lambda p, c: load_codex(p),
    "opencode": lambda p, c: load_opencode(p),
}


def load_servers(path: Path, client: str, kind: str) -> list[Server]:
    return LOADERS[kind](path, client)
