# -*- coding: utf-8 -*-
"""
bot.py — Telegram-бот «финансовый помощник путешественника».

Возможности:
    /start      — приветствие и главное меню
    /newtrip    — создать новое путешествие
    /switch     — переключиться между путешествиями
    /balance    — показать баланс активного путешествия
    /history    — история расходов
    /setrate    — временно изменить курс
    любое число — трактуется как расход в валюте страны пребывания
"""
import os
import re
from decimal import Decimal, ROUND_HALF_UP

import telebot
from dotenv import load_dotenv
from telebot import types

import database as db
from countries import resolve_country
from current_api import CurrencyAPIError, convert_currency, get_current_rate, get_quota_info

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
bot = telebot.TeleBot(TOKEN)

# Простая машина состояний: {telegram_id: {"state": ..., "data": {...}}}
user_states: dict[int, dict] = {}


# ---------------------------------------------------------------- helpers

def fmt(amount: float) -> str:
    """Красиво форматирует денежную сумму."""
    d = Decimal(str(amount)).quantize(Decimal("0.01")).normalize()
    s = f"{d:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def fmt_rate(rate: float) -> str:
    """Курс валюты: всегда 4 знака после запятой (точность для слабых курсов)."""
    return f"{Decimal(str(rate)).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP):f}"


def quota_text() -> str:
    """Остаток запросов API за текущий месяц (из заголовков последнего ответа)."""
    info = get_quota_info()
    if not info or info.get("limit") is None:
        return ""
    return f"⚡ Запросы API ({info['month']}): {info['remaining']} из {info['limit']}"


def main_menu() -> types.InlineKeyboardMarkup:
    """Главное inline-меню бота."""
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🗺 Новое путешествие", callback_data="menu:newtrip"))
    kb.row(types.InlineKeyboardButton("🧳 Мои путешествия", callback_data="menu:trips"))
    kb.row(types.InlineKeyboardButton("💰 Баланс", callback_data="menu:balance"))
    kb.row(types.InlineKeyboardButton("📜 История расходов", callback_data="menu:history"))
    kb.row(types.InlineKeyboardButton("⚙ Изменить курс", callback_data="menu:setrate"))
    return kb


# Подписи для постоянной клавиатуры внизу чата
MENU_LABELS = [
    "🗺 Новое путешествие",
    "🧳 Мои путешествия",
    "💰 Баланс",
    "📜 История расходов",
    "⚙ Изменить курс",
]


def menu_keyboard() -> types.ReplyKeyboardMarkup:
    """Постоянная клавиатура внизу чата: меню всегда под рукой."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True)
    kb.row("🗺 Новое путешествие", "🧳 Мои путешествия")
    kb.row("💰 Баланс", "📜 История расходов")
    kb.row("⚙ Изменить курс")
    return kb


def back_to_menu_kb() -> types.InlineKeyboardMarkup:
    """Кнопка «☰ Меню» — прикрепляется к ответам бота."""
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("☰ Меню", callback_data="menu:open"))
    return kb


def route_text(trip) -> str:
    """Маршрут путешествия: названия стран + валютная пара."""
    home = trip["home_country"] or trip["home_currency"]
    dest = trip["dest_country"] or trip["dest_currency"]
    return f"{home} → {dest} ({trip['home_currency']} → {trip['dest_currency']})"


def balance_text(trip) -> str:
    """Текст баланса активного путешествия в двух валютах."""
    spent_d = db.spent_dest(trip["id"])
    spent_h = db.spent_home(trip["id"])
    rest_d = trip["initial_dest"] - spent_d
    rest_h = trip["initial_home"] - spent_h
    return (
        f"🧳 Путешествие: {route_text(trip)}\n"
        f"Курс: 1 {trip['home_currency']} = {fmt_rate(trip['rate'])} {trip['dest_currency']}\n\n"
        f"💰 Остаток: {fmt(rest_d)} {trip['dest_currency']} = {fmt(rest_h)} {trip['home_currency']}\n"
        f"Потрачено: {fmt(spent_d)} {trip['dest_currency']} ({fmt(spent_h)} {trip['home_currency']})"
        + (f"\n\n{quota_text()}" if quota_text() else "")
    )


# Категории расходов: (название в БД, эмодзи для кнопки)
CATEGORIES: list[tuple[str, str]] = [
    ("Еда", "🍔"),
    ("Транспорт", "🚕"),
    ("Жильё", "🏨"),
    ("Развлечения", "🎉"),
    ("Прочее", "📦"),
]


def category_emoji(name: str) -> str:
    """Эмодзи категории по её названию (для истории)."""
    for cat_name, emoji in CATEGORIES:
        if cat_name == name:
            return emoji
    return "📦"


def expense_category_kb() -> types.InlineKeyboardMarkup:
    """Клавиатура выбора категории расхода (вместо Да/Нет)."""
    kb = types.InlineKeyboardMarkup()
    for name, emoji in CATEGORIES:
        kb.row(types.InlineKeyboardButton(f"{emoji} {name}", callback_data=f"expense:cat:{name}"))
    kb.row(types.InlineKeyboardButton("❌ Отмена", callback_data="expense:no"))
    return kb


def clear_state(chat_id: int) -> None:
    user_states.pop(chat_id, None)


# ---------------------------------------------------------------- handlers: команды

@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message) -> None:
    clear_state(message.chat.id)
    db.get_or_create_user(message.from_user.id)
    text = (
        "👋 Привет! Я финансовый помощник путешественника.\n\n"
        "Я помогу спланировать бюджет поездки:\n"
        "• создам путешествие с валютной парой вашей страны и страны назначения;\n"
        "• возьму актуальный курс через API exchangerate.host;\n"
        "• буду конвертировать и учитывать ваши расходы;\n"
        "• покажу остаток сразу в двух валютах.\n\n"
        "Выберите действие в меню 👇\n"
        "Команды: /newtrip /switch /balance /history /setrate"
    )
    bot.send_message(message.chat.id, text, reply_markup=main_menu())
    bot.send_message(
        message.chat.id,
        "💡 А ещё я закрепил кнопки меню внизу чата — они всегда под рукой.",
        reply_markup=menu_keyboard(),
    )


# Кнопки постоянной клавиатуры = пункты меню.
# Регистрируется до обработчиков диалогов, чтобы нажатие кнопки меню
# прерывало текущий диалог и показывало нужный раздел.
@bot.message_handler(func=lambda m: m.text in MENU_LABELS)
def menu_label(message: types.Message) -> None:
    chat_id, tg_id = message.chat.id, message.from_user.id
    clear_state(chat_id)
    label = message.text
    if label == "🗺 Новое путешествие":
        cmd_newtrip(message)
    elif label == "🧳 Мои путешествия":
        show_trips(chat_id, tg_id)
    elif label == "💰 Баланс":
        show_balance(chat_id, tg_id)
    elif label == "📜 История расходов":
        show_history(chat_id, tg_id)
    elif label == "⚙ Изменить курс":
        setrate_start(chat_id, tg_id)


@bot.message_handler(commands=["newtrip"])
def cmd_newtrip(message: types.Message) -> None:
    db.get_or_create_user(message.from_user.id)
    user_states[message.chat.id] = {"state": "trip_from", "data": {}}
    bot.send_message(
        message.chat.id,
        "🗺 Создаём новое путешествие.\n\n"
        "1/3. Из какой страны выезжаем? (например, Россия)",
    )


@bot.message_handler(commands=["switch"])
def cmd_switch(message: types.Message) -> None:
    show_trips(message.chat.id, message.from_user.id)


def show_trips(chat_id: int, telegram_id: int) -> None:
    """Список путешествий пользователя (для команды /switch и кнопки меню)."""
    user_id = db.get_or_create_user(telegram_id)
    trips = db.get_trips(user_id)
    if not trips:
        bot.send_message(chat_id, "🧳 У вас пока нет путешествий. Создайте: /newtrip")
        return
    kb = types.InlineKeyboardMarkup()
    for t in trips:
        mark = "✅ " if t["is_active"] else ""
        date = (t["created_at"] or "")[:10]
        kb.row(
            types.InlineKeyboardButton(
                f"{mark}{route_text(t)} · {date}",
                callback_data=f"switch:{t['id']}",
            )
        )
    bot.send_message(chat_id, "Выберите активное путешествие:", reply_markup=kb)


@bot.message_handler(commands=["balance"])
def cmd_balance(message: types.Message) -> None:
    show_balance(message.chat.id, message.from_user.id)


def show_balance(chat_id: int, telegram_id: int) -> None:
    """Баланс активного путешествия (для /balance и кнопки меню)."""
    user_id = db.get_or_create_user(telegram_id)
    trip = db.get_active_trip(user_id)
    if not trip:
        bot.send_message(chat_id, "🧳 Нет активного путешествия. Создайте: /newtrip")
        return
    bot.send_message(chat_id, balance_text(trip), reply_markup=back_to_menu_kb())


@bot.message_handler(commands=["history"])
def cmd_history(message: types.Message) -> None:
    show_history(message.chat.id, message.from_user.id)


def show_history(chat_id: int, telegram_id: int) -> None:
    """История расходов активного путешествия (для /history и кнопки меню)."""
    user_id = db.get_or_create_user(telegram_id)
    trip = db.get_active_trip(user_id)
    if not trip:
        bot.send_message(chat_id, "🧳 Нет активного путешествия. Создайте: /newtrip")
        return
    expenses = db.get_expenses(trip["id"])
    if not expenses:
        bot.send_message(chat_id, "📜 Расходов пока нет. Пришлите число — запишу трату.")
        return
    lines = ["📜 История расходов:"]
    for e in reversed(expenses):
        lines.append(
            f"• {category_emoji(e['category'] or 'Прочее')} {e['category'] or 'Прочее'}: "
            f"{fmt(e['amount_dest'])} {trip['dest_currency']} "
            f"(= {fmt(e['amount_home'])} {trip['home_currency']}) — "
            f"{e['created_at'][:16].replace('T', ' ')}"
        )
    bot.send_message(chat_id, "\n".join(lines) + f"\n\n{balance_text(trip)}", reply_markup=back_to_menu_kb())


@bot.message_handler(commands=["setrate"])
def cmd_setrate(message: types.Message) -> None:
    setrate_start(message.chat.id, message.from_user.id)


def setrate_start(chat_id: int, telegram_id: int) -> None:
    """Запрос нового курса (для /setrate и кнопки меню)."""
    user_id = db.get_or_create_user(telegram_id)
    trip = db.get_active_trip(user_id)
    if not trip:
        bot.send_message(chat_id, "🧳 Нет активного путешествия. Создайте: /newtrip")
        return
    user_states[chat_id] = {"state": "setrate", "data": {"trip_id": trip["id"]}}
    bot.send_message(
        chat_id,
        f"⚙ Текущий курс: 1 {trip['home_currency']} = {fmt_rate(trip['rate'])} {trip['dest_currency']}\n"
        f"Введите новый курс: сколько {trip['dest_currency']} дают за 1 {trip['home_currency']}?",
    )


# ---------------------------------------------------------------- handlers: диалоги

def is_number(text: str) -> bool:
    return bool(re.fullmatch(r"\d+([.,]\d+)?", text.strip()))


def to_number(text: str) -> float:
    return float(text.strip().replace(",", "."))


def ask_rate_confirmation(chat_id: int, home: str, dest: str) -> None:
    """Запрашивает курс у API и просит подтвердить (или ввести вручную)."""
    state = user_states.get(chat_id, {"data": {}})
    try:
        rates = get_current_rate(home, [dest])
        rate = rates[dest]
    except CurrencyAPIError as exc:
        state["state"] = "trip_manual_rate"
        user_states[chat_id] = state
        bot.send_message(
            chat_id,
            f"⚠ Не удалось получить курс: {exc}\n"
            f"Введите курс вручную: сколько {dest} дают за 1 {home}?",
        )
        return

    state["data"]["rate"] = rate
    state["state"] = "trip_rate_confirm"
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Курс подходит", callback_data="rate:ok"),
        types.InlineKeyboardButton("✍ Ввести свой курс", callback_data="rate:manual"),
    )
    bot.send_message(
        chat_id,
        f"💱 Актуальный курс: 1 {home} = {fmt_rate(rate)} {dest}\n\nПодходит курс?",
        reply_markup=kb,
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("state") == "trip_from")
def step_trip_from(message: types.Message) -> None:
    state = user_states[message.chat.id]
    currency = resolve_country(message.text)
    if not currency:
        bot.send_message(
            message.chat.id,
            "🤔 Не знаю такой страны. Попробуйте ещё раз (например: Россия, Китай, Италия).",
        )
        return
    state["data"]["home"] = currency
    state["data"]["home_country"] = message.text.strip()
    state["state"] = "trip_to"
    bot.send_message(
        message.chat.id,
        f"Валюта отправления: {currency}\n\n2/3. В какую страну едете?",
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("state") == "trip_to")
def step_trip_to(message: types.Message) -> None:
    state = user_states[message.chat.id]
    home = state["data"]["home"]
    currency = resolve_country(message.text)
    if not currency:
        bot.send_message(
            message.chat.id,
            "🤔 Не знаю такой страны. Попробуйте ещё раз (например: Китай, Япония, Турция).",
        )
        return
    if currency == home:
        bot.send_message(
            message.chat.id,
            "Страны совпадают — валютная пара одинаковая. Введите другую страну назначения.",
        )
        return
    state["data"]["dest"] = currency
    state["data"]["dest_country"] = message.text.strip()
    ask_rate_confirmation(message.chat.id, home, currency)


@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("state") == "trip_manual_rate")
def step_manual_rate(message: types.Message) -> None:
    state = user_states[message.chat.id]
    if not is_number(message.text):
        bot.send_message(message.chat.id, "Введите, пожалуйста, число (например: 104.5)")
        return
    rate = to_number(message.text)
    if rate <= 0:
        bot.send_message(message.chat.id, "Курс должен быть больше нуля. Попробуйте ещё раз.")
        return
    state["data"]["rate"] = rate
    state["state"] = "trip_amount"
    home, dest = state["data"]["home"], state["data"]["dest"]
    bot.send_message(
        message.chat.id,
        f"Курс принят: 1 {home} = {fmt_rate(rate)} {dest}\n\n3/3. Какая стартовая сумма в {home}?",
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("state") == "trip_amount")
def step_trip_amount(message: types.Message) -> None:
    state = user_states[message.chat.id]
    if not is_number(message.text):
        bot.send_message(message.chat.id, "Введите, пожалуйста, число (например: 200000)")
        return
    amount_home = to_number(message.text)
    if amount_home <= 0:
        bot.send_message(message.chat.id, "Сумма должна быть больше нуля. Попробуйте ещё раз.")
        return

    data = state["data"]
    home, dest, rate = data["home"], data["dest"], data["rate"]
    amount_dest = amount_home * rate

    user_id = db.get_or_create_user(message.from_user.id)
    trip_id = db.create_trip(
        user_id, home, dest, rate, amount_home, amount_dest,
        data.get("home_country", ""), data.get("dest_country", ""),
    )
    trip = db.get_trip_by_id(trip_id, user_id)
    clear_state(message.chat.id)

    bot.send_message(
        message.chat.id,
        f"✅ Путешествие создано!\n\n"
        f"{fmt(amount_home)} {home} = {fmt(amount_dest)} {dest}\n\n{balance_text(trip)}\n\n"
        f"💡 Дальше просто живите поездкой: если пришлёте мне число, "
        f"я запишу его как расход в {dest} и пересчитаю баланс.\n\n"
        f"Полезно: /setrate — реальный курс, /switch — несколько поездок, /history — траты.",
        reply_markup=menu_keyboard(),
    )


@bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("state") == "setrate")
def step_setrate(message: types.Message) -> None:
    state = user_states[message.chat.id]
    if not is_number(message.text):
        bot.send_message(message.chat.id, "Введите, пожалуйста, число (например: 0.0108)")
        return
    rate = to_number(message.text)
    if rate <= 0:
        bot.send_message(message.chat.id, "Курс должен быть больше нуля. Попробуйте ещё раз.")
        return
    user_id = db.get_or_create_user(message.from_user.id)
    trip = db.get_trip_by_id(state["data"]["trip_id"], user_id)
    if not trip:
        clear_state(message.chat.id)
        bot.send_message(message.chat.id, "⚠ Путешествие не найдено.")
        return
    db.update_trip_rate(trip["id"], rate)
    trip = db.get_trip_by_id(trip["id"], user_id)
    clear_state(message.chat.id)
    bot.send_message(message.chat.id, f"✅ Курс обновлён.\n\n{balance_text(trip)}", reply_markup=back_to_menu_kb())


@bot.message_handler(
    func=lambda m: is_number(m.text or ""),
    content_types=["text"],
)
def step_expense(message: types.Message) -> None:
    """Любое число = расход в валюте страны пребывания активного путешествия."""
    user_id = db.get_or_create_user(message.from_user.id)
    trip = db.get_active_trip(user_id)
    if not trip:
        bot.send_message(message.chat.id, "🧳 Нет активного путешествия. Создайте: /newtrip")
        return

    amount_dest = to_number(message.text)
    if amount_dest <= 0:
        bot.send_message(message.chat.id, "Сумма должна быть больше нуля.")
        return
    if amount_dest > trip["initial_dest"] - db.spent_dest(trip["id"]):
        bot.send_message(
            message.chat.id,
            "⚠ Сумма больше остатка баланса. Проверьте число или измените курс (/setrate).",
        )
        return

    try:
        amount_home = convert_currency(amount_dest, trip["dest_currency"], trip["home_currency"])
    except CurrencyAPIError as exc:
        # fallback: считаем по курсу, сохранённому в путешествии
        amount_home = amount_dest / trip["rate"]

    user_states[message.chat.id] = {
        "state": "expense_confirm",
        "data": {"amount_dest": amount_dest, "amount_home": amount_home},
    }
    bot.send_message(
        message.chat.id,
        f"{fmt(amount_dest)} {trip['dest_currency']} = {fmt(amount_home)} {trip['home_currency']}\n\n"
        f"В какую категорию записать расход?",
        reply_markup=expense_category_kb(),
    )


# ---------------------------------------------------------------- handlers: колбэки

@bot.callback_query_handler(func=lambda c: c.data.startswith("menu:"))
def cb_menu(call: types.CallbackQuery) -> None:
    bot.answer_callback_query(call.id)
    action = call.data.split(":", 1)[1]
    clear_state(call.message.chat.id)
    if action == "open":
        bot.send_message(
            call.message.chat.id,
            "☰ Главное меню. Выберите действие:",
            reply_markup=main_menu(),
        )
        return
    if action == "newtrip":
        user_states[call.message.chat.id] = {"state": "trip_from", "data": {}}
        bot.send_message(
            call.message.chat.id,
            "🗺 Создаём новое путешествие.\n\n1/3. Из какой страны выезжаем? (например, Россия)",
        )
    elif action == "trips":
        show_trips(call.message.chat.id, call.from_user.id)
    elif action == "balance":
        show_balance(call.message.chat.id, call.from_user.id)
    elif action == "history":
        show_history(call.message.chat.id, call.from_user.id)
    elif action == "setrate":
        setrate_start(call.message.chat.id, call.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("switch:"))
def cb_switch(call: types.CallbackQuery) -> None:
    user_id = db.get_or_create_user(call.from_user.id)
    trip_id = int(call.data.split(":", 1)[1])
    if db.switch_trip(trip_id, user_id):
        trip = db.get_trip_by_id(trip_id, user_id)
        bot.answer_callback_query(call.id, "Путешествие выбрано")
        bot.edit_message_text(
            f"✅ Активное путешествие изменено.\n\n{balance_text(trip)}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=back_to_menu_kb(),
        )
    else:
        bot.answer_callback_query(call.id, "⚠ Не удалось переключиться")


@bot.callback_query_handler(func=lambda c: c.data.startswith("rate:"))
def cb_rate(call: types.CallbackQuery) -> None:
    bot.answer_callback_query(call.id)
    state = user_states.get(call.message.chat.id)
    if not state or state.get("state") != "trip_rate_confirm":
        return
    choice = call.data.split(":", 1)[1]
    home, dest = state["data"]["home"], state["data"]["dest"]
    if choice == "ok":
        state["state"] = "trip_amount"
        bot.edit_message_text(
            f"Курс подтверждён.\n\n3/3. Какая стартовая сумма в {home}?",
            call.message.chat.id,
            call.message.message_id,
        )
    else:
        state["state"] = "trip_manual_rate"
        state["data"].pop("rate", None)
        bot.edit_message_text(
            f"Введите курс вручную: сколько {dest} дают за 1 {home}? (например: 104.5)",
            call.message.chat.id,
            call.message.message_id,
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("expense:"))
def cb_expense(call: types.CallbackQuery) -> None:
    state = user_states.get(call.message.chat.id)
    if not state or state.get("state") != "expense_confirm":
        bot.answer_callback_query(call.id)
        return
    user_id = db.get_or_create_user(call.from_user.id)
    trip = db.get_active_trip(user_id)
    if not trip:
        bot.answer_callback_query(call.id, "⚠ Нет активного путешествия")
        clear_state(call.message.chat.id)
        return
    bot.answer_callback_query(call.id)
    if call.data == "expense:no":
        clear_state(call.message.chat.id)
        bot.edit_message_text("Расход не записан.", call.message.chat.id, call.message.message_id)
        return
    # определяем категорию: "expense:cat:<имя>" или старая кнопка "expense:yes"
    if call.data.startswith("expense:cat:"):
        category = call.data.split(":", 2)[2]
    else:
        category = "Прочее"
    amount_dest = state["data"]["amount_dest"]
    amount_home = state["data"]["amount_home"]
    db.add_expense(trip["id"], amount_dest, amount_home, category)
    clear_state(call.message.chat.id)
    trip = db.get_trip_by_id(trip["id"], user_id)
    bot.edit_message_text(
        f"✅ Расход учтён: {category_emoji(category)} {category}\n\n{balance_text(trip)}",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=back_to_menu_kb(),
    )


# ---------------------------------------------------------------- fallback

@bot.message_handler(content_types=["text"])
def fallback(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        "🤔 Не понял сообщение. Пришлите число — запишу расход, "
        "или выберите действие в меню.",
        reply_markup=menu_keyboard(),
    )


# ---------------------------------------------------------------- запуск

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан. Пропишите его в файле .env")
    if not os.getenv("CURRENCY_API_KEY"):
        print("⚠ CURRENCY_API_KEY не задан — API-запросы работать не будут (см. .env)")
    db.init_db()
    print("Бот запущен. Для остановки: Ctrl+C")
    bot.infinity_polling(skip_pending=True)
