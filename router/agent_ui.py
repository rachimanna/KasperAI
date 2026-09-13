"""
Inline-клавиатуры для AI-агент режима. Вынесены отдельно от handlers.py,
чтобы не раздувать и без того большой файл — сама логика планирования и
выполнения живёт в router/agent.py, здесь только UI.
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def plan_confirmation_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=3)
    keyboard.add(
        InlineKeyboardButton("✅ Выполнить", callback_data="agent_confirm"),
        InlineKeyboardButton("✏️ Изменить", callback_data="agent_edit"),
        InlineKeyboardButton("❌ Отмена", callback_data="agent_cancel"),
    )
    return keyboard


def stop_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("⏹ Остановить", callback_data="agent_stop"),
    )
    return keyboard
