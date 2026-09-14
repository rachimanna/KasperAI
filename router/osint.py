"""
OSINT lookup for Telegram username / phone / user id.
Public sources + local dump placeholder (MVD-style table if you load one).
No paid APIs required for basic pass.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

# Optional local dump: put a sqlite with table `persons`
# columns: phone, telegram_id, username, full_name, passport, address, region, notes
LOCAL_DUMP_PATH = Path("data/osint_dump.db")

PHONE_RE = re.compile(r"^\+?\d{10,15}$")
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")
ID_RE = re.compile(r"^\d{5,15}$")


def normalize_query(raw: str) -> Dict[str, str]:
    q = (raw or "").strip()
    if not q:
        return {"type": "empty", "value": ""}
    if q.startswith("@"):
        return {"type": "username", "value": q.lstrip("@").lower()}
    if PHONE_RE.match(q.replace(" ", "").replace("-", "")):
        digits = re.sub(r"\D", "", q)
        if digits.startswith("8") and len(digits) == 11:
            digits = "7" + digits[1:]
        if not digits.startswith("+"):
            digits = "+" + digits
        return {"type": "phone", "value": digits}
    if ID_RE.match(q):
        return {"type": "telegram_id", "value": q}
    if USERNAME_RE.match(q):
        return {"type": "username", "value": q.lstrip("@").lower()}
    return {"type": "unknown", "value": q}


async def _http_get(session: aiohttp.ClientSession, url: str, **kwargs) -> Optional[str]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12), **kwargs) as r:
            if r.status == 200:
                return await r.text()
    except Exception:
        return None
    return None


async def lookup_tg_public(session: aiohttp.ClientSession, username: str) -> Dict[str, Any]:
    """Public Telegram web preview + basic existence check."""
    out: Dict[str, Any] = {
        "username": username,
        "exists": None,
        "title": None,
        "description": None,
        "photo": None,
    }
    url = f"https://t.me/{username}"
    html = await _http_get(session, url, headers={"User-Agent": "Mozilla/5.0"})
    if not html:
        out["exists"] = False
        return out
    if "tgme_page_title" in html or "tgme_page_photo" in html:
        out["exists"] = True
        m = re.search(r'og:title" content="([^"]+)"', html)
        if m:
            out["title"] = m.group(1)
        m = re.search(r'og:description" content="([^"]+)"', html)
        if m:
            out["description"] = m.group(1)
        m = re.search(r'og:image" content="([^"]+)"', html)
        if m:
            out["photo"] = m.group(1)
    else:
        out["exists"] = "unknown"
    return out


def lookup_local_dump(query: Dict[str, str]) -> List[Dict[str, Any]]:
    """Search local MVD-style dump if file present."""
    if not LOCAL_DUMP_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(LOCAL_DUMP_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows: List[sqlite3.Row] = []
        if query["type"] == "phone":
            phone = query["value"].lstrip("+")
            cur.execute(
                "SELECT * FROM persons WHERE phone LIKE ? OR phone LIKE ? LIMIT 20",
                (f"%{phone}%", f"%{phone[-10:]}%"),
            )
            rows = cur.fetchall()
        elif query["type"] == "username":
            cur.execute(
                "SELECT * FROM persons WHERE lower(username) = ? LIMIT 20",
                (query["value"].lower(),),
            )
            rows = cur.fetchall()
        elif query["type"] == "telegram_id":
            cur.execute(
                "SELECT * FROM persons WHERE telegram_id = ? LIMIT 20",
                (query["value"],),
            )
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


async def lookup_phone_hint(session: aiohttp.ClientSession, phone: str) -> Dict[str, Any]:
    """Light public hints (country/carrier style)."""
    digits = re.sub(r"\D", "", phone)
    out = {
        "phone": phone,
        "country_hint": None,
        "note": "публичный lookup без платных API",
    }
    if digits.startswith("7") or digits.startswith("8"):
        out["country_hint"] = "RU / KZ"
    elif digits.startswith("375"):
        out["country_hint"] = "BY"
    elif digits.startswith("380"):
        out["country_hint"] = "UA"
    return out


async def run_osint(raw_query: str) -> str:
    q = normalize_query(raw_query)
    if q["type"] == "empty":
        return "Пустой запрос. Пришли @username, номер телефона или telegram id."

    lines: List[str] = []
    lines.append(
        f"🔍 <b>OSINT</b> · тип: <code>{q['type']}</code> · запрос: <code>{q['value']}</code>\n"
    )

    local = lookup_local_dump(q)
    if local:
        lines.append("<b>📦 Локальный дамп (MVD-style)</b>")
        for i, row in enumerate(local, 1):
            if "error" in row:
                lines.append(f"  ошибка БД: {row['error']}")
                continue
            parts = []
            for k, v in row.items():
                if v is not None and str(v).strip():
                    parts.append(f"{k}: <code>{v}</code>")
            lines.append(f"{i}. " + " | ".join(parts))
        lines.append("")
    else:
        lines.append(
            "📦 Локальный дамп: файл <code>data/osint_dump.db</code> не найден или пусто.\n"
        )

    async with aiohttp.ClientSession() as session:
        if q["type"] == "username":
            tg = await lookup_tg_public(session, q["value"])
            lines.append("<b>📡 Telegram public</b>")
            lines.append(f"  exists: <code>{tg.get('exists')}</code>")
            if tg.get("title"):
                lines.append(f"  title: {tg['title']}")
            if tg.get("description"):
                lines.append(f"  about: {tg['description'][:300]}")
            if tg.get("photo"):
                lines.append(f"  photo: {tg['photo']}")
            lines.append(f"  link: https://t.me/{q['value']}")
        elif q["type"] == "phone":
            hint = await lookup_phone_hint(session, q["value"])
            lines.append("<b>📞 Phone hint</b>")
            lines.append(f"  normalized: <code>{hint['phone']}</code>")
            lines.append(f"  country: <code>{hint.get('country_hint') or '?'}</code>")
            lines.append(f"  note: {hint.get('note')}")
        elif q["type"] == "telegram_id":
            lines.append("<b>🆔 Telegram ID</b>")
            lines.append(f"  id: <code>{q['value']}</code>")
            lines.append(
                "  (публично id → username без bot API/getChat не резолвится)"
            )
        else:
            lines.append("Не разобрал тип. Пришли @user, +79… или числовой id.")

    lines.append("\n— конец отчёта —")
    return "\n".join(lines)


# Schema helper for local dump (run once if you load a dump)
CREATE_DUMP_SQL = """
CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT,
    telegram_id TEXT,
    username TEXT,
    full_name TEXT,
    passport TEXT,
    address TEXT,
    region TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_persons_phone ON persons(phone);
CREATE INDEX IF NOT EXISTS idx_persons_username ON persons(username);
CREATE INDEX IF NOT EXISTS idx_persons_tg ON persons(telegram_id);
"""
