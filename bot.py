import os
import json
import hmac
import hashlib
import base64
import time
import sqlite3
import logging
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiohttp import web

# --- ENV VARS ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROJECT_UUID = os.getenv("PROJECT_UUID")
API_KEY = os.getenv("API_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
DIFY_POLITE_KEY = os.getenv("DIFY_POLITE_KEY")
DIFY_AGGRESSIVE_KEY = os.getenv("DIFY_AGGRESSIVE_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    query_text TEXT,
                    status TEXT DEFAULT 'free',
                    order_id TEXT,
                    referrer_id INTEGER
                 )''')
    conn.commit()
    conn.close()
init_db()

# --- DIFY API WRAPPER ---
async def ask_dify(api_key: str, query: str) -> str:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.dify.ai/v1/chat-messages",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "inputs": {"user_query": query},
                "query": "Начинай анализ",
                "response_mode": "blocking",
                "user": "system"
            },
            timeout=30.0
        )
        return r.json().get("answer", "Ошибка получения ответа от ИИ.")

# --- 2328.io API ---
def api_sign(body: str, api_key: str) -> str:
    b64 = base64.b64encode(body.encode("utf-8")).decode()
    return hmac.new(api_key.encode(), b64.encode(), hashlib.sha256).hexdigest()

# --- ЛОГИКА БОТА ---

@dp.message(Command("start"))
async def start_cmd(message: Message):
    user_id = message.from_user.id
    args = message.text.split()
    referrer_id = None
    
    # Ловим реферала (Вирусный трюк)
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1].split("_")[1])
            # TODO: Здесь можно начислять бонус рефералу, если юзер новый
        except ValueError:
            pass

    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, referrer_id) VALUES (?, ?)", (user_id, referrer_id))
    conn.commit()
    conn.close()
    
    await message.answer("Привет. Кидай ссылку текст объявления. Посмотрим, что там за хрень.")

@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    text = message.text
    
    # 1. Сохраняем запрос юзера в БД
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("UPDATE users SET query_text = ?, status = 'free' WHERE user_id = ?", (text, user_id))
    conn.commit()
    conn.close()

    await message.bot.send_chat_action(message.chat.id, "typing")
    
    # 2. Идем в ПОЛИТЕКНУЮ LLM (Dify)
    polite_answer = await ask_dify(DIFY_POLITE_KEY, text)
    
    # 3. Формируем кнопку оплаты
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Разнести брокера ($10)", callback_data="create_invoice")]
    ])
    
    await message.answer(polite_answer, reply_markup=keyboard)

@dp.callback_query(F.data == "create_invoice")
async def create_invoice(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    
    # Проверяем, не платил ли уже
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row and row[0] == 'paid':
        await callback.message.answer("Ты уже разблокировал тяжелую артиллерию.")
        conn.close()
        return

    # Генерируем счет в 2328.io
    order_id = f"RIOT-{user_id}-{int(time.time())}"
    data = {
        "amount": "10.00",
        "currency": "USD",
        "order_id": order_id,
        "url_callback": f"{RENDER_EXTERNAL_URL}/webhook/2328" # Вебхук на Render
    }
    
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    sign = api_sign(body, API_KEY)

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.2328.io/api/v1/payment",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "HagilBot/1.0",
                "project": PROJECT_UUID,
                "sign": sign,
            },
            content=body.encode("utf-8"),
            timeout=10.0
        )
        resp = r.json()

    if resp.get("state") == 0:
        tg_deeplink = resp["result"]["tg_deeplink"]
        
        # Обновляем БД
        c.execute("UPDATE users SET order_id = ?, status = 'pending' WHERE user_id = ?", (order_id, user_id))
        conn.commit()
        conn.close()
        
        await callback.message.answer(f"Оплата доступа к 'Тяжелой артиллерии':\n{tg_deeplink}")
    else:
        conn.close()
        await callback.message.answer("Ошибка платежки.")

# --- WEBHOOK 2328.IO (СЕРВЕРНАЯ ЧАСТЬ) ---
async def webhook_2328_handler(request):
    payload = await request.json()
    order_id = payload.get("order_id")
    status = payload.get("payment_status") or payload.get("status")
    
    if order_id and status == "paid":
        # Ищем юзера по order_id
        def db_update():
            conn = sqlite3.connect('bot.db')
            c = conn.cursor()
            c.execute("SELECT user_id, query_text FROM users WHERE order_id = ?", (order_id,))
            row = c.fetchone()
            if row:
                user_id, query_text = row
                c.execute("UPDATE users SET status = 'paid' WHERE user_id = ?", (user_id,))
                conn.commit()
            conn.close()
            return row
        
        row = await asyncio.to_thread(db_update)
        
        if row:
            user_id, query_text = row
            
            # 1. Вызываем ЗЛОГО бота
            aggressive_answer = await ask_dify(DIFY_AGGRESSIVE_KEY, query_text)
            
            # 2. TODO: Здесь можно впилить рекламу брокера-партнера
            
            # 3. Формируем ВИРУСНУЮ ссылку
            bot_username = (await bot.get_me()).username
            viral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Расскажи коллегам ($)", url=viral_link)]
            ])
            
            # 4. Отправляем результат
            await bot.send_message(
                user_id, 
                f"💣 Hagil Mode:\n\n{aggressive_answer}",
                reply_markup=keyboard
            )
            
    return web.Response(text="OK")

async def health_check(request):
    return web.Response(text="Bot is alive")

async def main():
    bot_task = asyncio.create_task(dp.start_polling(bot))
    
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_post('/webhook/2328', webhook_2328_handler) # Путь для 2328.io
    
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    await bot_task

if __name__ == "__main__":
    asyncio.run(main())
