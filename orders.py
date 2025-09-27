import asyncio
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiohttp import web
import pandas as pd
from datetime import datetime, timedelta
import re

# === КОНФИГ ===
BOT_TOKEN = "8406739433:AAGyexTjkz8yqBsiY-b8ItlEyrFEux9PohI"  # ← ВСТАВЬ СВОЙ ТОКЕН!
ADMIN_CHAT_ID = 1062092565  # ← ТВОЙ TELEGRAM ID
EXCEL_FILE = "orders.xlsx"
MAX_ORDERS_PER_DAY = 3  # ← МАКСИМАЛЬНОЕ КОЛ-ВО ЗАКАЗОВ В ДЕНЬ

# === ИНИЦИАЛИЗАЦИЯ ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# === СОСТОЯНИЯ (FSM) ===
class OrderForm(StatesGroup):
    character = State()
    date = State()
    time = State()
    address = State()
    children_count = State()
    child_name = State()
    phone = State()
    comments = State()

# === КЛАВИАТУРЫ ===
def get_character_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="Дед Мороз")
    kb.button(text="Снегурочка")
    kb.button(text="Дед Мороз и Снегурочка")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)

def get_confirm_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить заказ", callback_data="confirm")
    kb.button(text="🔄 Заполнить заново", callback_data="restart")
    kb.adjust(1)
    return kb.as_markup()

# === ЧТЕНИЕ ИЗ EXCEL ===
def load_orders():
    if not os.path.exists(EXCEL_FILE):
        return pd.DataFrame()
    return pd.read_excel(EXCEL_FILE)

# === ПРОВЕРКА: ЗАНЯТО ЛИ ВРЕМЯ? ===
def get_booked_slots():
    df = load_orders()
    if df.empty:
        return {}
    # Группируем по дате и времени
    booked = {}
    for _, row in df.iterrows():
        date_time = f"{row['Дата визита']} {row['Время визита']}"
        booked[date_time] = booked.get(date_time, 0) + 1
    return booked

def is_slot_available(date_str, time_str):
    slot = f"{date_str} {time_str}"
    booked = get_booked_slots()
    count = booked.get(slot, 0)
    return count < MAX_ORDERS_PER_DAY

# === НАЙТИ БЛИЖАЙШИЕ ДОСТУПНЫЕ ДАТЫ ===
def find_next_available_slots(start_date_str):
    # Парсим дату
    try:
        today = datetime.strptime(start_date_str, "%d %B %Y")  # Например: "24 декабря 2024"
    except:
        try:
            today = datetime.strptime(start_date_str, "%d.%m.%Y")  # "24.12.2024"
        except:
            today = datetime.now()

    # Генерируем ближайшие 7 дней
    available = []
    for i in range(1, 8):  # От следующего дня до 7 дней вперёд
        next_day = today + timedelta(days=i)
        date_str = next_day.strftime("%d %B %Y")  # "25 декабря 2024"
        # Проверяем все возможные времена (например, 14:00, 15:00, 16:00)
        for hour in [14, 15, 16]:
            time_str = f"{hour:02d}:00"
            slot = f"{date_str} {time_str}"
            if is_slot_available(date_str, time_str):
                available.append(f"{date_str}, {time_str}")
                if len(available) >= 3:  # Предлагаем максимум 3 варианта
                    break
        if len(available) >= 3:
            break

    return available

# === ОБРАБОТЧИКИ БОТА ===

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await message.answer(
        "🎄 Добро пожаловать в бота предзаказа Деда Мороза и Снегурочки!\n\n"
        "Выберите, кого хотите пригласить:",
        reply_markup=get_character_kb()
    )
    await state.set_state(OrderForm.character)

@dp.message(OrderForm.character)
async def process_character(message: Message, state: FSMContext):
    if message.text not in ["Дед Мороз", "Снегурочка", "Дед Мороз и Снегурочка"]:
        await message.answer("Пожалуйста, выберите вариант из кнопок ниже.")
        return
    await state.update_data(character=message.text)
    await message.answer("📅 Введите дату визита (например: 20 декабря 2024 или 20.12.2024):")
    await state.set_state(OrderForm.date)

@dp.message(OrderForm.date)
async def process_date(message: Message, state: FSMContext):
    date_str = message.text.strip()
    # Сохраняем дату для последующего использования
    await state.update_data(date=date_str)
    
    # Проверяем, есть ли уже 3 заказа на эту дату
    booked = get_booked_slots()
    # Попробуем найти совпадение по дате (без времени)
    found = False
    for slot in booked:
        if date_str in slot:  # Дата частично совпадает
            found = True
            break

    if found and booked.get(f"{date_str} 14:00", 0) >= MAX_ORDERS_PER_DAY:
        # Дата уже полностью занята
        available = find_next_available_slots(date_str)
        if available:
            msg = f"❌ На {date_str} все места заняты (максимум {MAX_ORDERS_PER_DAY} заказов в день).\n\n"
            msg += "Доступны следующие даты:\n"
            for a in available:
                msg += f"• {a}\n"
            msg += "\nПожалуйста, выберите другую дату."
            await message.answer(msg)
        else:
            await message.answer(f"❌ На {date_str} все места заняты. Попробуйте выбрать позже.")
        return  # Останавливаем流程 — не переходим к времени!

    # Если не занято — спрашиваем время
    await message.answer("⏰ Введите время визита (например: 14:00, 15:00, 16:00):")
    await state.set_state(OrderForm.time)

@dp.message(OrderForm.time)
async def process_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    data = await state.get_data()
    date_str = data['date']

    # Проверяем, доступно ли именно это время
    if not is_slot_available(date_str, time_str):
        available = find_next_available_slots(date_str)
        if available:
            msg = f"❌ На {date_str} в {time_str} все места заняты (максимум {MAX_ORDERS_PER_DAY} заказов).\n\n"
            msg += "Доступны следующие даты и время:\n"
            for a in available:
                msg += f"• {a}\n"
            msg += "\nПожалуйста, выберите другое время или дату."
            await message.answer(msg)
        else:
            await message.answer(f"❌ На {date_str} в {time_str} все места заняты. Попробуйте выбрать позже.")
        return  # Не продолжаем!

    # Если всё ок — сохраняем время
    await state.update_data(time=time_str)
    await message.answer("📍 Введите адрес проведения мероприятия (полный адрес):")
    await state.set_state(OrderForm.address)

@dp.message(OrderForm.address)
async def process_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)
    await message.answer("🧒 Сколько детей будет на мероприятии? (например: 15)")
    await state.set_state(OrderForm.children_count)

@dp.message(OrderForm.children_count)
async def process_children_count(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите число (например: 12)")
        return
    await state.update_data(children_count=message.text)
    await message.answer("👶 Как зовут главного ребёнка? (для персонализации)")
    await state.set_state(OrderForm.child_name)

@dp.message(OrderForm.child_name)
async def process_child_name(message: Message, state: FSMContext):
    await state.update_data(child_name=message.text)
    await message.answer("📞 Введите ваш контактный телефон (с кодом страны, например: +79991234567):")
    await state.set_state(OrderForm.phone)

@dp.message(OrderForm.phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if len(phone) < 10 or not phone.startswith(('+7', '8')):
        await message.answer("❗ Неверный формат телефона. Введите в формате: +79991234567")
        return
    await state.update_data(phone=phone)
    await message.answer("💬 Есть пожелания? (например: 'Хочу песню про снег, и чтобы Дед Мороз рассказал сказку про лису')\n(можно пропустить — напишите «нет»)")
    await state.set_state(OrderForm.comments)

@dp.message(OrderForm.comments)
async def process_comments(message: Message, state: FSMContext):
    await state.update_data(comments=message.text if message.text.lower() != "нет" else "-")
    data = await state.get_data()
    save_order_to_excel(data)
    await message.answer(f"🎉 Заказ принят! С вами свяжутся в ближайшее время.\n\n"
                         f"Вы можете также оформить заказ через сайт: http://ny-bvfm.render.com")
    await bot.send_message(
        ADMIN_CHAT_ID,
        f"🔔 НОВЫЙ ЗАКАЗ!\n\n{format_order_for_admin(data)}"
    )
    await state.clear()

@dp.callback_query(F.data == "confirm")
async def confirm_order(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    save_order_to_excel(data)
    await callback.message.edit_text("🎉 Заказ принят! С вами свяжутся в ближайшее время.")
    await bot.send_message(
        ADMIN_CHAT_ID,
        f"🔔 НОВЫЙ ЗАКАЗ!\n\n{format_order_for_admin(data)}"
    )
    await state.clear()

@dp.callback_query(F.data == "restart")
async def restart_order(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔄 Начнём заново!")
    await cmd_start(callback.message, state)

# === СОХРАНЕНИЕ В EXCEL ===
def save_order_to_excel(data):
    df = pd.DataFrame()
    if os.path.exists(EXCEL_FILE):
        df = pd.read_excel(EXCEL_FILE)

    new_row = {
        "Дата и время заказа": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "Кого пригласить": data['character'],
        "Дата визита": data['date'],
        "Время визита": data['time'],
        "Адрес": data['address'],
        "Количество детей": int(data['children_count']),
        "Имя ребёнка": data['child_name'],
        "Телефон": data['phone'],
        "Пожелания": data['comments']
    }

    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    df.to_excel(EXCEL_FILE, index=False)

def format_order_for_admin(data):
    return f"""
🆕 НОВЫЙ ЗАКАЗ:
Кого: {data['character']}
Дата: {data['date']}
Время: {data['time']}
Адрес: {data['address']}
Детей: {data['children_count']}
Имя ребёнка: {data['child_name']}
Телефон: {data['phone']}
Пожелания: {data['comments']}
    """

# === ВЕБ-СЕРВЕР (для сайта) ===
async def handle_order(request):
    try:
        data = await request.json()
        required = ['character', 'date', 'time', 'address', 'children_count', 'child_name', 'phone', 'comments']
        if not all(k in data for k in required):
            return web.json_response({"error": "Недостаточно данных"}, status=400)

        # Проверка доступности
        if not is_slot_available(data['date'], data['time']):
            available = find_next_available_slots(data['date'])
            return web.json_response({
                "error": "Выбранное время занято",
                "available": available,
                "max_per_day": MAX_ORDERS_PER_DAY
            }, status=409)  # Conflict

        save_order_to_excel(data)
        return web.json_response({"status": "ok", "message": "Заказ принят!"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_download(request):
    return web.FileResponse(EXCEL_FILE)

async def web_app():
    app = web.Application()
    app.router.add_post('/api/order', handle_order)
    app.router.add_get('/download', handle_download)
    app.router.add_get('/api/check', handle_check_availability)
    app.router.add_get('/', handle_index)  # ← ✅ ДОБАВЛЕНО!
    return app

# === ЗАПУСК ===
async def main():
    # Запускаем веб-сервер
    web_app_instance = await web_app()
    runner = web.AppRunner(web_app_instance)
    await runner.setup()
    site = web.TCPSite(runner, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
    await site.start()

    print(f"🌐 Веб-сервер запущен на https://ny-bvfm.onrender.com")
    print(f"📥 Скачать Excel: http://ny-bvfm.onrender.com/download")

    # Запускаем бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    # Создаём пустой Excel, если его нет
    if not os.path.exists(EXCEL_FILE):
        pd.DataFrame(columns=[
            "Дата и время заказа",
            "Кого пригласить",
            "Дата визита",
            "Время визита",
            "Адрес",
            "Количество детей",
            "Имя ребёнка",
            "Телефон",
            "Пожелания"
        ]).to_excel(EXCEL_FILE, index=False)
        print(f"✅ Создан файл {EXCEL_FILE}")

    asyncio.run(main())