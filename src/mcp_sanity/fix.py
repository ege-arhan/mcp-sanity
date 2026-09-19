"""Fix suggestions: map probe status to a concrete next action."""
from __future__ import annotations

import os
import re
from pathlib import Path


def env_keys_with_unresolved(server_env: dict[str, str]) -> list[str]:
    bad = []
    for k, v in server_env.items():
        # $ {VAR} not expanded (variable unset in current shell)
        if re.search(r"\$\{?\w", str(v)):
            bad.append(k)
    return sorted(bad)


def fix_hint(server, result):
    st = result.status
    if st == "OK":
        return None
    if st == "MISSING_BIN":
        cmd = server.command or ""
        if cmd in ("npx", "bunx"):
            return "Node.js eksik veya PATH'te değil: https://nodejs.org veya `brew install node`"
        if cmd in ("uvx", "uv"):
            return "uv eksik: `curl -LsSf https://astral.sh/uv/install.sh | sh`"
        if cmd in ("python", "python3"):
            return "Python 3 PATH'te değil: sistemi kur veya config'deki 'python' yerine tam yol ver"
        return f"'{cmd}' PATH'te yok. Kur ya da config'de tam yol yaz: $(which {cmd})"
    if st == "NOT_EXECUTABLE":
        return f"chmod +x '{server.command}' (dosya var ama çalıştırılamıyor)"
    if st == "PROCESS_EXIT":
        tail = result.detail
        if "ModuleNotFoundError" in tail:
            mod = tail.split("ModuleNotFoundError")[-1][:60]
            return f"Python modülü eksik:{mod} — server'ın bağımlılıklarını kur (requirements.txt / uv sync)"
        return "Sunucu hemen ölüyor. Elle dene: `" + " ".join(filter(None, [server.command, *server.args])) + "`"
    if st == "HANDSHAKE_TIMEOUT":
        return ("Sunucu başladı ama initialize'a cevap yok. stdio yerine HTTP/SSE ile mi sunulmalı kontrol et; "
                "ya da `--timeout 30` ile tekrar dene")
    if st == "BAD_JSON":
        return "Sunucu stdout'a JSON dışı yazıyor (print/log sızıntısı). Sunucu kodunu düzelt: stdout SADECE JSON-RPC"
    if st == "EMPTY_RESPONSE":
        return "Sunucu stdout'u cevap vermeden kapattı. Komut argümanlarını kontrol et: `" + " ".join(filter(None, [server.command, *server.args])) + "`"
    if st == "TOOL_ERROR":
        return f"Sunucu initialize'ı geçti ama tools/list reddetti: {result.detail[:120]}"
    return None


def config_warnings(servers) -> list[str]:
    out = []
    seen: dict[tuple, str] = {}
    for s in servers:
        unresolved = env_keys_with_unresolved(s.env)
        if unresolved:
            out.append(
                f"{s.client}/{s.name}: env {', '.join('${' + k + '}' for k in unresolved)} "
                f"çözümlenemedi — kabukta değişken tanımlı değil, config'e yaz ya da export et"
            )
        for k, v in s.env.items():
            if re.match(r"^(api[-_]?key|token|secret|password)", k, re.I) and v and not re.search(r"\$\{?\w", v):
                out.append(
                    f"{s.client}/{s.name}: env {k} muhtemelen GERÇEK bir sır (düz metin). "
                    f"{Path(s.config).name} yedeğini alıp temizle; sızdıysa ROTATE et"
                )
        key = (s.client, s.command, tuple(s.args))
        if s.command:
            if key in seen:
                out.append(f"{s.client}/{s.name}: '{seen[key]}' ile aynı komut+argüman (yarı-molas duplicate)")
            else:
                seen[key] = s.name
        if s.url and not s.command:
            try:
                p = Path(os.path.expanduser(s.url.replace("file://", "")))
                if str(p).startswith("/") and not p.exists():
                    out.append(f"{s.client}/{s.name}: url bir dosya yolu var sayılıyor ama yok: {s.url}")
            except Exception:
                pass
    return out
