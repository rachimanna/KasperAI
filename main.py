
95
96
97
98
99
100
101
102
103
104
105
106
107
108
109
110
111
112
113
114
115
116
117
118
119
120
121
122
123
124
125
126
127
128
129
130
131
132
133
134
135
136
137
138
139
140
141
142
143
144
145
146
147
148
149
150
151
152
153
154
155
156
157
158
159
160
161
162
163
164
165
166
167
168
169
170
171
172
173
174
175
176
177
178
179
180
181
182
183
184
185
186
187
188
import asyncio
import logging
        BotCommand("limit", "Мой лимит запросов"),
        BotCommand("status", "Состояние бота"),
        BotCommand("agent", "AI-агент: найти/сравнить/сделать сайт"),
        BotCommand("shadowcity", "Начать игру «Теневой город»"),
        BotCommand("stopshadowcity", "Остановить текущую игру"),
        BotCommand("gamestats", "Моя статистика «Теневого города»"),
        BotCommand("stats", "Статистика бота (админ)"),
        BotCommand("ban", "Забанить пользователя (ответом/@username/id)"),
        BotCommand("unban", "Разбанить пользователя (ответом/@username/id)"),
        BotCommand("broadcast", "Рассылка всем пользователям"),
    ]
    for admin_id in ADMIN_IDS:
        try:
            await dp.bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as e:
            print(f"[Commands] admin scope error for {admin_id}: {e}", flush=True)

    # Фоновая задача мини-игры "Теневой город": раз в несколько секунд
    # проверяет БД на игры с истёкшей фазой (ночь/голосование) и
    # продвигает их. Живёт в games.phase_ends_at, поэтому переживает
    # пересыпание/передеплой Render.
    asyncio.create_task(phase_checker_loop(dp.bot))

    # Логирование памяти каждую минуту — см. _current_memory_mb выше:
    # единственный способ следить за потреблением на free-тарифе Render,
    # где графики памяти скрыты за платным планом.
    asyncio.create_task(memory_logger_loop())

    print("Database: OK")
    print("Kasper AI is running.")


async def on_shutdown(dp):
    try:
        print(f"[memory] RSS at shutdown = {_current_memory_mb():.1f} MB", flush=True)
    except Exception as e:
        print(f"[memory] shutdown logging error: {e}", flush=True)
    await close_http_session()
    await close_db()


def main():

    load_dotenv(override=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    print("Kasper AI starting...")

    threading.Thread(target=run_health_server, daemon=True).start()

    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured"
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    bot = Bot(token=token)
    dp = Dispatcher(bot)

    register_handlers(dp)

    # Telegram Business Mode: патчит aiogram.bot.api.check_result (см.
    # router/business_raw_diag.py) — единственная точка, где ещё виден
    # сырой JSON апдейта до того, как aiogram отбросит незнакомые ему поля
    # business_connection/business_message при типизации в types.Update.
    # Оттуда же теперь реально диспетчеризуется обработка business-сообщений
    # (router/business.py: триггер "Каспер" -> AI -> ответ в тот же чат).
    # Прежняя диагностика в BusinessDiagnosticMiddleware (telegram/handlers.py)
    # смотрит на уже готовый Update и структурно не может найти эти поля —
    # оставлена как есть, но полагаться на неё для этой цели не стоит.
    patch_check_result_for_business_diag(bot)

    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        loop=loop,
    )


if __name__ == "__main__":
    main()
