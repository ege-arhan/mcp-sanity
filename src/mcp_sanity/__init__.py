"""mcp-sanity — live doctor for MCP server configs."""
from __future__ import annotations

__version__ = "0.1.1"
__schema_version__ = "1"

from . import discover, fix, http_probe, probe
from .discover import Server, candidate_configs, load_servers
from .probe import probe_server, ProbeResult
from .http_probe import probe_url
from .cli import main

scanner = load_servers
prober = probe_server

__all__ = [
    "__version__",
    "__schema_version__",
    "discover",
    "probe",
    "http_probe",
    "fix",
    "Server",
    "candidate_configs",
    "load_servers",
    "scanner",
    "probe_server",
    "prober",
    "probe_url",
    "main",
]
