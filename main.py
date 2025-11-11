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
USER_ORDERS_FILE = "user_orders.json"  # Связь chat_id пользователя и order_id
MANAGERS_FILE = "managers.json"  # Список chat_id менеджеров
LAST_CLIENT_CHAT_FILE = "last_client_chat.json"  # Хранит последний chat_id клиента, которому писал менеджер (для /reply)

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


class SupportForm(StatesGroup):
    waiting_for_order_id = State()


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
            today = datetime.strptime(start_date_str, "%Y-%m-%d")

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


# === ОБНОВЛЁННАЯ ФУНКЦИЯ РАСЧЁТА ЦЕНЫ (с ночью 31.12 -> 01.01) ===
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
        elif "-" in date_str:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        elif "/" in date_str:
            dt = datetime.strptime(date_str, "%m/%d/%Y")
        else:
            dt = datetime.strptime(date_str, "%d %B %Y")

        # Извлекаем час из time_str
        time_parts = time_str.split(":")
        if len(time_parts) < 2:
            print(f"Ошибка: Неверный формат времени '{time_str}'")
            return 0
        hour = int(time_parts[0])

        # Цены для Экспресса (10 мин) — условно из фото
        if program_type == "Экспресс (10 мин)":
            if dt < datetime(2025, 12, 25):
                return round(5600 * sale)
            elif dt <= datetime(2025, 12, 27):
                return round(6400 * sale)
            elif dt == datetime(2025, 12, 28):
                return round(7000 * sale)
            elif dt == datetime(2025, 12, 29):
                return round(5475 * sale)
            elif dt == datetime(2025, 12, 30):
                return round(5175 * sale)
            elif dt == datetime(2025, 12, 31):
                if 9 <= hour < 14:
                    return round(7700 * sale)
                elif 14 <= hour < 16:
                    return round(8150 * sale)
                elif 16 <= hour < 19:
                    return round(11975 * sale)
                elif 19 <= hour < 21:
                    return round(13800 * sale)
                elif 21 <= hour < 23:
                    return round(14925 * sale)  # Исправлено: 13900 -> 14925 для 21-23
                elif 23 <= hour:  # 23:00-00:00 31 декабря
                    return round(25200 * sale)
            elif dt.month == 1 and dt.day == 1:  # 1 января
                if 0 <= hour < 3:  # 00:00-02:59
                    return round(
                        25200 * sale
                    )  # Используем высокую цену как для 31 декабря ночью
                elif 3 <= hour < 6:  # 03:00-05:59
                    return round(15000 * sale)  # Исправлено: 9000 -> 15000
                elif dt.day in [1, 2]:  # 06:00 и далее 1 и 2 января
                    return round(7000 * sale)
                elif 3 <= dt.day <= 7:
                    return round(5600 * sale)
                else:
                    return round(5000 * sale)
            elif dt.month == 1 and dt.day in [2]:
                return round(7000 * sale)
            elif dt.month == 1 and 3 <= dt.day <= 7:
                return round(5600 * sale)
            else:
                return round(5000 * sale)

        # Цены для Стандарта (30 мин) — как "классика" из текста
        elif program_type == "Стандарт (30 мин)":
            if dt < datetime(2025, 12, 25):
                return round(7400 * sale)
            elif dt <= datetime(2025, 12, 27):
                return round(8000 * sale)
            elif dt == datetime(2025, 12, 28):
                return round(8400 * sale)  # Исправлено: 8000 -> 8400
            elif dt == datetime(2025, 12, 29):
                return round(6525 * sale)
            elif dt == datetime(2025, 12, 30):
                return round(6150 * sale)
            elif dt == datetime(2025, 12, 31):
                if 9 <= hour < 14:
                    return round(8675 * sale)
                elif 14 <= hour < 16:
                    return round(9050 * sale)
                elif 16 <= hour < 19:
                    return round(13400 * sale)
                elif 19 <= hour < 21:
                    return round(15150 * sale)
                elif 21 <= hour < 23:
                    return round(16050 * sale)
                elif 23 <= hour:  # 23:00-00:00 31 декабря
                    return round(26250 * sale)
            elif dt.month == 1 and dt.day == 1:  # 1 января
                if 0 <= hour < 3:  # 00:00-02:59
                    return round((150000 / 2) * sale)  # Цена за 1 час -> 30 мин
                elif 3 <= hour < 6:  # 03:00-05:59
                    return round((90000 / 2) * sale)  # Цена за 1 час -> 30 мин
                elif dt.day in [1, 2]:  # 06:00 и далее 1 и 2 января
                    return round(8500 * sale)
                elif 3 <= dt.day <= 7:
                    return round(7400 * sale)
                else:
                    return round(7000 * sale)
            elif dt.month == 1 and dt.day in [2]:
                return round(8500 * sale)
            elif dt.month == 1 and 3 <= dt.day <= 7:
                return round(7400 * sale)
            else:
                return round(7000 * sale)

        # Цены для Расширенного (1 час) — условно выше
        elif program_type == "Расширенный (1 час)":
            if dt < datetime(2025, 12, 25):
                return round(17000 * sale)
            elif dt <= datetime(2025, 12, 28):  # 25, 26, 27, 28
                return round(17000 * sale)
            elif dt <= datetime(2025, 12, 30):  # 29, 30
                return round(22500 * sale)
            elif dt == datetime(2025, 12, 31):  # 31 декабря
                return round(50000 * sale)
            elif dt.month == 1 and dt.day == 1:  # 1 января
                if 0 <= hour < 3:  # 00:00-02:59
                    return round(150000 * sale)
                elif 3 <= hour < 6:  # 03:00-05:59
                    return round(90000 * sale)
                else:  # 09:00-23:59
                    return round(16000 * sale)  # "С 1 -3 января 16000"
            elif dt.month == 1 and dt.day in [2]:  # 2 января
                return round(16000 * sale)
            elif dt.month == 1 and dt.day in [3]:  # 3 января
                return round(16000 * sale)
            elif dt.month == 1 and 3 < dt.day <= 7:  # 4, 5, 6, 7 января
                return round(12000 * sale)
            else:
                return round(17000 * sale)

    except Exception as e:
        print(f"Ошибка в get_price: {e}")
        return 0


# === УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ И МЕНЕДЖЕРАМИ ===
def get_user_order(chat_id):
    """Возвращает order_id для chat_id, если есть"""
    if not os.path.exists(USER_ORDERS_FILE):
        return None
    with open(USER_ORDERS_FILE, "r", encoding="utf-8") as f:
        user_orders = json.load(f)
    return user_orders.get(str(chat_id))


def set_user_order(chat_id, order_id):
    """Сохраняет связь chat_id -> order_id"""
    user_orders = {}
    if os.path.exists(USER_ORDERS_FILE):
        with open(USER_ORDERS_FILE, "r", encoding="utf-8") as f:
            user_orders = json.load(f)
    user_orders[str(chat_id)] = order_id
    with open(USER_ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(user_orders, f, ensure_ascii=False, indent=2)


def get_managers():
    """Возвращает список chat_id менеджеров"""
    if not os.path.exists(MANAGERS_FILE):
        return []
    with open(MANAGERS_FILE, "r", encoding="utf-8") as f:
        managers = json.load(f)
    return managers


def add_manager(chat_id):
    """Добавляет chat_id в список менеджеров"""
    managers = get_managers()
    if str(chat_id) not in managers:
        managers.append(str(chat_id))
        with open(MANAGERS_FILE, "w", encoding="utf-8") as f:
            json.dump(managers, f, ensure_ascii=False, indent=2)
        return True
    return False


def get_last_client_chat(manager_chat_id):
    """Возвращает последний chat_id клиента, которому писал менеджер"""
    if not os.path.exists(LAST_CLIENT_CHAT_FILE):
        return None
    with open(LAST_CLIENT_CHAT_FILE, "r", encoding="utf-8") as f:
        last_chats = json.load(f)
    return last_chats.get(str(manager_chat_id))


def set_last_client_chat(manager_chat_id, client_chat_id):
    """Сохраняет последний chat_id клиента для менеджера"""
    last_chats = {}
    if os.path.exists(LAST_CLIENT_CHAT_FILE):
        with open(LAST_CLIENT_CHAT_FILE, "r", encoding="utf-8") as f:
            last_chats = json.load(f)
    last_chats[str(manager_chat_id)] = str(client_chat_id)
    with open(LAST_CLIENT_CHAT_FILE, "w", encoding="utf-8") as f:
        json.dump(last_chats, f, ensure_ascii=False, indent=2)


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


def get_time_slots_keyboard(date_str, city, program_type):
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
            return kb.as_markup()  # Возвращаем пустую клавиатуру при ошибке

    # Список часов для генерации слотов
    standard_hours = [14, 15, 16, 17, 18, 19, 20, 21]
    night_hours_31 = [23]  # 23:00-00:00
    night_hours_1st = [0, 1, 2, 3, 4, 5]  # 00:00-01:00, 01:00-02:00, ..., 05:00-06:00

    hours_to_generate = standard_hours[:]
    if dt.date() == datetime(2025, 12, 31).date():
        hours_to_generate.extend(night_hours_31)
    elif dt.date() == datetime(2026, 1, 1).date():  # 1 января
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
    return kb.as_markup()


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


def get_payment_keyboard(price):
    """
    Клавиатура с кнопкой "Оплатить"
    """
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💳 Оплатить {price} ₽", url="https://yoomoney.ru/...")  # Заглушка
    kb.adjust(1)
    return kb.as_markup()


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК /start ===
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """
    Начало работы с ботом — выбор: сделать заказ или ввести ID
    """
    # Проверяем, является ли пользователь админом
    if message.from_user.id == ADMIN_CHAT_ID:
        kb = InlineKeyboardBuilder()
        kb.button(text="📝 Сделать заказ", callback_data="new_order")
        kb.button(text="🔑 Ввести ID заказа", callback_data="use_id")
        kb.button(text="➕ Добавить менеджера", callback_data="add_manager_cmd")
        kb.adjust(1)
        await message.answer(
            "🎄 Привет, админ! Выберите действие:", reply_markup=kb.as_markup()
        )
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text="📝 Сделать заказ", callback_data="new_order")
        kb.button(text="🔑 Ввести ID заказа", callback_data="use_id")
        kb.adjust(1)
        await message.answer(
            "🎄 Добро пожаловать! Выберите действие:", reply_markup=kb.as_markup()
        )
    await state.set_data({})  # Сброс состояния
    await state.clear()  # Полная очистка


# === НОВЫЙ ОБРАБОТЧИК КНОПКИ "ДОБАВИТЬ МЕНЕДЖЕРА" ===
@dp.callback_query(F.data == "add_manager_cmd")
async def prompt_add_manager(callback: CallbackQuery):
    """
    Отправляет админу инструкцию, как добавить менеджера.
    """
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("❌ У вас нет прав для этого действия.", show_alert=True)
        return
    await callback.message.edit_text(
        "➕ Чтобы добавить менеджера, отправьте мне его chat_id в формате:\n`/add_manager <chat_id>`\n\n"
        "Например: `/add_manager 123456789`",
        parse_mode="Markdown",
    )
    await callback.answer()


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК КОМАНДЫ /add_manager ===
@dp.message(Command("add_manager"))
async def cmd_add_manager(message: Message):
    """
    Обработчик команды /add_manager. Добавляет chat_id в список менеджеров.
    """
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("❌ У вас нет прав для этого действия.")
        return

    try:
        # /add_manager 123456789
        parts = message.text.split()
        if len(parts) != 2:
            await message.answer("❌ Используйте: /add_manager <chat_id>")
            return
        new_manager_id = int(parts[1])
        if add_manager(new_manager_id):
            await message.answer(
                f"✅ Пользователь {new_manager_id} добавлен как менеджер."
            )
        else:
            await message.answer(
                f"⚠️ Пользователь {new_manager_id} уже является менеджером."
            )
    except ValueError:
        await message.answer("❌ Неверный формат chat_id. Укажите число.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК ТЕКСТА (для поддержки по ID и ответов менеджера) ===
@dp.message(F.text)
async def handle_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    # Если FSM активен (например, заполняем форму), не трогаем
    if current_state and not current_state.startswith("SupportForm"):
        data = await state.get_data()
        if data.get("intent") == "new_order":
            # Это значит, что FSM для нового заказа активен
            # Логика для OrderForm должна быть в соответствующих обработчиках
            # Этот хендлер сработает, только если сообщение не подошло под другие
            # Для простоты, если FSM активен и intent не support, выходим
            return

    # Проверяем, является ли отправитель админом
    if message.from_user.id == ADMIN_CHAT_ID:
        # Команды админа, кроме /add_manager, обрабатываются отдельно
        # или можно проверить здесь, если не хочется отдельный хендлер
        # Но /add_manager уже обработан выше как команда
        # Проверяем, начинается ли сообщение с /reply_to
        if message.text.startswith("/reply_to"):
            # /reply_to 123456789 тут текст ответа
            try:
                # Разбиваем по первому пробелу после /reply_to
                command_part, rest = message.text.split(" ", 1)
                client_id_str, reply_text = rest.split(" ", 1)
                client_chat_id = int(client_id_str)
                # Отправляем ответ клиенту
                await bot.send_message(
                    client_chat_id, f"Ответ от поддержки:\n{reply_text}"
                )
                # Отправляем копию админу
                await message.answer(
                    f"✅ Ответ отправлен клиенту {client_chat_id} и копия сохранена."
                )
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"Копия ответа админа клиенту {client_chat_id}:\n{reply_text}",
                )
            except ValueError:
                await message.answer(
                    "❌ Неверный формат. Используйте: /reply_to <chat_id> <текст>"
                )
            except Exception as e:
                await message.answer(f"❌ Ошибка при отправке: {e}")
        # Не обрабатываем просто текст админа как команду
        return  # Выходим, если это админ и не команда FSM

    # Проверяем, является ли отправитель менеджером
    if str(message.from_user.id) in get_managers():
        # Менеджер пишет
        # Если сообщение содержит только числа, возможно, это chat_id клиента
        if message.text.isdigit():
            client_chat_id = int(message.text)
            # Проверим, существует ли такой пользователь с привязанным заказом
            # Это необязательно, можно просто сохранить как последнего
            set_last_client_chat(message.from_user.id, client_chat_id)
            await message.answer(
                f"✅ Установлен чат с клиентом {client_chat_id} как последний для ответа."
            )
            return

        # Иначе, это, вероятно, ответ менеджера
        last_client_id = get_last_client_chat(message.from_user.id)
        if last_client_id:
            try:
                # Отправляем ответ клиенту
                await bot.send_message(
                    int(last_client_id), f"Ответ от менеджера:\n{message.text}"
                )
                # Отправляем копию админу
                await message.answer(
                    f"✅ Ответ отправлен клиенту {last_client_id} и копия сохранена админу."
                )
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"Копия ответа менеджера (ID: {message.from_user.id}) клиенту {last_client_id}:\n{message.text}",
                )
            except Exception as e:
                await message.answer(f"❌ Ошибка при отправке ответа: {e}")
        else:
            await message.answer(
                "❌ Неизвестно, кому отвечать. Напишите сначала ID клиента или используйте /reply_to через админа."
            )
        return  # Выходим, если это менеджер

    # Если не админ и не менеджер, проверяем, привязан ли чат к заказу
    user_order_id = get_user_order(message.chat.id)
    if user_order_id:
        # Перенаправляем сообщение админу и/или менеджерам
        await message.answer("💬 Ваше сообщение передано в поддержку по заказу.")
        # Отправить админу
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"Сообщение от клиента (chat_id: {message.chat.id}, order_id: {user_order_id}):\n{message.text}",
        )
        # Отправить всем менеджерам
        managers = get_managers()
        for manager_id in managers:
            try:
                await bot.send_message(
                    int(manager_id),
                    f"Новое сообщение от клиента (chat_id: {message.chat.id}, order_id: {user_order_id}):\n{message.text}\n\n(Для ответа напишите сначала chat_id клиента, затем сообщение)",
                )
            except Exception as e:
                print(f"Ошибка отправки менеджеру {manager_id}: {e}")
    else:
        # Если нет связи и FSM неактивен, возможно, пользователь просто пишет
        await message.answer("Привет! Используйте /start, чтобы начать.")


# === ОБРАБОТЧИК КНОПКИ "ВВЕСТИ ID" ===
@dp.callback_query(F.data == "use_id")
async def prompt_for_order_id(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔑 Пожалуйста, введите ID вашего заказа:")
    await state.set_state(SupportForm.waiting_for_order_id)
    await callback.answer()


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК ВВОДА ID ЗАКАЗА ===


def find_order_by_id(order_id):
    """
    Ищет заказ по ID в temp_orders.json или orders.xlsx
    Возвращает (data, source) или (None, None)
    """
    # Проверяем во временных заказах
    if os.path.exists(TEMP_ORDERS_FILE):
        with open(TEMP_ORDERS_FILE, "r", encoding="utf-8") as f:
            temp_orders = json.load(f)
            if order_id in temp_orders:
                return temp_orders[order_id], "temp"
    # Проверяем в оплаченных заказах
    df = load_orders()
    if not df.empty:
        if "Order ID" in df.columns:
            row = df[df["Order ID"] == order_id]
            if not row.empty:
                return row.iloc[0].to_dict(), "paid"
    return None, None
    # Сохраняем связь chat_id -> order_id


@dp.message(SupportForm.waiting_for_order_id)
async def process_order_id(message: Message, state: FSMContext):
    order_id = message.text.strip()
    if not order_id:
        await message.answer("❌ ID заказа не может быть пустым. Попробуйте снова.")
        return
    order_data, source = find_order_by_id(order_id)
    if not order_data:
        await message.answer(
            "❌ Заказ с таким ID не найден. Проверьте ID и попробуйте снова."
        )
        await state.clear()
        return
    set_user_order(message.chat.id, order_id)
    await state.clear()  # Сбрасываем FSM
    # Отправляем информацию о заказе
    await message.answer(
        f"✅ Вы успешно привязаны к заказу #{order_id}.\n\n"
        f"Информация о заказе:\n"
        f"Кого: {order_data.get('Кого пригласить', 'N/A')}\n"
        f"Город: {order_data.get('Город', 'N/A')}\n"
        f"Дата: {order_data.get('Дата визита', 'N/A')}\n"
        f"Время: {order_data.get('Время визита', 'N/A')}\n"
        f"Программа: {order_data.get('Тип программы', 'N/A')}\n"
        f"Цена: {order_data.get('Цена', 'N/A')} ₽\n"
        f"Адрес: {order_data.get('Адрес', 'N/A')}\n"
        f"Детей: {order_data.get('Количество детей', 'N/A')}\n"
        f"Имя ребёнка: {order_data.get('Имя ребёнка', 'N/A')}\n"
        f"Телефон: {order_data.get('Телефон', 'N/A')}\n"
        f"Пожелания: {order_data.get('Пожелания', 'N/A')}\n\n"
        f"Теперь вы можете задавать вопросы по этому заказу, и мы постараемся вам помочь."
    )


@dp.message(SupportForm.waiting_for_order_id)
async def process_order_id(message: Message, state: FSMContext):
    order_id = message.text.strip()
    if not order_id:
        await message.answer("❌ ID заказа не может быть пустым. Попробуйте снова.")
        return


# === ОБРАБОТЧИК КНОПКИ "СДЕЛАТЬ ЗАКАЗ" ===
@dp.callback_query(F.data == "new_order")
async def start_new_order(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🏙️ Выберите город:", reply_markup=get_cities_keyboard()
    )
    # Продолжаем новый FSM процесс
    await state.set_data({"intent": "new_order"})
    await callback.answer()


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК ВЫБОРА ГОРОДА ===
@dp.callback_query(F.data.startswith("city_"))
async def select_city(callback: CallbackQuery, state: FSMContext):
    """
    Выбор города через инлайн-кнопку. Сохраняет город и запрашивает программу.
    """
    city = callback.data.replace("city_", "").title()
    await state.update_data(city=city)
    await callback.message.edit_text(
        f"🏙️ Вы выбрали {city}. Теперь выберите тип программы:",
        reply_markup=get_programs_keyboard(),
    )
    await callback.answer()


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК ВЫБОРА ПРОГРАММЫ ===
@dp.callback_query(F.data.startswith("program_"))
async def select_program(callback: CallbackQuery, state: FSMContext):
    """
    Выбор программы. Сохраняет программу и запрашивает дату.
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
    # Показываем календарь дат
    await callback.message.edit_text(
        f"🎯 Вы выбрали {program_type}. Теперь выберите дату:",
        reply_markup=get_dates_keyboard(),
    )
    await callback.answer()


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК ВЫБОРА ДАТЫ ===
@dp.callback_query(F.data.startswith("date_"))
async def select_date(callback: CallbackQuery, state: FSMContext):
    """
    Выбор даты через инлайн-кнопку. Сохраняет дату и запрашивает время с ценой.
    """
    date_str = callback.data.replace("date_", "")
    await state.update_data(date=date_str)
    data = await state.get_data()
    city = data["city"]
    program_type = data["program_type"]

    # Генерируем слоты времени с учётом выбранной программы и показом цены
    kb = get_time_slots_keyboard(date_str, city, program_type)
    await callback.message.edit_text(
        f"📅 Вы выбрали {date_str}. Выберите время:", reply_markup=kb
    )
    await callback.answer()


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК ВЫБОРА ВРЕМЕНИ ===
@dp.callback_query(F.data.startswith("time_"))
async def select_time(callback: CallbackQuery, state: FSMContext):
    """
    Выбор времени через инлайн-кнопку. Сохраняет время, показывает итоговую цену, запрашивает адрес.
    """
    time_str = callback.data.replace("time_", "")
    await state.update_data(time=time_str)
    data = await state.get_data()
    # Рассчитываем итоговую цену
    final_price = get_price(data["date"], time_str, data["program_type"])
    await state.update_data(price=final_price)  # Сохраняем итоговую цену

    await callback.message.edit_text(
        f"⏰ Вы выбрали {time_str}. Итоговая цена: {final_price} ₽\n\nВведите адрес:"
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


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК ВВОДА АДРЕСА ===
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


# === ОБНОВЛЁННЫЙ ОБРАБОТЧИК КОММЕНТАРИЕВ (показывает ID) ===
@dp.message(OrderForm.comments)
async def process_comments(message: Message, state: FSMContext):
    await state.update_data(
        comments=message.text if message.text.lower() != "нет" else "-"
    )
    data = await state.get_data()
    # Генерируем ID для временного заказа
    order_id = str(uuid.uuid4())  # <-- ГЕНЕРАЦИЯ ORDER_ID
    temp_data = {**data, "order_id": order_id}
    save_temp_order(order_id, temp_data)
    price = data["price"]
    kb = get_payment_keyboard(price)
    await message.answer(
        f"🎉 Заказ готов к оплате!\n"
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
        f"Пожелания: {data['comments']}\n"
        f"ID заказа: {order_id}\n\n"  # <-- ПОКАЗ ID ЗАКАЗА
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
        "Order ID": data.get("order_id", "N/A"),  # <-- ДОБАВЛЕН СТОЛБЕЦ
        "Дата и время заказа": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "Кого пригласить": "Дед Мороз и Снегурочка",  # Всегда пара
        "Город": data.get("city", "Москва"),
        "Дата визита": data["date"],
        "Время визита": data["time"],
        "Тип программы": data["program_type"],
        "Длительность": 10
        if data["program_type"] == "Экспресс (10 мин)"
        else (30 if data["program_type"] == "Стандарт (30 мин)" else 60),
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
        order_id = str(uuid.uuid4())  # <-- ГЕНЕРАЦИЯ ORDER_ID
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
    program_type = request.query.get("program_type", "Экспресс (10 мин)")  # Обновлено
    price = get_price(date, time, program_type)
    return web.json_response({"price": price})


# --- НОВЫЙ ЭНДПОИНТ: Получить слоты времени ---
async def handle_time_slots(request):
    """
    Возвращает список временных слотов с ценами и доступностью
    """
    date = request.query.get("date", "")
    city = request.query.get("city", "Москва")
    program_type = request.query.get("program_type", "Экспресс (10 мин)")  # Обновлено

    if not date or not city or not program_type:
        return web.json_response(
            {"error": "Не хватает параметров: date, city, program_type"}, status=400
        )

    # --- НОВОЕ: Попробуем распарсить оба формата даты ---
    dt = None
    try:
        # Попробуем формат DD Month YYYY
        dt = datetime.strptime(date, "%d %B %Y")
    except ValueError:
        try:
            # Попробуем формат YYYY-MM-DD
            dt = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            try:
                # Попробуем формат DD.MM.YYYY
                dt = datetime.strptime(date, "%d.%m.%Y")
            except ValueError:
                try:
                    # Попробуем формат MM/DD/YYYY
                    dt = datetime.strptime(date, "%m/%d/%Y")
                except ValueError:
                    return web.json_response(
                        {
                            "error": "Неверный формат даты. Ожидается DD Month YYYY, YYYY-MM-DD или DD.MM.YYYY"
                        },
                        status=400,
                    )

    # --- КОНЕЦ НОВОГО ---

    booked = get_booked_slots()
    max_slots = CITIES.get(city, 50)

    # Список часов для генерации слотов (включая ночные)
    standard_hours = [14, 15, 16, 17, 18, 19, 20, 21]
    night_hours_31 = [22, 23]  # 22:00-00:00
    night_hours_1st = [0, 1, 2, 3, 4, 5]  # 00:00-01:00, 01:00-02:00, ..., 05:00-06:00

    hours_to_generate = standard_hours[:]
    if dt.date() == datetime(2025, 12, 31).date():
        hours_to_generate.extend(night_hours_31)
    elif dt.date() == datetime(2026, 1, 1).date():  # 1 января
        hours_to_generate.extend(night_hours_1st)

    slots = []
    for hour in hours_to_generate:
        time_str = f"{hour:02d}:00"
        slot_key = f"{date} {time_str}"
        booked_count = booked.get(slot_key, {}).get(city, 0)
        available_count = max_slots - booked_count
        price = get_price(date, time_str, program_type)

        slots.append(
            {
                "time": time_str,
                "price": price,
                "available": available_count > 0,
                "available_count": available_count,
            }
        )

    return web.json_response({"slots": slots})


# --- КОНЕЦ НОВОГО ЭНДПОИНТА ---


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
    app.router.add_get("/api/time_slots", handle_time_slots)  # <-- НОВЫЙ ЭНДПОИНТ
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
                "Order ID",  # <-- ДОБАВЛЕН СТОЛБЕЦ
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

    # Создаём файлы для поддержки, если их нет
    if not os.path.exists(USER_ORDERS_FILE):
        with open(USER_ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
        print(f"✅ Создан файл {USER_ORDERS_FILE}")

    if not os.path.exists(MANAGERS_FILE):
        with open(MANAGERS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)  # Массив chat_id
        print(f"✅ Создан файл {MANAGERS_FILE}")

    if not os.path.exists(LAST_CLIENT_CHAT_FILE):
        with open(LAST_CLIENT_CHAT_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
        print(f"✅ Создан файл {LAST_CLIENT_CHAT_FILE}")

    asyncio.run(main())
