"""
AI-агент режим Kasper: пользователь формулирует задачу своими словами, бот
строит пошаговый план (поиск в интернете / генерация сайта-файла / прямой
ответ), показывает план с кнопками подтверждения, и по подтверждению
выполняет шаги один за другим, показывая прогресс, а в конце синтезирует
единый читаемый результат.

Изолирован от обычного режима чата намеренно: любая ошибка здесь ловится
локально (см. AgentError и try/except в telegram/handlers.py), обычный
диалог с Каспером продолжает работать даже если весь этот модуль сломан.

Состояние сессии агента хранится в памяти процесса (AGENT_SESSIONS),
как и PENDING_SITE_REQUESTS для обычного режима — оно живёт ровно на
время диалога "план -> подтверждение -> выполнение" и не должно
переживать рестарт бота, поэтому отдельная таблица в БД не нужна.
"""

import asyncio
import json
import os
import re
import time

import aiohttp

from router.ai_router import (
    ask_provider,
    get_provider_order,
    generate_website_html,
)
from router.web_search import tavily_search, format_search_results


MAX_STEPS = 7
TASK_TIMEOUT_SECONDS = 110  # чуть меньше 2 минут — запас на отправку сообщений
STEP_TIMEOUT_SECONDS = 40

# Лимит агентских запусков в день на пользователя — отдельно от обычного
# лимита сообщений (check_and_increment_limit), т.к. один запуск агента
# по стоимости эквивалентен нескольким обычным AI-запросам (план + шаги
# + синтез финального ответа).
AGENT_DAILY_LIMIT = int(os.getenv("AGENT_DAILY_LIMIT", "15"))

# {user_id: {"day": "YYYY-MM-DD", "count": int}}
_AGENT_USAGE = {}

# {user_id: {"task": str, "plan": list[dict], "status": str,
#            "results": list[dict], "created_at": float}}
AGENT_SESSIONS = {}

STEP_SEARCH = "search"
STEP_GENERATE_SITE = "generate_site"
STEP_ANSWER = "answer"
VALID_STEP_TYPES = (STEP_SEARCH, STEP_GENERATE_SITE, STEP_ANSWER)


class AgentError(Exception):
    pass


# Дёшевый первый фильтр перед дорогим AI-классификатором should_use_agent:
# многошаговые задачи почти всегда содержат явные маркеры вроде "найди и
# сравни", "собери информацию", "сделай сайт для ...". Если ни одного
# маркера нет — не тратим лишний AI-запрос, обычный режим чата не трогаем.
AGENT_HINT_PATTERN = re.compile(
    r"\b(найди и сравни|собери информацию|сравни несколько|сравни варианты|"
    r"проанализируй и|исследуй и|найди и сделай|сделай сайт для|создай сайт для|"
    r"найди .* и сделай|подбери .* и сравни|составь список и|"
    r"найди .* сравни|проверь .* и напиши|собери .* и составь)",
    re.IGNORECASE,
)


def has_agent_hint(text: str) -> bool:
    return bool(AGENT_HINT_PATTERN.search(text))


# Явно не-задачи: короткие реплики/приветствия/эмоции, которые не должны
# уходить в построение плана, даже когда пользователь только что вызвал
# /agent и следующим сообщением написал что-то не по адресу.
NOT_A_TASK_PATTERN = re.compile(
    r"^\s*("
    r"привет\w*|здравствуй\w*|хай|йо|ку|"
    r"как дела\??|как ты\??|как жизнь\??|что как\??|"
    r"да|нет|ок|окей|ладно|спасибо|благодарю|пока|"
    r"ору|лол|ахах\w*|хах\w*|😂+|👍+"
    r")\s*[!.?]*\s*$",
    re.IGNORECASE,
)


TASK_CHECK_SYSTEM_PROMPT = (
    "Пользователь только что вызвал команду AI-агента и ему предложили "
    "описать задачу (что найти/сравнить/собрать/какой сайт сделать). "
    "Определи: то, что он написал СЕЙЧАС — это реальная задача для "
    "агента, или это случайная/неотносящаяся реплика (приветствие, "
    "вопрос 'как дела', благодарность, короткая эмоция, разговор не по "
    "теме)? Ответь СТРОГО JSON без пояснений: "
    '{"is_task": true/false}'
)


async def is_actual_task(session, provider, user_text: str) -> bool:
    """
    Дешёвый первый фильтр через regex, и только если он не дал однозначного
    ответа — уточняющий AI-запрос. Нужен, чтобы после /agent случайное
    "привет как дела" не улетало в план вместо вежливого переспроса.
    """
    if NOT_A_TASK_PATTERN.match(user_text.strip()):
        return False

    if len(user_text.strip()) < 4:
        return False

    try:
        messages = [
            {"role": "system", "content": TASK_CHECK_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]
        raw = await ask_provider(session, provider, messages)
        raw = _strip_json_fences(raw)
        data = json.loads(raw)
        return bool(data.get("is_task", True))
    except Exception as e:
        print(f"[agent] is_actual_task {provider} ERROR: {e}", flush=True)
        # При сбое классификатора не блокируем пользователя — считаем задачей,
        # как было в исходном поведении до этого фикса.
        return True


def _today_str():
    return time.strftime("%Y-%m-%d", time.gmtime())


def check_and_increment_agent_limit(user_id: int):
    """
    Простой дневной лимит на количество запусков агента, отдельно от
    обычного лимита сообщений — хранится в памяти, сбрасывается по UTC-дате
    (как и общий процесс бота: переживать рестарт Render не обязано, это
    не критичный для целостности данных счётчик).
    """
    today = _today_str()
    entry = _AGENT_USAGE.get(user_id)

    if not entry or entry["day"] != today:
        entry = {"day": today, "count": 0}
        _AGENT_USAGE[user_id] = entry

    if entry["count"] >= AGENT_DAILY_LIMIT:
        return False, 0

    entry["count"] += 1
    return True, AGENT_DAILY_LIMIT - entry["count"]


def _strip_json_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


PLAN_SYSTEM_PROMPT = (
    "Ты — планировщик AI-агента в Telegram-боте Kasper. Пользователь "
    "формулирует задачу своими словами. Разбей её на конкретную "
    "последовательность шагов, каждый из которых бот реально может "
    "выполнить. Доступные типы шагов:\n"
    '- "search": найти информацию в интернете (укажи в "description" '
    "конкретный поисковый запрос, а не общую формулировку задачи)\n"
    '- "generate_site": сгенерировать готовый HTML-сайт/лендинг/страницу '
    "(укажи в description чёткое описание сайта: тематика, стиль, что на "
    "нём должно быть)\n"
    '- "answer": сформулировать финальный текстовый ответ/вывод на основе '
    "того, что уже собрано на предыдущих шагах (обычно последний шаг)\n\n"
    f"Правила:\n"
    f"- Не больше {MAX_STEPS} шагов. Если задачу можно решить за 1-2 шага "
    "— не придумывай лишние.\n"
    "- Каждый шаг должен быть самостоятельным и осмысленным, без "
    "дублирования.\n"
    "- Всегда заканчивай шагом типа \"answer\", кроме случая, когда "
    "единственный шаг — это generate_site (тогда сайт и есть результат).\n"
    "- Если задача не требует нескольких шагов и это обычный вопрос — "
    "всё равно верни план из одного шага answer.\n\n"
    "Ответь СТРОГО JSON без пояснений и без markdown-обёртки:\n"
    '{"final_goal": "краткая формулировка итоговой цели на русском", '
    '"steps": [{"type": "search|generate_site|answer", '
    '"description": "конкретное описание шага на русском"}]}'
)


async def build_plan(session, provider, task_text: str) -> dict:
    messages = [
        {"role": "system", "content": PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": task_text},
    ]

    raw = await ask_provider(session, provider, messages)
    raw = _strip_json_fences(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AgentError(f"Планировщик вернул невалидный JSON: {e}") from e

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise AgentError("Планировщик не построил ни одного шага.")

    clean_steps = []
    for item in steps[:MAX_STEPS]:
        if not isinstance(item, dict):
            continue
        step_type = str(item.get("type", "")).strip().lower()
        description = str(item.get("description", "")).strip()
        if step_type not in VALID_STEP_TYPES or not description:
            continue
        clean_steps.append({"type": step_type, "description": description})

    if not clean_steps:
        raise AgentError("Планировщик не построил ни одного корректного шага.")

    final_goal = str(data.get("final_goal", "")).strip() or task_text

    return {"final_goal": final_goal, "steps": clean_steps}


AGENT_DETECT_SYSTEM_PROMPT = (
    "Определи, является ли сообщение пользователя МНОГОШАГОВОЙ задачей для "
    "AI-агента: пользователь просит что-то найти/собрать/сравнить/проверить "
    "в интернете И/ИЛИ сгенерировать сайт/лендинг/страницу, причём это явно "
    "не просто короткий разговорный вопрос. Примеры того, что ДА, агентская "
    "задача: 'найди 3 лучших ноутбука до 100000 и сравни их', 'собери "
    "информацию о компании X и сделай для неё лендинг', 'проверь последние "
    "новости про Y и напиши краткую сводку'. Примеры того, что НЕТ: обычный "
    "вопрос, просьба перевести текст, короткий разговорный обмен репликами, "
    "простая просьба включить музыку. Ответь СТРОГО JSON без пояснений: "
    '{"is_agent_task": true/false}'
)


async def should_use_agent(session, provider, user_text: str) -> bool:
    messages = [
        {"role": "system", "content": AGENT_DETECT_SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    try:
        raw = await ask_provider(session, provider, messages)
        raw = _strip_json_fences(raw)
        data = json.loads(raw)
        return bool(data.get("is_agent_task"))
    except Exception as e:
        print(f"[agent] should_use_agent {provider} ERROR: {e}", flush=True)
        return False


async def build_plan_with_fallback(task_text: str) -> dict:
    """Пробует провайдеров по очереди, как это делает ask() в ai_router."""
    errors = []
    async with aiohttp.ClientSession() as agent_session:
        for provider in get_provider_order():
            try:
                return await build_plan(agent_session, provider, task_text)
            except Exception as e:
                errors.append(f"{provider}: {e}")
                print(f"[agent] build_plan {provider} ERROR: {e}", flush=True)
                continue

    raise AgentError("Не удалось построить план ни одним провайдером: " + "; ".join(errors))


def format_plan_text(task_text: str, plan: dict) -> str:
    lines = [
        "🧠 <b>План выполнения задачи</b>",
        f"<i>{task_text}</i>",
        "",
    ]
    for i, step in enumerate(plan["steps"], start=1):
        icon = {
            STEP_SEARCH: "🔎",
            STEP_GENERATE_SITE: "🌐",
            STEP_ANSWER: "✍️",
        }.get(step["type"], "•")
        lines.append(f"{i}. {icon} {step['description']}")
    lines.append("")
    lines.append("Выполнить по этому плану?")
    return "\n".join(lines)


def format_progress_text(task_text: str, plan: dict, current_index: int) -> str:
    lines = [
        "⚙️ <b>Выполняю план</b>",
        f"<i>{task_text}</i>",
        "",
    ]
    for i, step in enumerate(plan["steps"], start=1):
        if i - 1 < current_index:
            mark = "✅"
        elif i - 1 == current_index:
            mark = "⏳"
        else:
            mark = "▫️"
        lines.append(f"{mark} {i}. {step['description']}")
    return "\n".join(lines)


async def _execute_search_step(agent_session, description: str) -> str:
    results = await tavily_search(description)
    formatted = format_search_results(results)
    if not formatted:
        return f"По запросу «{description}» ничего не нашлось в открытых источниках."
    return formatted


async def _execute_site_step(agent_session, description: str):
    """
    Возвращает (html_code, error). html_code is None при неудаче всех
    провайдеров.
    """
    for provider in get_provider_order():
        try:
            html_code = await generate_website_html(agent_session, provider, description)
            if html_code:
                return html_code, None
        except Exception as e:
            print(f"[agent] generate_site {provider} ERROR: {e}", flush=True)
            continue
    return None, "Не удалось сгенерировать сайт ни одним провайдером."


ANSWER_SYSTEM_PROMPT = (
    "Ты — Kasper AI в режиме агента. Пользователь поставил задачу, ты уже "
    "прошёл по плану и собрал промежуточные результаты (поиск в интернете "
    "и т.п.), они приведены ниже. Собери из этого один связный, чётко "
    "структурированный финальный ответ на русском языке, отвечающий на "
    "исходную задачу пользователя. Не упоминай слова \"шаг\", \"план\", "
    "не описывай сам процесс работы — сразу выдай содержательный результат. "
    "Если среди промежуточных результатов есть противоречия — отметь это "
    "коротко. Если данных недостаточно для полного ответа — честно скажи, "
    "какой информации не хватило."
)


async def _execute_answer_step(agent_session, task_text: str, collected: list) -> str:
    context_lines = []
    for item in collected:
        if item["type"] == STEP_SEARCH:
            context_lines.append(f"[Результаты поиска: {item['description']}]\n{item['output']}")
        elif item["type"] == STEP_GENERATE_SITE:
            context_lines.append(f"[Сгенерирован сайт: {item['description']}] — готовый HTML-файл прикреплён отдельно.")

    user_content = (
        f"Исходная задача пользователя: {task_text}\n\n"
        + ("\n\n".join(context_lines) if context_lines else "(промежуточных данных нет)")
    )

    messages = [
        {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    errors = []
    for provider in get_provider_order():
        try:
            return await ask_provider(agent_session, provider, messages)
        except Exception as e:
            errors.append(f"{provider}: {e}")
            print(f"[agent] answer {provider} ERROR: {e}", flush=True)
            continue

    raise AgentError("Не удалось получить финальный ответ ни одним провайдером: " + "; ".join(errors))


async def run_step(agent_session, task_text: str, step: dict, collected: list):
    """
    Выполняет один шаг плана. Возвращает dict с результатом шага —
    добавляется в collected. Для generate_site отдельно возвращает html_code
    через ключ "html_code", чтобы вызывающий код мог отправить файл.
    """
    step_type = step["type"]
    description = step["description"]

    if step_type == STEP_SEARCH:
        output = await asyncio.wait_for(
            _execute_search_step(agent_session, description),
            timeout=STEP_TIMEOUT_SECONDS,
        )
        return {"type": step_type, "description": description, "output": output}

    if step_type == STEP_GENERATE_SITE:
        html_code, error = await asyncio.wait_for(
            _execute_site_step(agent_session, description),
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if error:
            return {"type": step_type, "description": description, "output": error, "html_code": None}
        return {"type": step_type, "description": description, "output": "Сайт сгенерирован.", "html_code": html_code}

    if step_type == STEP_ANSWER:
        output = await asyncio.wait_for(
            _execute_answer_step(agent_session, task_text, collected),
            timeout=STEP_TIMEOUT_SECONDS,
        )
        return {"type": step_type, "description": description, "output": output}

    raise AgentError(f"Неизвестный тип шага: {step_type}")
