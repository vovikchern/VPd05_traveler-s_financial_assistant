# -*- coding: utf-8 -*-
"""
database.py — слой данных на SQLite.

Таблицы:
    users    — пользователи бота
    trips    — путешествия (валютная пара, курс, начальный баланс, активность)
    expenses — расходы по путешествиям
"""
import sqlite3
from datetime import datetime

DB_PATH = "currency_travel_bot.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                home_currency TEXT NOT NULL,
                dest_currency TEXT NOT NULL,
                home_country TEXT DEFAULT '',
                dest_country TEXT DEFAULT '',
                rate REAL NOT NULL,
                initial_home REAL NOT NULL,
                initial_dest REAL NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
                amount_dest REAL NOT NULL,
                amount_home REAL NOT NULL,
                category TEXT DEFAULT 'Прочее',
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        # миграция старой базы: добавляем страны и категорию, если колонок ещё нет
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(trips)")}
        if "home_country" not in cols:
            conn.execute("ALTER TABLE trips ADD COLUMN home_country TEXT DEFAULT ''")
            conn.execute("ALTER TABLE trips ADD COLUMN dest_country TEXT DEFAULT ''")
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(expenses)")}
        if "category" not in cols:
            conn.execute(
                "ALTER TABLE expenses ADD COLUMN category TEXT DEFAULT 'Прочее'"
            )
        if "note" not in cols:
            conn.execute("ALTER TABLE expenses ADD COLUMN note TEXT DEFAULT ''")


def get_or_create_user(telegram_id: int) -> int:
    """Возвращает внутренний id пользователя, при первом обращении создаёт его."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO users (telegram_id, created_at) VALUES (?, ?)",
            (telegram_id, datetime.now().isoformat()),
        )
        return cur.lastrowid


def create_trip(
    user_id: int,
    home_currency: str,
    dest_currency: str,
    rate: float,
    initial_home: float,
    initial_dest: float,
    home_country: str = "",
    dest_country: str = "",
) -> int:
    """Создаёт путешествие и делает его активным для пользователя."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE trips SET is_active = 0 WHERE user_id = ?", (user_id,)
        )
        cur = conn.execute(
            """
            INSERT INTO trips
                (user_id, home_currency, dest_currency, home_country, dest_country,
                 rate, initial_home, initial_dest, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (user_id, home_currency, dest_currency, home_country, dest_country,
             rate, initial_home, initial_dest, now),
        )
        return cur.lastrowid


def get_trips(user_id: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM trips WHERE user_id = ?
            ORDER BY is_active DESC, created_at DESC
            """,
            (user_id,),
        ).fetchall()


def get_active_trip(user_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM trips WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()


def get_trip_by_id(trip_id: int, user_id: int) -> sqlite3.Row | None:
    """Путешествие пользователя (защита от чужих trip_id)."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM trips WHERE id = ? AND user_id = ?",
            (trip_id, user_id),
        ).fetchone()


def switch_trip(trip_id: int, user_id: int) -> bool:
    """Делает trip_id активным путешествием пользователя."""
    trip = get_trip_by_id(trip_id, user_id)
    if not trip:
        return False
    with get_connection() as conn:
        conn.execute("UPDATE trips SET is_active = 0 WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE trips SET is_active = 1 WHERE id = ?", (trip_id,))
    return True


def update_trip_rate(trip_id: int, rate: float) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE trips SET rate = ? WHERE id = ?", (rate, trip_id))


def spent_dest(trip_id: int) -> float:
    """Сколько уже потрачено в валюте назначения."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_dest), 0) AS s FROM expenses WHERE trip_id = ?",
            (trip_id,),
        ).fetchone()
        return row["s"]


def spent_home(trip_id: int) -> float:
    """Сколько уже потрачено в домашней валюте."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_home), 0) AS s FROM expenses WHERE trip_id = ?",
            (trip_id,),
        ).fetchone()
        return row["s"]


def add_expense(trip_id: int, amount_dest: float, amount_home: float,
                category: str = "Прочее", note: str = "") -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO expenses (trip_id, amount_dest, amount_home, category, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (trip_id, amount_dest, amount_home, category, note, datetime.now().isoformat()),
        )


def get_expenses(trip_id: int, limit: int = 20) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM expenses WHERE trip_id = ?
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (trip_id, limit),
        ).fetchall()


def get_expense_summary(trip_id: int) -> list[sqlite3.Row]:
    """Итоги по статьям расходов: сумма и количество покупок в каждой категории."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT category,
                   SUM(amount_dest) AS total_dest,
                   SUM(amount_home) AS total_home,
                   COUNT(*) AS cnt
            FROM expenses
            WHERE trip_id = ?
            GROUP BY category
            ORDER BY total_dest DESC
            """,
            (trip_id,),
        ).fetchall()
