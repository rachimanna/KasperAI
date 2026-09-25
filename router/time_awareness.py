"""
Понимание времени для Kasper AI.

Языковые модели сами по себе НЕ знают, какое сейчас число и который час,
и плохо считают даты («сколько дней до 12 марта», «какой день недели был
12.04.1961», «который час в Токио»). Поэтому всё, что касается времени,
здесь вычисляется точно на Python, а модели передаётся уже готовыми
фактами — она только красиво формулирует ответ в характере Каспера.

Что умеет модуль:
- текущие дата/время в часовом поясе пользователя (по умолчанию BOT_TIMEZONE);
- «сколько прошло» с прошлого сообщения пользователя (вернулся через 3 дня);
- метки времени для истории переписки ([вчера 21:40]);
- время в ~100 городах мира и разница во времени между городами;
- сколько дней до даты / праздника, сколько прошло с даты, возраст;
- день недели для любой даты;
- «который час будет через 3 часа», «сколько осталось до 18:00»;
- разбор напоминаний: «напомни через 10 минут…», «напомни завтра в 9…»;
- разбор часового пояса: «Берлин», «Europe/Berlin», «+3», «UTC-5».

Никаких внешних зависимостей: только стандартная библиотека (zoneinfo).
На случай, если в системе нет базы часовых поясов, в requirements.txt
добавлен пакет tzdata.
"""

import calendar
import os
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = os.getenv("BOT_TIMEZONE", "Europe/Moscow")

WEEKDAYS = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
# винительный падеж: «в среду», «в пятницу»
WEEKDAY_STEMS = [
    r"понедельник", r"вторник", r"сред[аеуы]", r"четверг",
    r"пятниц[аеуы]", r"суббот[аеуы]", r"воскресень[ея]",
]

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
MONTH_STEMS = [
    "январ", "феврал", "март", "апрел", "ма[йяюе]", "июн",
    "июл", "август", "сентябр", "октябр", "ноябр", "декабр",
]
_MONTH_RE = "(" + "|".join(MONTH_STEMS) + r")[а-я]*"


# =====================================================================
# Базовые помощники
# =====================================================================

def get_zone(tz_name=None):
    """ZoneInfo по имени; при ошибке — пояс по умолчанию, в крайнем случае UTC."""
    for name in (tz_name, DEFAULT_TZ, "UTC"):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
    return timezone.utc


def now_in(tz_name=None):
    return datetime.now(get_zone(tz_name))


def utc_now_str():
    """Текущее время UTC в формате SQLite CURRENT_TIMESTAMP."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_db_ts(value):
    """Строка из БД (UTC, формат CURRENT_TIMESTAMP или ISO) -> aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("T", " ")
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(text[:26], fmt)
                break
            except ValueError:
                continue
        if dt is None:
            try:
                dt = datetime.fromisoformat(str(value))
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _pl(n, one, few, many):
    return f"{n} {plural(n, one, few, many)}"


def humanize_delta(seconds, parts=2):
    """3725 -> '1 час 2 минуты'. Берёт не больше `parts` крупных единиц."""
    seconds = int(abs(seconds))
    if seconds < 60:
        return _pl(seconds, "секунда", "секунды", "секунд")
    units = [
        (365 * 86400, ("год", "года", "лет")),
        (30 * 86400, ("месяц", "месяца", "месяцев")),
        (7 * 86400, ("неделя", "недели", "недель")),
        (86400, ("день", "дня", "дней")),
        (3600, ("час", "часа", "часов")),
        (60, ("минута", "минуты", "минут")),
    ]
    out = []
    for size, forms in units:
        if seconds >= size:
            value, seconds = divmod(seconds, size)
            out.append(_pl(value, *forms))
            if len(out) >= parts:
                break
    return " ".join(out)


def format_date_ru(d, with_weekday=True, with_year=True):
    s = f"{d.day} {MONTHS_GEN[d.month - 1]}"
    if with_year:
        s += f" {d.year}"
    if with_weekday:
        s = f"{WEEKDAYS[d.weekday()]}, {s}"
    return s


def format_dt_ru(dt):
    return f"{format_date_ru(dt)}, {dt:%H:%M}"


def part_of_day(hour):
    if 5 <= hour < 12:
        return "утро"
    if 12 <= hour < 17:
        return "день"
    if 17 <= hour < 23:
        return "вечер"
    return "ночь"


def utc_offset_str(dt):
    offset = dt.utcoffset() or timedelta(0)
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    h, m = divmod(abs(total), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def add_months(d, months):
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


# =====================================================================
# Города и часовые пояса
# =====================================================================

# (регулярка по основе слова, IANA-зона, как называть в ответе)
_CITIES = [
    (r"москв|мск\b", "Europe/Moscow", "Москва"),
    (r"питер|петербург|спб\b", "Europe/Moscow", "Санкт-Петербург"),
    (r"калининград", "Europe/Kaliningrad", "Калининград"),
    (r"самар", "Europe/Samara", "Самара"),
    (r"казан", "Europe/Moscow", "Казань"),
    (r"нижн\w* новгород", "Europe/Moscow", "Нижний Новгород"),
    (r"волгоград", "Europe/Volgograd", "Волгоград"),
    (r"ростов", "Europe/Moscow", "Ростов-на-Дону"),
    (r"краснодар", "Europe/Moscow", "Краснодар"),
    (r"сочи", "Europe/Moscow", "Сочи"),
    (r"махачкал", "Europe/Moscow", "Махачкала"),
    (r"грозн", "Europe/Moscow", "Грозный"),
    (r"уф[аеуы]\b", "Asia/Yekaterinburg", "Уфа"),
    (r"пермь|перми", "Asia/Yekaterinburg", "Пермь"),
    (r"екатеринбург|екб\b", "Asia/Yekaterinburg", "Екатеринбург"),
    (r"челябинск", "Asia/Yekaterinburg", "Челябинск"),
    (r"тюмен", "Asia/Yekaterinburg", "Тюмень"),
    (r"омск", "Asia/Omsk", "Омск"),
    (r"новосибирск", "Asia/Novosibirsk", "Новосибирск"),
    (r"томск", "Asia/Tomsk", "Томск"),
    (r"барнаул", "Asia/Barnaul", "Барнаул"),
    (r"кемеров", "Asia/Novokuznetsk", "Кемерово"),
    (r"красноярск", "Asia/Krasnoyarsk", "Красноярск"),
    (r"иркутск", "Asia/Irkutsk", "Иркутск"),
    (r"улан-удэ", "Asia/Irkutsk", "Улан-Удэ"),
    (r"чит[аеуы]\b", "Asia/Chita", "Чита"),
    (r"якутск", "Asia/Yakutsk", "Якутск"),
    (r"хабаровск", "Asia/Vladivostok", "Хабаровск"),
    (r"владивосток", "Asia/Vladivostok", "Владивосток"),
    (r"сахалин|южно-сахалинск", "Asia/Sakhalin", "Южно-Сахалинск"),
    (r"магадан", "Asia/Magadan", "Магадан"),
    (r"камчат", "Asia/Kamchatka", "Петропавловск-Камчатский"),
    (r"киев|київ", "Europe/Kyiv", "Киев"),
    (r"харьков", "Europe/Kyiv", "Харьков"),
    (r"одесс", "Europe/Kyiv", "Одесса"),
    (r"минск", "Europe/Minsk", "Минск"),
    (r"кишин[её]в", "Europe/Chisinau", "Кишинёв"),
    (r"рига|риге|ригу", "Europe/Riga", "Рига"),
    (r"вильнюс", "Europe/Vilnius", "Вильнюс"),
    (r"таллин", "Europe/Tallinn", "Таллин"),
    (r"алмат", "Asia/Almaty", "Алматы"),
    (r"астан", "Asia/Almaty", "Астана"),
    (r"ташкент", "Asia/Tashkent", "Ташкент"),
    (r"самарканд", "Asia/Samarkand", "Самарканд"),
    (r"бишкек", "Asia/Bishkek", "Бишкек"),
    (r"душанбе", "Asia/Dushanbe", "Душанбе"),
    (r"ашхабад", "Asia/Ashgabat", "Ашхабад"),
    (r"баку", "Asia/Baku", "Баку"),
    (r"тбилис", "Asia/Tbilisi", "Тбилиси"),
    (r"батуми", "Asia/Tbilisi", "Батуми"),
    (r"ереван", "Asia/Yerevan", "Ереван"),
    (r"стамбул", "Europe/Istanbul", "Стамбул"),
    (r"анкар", "Europe/Istanbul", "Анкара"),
    (r"анталь", "Europe/Istanbul", "Анталья"),
    (r"лондон", "Europe/London", "Лондон"),
    (r"дублин", "Europe/Dublin", "Дублин"),
    (r"лиссабон", "Europe/Lisbon", "Лиссабон"),
    (r"мадрид", "Europe/Madrid", "Мадрид"),
    (r"барселон", "Europe/Madrid", "Барселона"),
    (r"париж", "Europe/Paris", "Париж"),
    (r"брюссел", "Europe/Brussels", "Брюссель"),
    (r"амстердам", "Europe/Amsterdam", "Амстердам"),
    (r"берлин", "Europe/Berlin", "Берлин"),
    (r"мюнхен", "Europe/Berlin", "Мюнхен"),
    (r"гамбург", "Europe/Berlin", "Гамбург"),
    (r"франкфурт", "Europe/Berlin", "Франкфурт"),
    (r"вен[аеуы]\b", "Europe/Vienna", "Вена"),
    (r"цюрих", "Europe/Zurich", "Цюрих"),
    (r"женев", "Europe/Zurich", "Женева"),
    (r"рим\b|рима\b|риме\b", "Europe/Rome", "Рим"),
    (r"милан", "Europe/Rome", "Милан"),
    (r"праг", "Europe/Prague", "Прага"),
    (r"варшав", "Europe/Warsaw", "Варшава"),
    (r"будапешт", "Europe/Budapest", "Будапешт"),
    (r"белград", "Europe/Belgrade", "Белград"),
    (r"софи[яиюе]\b", "Europe/Sofia", "София"),
    (r"бухарест", "Europe/Bucharest", "Бухарест"),
    (r"афин", "Europe/Athens", "Афины"),
    (r"хельсинк", "Europe/Helsinki", "Хельсинки"),
    (r"стокгольм", "Europe/Stockholm", "Стокгольм"),
    (r"осло", "Europe/Oslo", "Осло"),
    (r"копенгаген", "Europe/Copenhagen", "Копенгаген"),
    (r"каир", "Africa/Cairo", "Каир"),
    (r"тель-авив|израил", "Asia/Jerusalem", "Тель-Авив"),
    (r"дуба[йе]", "Asia/Dubai", "Дубай"),
    (r"абу-даби", "Asia/Dubai", "Абу-Даби"),
    (r"доха|катар", "Asia/Qatar", "Доха"),
    (r"эр-рияд|рияд", "Asia/Riyadh", "Эр-Рияд"),
    (r"тегеран", "Asia/Tehran", "Тегеран"),
    (r"дели|мумба|индии|индия", "Asia/Kolkata", "Индия (Дели)"),
    (r"бангкок|таиланд|пхукет", "Asia/Bangkok", "Бангкок"),
    (r"бали\b|денпасар", "Asia/Makassar", "Бали"),
    (r"джакарт", "Asia/Jakarta", "Джакарта"),
    (r"сингапур", "Asia/Singapore", "Сингапур"),
    (r"куала-лумпур", "Asia/Kuala_Lumpur", "Куала-Лумпур"),
    (r"пекин|китае|китай|шанха|гуанчжоу", "Asia/Shanghai", "Пекин"),
    (r"гонконг", "Asia/Hong_Kong", "Гонконг"),
    (r"тайбэ|тайван", "Asia/Taipei", "Тайбэй"),
    (r"сеул|коре[ия]", "Asia/Seoul", "Сеул"),
    (r"токио|япони", "Asia/Tokyo", "Токио"),
    (r"сидне", "Australia/Sydney", "Сидней"),
    (r"мельбурн", "Australia/Melbourne", "Мельбурн"),
    (r"окленд", "Pacific/Auckland", "Окленд"),
    (r"нью-йорк|нью йорк|нью-йорке", "America/New_York", "Нью-Йорк"),
    (r"вашингтон", "America/New_York", "Вашингтон"),
    (r"майами", "America/New_York", "Майами"),
    (r"торонто", "America/Toronto", "Торонто"),
    (r"чикаго", "America/Chicago", "Чикаго"),
    (r"денвер", "America/Denver", "Денвер"),
    (r"лос-анджелес|лос анджелес|калифорни", "America/Los_Angeles", "Лос-Анджелес"),
    (r"сан-франциско", "America/Los_Angeles", "Сан-Франциско"),
    (r"ванкувер", "America/Vancouver", "Ванкувер"),
    (r"мехико|мексик", "America/Mexico_City", "Мехико"),
    (r"сан-паулу|бразили", "America/Sao_Paulo", "Сан-Паулу"),
    (r"буэнос-айрес|аргентин", "America/Argentina/Buenos_Aires", "Буэнос-Айрес"),
    (r"гаван|куб[аеуы]\b", "America/Havana", "Гавана"),
]
_CITY_RE = [(re.compile(p, re.IGNORECASE), tz, name) for p, tz, name in _CITIES]


def find_cities(text):
    """Все упомянутые города в порядке появления: [(name, tz), ...] без повторов."""
    found = []
    for rx, tz, name in _CITY_RE:
        m = rx.search(text)
        if m:
            found.append((m.start(), name, tz))
    found.sort()
    seen, out = set(), []
    for _, name, tz in found:
        if name not in seen:
            seen.add(name)
            out.append((name, tz))
    return out


_OFFSET_RE = re.compile(r"^(?:utc|gmt|мск)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def resolve_timezone(arg):
    """
    'Europe/Berlin' / 'Берлин' / '+3' / 'UTC-5' / 'мск+2' -> IANA-имя.
    Возвращает None, если распознать не удалось.
    """
    if not arg:
        return None
    arg = arg.strip()
    # IANA-имя как есть
    if "/" in arg or arg.upper() == "UTC":
        try:
            ZoneInfo(arg)
            return arg
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    low = arg.lower().replace(" ", "")
    m = _OFFSET_RE.match(low)
    if m:
        sign, hours, minutes = m.group(1), int(m.group(2)), m.group(3)
        if low.startswith("мск"):
            hours = hours + 3 if sign == "+" else 3 - hours
            sign = "+" if hours >= 0 else "-"
            hours = abs(hours)
        if minutes and minutes != "00":
            return None  # Etc/GMT не умеет получасовые пояса
        if hours > 14:
            return None
        if hours == 0:
            return "UTC"
        # В Etc/GMT знак инвертирован: UTC+3 == Etc/GMT-3
        return f"Etc/GMT{'-' if sign == '+' else '+'}{hours}"
    cities = find_cities(arg)
    if cities:
        return cities[0][1]
    return None


def describe_zone(tz_name):
    tz_name = tz_name or DEFAULT_TZ
    dt = now_in(tz_name)
    if tz_name.startswith("Etc/") or tz_name == "UTC":
        return utc_offset_str(dt)
    return f"{tz_name} ({utc_offset_str(dt)})"


# =====================================================================
# Контекст для системного промпта
# =====================================================================

def build_time_context(tz_name=None, last_user_at=None, now=None, is_first=False):
    """
    Текстовый блок для системного промпта: что сейчас за время, и сколько
    прошло с прошлого сообщения пользователя.
    """
    tz = get_zone(tz_name)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    lines = [
        "ТЕКУЩЕЕ ВРЕМЯ (точные данные с сервера — доверяй им, а не своим "
        "знаниям из обучения):",
        f"- Сейчас у пользователя: {format_dt_ru(now)} "
        f"({getattr(tz, 'key', str(tz))}, {utc_offset_str(now)}), время суток — {part_of_day(now.hour)}.",
        f"- Сегодня {now.timetuple().tm_yday}-й день {now.year} года, "
        f"неделя №{now.isocalendar()[1]}.",
    ]
    ny = date(now.year + 1, 1, 1)
    lines.append(f"- До Нового года: {_pl((ny - now.date()).days, 'день', 'дня', 'дней')}.")

    if last_user_at is not None:
        gap = (now - last_user_at.astimezone(tz)).total_seconds()
        prev_local = last_user_at.astimezone(tz)
        if gap >= 60:
            lines.append(
                f"- Предыдущее сообщение пользователь писал {humanize_delta(gap)} назад "
                f"({format_dt_ru(prev_local)})."
            )
        if gap >= 6 * 3600:
            lines.append(
                "  Это был перерыв — если уместно, учитывай его (например, "
                "не продолжай старую тему как будто она была минуту назад)."
            )
    elif is_first:
        lines.append("- Это первое сообщение пользователя в этом диалоге.")

    lines.append(
        "Правила про время: не говори, что не знаешь дату или время — они "
        "выше. Приветствуй по времени суток, если здороваешься. Слова «сегодня», "
        "«завтра», «вчера», «на выходных» считай от текущей даты пользователя. "
        "Сообщения пользователя в истории помечены временем в квадратных "
        "скобках — это служебные метки, в своих ответах их НЕ пиши."
    )
    return "\n".join(lines)


def message_time_tag(created_at, tz_name=None, now=None):
    """UTC-время сообщения из БД -> короткая метка '[вчера 21:40]'."""
    dt = parse_db_ts(created_at)
    if dt is None:
        return ""
    tz = get_zone(tz_name)
    local = dt.astimezone(tz)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    days = (now.date() - local.date()).days
    if days == 0:
        day = "сегодня"
    elif days == 1:
        day = "вчера"
    elif 0 < days < 7:
        day = WEEKDAYS_SHORT[local.weekday()]
    else:
        day = f"{local:%d.%m.%Y}" if local.year != now.year else f"{local:%d.%m}"
    return f"[{day} {local:%H:%M}]"


# =====================================================================
# Разбор чисел, дат и времени из русского текста
# =====================================================================

_NUM_WORDS = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "одного": 1, "две": 2, "два": 2,
    "двух": 2, "пару": 2, "пара": 2, "три": 3, "трёх": 3, "трех": 3,
    "четыре": 4, "четырёх": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
    "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40,
    "сорока": 40, "пятьдесят": 50, "шестьдесят": 60, "сто": 100,
}
_NUM_ALT = r"\d+(?:[.,]\d+)?|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True))

_UNIT_RE = (
    r"(?P<unit>сек(?:унд[уы]?)?|мин(?:ут[уы]?|\.)?|час(?:а|ов)?|"
    r"д(?:ень|ня|ней)|сут(?:ки|ок)|недел(?:ю|и|ь)|месяц(?:а|ев)?|год(?:а)?|лет)"
)
_DELTA_RE = re.compile(
    r"(?:через|спустя|назад|за)?\s*(?P<num>" + _NUM_ALT + r")?\s*" + _UNIT_RE + r"\b",
    re.IGNORECASE,
)
_SPECIAL_DELTAS = [
    (re.compile(r"полчаса", re.I), timedelta(minutes=30)),
    (re.compile(r"полтора\s+часа", re.I), timedelta(minutes=90)),
    (re.compile(r"полторы\s+минуты", re.I), timedelta(seconds=90)),
    (re.compile(r"четверть\s+часа", re.I), timedelta(minutes=15)),
    (re.compile(r"полдня", re.I), timedelta(hours=12)),
]


def _num(value):
    if value is None:
        return 1
    value = value.lower()
    if value in _NUM_WORDS:
        return _NUM_WORDS[value]
    return float(value.replace(",", "."))


def parse_delta(text):
    """
    'через 2 часа 30 минут' -> (timedelta, months, (start, end)).
    Месяцы/годы возвращаются отдельно (их нельзя честно выразить timedelta).
    None, если длительности в тексте нет.
    """
    total = timedelta(0)
    months = 0
    start = end = None
    for rx, delta in _SPECIAL_DELTAS:
        m = rx.search(text)
        if m:
            total += delta
            start = m.start() if start is None else min(start, m.start())
            end = m.end() if end is None else max(end, m.end())
    for m in _DELTA_RE.finditer(text):
        unit = m.group("unit").lower()
        raw_num = m.group("num")
        # «час» без числа внутри фраз вроде «который час» — не длительность
        if raw_num is None:
            before = text[max(0, m.start() - 12):m.start("unit")].lower()
            if not re.search(r"(через|спустя|за|на)\s*$", before):
                continue
        n = _num(raw_num)
        if unit.startswith("сек"):
            total += timedelta(seconds=n)
        elif unit.startswith("мин"):
            total += timedelta(minutes=n)
        elif unit.startswith("час"):
            total += timedelta(hours=n)
        elif unit.startswith(("д", "сут")):
            total += timedelta(days=n)
        elif unit.startswith("недел"):
            total += timedelta(weeks=n)
        elif unit.startswith("месяц"):
            months += int(n)
        else:  # год / лет
            months += int(n) * 12
        s = m.start("num") if raw_num else m.start("unit")
        start = s if start is None else min(start, s)
        end = m.end() if end is None else max(end, m.end())
    if total == timedelta(0) and months == 0:
        return None
    return total, months, (start, end)


_CLOCK_RE = re.compile(
    r"(?:\bв|\bк|\bдо|\bна|\bпосле|\bс)?\s*"
    r"(?P<h>[01]?\d|2[0-3])(?:[:.](?P<m>[0-5]\d)|\s*ч(?:ас(?:а|ов)?)?\.?)?"
    r"(?:\s*(?P<ampm>утра|дня|вечера|ночи))?\b",
    re.IGNORECASE,
)


def parse_clock(text):
    """
    Время суток из текста: 'в 18:30', 'к 7 утра', 'в 9 вечера', 'в полдень'.
    Возвращает ((hour, minute), (start, end)) или None.
    Голые числа без ':' принимаются, только если рядом предлог «в/к/до» или
    уточнение «утра/вечера/часов» — иначе «через 5 минут» стало бы «в 5:00».
    """
    low = text.lower()
    m = re.search(r"\bв\s+полдень\b|\bв\s+полночь\b", low)
    if m:
        return ((12, 0) if "полдень" in m.group(0) else (0, 0)), m.span()
    for m in _CLOCK_RE.finditer(low):
        h = int(m.group("h"))
        minute = int(m.group("m")) if m.group("m") else 0
        ampm = m.group("ampm")
        has_colon = m.group("m") is not None
        prefix = m.group(0).strip().split()[0] if m.group(0).strip() else ""
        has_prep = prefix in ("в", "к", "до", "на", "после", "с")
        has_hour_word = bool(re.search(r"\d\s*ч", m.group(0)))
        if not (has_colon or ampm or (has_prep and has_hour_word) or (has_prep and h <= 23 and _looks_like_clock(low, m))):
            continue
        # «в 5 минут», «на 3 дня» — это не часы
        tail = low[m.end():m.end() + 8]
        if re.match(r"\s*(мин|сек|дн|дня|дней|недел|месяц|год|лет|раз|%|руб|\$)", tail):
            continue
        # «5.10» — это дата (день.месяц), а не 5:10, если дальше нет контекста времени
        if has_colon and "." in m.group(0) and not ampm and not has_prep:
            continue
        if ampm in ("вечера", "дня") and h < 12:
            h += 12
        elif ampm == "ночи" and h == 12:
            h = 0
        elif ampm == "утра" and h == 12:
            h = 0
        return (h, minute), (m.start(), m.end())
    return None


def _looks_like_clock(low, m):
    """«в 9» без уточнений: считаем временем, если нет даты-месяца сразу после."""
    tail = low[m.end():m.end() + 12]
    return not re.match(r"\s*" + _MONTH_RE, tail)


_DATE_NUM_RE = re.compile(r"\b(?P<d>[0-3]?\d)[./](?P<m>[01]?\d)(?:[./](?P<y>\d{2,4}))?\b")
_DATE_WORD_RE = re.compile(r"\b(?P<d>[0-3]?\d)\s+" + _MONTH_RE + r"(?:\s+(?P<y>\d{4}))?", re.IGNORECASE)
_RELATIVE_DAYS = [
    (r"\bпозавчера\b", -2), (r"\bвчера\b", -1), (r"\bсегодня\b", 0),
    (r"\bпослезавтра\b", 2), (r"\bзавтра\b", 1),
]


def _month_from_stem(word):
    word = word.lower()
    for i, stem in enumerate(MONTH_STEMS):
        if re.match(stem, word):
            return i + 1
    return None


def parse_dates(text, today, prefer_past=False):
    """
    Все даты из текста: '5 октября', '05.10', '5.10.2026', 'завтра',
    'в пятницу'. Возвращает список (date, (start, end), has_year).
    Для дат без года берётся ближайшая будущая (или сегодняшняя).
    """
    out = []
    low = text.lower()
    for m in _DATE_WORD_RE.finditer(low):
        month = _month_from_stem(m.group(2))
        day = int(m.group("d"))
        year = int(m.group("y")) if m.group("y") else None
        d = _safe_date(year or today.year, month, day)
        if d is None:
            continue
        d = _pick_year(d, today, year, prefer_past, month, day)
        out.append((d, m.span(), year is not None))
    for m in _DATE_NUM_RE.finditer(low):
        if any(s <= m.start() < e for _, (s, e), _ in out):
            continue
        day, month = int(m.group("d")), int(m.group("m"))
        if not (1 <= month <= 12):
            continue
        # «18.30» — скорее время, чем дата
        after = low[m.end():m.end() + 8]
        before = low[max(0, m.start() - 3):m.start()]
        if not m.group("y") and re.match(r"\s*(утра|вечера|дня|ночи)", after):
            continue
        if not m.group("y") and "." in m.group(0) and re.search(r"\b(в|к|до)\s*$", before):
            continue  # «в 18.30» — это время
        year = m.group("y")
        if year:
            year = int(year)
            if year < 100:
                year += 2000 if year <= (today.year % 100) + 10 else 1900
        d = _safe_date(year or today.year, month, day)
        if d is None:
            continue
        d = _pick_year(d, today, year, prefer_past, month, day)
        out.append((d, m.span(), year is not None))
    for pattern, shift in _RELATIVE_DAYS:
        m = re.search(pattern, low)
        if m:
            out.append((today + timedelta(days=shift), m.span(), True))
    for i, stem in enumerate(WEEKDAY_STEMS):
        m = re.search(r"\b(?:в|во|на|к|до|этот|эту|это|следующ\w+|ближайш\w+)?\s*" + stem + r"\b", low)
        if m:
            ahead = (i - today.weekday()) % 7
            if ahead == 0:
                ahead = 7 if not re.search(r"\bсегодня\b", low) else 0
            out.append((today + timedelta(days=ahead), m.span(), True))
    out.sort(key=lambda x: x[1][0])
    return out


def _pick_year(d, today, year, prefer_past, month, day):
    """Дата без года: ближайшая будущая, либо ближайшая прошедшая («сколько прошло с…»)."""
    if year is not None:
        return d
    if prefer_past and d > today:
        return _safe_date(today.year - 1, month, day) or d
    if not prefer_past and d < today:
        return _safe_date(today.year + 1, month, day) or d
    return d


def _safe_date(year, month, day):
    try:
        return date(year, month, day)
    except (ValueError, TypeError):
        return None


# Праздники и сезоны для «сколько дней до …»
_EVENTS = [
    (r"нов\w*\s+год", 1, 1, "Нового года"),
    (r"8\s*марта|международн\w+ женск", 3, 8, "8 Марта"),
    (r"23\s*феврал|защитник\w* отечества", 2, 23, "23 Февраля"),
    (r"9\s*мая|дн\w+ победы", 5, 9, "Дня Победы"),
    (r"валентин", 2, 14, "Дня святого Валентина"),
    (r"хэллоуин|хеллоуин|halloween", 10, 31, "Хэллоуина"),
    (r"1\s*сентябр|дн\w+ знаний", 9, 1, "1 сентября"),
    (r"\bлет[ао]\b|\bлету\b", 6, 1, "лета"),
    (r"\bосен[иь]\b", 9, 1, "осени"),
    (r"\bзим[ыау]\b", 12, 1, "зимы"),
    (r"\bвесн[ыау]\b", 3, 1, "весны"),
]


def _event_date(text, today):
    for pattern, month, day, label in _EVENTS:
        if re.search(pattern, text, re.IGNORECASE):
            d = date(today.year, month, day)
            if d <= today:
                d = date(today.year + 1, month, day)
            return d, label
    return None


# =====================================================================
# Точные «факты о времени» по вопросу пользователя
# =====================================================================

_TIME_WORDS = re.compile(
    r"(котор\w+\s+час|сколько\s+(сейчас\s+)?(времени|время)|какое\s+(сейчас\s+)?время|"
    r"время\s+в\b|времени\s+в\b|час\w*\s+в\b|часовой\s+пояс|разниц\w+\s+во\s+времени|"
    r"какое\s+(сегодня\s+)?число|какой\s+(сегодня\s+)?день|какая\s+дата|какой\s+год|"
    r"день\s+недели|сколько\s+(осталось\s+)?(дней|часов|минут|недель|месяцев)|"
    r"сколько\s+осталось|сколько\s+прошло|сколько\s+мне\s+лет|через\s+сколько|"
    r"до\s+нового\s+года|какое\s+будет\s+число|который\s+будет\s+час|"
    r"сколько\s+будет\s+времени|во\s+сколько)",
    re.IGNORECASE,
)


_RELEVANT_RE = re.compile(
    r"(когда|сколько|день\s+недели|осталось|прошло|через|назад|спустя|родил|"
    r"\bдо\s+(нового|лета|зимы|осени|весны|\d)|\bвремя\b|\bчас\b|\bчасов\b|"
    r"какое\s+число|какой\s+день|какого\s+числа)",
    re.IGNORECASE,
)


def mentions_time(text):
    return bool(_TIME_WORDS.search(text or ""))


def time_facts(text, tz_name=None, now=None):
    """
    Считает всё, что можно посчитать по вопросу, и возвращает список строк
    с точными фактами. Пустой список — ничего временного в вопросе нет.
    """
    if not text:
        return []
    low = text.lower()
    # Считаем только когда вопрос реально про время — иначе «версия 2.5»
    # превратилась бы в «2 мая», а «в 5 утра проснулся» — в расчёт.
    if not (mentions_time(low) or _RELEVANT_RE.search(low)):
        return []
    tz = get_zone(tz_name)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = now.date()
    facts = []

    cities = find_cities(low)
    asks_time_somewhere = bool(re.search(r"(врем|час|пояс|сколько\s+там|разниц)", low))

    # --- время в городах / разница во времени -------------------------
    if cities and asks_time_somewhere and not re.search(r"напомн", low):
        for name, city_tz in cities[:4]:
            local = now.astimezone(ZoneInfo(city_tz))
            day_note = ""
            if local.date() != today:
                day_note = " (там уже завтра)" if local.date() > today else " (там ещё вчера)"
            facts.append(
                f"Сейчас в городе {name}: {local:%H:%M}, {format_date_ru(local, with_year=False)}"
                f"{day_note}, {utc_offset_str(local)}."
            )
        base_offset = now.utcoffset()
        if len(cities) >= 2:
            a = now.astimezone(ZoneInfo(cities[0][1])).utcoffset()
            b = now.astimezone(ZoneInfo(cities[1][1])).utcoffset()
            diff = (b - a).total_seconds() / 3600
            facts.append(
                f"Разница во времени: {cities[1][0]} {'опережает' if diff > 0 else 'отстаёт от'} "
                f"{cities[0][0]} на {_fmt_hours(abs(diff))}." if diff else
                f"У {cities[0][0]} и {cities[1][0]} сейчас одинаковое время."
            )
        else:
            city_offset = now.astimezone(ZoneInfo(cities[0][1])).utcoffset()
            diff = (city_offset - base_offset).total_seconds() / 3600
            if diff:
                facts.append(
                    f"Относительно пользователя ({utc_offset_str(now)}) там время "
                    f"{'впереди' if diff > 0 else 'позади'} на {_fmt_hours(abs(diff))}."
                )

    delta = parse_delta(low)
    prefer_past = bool(re.search(r"(прошл[оа]|прошел|прошёл|назад|\bбыл[аио]?\b|\bс\s+\d)", low))
    dates = parse_dates(low, today, prefer_past=prefer_past)
    clock = parse_clock(low)
    event = _event_date(low, today)

    # --- который час будет через N / что за дата будет через N -----------
    if delta and re.search(r"(через|спустя)", low) and not re.search(r"напомн", low):
        td, months, _ = delta
        target = add_months(now, months) + td if months else now + td
        facts.append(
            f"Через {_describe_delta(td, months)} будет: {format_dt_ru(target)}."
        )
    if delta and re.search(r"\bназад\b", low):
        td, months, _ = delta
        target = add_months(now, -months) - td if months else now - td
        facts.append(f"{_describe_delta(td, months).capitalize()} назад было: {format_dt_ru(target)}.")

    # --- сколько осталось до времени суток («до 18:00») ----------------
    if clock and re.search(r"(осталось|через сколько|сколько.*до|до\s)", low) and not dates:
        (h, mnt), _ = clock
        target = now.replace(hour=h, minute=mnt, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        facts.append(
            f"До {h:02d}:{mnt:02d} ({'сегодня' if target.date() == today else 'завтра'}) "
            f"осталось {humanize_delta((target - now).total_seconds())}."
        )

    # --- сколько дней до праздника/даты, сколько прошло с даты ---------
    if event and not dates:
        d, label = event
        days = (d - today).days
        facts.append(
            f"До {label} ({format_date_ru(d)}) осталось {_pl(days, 'день', 'дня', 'дней')}"
            f" (≈ {_weeks_days(days)})."
        )

    born = bool(re.search(r"родил|рожден|др\b|день\s+рождени", low))
    for d, _span, has_year in dates[:3]:
        days = (d - today).days
        weekday = WEEKDAYS[d.weekday()]
        if born and has_year and d < today:
            years = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
            next_bd = _safe_date(today.year, d.month, d.day) or date(today.year, 3, 1)
            if next_bd < today:
                next_bd = _safe_date(today.year + 1, d.month, d.day) or date(today.year + 1, 3, 1)
            facts.append(
                f"Дата {format_date_ru(d)}: возраст сейчас — {_pl(years, 'год', 'года', 'лет')}; "
                f"следующий день рождения {format_date_ru(next_bd)}, через "
                f"{_pl((next_bd - today).days, 'день', 'дня', 'дней')}."
            )
            continue
        if days > 0:
            facts.append(
                f"{format_date_ru(d, with_weekday=False)} — это {weekday}; до этой даты "
                f"{_pl(days, 'день', 'дня', 'дней')} (≈ {_weeks_days(days)})."
            )
        elif days < 0:
            facts.append(
                f"{format_date_ru(d, with_weekday=False)} — это был(а) {weekday}; с тех пор прошло "
                f"{_pl(-days, 'день', 'дня', 'дней')}"
                + (f" (≈ {humanize_delta(-days * 86400)})" if -days > 60 else "")
                + "."
            )
        else:
            facts.append(f"Сегодня: {format_date_ru(d)}.")

    # --- простые «какое сегодня число / день недели / год» ---------------
    if re.search(r"(какое\s+(сегодня\s+)?число|какой\s+сегодня\s+день|какая\s+(сегодня\s+)?дата|"
                 r"какой\s+(сейчас\s+)?год|какой\s+день\s+недели\s*(сегодня)?\s*\??$)", low) and not dates:
        facts.append(f"Сегодня {format_date_ru(today)} (неделя №{today.isocalendar()[1]}).")
    if re.search(r"(котор\w+\s+час|сколько\s+(сейчас\s+)?(времени|время)|какое\s+(сейчас\s+)?время)", low) \
            and not cities and not delta:
        facts.append(f"Сейчас у пользователя {now:%H:%M} ({utc_offset_str(now)}), {part_of_day(now.hour)}.")

    # уникализируем, сохраняя порядок
    seen, out = set(), []
    for f in facts:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _fmt_hours(h):
    whole = int(h)
    if abs(h - whole) < 1e-6:
        return _pl(whole, "час", "часа", "часов")
    minutes = round((h - whole) * 60)
    return f"{_pl(whole, 'час', 'часа', 'часов')} {_pl(minutes, 'минута', 'минуты', 'минут')}"


def _weeks_days(days):
    w, d = divmod(abs(days), 7)
    if not w:
        return _pl(d, "день", "дня", "дней")
    return _pl(w, "неделя", "недели", "недель") + (f" и {_pl(d, 'день', 'дня', 'дней')}" if d else "")


def _describe_delta(td, months):
    parts = []
    if months:
        y, mo = divmod(months, 12)
        if y:
            parts.append(_pl(y, "год", "года", "лет"))
        if mo:
            parts.append(_pl(mo, "месяц", "месяца", "месяцев"))
    if td:
        parts.append(humanize_delta(td.total_seconds(), parts=3))
    return " ".join(parts) or "0 минут"


def is_pure_time_question(text):
    """
    Вопрос целиком про время/дату, на который есть точный локальный ответ —
    такой вопрос не нужно отправлять в веб-поиск («сколько сейчас времени»
    раньше триггерило поиск из-за слова «сейчас»).
    """
    if not mentions_time(text):
        return False
    return bool(time_facts(text))


# =====================================================================
# Напоминания
# =====================================================================

_REMIND_RE = re.compile(r"\b(напомни|напомнить|напоминалк\w*|поставь\s+напоминани\w*|разбуди)\b", re.IGNORECASE)


def looks_like_reminder(text):
    return bool(_REMIND_RE.search(text or ""))


def parse_reminder(text, tz_name=None, now=None):
    """
    'напомни через 20 минут выключить духовку' ->
        {"due_utc": datetime, "text": "выключить духовку", "local": datetime}
    'напомни выключить духовку' (без времени) -> {"due_utc": None, "text": ...}
    Не напоминание -> None.
    """
    if not looks_like_reminder(text):
        return None
    tz = get_zone(tz_name)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = now.date()
    low = text.lower()
    spans = [m.span() for m in _REMIND_RE.finditer(low)]

    due = None
    delta = parse_delta(low)
    dates = parse_dates(low, today)
    clock = parse_clock(low)

    if delta and re.search(r"\b(через|спустя)\b", low):
        td, months, span = delta
        due = (add_months(now, months) if months else now) + td
        spans.append(span)
        m = re.search(r"\b(через|спустя)\b", low)
        spans.append(m.span())
    elif dates or clock:
        d = dates[0][0] if dates else today
        if dates:
            spans.append(dates[0][1])
        h, mnt = (9, 0)
        if clock:
            (h, mnt), cspan = clock
            spans.append(cspan)
        due = datetime(d.year, d.month, d.day, h, mnt, tzinfo=tz)
        if due <= now:
            if not dates:
                due += timedelta(days=1)  # «в 7 утра», а уже 9 — значит завтра
            elif clock is None and d == today:
                due = now + timedelta(hours=1)
    reminder_text = _cut_spans(text, spans)
    for _ in range(4):
        reminder_text = re.sub(
            r"^\s*(мне|меня|нам|нас|пожалуйста|плиз|каспер|kasper|о\s+том,?\s*что(бы)?|что(бы)?)\b[\s,]*",
            "", reminder_text, flags=re.I,
        )
    reminder_text = re.sub(r"\s+", " ", reminder_text).strip(" ,.:;-—!")
    if not reminder_text:
        reminder_text = "подъём! ⏰" if re.search(r"разбуди", low) else "ты просил напомнить 🙂"
    return {
        "due_utc": due.astimezone(timezone.utc) if due else None,
        "local": due,
        "text": reminder_text,
    }


def _cut_spans(text, spans):
    chars = list(text)
    for s, e in spans:
        if s is None or e is None:
            continue
        for i in range(max(0, s), min(len(chars), e)):
            chars[i] = " "
    out = "".join(chars)
    out = re.sub(r"\b(в|во|на|к|до|через|спустя)\s*(?=[,.!?]|$)", " ", out, flags=re.I)
    out = re.sub(r"\b(в|во)\s+(?=\s)", " ", out, flags=re.I)
    return re.sub(r"\s{2,}", " ", out).strip()
