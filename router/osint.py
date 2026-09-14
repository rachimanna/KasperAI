"""
OSINT / «глаз»-style lookup for KasperAI.
Источники:
  1) локальный дамп data/osint_dump.db (ты загружаешь сам)
  2) публичная страница t.me/username
  3) эвристики по телефону (страна, форматы)
  4) готовые поисковые ссылки (Google/Yandex/DuckDuckGo dorks)
  5) опционально: Numverify / Abstract API если ключи в env

Формат отчёта: ФИО, телефон, адрес, город, паспорт, соцсети — из дампа + публичное.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import aiohttp

LOCAL_DUMP_PATH = Path("data/osint_dump.db")

PHONE_RE = re.compile(r"^\+?\d{10,15}$")
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")
ID_RE = re.compile(r"^\d{5,15}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FIO_RE = re.compile(r"^[А-Яа-яA-Za-zёЁ\-]+(?:\s+[А-Яа-яA-Za-zёЁ\-]+){1,4}$")


def normalize_query(raw: str) -> Dict[str, str]:
    q = (raw or "").strip()
    if not q:
        return {"type": "empty", "value": ""}
    if q.startswith("@"):
        return {"type": "username", "value": q.lstrip("@").lower()}
    if EMAIL_RE.match(q):
        return {"type": "email", "value": q.lower()}
    cleaned = q.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if PHONE_RE.match(cleaned):
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
    if FIO_RE.match(q):
        return {"type": "fio", "value": " ".join(q.split())}
    return {"type": "unknown", "value": q}


async def _http_get(
    session: aiohttp.ClientSession, url: str, **kwargs
) -> Optional[str]:
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=15), **kwargs
        ) as r:
            if r.status == 200:
                return await r.text()
    except Exception:
        return None
    return None


async def _http_json(
    session: aiohttp.ClientSession, url: str, **kwargs
) -> Optional[dict]:
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=15), **kwargs
        ) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except Exception:
        return None
    return None


async def lookup_tg_public(
    session: aiohttp.ClientSession, username: str
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "username": username,
        "exists": None,
        "title": None,
        "description": None,
        "photo": None,
        "link": f"https://t.me/{username}",
    }
    html = await _http_get(
        session,
        f"https://t.me/{username}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; KasperOSINT/1.0)"},
    )
    if not html:
        out["exists"] = False
        return out
    if "tgme_page_title" in html or "tgme_page_photo" in html or "tgme_page_description" in html:
        out["exists"] = True
        for key, pattern in (
            ("title", r'og:title" content="([^"]+)"'),
            ("description", r'og:description" content="([^"]+)"'),
            ("photo", r'og:image" content="([^"]+)"'),
        ):
            m = re.search(pattern, html)
            if m:
                out[key] = m.group(1)
    else:
        out["exists"] = "unknown"
    return out


def lookup_local_dump(query: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    Таблица persons — максимально «глаз бога»-поля.
    Загружаешь свой дамп, бот отдаёт всё что нашёл.
    """
    if not LOCAL_DUMP_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(LOCAL_DUMP_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows: List[sqlite3.Row] = []
        t, v = query["type"], query["value"]

        if t == "phone":
            phone = v.lstrip("+")
            cur.execute(
                """
                SELECT * FROM persons
                WHERE phone LIKE ? OR phone LIKE ? OR phone LIKE ?
                LIMIT 30
                """,
                (f"%{phone}%", f"%{phone[-10:]}%", f"%{phone[-11:]}%"),
            )
            rows = cur.fetchall()
        elif t == "username":
            cur.execute(
                "SELECT * FROM persons WHERE lower(username) = ? OR lower(username) LIKE ? LIMIT 30",
                (v.lower(), f"%{v.lower()}%"),
            )
            rows = cur.fetchall()
        elif t == "telegram_id":
            cur.execute(
                "SELECT * FROM persons WHERE telegram_id = ? OR telegram_id LIKE ? LIMIT 30",
                (v, f"%{v}%"),
            )
            rows = cur.fetchall()
        elif t == "email":
            cur.execute(
                "SELECT * FROM persons WHERE lower(email) = ? OR lower(email) LIKE ? LIMIT 30",
                (v.lower(), f"%{v.lower()}%"),
            )
            rows = cur.fetchall()
        elif t == "fio":
            parts = v.split()
            like = "%" + "%".join(parts) + "%"
            cur.execute(
                """
                SELECT * FROM persons
                WHERE full_name LIKE ? OR full_name LIKE ?
                LIMIT 30
                """,
                (like, f"%{parts[0]}%{parts[-1]}%" if len(parts) > 1 else like),
            )
            rows = cur.fetchall()
        else:
            cur.execute(
                """
                SELECT * FROM persons
                WHERE full_name LIKE ? OR phone LIKE ? OR username LIKE ?
                   OR address LIKE ? OR notes LIKE ?
                LIMIT 20
                """,
                (f"%{v}%", f"%{v}%", f"%{v}%", f"%{v}%", f"%{v}%"),
            )
            rows = cur.fetchall()

        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def phone_formats(phone: str) -> List[str]:
    digits = re.sub(r"\D", "", phone)
    variants = {digits, phone}
    if digits.startswith("7") and len(digits) == 11:
        variants.add("8" + digits[1:])
        variants.add("+7 (" + digits[1:4] + ") " + digits[4:7] + "-" + digits[7:9] + "-" + digits[9:])
        variants.add("+7" + digits[1:])
        variants.add(digits[1:])
    return sorted(variants)


def country_from_phone(phone: str) -> str:
    d = re.sub(r"\D", "", phone)
    if d.startswith("7"):
        return "RU / KZ"
    if d.startswith("375"):
        return "BY"
    if d.startswith("380"):
        return "UA"
    if d.startswith("998"):
        return "UZ"
    if d.startswith("996"):
        return "KG"
    if d.startswith("1"):
        return "US/CA"
    return "?"


def build_dorks(query: Dict[str, str]) -> List[str]:
    v = query["value"]
    t = query["type"]
    links = []
    if t == "phone":
        for fmt in phone_formats(v)[:4]:
            q = quote_plus(fmt)
            links.append(f"Google: https://www.google.com/search?q={q}")
            links.append(f"Yandex: https://yandex.ru/search/?text={q}")
        links.append(f"DuckDuckGo: https://duckduckgo.com/?q={quote_plus(v)}")
    elif t == "username":
        q = quote_plus(f'"{v}" OR "@{v}" telegram OR vk OR instagram')
        links.append(f"Google: https://www.google.com/search?q={q}")
        links.append(f"Yandex: https://yandex.ru/search/?text={q}")
        links.append(f"t.me: https://t.me/{v}")
    elif t == "fio":
        q = quote_plus(v)
        links.append(f"Google: https://www.google.com/search?q={q}")
        links.append(f"Yandex: https://yandex.ru/search/?text={q}")
        links.append(f"VK: https://vk.com/search?c%5Bq%5D={q}&c%5Bsection%5D=people")
    elif t == "email":
        q = quote_plus(v)
        links.append(f"Google: https://www.google.com/search?q={q}")
        links.append(f"Hunter-style dork: https://www.google.com/search?q={quote_plus(v + ' site:pastebin.com OR site:breach')}")
    elif t == "telegram_id":
        links.append(f"tg://user?id={v}")
    return links


async def lookup_phone_api(session: aiohttp.ClientSession, phone: str) -> Dict[str, Any]:
    """Опционально: NUMVERIFY_API_KEY или ABSTRACT_API_KEY в env."""
    out: Dict[str, Any] = {}
    numverify = os.getenv("NUMVERIFY_API_KEY", "").strip()
    if numverify:
        data = await _http_json(
            session,
            f"http://apilayer.net/api/validate?access_key={numverify}&number={phone.lstrip('+')}&format=1",
        )
        if data and data.get("valid") is not None:
            out["numverify"] = {
                "valid": data.get("valid"),
                "country": data.get("country_name"),
                "location": data.get("location"),
                "carrier": data.get("carrier"),
                "line_type": data.get("line_type"),
            }
    abstract = os.getenv("ABSTRACT_API_KEY", "").strip()
    if abstract:
        data = await _http_json(
            session,
            f"https://phonevalidation.abstractapi.com/v1/?api_key={abstract}&phone={phone}",
        )
        if data:
            out["abstract"] = {
                "valid": data.get("valid"),
                "country": (data.get("country") or {}).get("name"),
                "location": data.get("location"),
                "carrier": (data.get("carrier") or ""),
                "type": data.get("type"),
            }
    return out


def format_person_row(row: Dict[str, Any], idx: int) -> str:
    if "error" in row:
        return f"  ошибка БД: {row['error']}"
    # приоритет полей «как глаз бога»
    order = [
        ("full_name", "ФИО"),
        ("phone", "Телефон"),
        ("telegram_id", "TG ID"),
        ("username", "Username"),
        ("email", "Email"),
        ("passport", "Паспорт"),
        ("birth_date", "Дата рождения"),
        ("address", "Адрес"),
        ("city", "Город"),
        ("region", "Регион"),
        ("inn", "ИНН"),
        ("snils", "СНИЛС"),
        ("auto", "Авто"),
        ("relatives", "Родственники"),
        ("socials", "Соцсети"),
        ("notes", "Заметки"),
    ]
    parts = []
    used = set()
    for key, label in order:
        val = row.get(key)
        if val is not None and str(val).strip():
            parts.append(f"  <b>{label}:</b> <code>{val}</code>")
            used.add(key)
    for k, v in row.items():
        if k in used or k == "id":
            continue
        if v is not None and str(v).strip():
            parts.append(f"  <b>{k}:</b> <code>{v}</code>")
    if not parts:
        return f"{idx}. (пустая запись)"
    return f"{idx}.\n" + "\n".join(parts)


async def run_osint(raw_query: str) -> str:
    q = normalize_query(raw_query)
    if q["type"] == "empty":
        return (
            "Пустой запрос.\n"
            "Пришли: <code>@username</code> / <code>+79001234567</code> / "
            "<code>telegram_id</code> / email / ФИО"
        )

    lines: List[str] = []
    lines.append(
        f"👁 <b>OSINT</b> · тип: <code>{q['type']}</code> · запрос: <code>{q['value']}</code>\n"
    )

    # 1) Локальный дамп — главный источник «фио/адрес/город/паспорт»
    local = lookup_local_dump(q)
    if local:
        lines.append(f"<b>📦 Локальный дамп</b> · найдено: {len(local)}")
        for i, row in enumerate(local, 1):
            lines.append(format_person_row(row, i))
        lines.append("")
    else:
        lines.append(
            "📦 Локальный дамп: <code>data/osint_dump.db</code> пуст или нет совпадений.\n"
            "Залей свою базу — появятся ФИО, адреса, паспорта и т.д.\n"
        )

    async with aiohttp.ClientSession() as session:
        # 2) Telegram public
        if q["type"] == "username":
            tg = await lookup_tg_public(session, q["value"])
            lines.append("<b>📡 Telegram public</b>")
            lines.append(f"  exists: <code>{tg.get('exists')}</code>")
            if tg.get("title"):
                lines.append(f"  title: {tg['title']}")
            if tg.get("description"):
                lines.append(f"  about: {tg['description'][:400]}")
            if tg.get("photo"):
                lines.append(f"  photo: {tg['photo']}")
            lines.append(f"  link: {tg['link']}")
            lines.append("")

        # 3) Phone
        if q["type"] == "phone":
            lines.append("<b>📞 Телефон</b>")
            lines.append(f"  normalized: <code>{q['value']}</code>")
            lines.append(f"  country: <code>{country_from_phone(q['value'])}</code>")
            lines.append("  formats: " + ", ".join(f"<code>{f}</code>" for f in phone_formats(q["value"])[:5]))
            api = await lookup_phone_api(session, q["value"])
            for name, data in api.items():
                lines.append(f"  <b>{name}</b>:")
                for k, v in data.items():
                    if v is not None and str(v).strip():
                        lines.append(f"    {k}: <code>{v}</code>")
            lines.append("")

        if q["type"] == "telegram_id":
            lines.append("<b>🆔 Telegram ID</b>")
            lines.append(f"  id: <code>{q['value']}</code>")
            lines.append("  deep-link: tg://user?id=" + q["value"])
            lines.append("")

        if q["type"] == "email":
            lines.append("<b>📧 Email</b>")
            lines.append(f"  value: <code>{q['value']}</code>")
            lines.append("")

        if q["type"] == "fio":
            lines.append("<b>👤 ФИО</b>")
            lines.append(f"  query: <code>{q['value']}</code>")
            lines.append("")

    # 4) Dorks — реальный поиск в открытом вебе
    dorks = build_dorks(q)
    if dorks:
        lines.append("<b>🔗 Открытый поиск (dorks)</b>")
        for link in dorks[:12]:
            lines.append(f"  {link}")
        lines.append("")

    lines.append("— конец отчёта —")
    return "\n".join(lines)


# Схема дампа под «глаз бога»-поля
CREATE_DUMP_SQL = """
CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT,
    telegram_id TEXT,
    username TEXT,
    email TEXT,
    full_name TEXT,
    birth_date TEXT,
    passport TEXT,
    inn TEXT,
    snils TEXT,
    address TEXT,
    city TEXT,
    region TEXT,
    auto TEXT,
    relatives TEXT,
    socials TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_persons_phone ON persons(phone);
CREATE INDEX IF NOT EXISTS idx_persons_username ON persons(username);
CREATE INDEX IF NOT EXISTS idx_persons_tg ON persons(telegram_id);
CREATE INDEX IF NOT EXISTS idx_persons_email ON persons(email);
CREATE INDEX IF NOT EXISTS idx_persons_fio ON persons(full_name);
"""
