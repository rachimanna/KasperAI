```python
"""
UI-клавиатуры для AI-агента Kasper AI.

Здесь находятся только Telegram InlineKeyboardMarkup.
Логика планирования и выполнения агента находится в router/agent.py.
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def plan_confirmation_keyboard() -> InlineKeyboardMarkup:
    """
    Кнопки подтверждения построенного плана агента.
    """
    keyboard = InlineKeyboardMarkup(row_width=3)

    keyboard.add(
        InlineKeyboardButton(
            text="✅ Выполнить",
            callback_data="agent_confirm",
        ),
        InlineKeyboardButton(
            text="✏️ Изменить",
            callback_data="agent_edit",
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="agent_cancel",
        ),
    )

    return keyboard


def stop_keyboard() -> InlineKeyboardMarkup:
    """
    Кнопка остановки выполняющегося агента.
    """
    keyboard = InlineKeyboardMarkup(row_width=1)

    keyboard.add(
        InlineKeyboardButton(
            text="⏹ Остановить",
            callback_data="agent_stop",
        )
    )

    return keyboard
```
