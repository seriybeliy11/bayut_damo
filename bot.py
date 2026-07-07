import os
import json
import hmac
import hashlib
import base64
import time
import csv
import logging
from datetime import datetime

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiohttp import web

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Безопасное получение токенов из переменных окружения Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROJECT_UUID = os.getenv("PROJECT_UUID")
API_KEY = os.getenv("API_KEY")

if not all([BOT_TOKEN, PROJECT_UUID, API_KEY]):
    raise ValueError("Не заданы переменные окружения BOT_TOKEN, PROJECT_UUID или API_KEY")

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Функция для создания подписи (строго по спецификации 2328.io)
def api_sign(body: str, api_key: str) -> str:
    b64 = base64.b64encode(body.encode("utf-8")).decode()
    return hmac.new(api_key.encode(), b64.encode(), hashlib.sha256).hexdigest()

# Функция для сохранения в CSV
def save_subscription_to_csv(user_id: int, username: str, amount: float, order_id: str):
    file_exists = os.path.isfile('subscriptions.csv')
    with open('subscriptions.csv', 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        # Если файл пустой, пишем заголовки
        if not file_exists:
            writer.writerow(['user_id', 'username', 'amount', 'order_id', 'created_at'])
        # Пишем данные (дата создания инвойса)
        writer.writerow([user_id, username or "Unknown", amount, order_id, datetime.now().isoformat()])

# Обработчик команды /pay
@dp.message(Command("pay"))
async def send_payment(message: Message):
    # Парсим сумму
    try:
        amount_str = message.text.split()[1]
        amount = float(amount_str)
        if amount <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await message.answer("Используйте формат: /pay <сумма>\nПример: /pay 100")
        return

    # Формируем уникальный ID заказа
    order_id = f"ORDER-{message.from_user.id}-{int(time.time())}"

    # Подготавливаем payload строго по ТЗ (без лишних пробелов)
    data = {
        "amount": f"{amount:.2f}",
        "currency": "USD",
        "order_id": order_id,
        "url_callback": "https://placeholder.com/webhook" # Заглушка, так как вебхук не обрабатывается
    }
    
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    sign = api_sign(body, API_KEY)

    # Выполняем запрос к API 2328.io
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                "https://api.2328.io/api/v1/payment",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "MyTelegramBot/1.0",
                    "project": PROJECT_UUID,
                    "sign": sign,
                },
                content=body.encode("utf-8"),
                timeout=10.0
            )
            response = r.json()
        except httpx.RequestError as e:
            logging.error(f"Ошибка сети при обращении к 2328.io: {e}")
            await message.answer("Ошибка связи с платежной системой. Попробуйте позже.")
            return

    # Обработка ответа
    if response.get("state") == 0:
        tg_deeplink = response["result"]["tg_deeplink"]
        
        # Сохраняем в CSV
        save_subscription_to_csv(
            user_id=message.from_user.id,
            username=message.from_user.username,
            amount=amount,
            order_id=order_id
        )
        
        await message.answer(f"💰 Оплата подписки на ${amount:.2f}\n\nПерейдите по ссылке для оплаты:\n{tg_deeplink}")
    else:
        logging.error(f"Ошибка API 2328.io: {response}")
        await message.answer("Ошибка при создании платежа. Проверьте логи.")

# --- Настройка веб-сервера для Render ---
# Render требует, чтобы приложение слушало порт, мы используем aiohttp для здоровья сервиса
async def health_check(request):
    return web.Response(text="Bot is running")

async def main():
    # Запускаем поллинг бота в фоне
    bot_task = asyncio.create_task(dp.start_polling(bot))
    
    # Поднимаем веб-сервер для Render (Health Check)
    app = web.Application()
    app.router.add_get('/', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    logging.info(f"Web server started on port {port}")
    
    # Ожидаем завершения работы бота
    await bot_task

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
