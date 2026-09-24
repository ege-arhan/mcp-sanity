#!/usr/bin/env bash
# mcp-sanity demo (G7): 60 saniyelik asciinema/VHS senaryosu.
# Kullanim:
#   bash demo/run.sh            # canli cikti
#   bash demo/run.sh --record demo/demo.cast   # typescript kaydi (`script` ile, stdlib disi bagimlilik yok)
# Kayit sonrasi asciinema'ya yukle: asciinema upload demo/demo.cast
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RECORD="${2:-}"
PYBIN="$(command -v python3)"
FIXDIR="$ROOT/tests/fixtures"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
sed -e "s|PYBIN|$PYBIN|g" -e "s|FIXDIR|$FIXDIR|g" "$ROOT/demo/mcp.json.tpl" > "$TMP/mcp.json"

run_demo() {
  echo '$ mcp-sanity --config demo/mcp.json'
  PYTHONPATH="$ROOT/src" python3 -m mcp_sanity --config "$TMP/mcp.json" --timeout 3 || true
  echo
  echo '$ mcp-sanity --config demo/mcp.json --only "good-notes" --json'
  PYTHONPATH="$ROOT/src" python3 -m mcp_sanity --config "$TMP/mcp.json" --timeout 3 --only "good-notes" --json || true
}

if [ "${1:-}" = "--record" ]; then
  OUT="${RECORD:-demo/demo.cast}"
  script -qec "bash $0" "$TMP/typescript"
  python3 - "$TMP/typescript" "$ROOT/$OUT" <<'EOF'
import json, sys, time
src, dst = sys.argv[1], sys.argv[2]
data = open(src, 'rb').read().decode('utf-8', 'replace')
cast = {"version": 2, "width": 100, "height": 30,
        "timestamp": int(time.time()), "env": {"TERM": "xterm-256color"}}
lines = ["[cast-header " + json.dumps(cast) + "]"]
lines += ["[0.5, \"o\", " + json.dumps(data) + "]"]
open(dst, 'w').write("\n".join(lines) + "\n")
print("wrote", dst)
EOF
else
  run_demo
fi
