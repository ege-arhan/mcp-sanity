"""Fix suggestions: map probe status to a concrete next action."""
from __future__ import annotations

import os
import re
from pathlib import Path

_SECRET_KEY_RE = re.compile(
    r"^(api[-_]?key|token|secret|passwd|password|auth|bearer|private[-_]?key)", re.I)
_KNOWN_SECRET_RES = [
    ("OpenAI anahtari", re.compile(r"sk-(proj-)?[A-Za-z0-9-_]{20,}")),
    ("Anthropic anahtari", re.compile(r"sk-ant-[A-Za-z0-9-_]{10,}")),
    ("GitHub token", re.compile(r"(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{10,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Slack token", re.compile(r"xox[bpars]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z\-_]{20,}")),
]
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/=]{20,}")
_PLACEHOLDER_RE = re.compile(
    r"^(?:\*+|x{4,}|your[ _-].*|.*placeholder.*|changeme|example|test123?|todo|tbd|<.*>)$",
    re.I)
_ARG_SECRET_FLAG_RE = re.compile(r"(?i)^--?(token|api[-_]?key|password|secret|auth|bearer)(=|$)")


def _unresolved(v: object) -> bool:
    return bool(re.search(r"\$\{?\w", str(v)))


def _secret_label(v: str) -> str | None:
    for label, rx in _KNOWN_SECRET_RES:
        if rx.search(v):
            return label
    if _BEARER_RE.search(v):
        return "bearer token"
    return None


def env_keys_with_unresolved(server_env: dict[str, str]) -> list[str]:
    bad = []
    for k, v in server_env.items():
        # $ {VAR} not expanded (variable unset in current shell)
        if _unresolved(v):
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
    if st == "HTTP_UNREACHABLE":
        return ("Uzak sunucuya ulaşılamıyor (bağlantı reddi/DNS/TLS). "
                "Sunucunun çalıştığını, port/host ve VPN'i kontrol et")
    if st == "HTTP_STATUS":
        d = result.detail
        code = d.split(";", 1)[0].strip()
        if "401" in code or "403" in code:
            return (f"{code}: auth gerekli. Config'e Authorization header/token ekle "
                    f"(Bearer anahtar eksik olabilir); detay: {d[:120]}")
        if "404" in code:
            return ("404: path yanlış. Streamable HTTP endpoint genelde /mcp ile biter "
                    "(örn. https://host/mcp); url'yi düzelt")
        return f"{code}: uzak sunucu HTTP hatası döndü; gövde: {d[:150]}"
    if st == "HTTP_TIMEOUT":
        return ("Uzak sunucu zamanında cevap vermedi. /mcp endpoint'ini ve "
                "`--timeout 30` ile tekrar dene")
    if st == "HTTP_BAD_JSONRPC":
        return ("HTTP 200 ama gövde JSON-RPC değil. Endpoint MCP konuşmuyor olabilir; "
                f"url'yi kontrol et: {result.detail[:120]}")
    if st == "FLAKY":
        return ("Arada bir ölüyor: aynı komutu tekrar denemek yerine kaynağı bul — "
                "stderr'deki crash stack'ine bak; race/OOM/eksik bağımlılık şüphelisi. "
                "Detay: " + result.detail[:160])
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
        for k, v in (s.env or {}).items():
            val = "" if v is None else str(v)
            if _unresolved(val):
                continue  # yukaridaki cozümlenemedi uyarisi yeterli
            if val == "":
                out.append(
                    f"{s.client}/{s.name}: env {k} boş — değer gir ya da export et"
                )
                continue
            if _PLACEHOLDER_RE.match(val.strip()):
                out.append(
                    f"{s.client}/{s.name}: env {k} placeholder görünüyor — gerçek değeri yaz ya da export et"
                )
                continue
            label = _secret_label(val)
            if label:
                out.append(
                    f"{s.client}/{s.name}: env {k} GERÇEK bir sır gibi görünüyor ({label}). "
                    f"{Path(s.config).name} dosyasına düz metin koyma, env'den ver; sızdıysa ROTATE et"
                )
            elif _SECRET_KEY_RE.match(k) and val:
                out.append(
                    f"{s.client}/{s.name}: env {k} muhtemelen GERÇEK bir sır (düz metin). "
                    f"{Path(s.config).name} yedeğini alıp temizle; sızdıysa ROTATE et"
                )
        for i, a in enumerate(s.args or []):
            astr = str(a)
            if _ARG_SECRET_FLAG_RE.match(astr) and ("=" in astr or (i + 1 < len(s.args) and str(s.args[i + 1]) and not str(s.args[i + 1]).startswith("-"))):
                out.append(
                    f"{s.client}/{s.name}: args içinde açık secret ('{astr.split('=')[0]}') — "
                    f"komut satırı history'ye düşer, env'ye taşı"
                )
                break
            if _secret_label(astr):
                out.append(
                    f"{s.client}/{s.name}: args içinde açık secret değeri var — env'ye taşı, config'de düz metin bırakma"
                )
                break
        if s.url:
            u = str(s.url)
            if re.search(r"://[^/\s:]+:[^/\s@]+@", u):
                out.append(
                    f"{s.client}/{s.name}: url içinde gömülü kullanıcı/şifre var — url'yi temizle, kimlik bilgisini header/env'ye taşı"
                )
            elif re.search(r"(?i)[?&](api[_-]?key|token|secret|access_token|auth)=[^&\s]+", u):
                out.append(
                    f"{s.client}/{s.name}: url içinde query'de açık secret var — token'ı header/env'ye taşı"
                )
            elif not s.command:
                try:
                    p = Path(os.path.expanduser(u.replace("file://", "")))
                    if str(p).startswith("/") and not p.exists():
                        out.append(f"{s.client}/{s.name}: url bir dosya yolu var sayılıyor ama yok: {s.url}")
                except Exception:
                    pass
        key = (s.client, s.command, tuple(s.args))
        if s.command:
            if key in seen:
                out.append(f"{s.client}/{s.name}: '{seen[key]}' ile aynı komut+argüman (yarı-molas duplicate)")
            else:
                seen[key] = s.name
    return out
