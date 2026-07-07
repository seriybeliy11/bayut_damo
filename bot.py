"""
Точка входа: Telegram-бот для управления инвойсами 2328.io.

Использует long polling — идеально подходит для Render Background Worker.
Никаких внешних фреймворков, только requests.
"""
import logging
import sys
import time

import config
import handlers
import state
import telegram_api
from telegram_api import TelegramError

# ───────────────────────── Логирование ─────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Уменьшаем спам от requests.
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("bot")


# ───────────────────────── Роутинг апдейтов ─────────────────────────

def route_update(update: dict):
    """Маршрутизирует апдейт в нужный обработчик."""
    try:
        if "message" in update:
            msg = update["message"]
            text = (msg.get("text") or "").strip()
            chat_id = msg.get("chat", {}).get("id")
            user_id = msg.get("from", {}).get("id")

            # Если пользователь в каком-то состоянии — сначала обрабатываем FSM.
            if (
                user_id
                and state.get_state(user_id) != state.STATE_IDLE
                and not text.startswith("/")
            ):
                handlers.handle_text(update)
                return

            # Команды.
            if text.startswith("/"):
                cmd = text.split()[0].lower().split("@")[0]
                if cmd in ("/start", "/help", "/pay", "/status", "/list", "/cancel"):
                    {
                        "/start":  handlers.cmd_start,
                        "/help":   handlers.cmd_help,
                        "/pay":    handlers.cmd_pay,
                        "/status": handlers.cmd_status,
                        "/list":   handlers.cmd_list,
                        "/cancel": handlers.cmd_cancel,
                    }[cmd](update)
                    return
                # Неизвестная команда.
                telegram_api.send_message(
                    chat_id,
                    f"Неизвестная команда: <code>{cmd}</code>. Напиши /help.",
                )
                return

            # Обычный текст (без состояния).
            handlers.handle_text(update)
            return

        if "callback_query" in update:
            handlers.handle_callback(update)
            return

        log.debug("Unknown update type, skipping: %s", list(update.keys()))
    except Exception:  # noqa: BLE001
        log.exception("route_update failed")


# ───────────────────────── Главный цикл ─────────────────────────

def main():
    errors = config.validate()
    if errors:
        log.error("Конфигурация неполная:")
        for e in errors:
            log.error("  • %s", e)
        sys.exit(1)

    log.info("Bot starting…")
    handlers.setup_bot_commands()

    offset = None
    empty_polls = 0

    while True:
        try:
            updates = telegram_api.get_updates(offset=offset, timeout=config.LONG_POLL_TIMEOUT)
            if updates:
                empty_polls = 0
                for upd in updates:
                    offset = upd["update_id"] + 1
                    route_update(upd)
            else:
                empty_polls += 1
                if empty_polls % 60 == 0:
                    log.info("Long-poll idle… (offset=%s)", offset)

        except TelegramError as e:
            log.warning("Telegram error в главном цикле: %s", e)
            # На 409 Conflict — кто-то ещё запустил бота с тем же токеном.
            if e.error_code == 409:
                log.error("409 Conflict: возможно, бот уже запущен в другом месте. Жду 10 сек.")
                time.sleep(10)
            else:
                time.sleep(3)
        except KeyboardInterrupt:
            log.info("Остановка по Ctrl+C.")
            break
        except Exception:  # noqa: BLE001
            log.exception("Непредвиденная ошибка в главном цикле. Жду 5 сек.")
            time.sleep(5)


if __name__ == "__main__":
    main()
