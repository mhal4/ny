import asyncio
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, LabeledPrice
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiohttp import web
import pandas as pd
from datetime import datetime, timedelta
import json
import uuid

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = "8406739433:AAGyexTjkz8yqBsiY-b8ItlEyrFEux9PohI"  # ← ВСТАВЬ СВОЙ ТОКЕН!
ADMIN_CHAT_ID = 1062092565  # ← ТВОЙ TELEGRAM ID
EXCEL_FILE = "orders.xlsx"  # Файл с оплаченными заказами
TEMP_ORDERS_FILE = "temp_orders.json"  # Временные заказы до оплаты

# === МАКСИМАЛЬНОЕ КОЛ-ВО ПАР ПО ГОРОДАМ ===
CITIES = {"Москва": 50, "СПб": 27}
sale = 0.7 if datetime.now() < datetime(2025, 12, 1) else 1
# === ИНИЦИАЛИЗАЦИЯ ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# === СОСТОЯНИЯ (FSM) ===
class OrderForm(StatesGroup):
    address = State()
    children_count = State()
    child_name = State()
    phone = State()
    comments = State()


# === УПРАВЛЕНИЕ ВРЕМЕННЫМИ ЗАКАЗАМИ ===
def save_temp_order(order_id, data):
    """
    Сохраняет временный заказ до оплаты
    """
    orders = {}
    if os.path.exists(TEMP_ORDERS_FILE):
        with open(TEMP_ORDERS_FILE, "r", encoding="utf-8") as f:
            orders = json.load(f)
    orders[order_id] = data
    with open(TEMP_ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)


def get_temp_order(order_id):
    """
    Возвращает временный заказ по ID
    """
    if not os.path.exists(TEMP_ORDERS_FILE):
        return None
    with open(TEMP_ORDERS_FILE, "r", encoding="utf-8") as f:
        orders = json.load(f)
    return orders.get(order_id)


def confirm_order_from_temp(order_id):
    """
    Подтверждает заказ (удаляет из временных, записывает в Excel)
    """
    temp_data = get_temp_order(order_id)
    if not temp_data:
        return False

    # Удаляем из временных
    orders = {}
    if os.path.exists(TEMP_ORDERS_FILE):
        with open(TEMP_ORDERS_FILE, "r", encoding="utf-8") as f:
            orders = json.load(f)
    if order_id in orders:
        del orders[order_id]
    with open(TEMP_ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)

    # Сохраняем в Excel
    save_order_to_excel(temp_data)
    return True


# === ЧТЕНИЕ ИЗ EXCEL ===
def load_orders():
    """
    Загружает все оплаченные заказы из Excel
    """
    if not os.path.exists(EXCEL_FILE):
        return pd.DataFrame()
    return pd.read_excel(EXCEL_FILE)


# === ПРОВЕРКА: СКОЛЬКО ПАР ЗАНЯТО НА ДАТУ/ВРЕМЯ/ГОРОД ===
def get_booked_slots():
    """
    Возвращает словарь: { 'дата время': { 'Москва': 3, 'СПб': 1 } }
    """
    df = load_orders()
    if df.empty:
        return {}
    booked = {}
    for _, row in df.iterrows():
        city = row.get("Город", "Москва")  # По умолчанию Москва
        date_time = f"{row['Дата визита']} {row['Время визита']}"
        if date_time not in booked:
            booked[date_time] = {}
        booked[date_time][city] = booked[date_time].get(city, 0) + 1
    return booked


def is_slot_available(date_str, time_str, city):
    """
    Проверяет, доступно ли время в городе
    """
    slot = f"{date_str} {time_str}"
    booked = get_booked_slots()
    booked_count = booked.get(slot, {}).get(city, 0)
    max_slots = CITIES.get(city, 50)
    return booked_count < max_slots


def find_next_available_slots(start_date_str, city):
    """
    Находит ближайшие доступные даты/время
    """
    try:
        today = datetime.strptime(start_date_str, "%d %B %Y")
    except:
        try:
            today = datetime.strptime(start_date_str, "%d.%m.%Y")
        except:
            today = datetime.now()

    available = []
    for i in range(1, 8):
        next_day = today + timedelta(days=i)
        date_str = next_day.strftime("%d %B %Y")
        for hour in [14, 15, 16, 17, 18, 19, 20, 21]:
            time_str = f"{hour:02d}:00"
            if is_slot_available(date_str, time_str, city):
                available.append(f"{date_str}, {time_str}")
                if len(available) >= 3:
                    break
        if len(available) >= 3:
            break

    return available


# === ОБНОВЛЁННАЯ ФУНКЦИЯ РАСЧЁТА ЦЕНЫ (с фиксированными ценами для 1 января ночью) ===
def get_price(date_str, time_str, program_type):
    """
    Возвращает цену по дате, времени и типу программы
    - Экспресс (10 мин) — цены из фото (условно)
    - Стандарт (30 мин) — цены из текста (условно)
    - Расширенный (1 час) — цены из текста (условно)
    """
    from datetime import datetime

    try:
        if "." in date_str:
            dt = datetime.strptime(date_str, "%d.%m.%Y")
        else:
            dt = datetime.strptime(date_str, "%d %B %Y")

        # Извлекаем час из time_str
        time_parts = time_str.split(":")
        if len(time_parts) < 2:
            print(f"Ошибка: Неверный формат времени '{time_str}'")
            return 0
        hour = int(time_parts[0])

        # Цены для Экспресса (10 мин)
        if program_type == "Экспресс (10 мин)":
            if dt < datetime(2025, 12, 25):
                return 5600 * sale
            elif dt <= datetime(2025, 12, 27):
                return 6400 * sale
            elif dt == datetime(2025, 12, 28):
                return 7000 * sale
            elif dt == datetime(2025, 12, 29):
                return 5475 * sale
            elif dt == datetime(2025, 12, 30):
                return 5175 * sale
            elif dt == datetime(2025, 12, 31):
                if 9 <= hour < 14:
                    return 7700 * sale
                elif 14 <= hour < 16:
                    return 8150 * sale
                elif 16 <= hour < 19:
                    return 11975 * sale
                elif 19 <= hour < 21:
                    return 13800 * sale
                elif 21 <= hour < 23:
                    return 14925 * sale  # Исправлено: 13900 -> 14925 для 21-23
                elif 23 <= hour:  # 23:00-00:00 31 декабря
                    return 25200 * sale
            elif dt.month == 1 and dt.day == 1:  # 1 января
                if 0 <= hour < 3:  # 00:00-02:59
                    return (
                        25200 * sale
                    )  # Используем высокую цену как для 31 декабря ночью
                elif 3 <= hour < 6:  # 03:00-05:59
                    return 15000 * sale  # Исправлено: 9000 -> 15000
                elif dt.day in [1, 2]:  # 06:00 и далее 1 и 2 января
                    return 7000 * sale
                elif 3 <= dt.day <= 7:
                    return 5600 * sale
                else:
                    return 5000 * sale
            elif dt.month == 1 and dt.day in [2]:
                return 7000 * sale
            elif dt.month == 1 and 3 <= dt.day <= 7:
                return 5600 * sale
            else:
                return 5000 * sale

        # Цены для Стандарта (30 мин)
        elif program_type == "Стандарт (30 мин)":
            if dt < datetime(2025, 12, 25):
                return 7400 * sale
            elif dt <= datetime(2025, 12, 27):
                return 8000 * sale
            elif dt == datetime(2025, 12, 28):
                return 8400 * sale  # Исправлено: 8000 -> 8400
            elif dt == datetime(2025, 12, 29):
                return 6525 * sale
            elif dt == datetime(2025, 12, 30):
                return 6150 * sale
            elif dt == datetime(2025, 12, 31):
                if 9 <= hour < 14:
                    return 8675 * sale
                elif 14 <= hour < 16:
                    return 9050 * sale
                elif 16 <= hour < 19:
                    return 13400 * sale
                elif 19 <= hour < 21:
                    return 15150 * sale
                elif 21 <= hour < 23:
                    return 16050 * sale
                elif 23 <= hour:  # 23:00-00:00 31 декабря
                    return 26250 * sale
            elif dt.month == 1 and dt.day == 1:  # 1 января
                if 0 <= hour < 3:  # 00:00-02:59
                    return (150000 / 2) * sale  # Цена за 1 час -> 30 мин
                elif 3 <= hour < 6:  # 03:00-05:59
                    return (90000 / 2) * sale  # Цена за 1 час -> 30 мин
                elif dt.day in [1, 2]:  # 06:00 и далее 1 и 2 января
                    return 8500 * sale
                elif 3 <= dt.day <= 7:
                    return 7400 * sale
                else:
                    return 7000 * sale
            elif dt.month == 1 and dt.day in [2]:
                return 8500 * sale
            elif dt.month == 1 and 3 <= dt.day <= 7:
                return 7400 * sale
            else:
                return 7000 * sale

        # Цены для Расширенного (1 час)
        elif program_type == "Расширенный (1 час)":
            if dt < datetime(2025, 12, 25):
                return 17000 * sale
            elif dt <= datetime(2025, 12, 28):  # 25, 26, 27, 28
                return 17000 * sale
            elif dt <= datetime(2025, 12, 30):  # 29, 30
                return 22500 * sale
            elif dt == datetime(2025, 12, 31):  # 31 декабря
                return 50000 * sale
            elif dt.month == 1 and dt.day == 1:  # 1 января
                if 0 <= hour < 3:  # 00:00-02:59
                    return 150000 * sale
                elif 3 <= hour < 6:  # 03:00-05:59
                    return 90000 * sale
                else:  # 06:00-23:59
                    return 16000 * sale
            elif dt.month == 1 and dt.day in [2]:  # 2 января
                return 16000 * sale
            elif dt.month == 1 and dt.day in [3]:  # 3 января
                return 16000 * sale
            elif dt.month == 1 and 3 < dt.day <= 7:  # 4, 5, 6, 7 января
                return 12000 * sale
            else:
                return 17000 * sale

    except Exception as e:
        print(f"Ошибка в get_price: {e}")
        return 0


# === ИНЛАЙН-КЛАВИАТУРЫ ===


def get_cities_keyboard():
    """
    Клавиатура для выбора города
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="Москва", callback_data="city_moscow")
    kb.button(text="СПб", callback_data="city_spb")
    kb.adjust(1)
    return kb.as_markup()


# === ОБНОВЛЁННАЯ ФУНКЦИЯ ГЕНЕРАЦИИ ДАТ ===
def get_dates_keyboard():
    """
    Клавиатура с датами с 25.12.2025 по 07.01.2026
    """
    kb = InlineKeyboardBuilder()
    start_date = datetime(2025, 12, 25)
    end_date = datetime(2026, 1, 7)
    current = start_date
    while current <= end_date:
        day = current.strftime("%d %B %Y")
        kb.button(text=day, callback_data=f"date_{day}")
        current += timedelta(days=1)
    kb.adjust(2)
    return kb.as_markup()


# === ОБНОВЛЁННАЯ ФУНКЦИЯ ГЕНЕРАЦИИ КЛАВИАТУРЫ ПРОГРАММЫ ===
def get_programs_keyboard():
    """
    Клавиатура для выбора типа программы (синхронизирована с сайтом)
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="Экспресс (10 мин)", callback_data="program_10")
    kb.button(text="Стандарт (30 мин)", callback_data="program_30")
    kb.button(text="Расширенный (1 час)", callback_data="program_60")
    kb.adjust(1)
    return kb.as_markup()


# === ОБНОВЛЁННАЯ ФУНКЦИЯ ГЕНЕРАЦИИ ВРЕМЕННЫХ СЛОТОВ (с учётом новых типов программ и ночи) ===
def get_time_slots_keyboard(
    date_str, city, program_type
):  # Добавляем program_type как аргумент
    """
    Клавиатура с временными слотами (с ценой и оставшимися парами)
    Включает стандартные часы (14-21) и специальные для 31 декабря и 1 января (0-5, 23).
    """
    kb = InlineKeyboardBuilder()
    booked = get_booked_slots()
    max_slots = CITIES.get(city, 50)

    try:
        dt = datetime.strptime(date_str, "%d %B %Y")
    except:
        try:
            dt = datetime.strptime(date_str, "%d.%m.%Y")
        except:
            print(f"Ошибка: Невозможно распознать дату '{date_str}'")
            return kb.as_markup()

    standard_hours = [14, 15, 16, 17, 18, 19, 20, 21]
    night_hours_31 = [23]
    night_hours_1st = [0, 1, 2, 3, 4, 5]

    hours_to_generate = standard_hours[:]
    if dt.date() == datetime(2025, 12, 31).date():
        hours_to_generate.extend(night_hours_31)
    elif dt.date() == datetime(2026, 1, 1).date():
        hours_to_generate.extend(night_hours_1st)

    for hour in hours_to_generate:
        time_str = f"{hour:02d}:00"
        slot_key = f"{date_str} {time_str}"
        booked_count = booked.get(slot_key, {}).get(city, 0)
        available_count = max_slots - booked_count
        price = get_price(
            date_str, time_str, program_type
        )  # Передаём актуальный program_type

        if available_count > 0:
            kb.button(
                text=f"{time_str} — {price} ₽ (осталось {available_count})",
                callback_data=f"time_{time_str}",
            )
        else:
            kb.button(
                text=f"{time_str} — {price} ₽ (нет мест)",
                callback_data=f"unavailable_{time_str}",
            )

    kb.adjust(2)
    return


def get_payment_keyboard(price):
    """
    Клавиатура с кнопкой "Оплатить"
    """
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💳 Оплатить {price} ₽", url="https://yoomoney.ru/...")  # Заглушка
    kb.adjust(1)
    return kb.as_markup()


# === ОБРАБОТЧИКИ БОТА ===


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """
    Начало работы с ботом — выбор города
    """
    await message.answer(
        "🎄 Добро пожаловать! Выберите город:", reply_markup=get_cities_keyboard()
    )
    await state.set_data({})  # Сброс состояния


@dp.callback_query(F.data.startswith("city_"))
async def select_city(callback: CallbackQuery, state: FSMContext):
    """
    Выбор города через инлайн-кнопку
    """
    city = callback.data.replace("city_", "").title()
    await state.update_data(city=city)
    await callback.message.edit_text(
        f"🏙️ Вы выбрали {city}. Выберите дату:", reply_markup=get_dates_keyboard()
    )
    await callback.answer()


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК ВЫБОРА ДАТЫ (теперь запрашивает программу) ===
@dp.callback_query(F.data.startswith("date_"))
async def select_date(callback: CallbackQuery, state: FSMContext):
    """
    Выбор даты через инлайн-кнопку. Сохраняет дату и запрашивает программу.
    """
    date_str = callback.data.replace("date_", "")
    await state.update_data(date=date_str)
    await callback.message.edit_text(
        f"📅 Вы выбрали {date_str}. Теперь выберите тип программы:",
        reply_markup=get_programs_keyboard(),  # Показываем клавиатуру с программами
    )
    await callback.answer()


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК ВЫБОРА ПРОГРАММЫ (теперь запрашивает время) ===
@dp.callback_query(F.data.startswith("program_"))
async def select_program(callback: CallbackQuery, state: FSMContext):
    """
    Выбор программы. Сохраняет программу и запрашивает время.
    """
    program_map = {
        "program_10": "Экспресс (10 мин)",
        "program_30": "Стандарт (30 мин)",
        "program_60": "Расширенный (1 час)",
    }
    program_type = program_map.get(callback.data)
    if not program_type:
        return
    await state.update_data(program_type=program_type)
    data = await state.get_data()
    date_str = data["date"]
    city = data["city"]

    # Генерируем слоты времени с учётом выбранной программы
    await callback.message.edit_text(
        f"🎯 Вы выбрали {program_type}. Теперь выберите время:",
        reply_markup=get_time_slots_keyboard(date_str, city, program_type),
    )
    await callback.answer()


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК ВЫБОРА ВРЕМЕНИ (теперь завершает выбор и показывает цену) ===
@dp.callback_query(F.data.startswith("time_"))
async def select_time(callback: CallbackQuery, state: FSMContext):
    """
    Выбор времени через инлайн-кнопку. Сохраняет время и показывает цену.
    """
    time_str = callback.data.replace("time_", "")
    await state.update_data(time=time_str)
    data = await state.get_data()
    price = get_price(data["date"], time_str, data["program_type"])
    await state.update_data(price=price)

    await callback.message.edit_text(
        f"⏰ Вы выбрали {time_str}. Цена: {price} ₽\n\nВведите адрес:"
    )
    await state.set_state(OrderForm.address)
    await callback.answer()


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК "НЕТ МЕСТ" (теперь с программой) ===
@dp.callback_query(F.data.startswith("unavailable_"))
async def unavailable_time(callback: CallbackQuery, state: FSMContext):
    """
    Обработка нажатия на "занятое" время. Показывает сообщение с учётом программы.
    """
    data = await state.get_data()
    program_type = data.get("program_type", "неизвестно")
    await callback.answer(
        f"❌ На это время нет свободных артистов для '{program_type}'. Выберите другое.",
        show_alert=True,
    )


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
    await message.answer(
        "📞 Введите ваш контактный телефон (с кодом страны, например: +79991234567):"
    )
    await state.set_state(OrderForm.phone)


@dp.message(OrderForm.phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if len(phone) < 10 or not phone.startswith(("+7", "8")):
        await message.answer(
            "❗ Неверный формат телефона. Введите в формате: +79991234567"
        )
        return
    await state.update_data(phone=phone)
    await message.answer(
        "💬 Есть пожелания? (например: 'Хочу песню про снег, и чтобы Дед Мороз рассказал сказку про лису')\n(можно пропустить — напишите «нет»)"
    )
    await state.set_state(OrderForm.comments)


@dp.message(OrderForm.comments)
async def process_comments(message: Message, state: FSMContext):
    await state.update_data(
        comments=message.text if message.text.lower() != "нет" else "-"
    )
    data = await state.get_data()

    # Генерируем ID для временного заказа
    order_id = str(uuid.uuid4())
    temp_data = {**data, "order_id": order_id}
    save_temp_order(order_id, temp_data)

    price = data["price"]
    kb = get_payment_keyboard(price)

    await message.answer(
        f"🎉 Заказ готов к оплате!\n\n"
        f"Кого: Дед Мороз и Снегурочка\n"
        f"Город: {data['city']}\n"
        f"Дата: {data['date']}\n"
        f"Время: {data['time']}\n"
        f"Программа: {data['program_type']}\n"
        f"Цена: {price} ₽\n"
        f"Адрес: {data['address']}\n"
        f"Детей: {data['children_count']}\n"
        f"Имя: {data['child_name']}\n"
        f"Телефон: {data['phone']}\n"
        f"Пожелания: {data['comments']}\n\n"
        f"Нажмите кнопку ниже для оплаты:",
        reply_markup=kb,
    )
    await state.clear()


# === ОПЛАТА ЧЕРЕЗ БОТА (не используется, т.к. через внешний сервис) ===
@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    # TODO: Реализовать связывание платежа с order_id
    await message.answer("🎉 Спасибо за оплату! Заказ принят и отправлен в обработку.")
    # Здесь нужно найти order_id по платежу и вызвать confirm_order_from_temp(order_id)


# === СОХРАНЕНИЕ В EXCEL ===
def save_order_to_excel(data):
    """
    Записывает оплаченный заказ в Excel
    """
    df = pd.DataFrame()
    if os.path.exists(EXCEL_FILE):
        df = pd.read_excel(EXCEL_FILE)

    new_row = {
        "Дата и время заказа": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "Кого пригласить": "Дед Мороз и Снегурочка",  # Всегда пара
        "Город": data.get("city", "Москва"),
        "Дата визита": data["date"],
        "Время визита": data["time"],
        "Тип программы": data["program_type"],
        "Длительность": 15 if data["program_type"] == "Экспресс (15 мин)" else 30,
        "Цена": data["price"],
        "Адрес": data["address"],
        "Количество детей": int(data["children_count"]),
        "Имя ребёнка": data["child_name"],
        "Телефон": data["phone"],
        "Пожелания": data["comments"],
    }

    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    df.to_excel(EXCEL_FILE, index=False)


# === ВЕБ-СЕРВЕР (для сайта) ===
async def handle_temp_order(request):
    """
    Принимает временный заказ от сайта
    """
    try:
        data = await request.json()
        order_id = str(uuid.uuid4())
        temp_data = {**data, "order_id": order_id}
        save_temp_order(order_id, temp_data)
        return web.json_response({"status": "ok", "order_id": order_id})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_confirm_order(request):
    """
    Подтверждает заказ (записывает в Excel)
    """
    try:
        data = await request.json()
        order_id = data.get("order_id")
        if confirm_order_from_temp(order_id):
            return web.json_response({"status": "ok", "message": "Заказ подтверждён!"})
        else:
            return web.json_response({"error": "Заказ не найден"}, status=404)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_price(request):
    """
    Возвращает цену по дате, времени и программе
    """
    date = request.query.get("date", "")
    time = request.query.get("time", "")
    program_type = request.query.get("program_type", "Экспресс (15 мин)")
    price = get_price(date, time, program_type)
    return web.json_response({"price": price})


async def handle_download(request):
    """
    Скачивание Excel-файла
    """
    return web.FileResponse(EXCEL_FILE)


async def handle_index(request):
    """
    Главная страница сайта
    """
    return web.FileResponse("/opt/render/project/src/index.html")


async def web_app():
    """
    Настройка веб-сервера
    """
    app = web.Application()
    app.router.add_post("/api/temp_order", handle_temp_order)
    app.router.add_post("/api/confirm_order", handle_confirm_order)
    app.router.add_get("/api/price", handle_price)
    app.router.add_get("/download", handle_download)
    app.router.add_get("/", handle_index)
    return app


# === ЗАПУСК ===
async def main():
    web_app_instance = await web_app()
    runner = web.AppRunner(web_app_instance)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    await site.start()

    print(f"🌐 Веб-сервер запущен на http://0.0.0.0:8080")
    print(f"📥 Скачать Excel: http://твой-сайт.onrender.com/download")

    await dp.start_polling(bot)


if __name__ == "__main__":
    # Создаём файлы, если их нет
    if not os.path.exists(EXCEL_FILE):
        pd.DataFrame(
            columns=[
                "Дата и время заказа",
                "Кого пригласить",
                "Город",
                "Дата визита",
                "Время визита",
                "Тип программы",
                "Длительность",
                "Цена",
                "Адрес",
                "Количество детей",
                "Имя ребёнка",
                "Телефон",
                "Пожелания",
            ]
        ).to_excel(EXCEL_FILE, index=False)
        print(f"✅ Создан файл {EXCEL_FILE}")

    if not os.path.exists(TEMP_ORDERS_FILE):
        with open(TEMP_ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
        print(f"✅ Создан файл {TEMP_ORDERS_FILE}")

    asyncio.run(main())
