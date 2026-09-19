#!/usr/bin/env python3
"""Broken fixture: prints log noise to stdout, then never answers. -> BAD_JSON/timeout."""
import time

print("INFO: server booting nicely...", flush=True)
print("DEBUG: loaded 3 plugins", flush=True)
while True:
    time.sleep(3600)
