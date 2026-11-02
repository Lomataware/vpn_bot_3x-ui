import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

DB_PATH = "vpn_bot.db"


def check_db_integrity():
    """Проверка целостности базы данных SQLite при старте."""
    if not os.path.exists(DB_PATH):
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check")
        result = cursor.fetchone()
        conn.close()
        if not result or result[0] != "ok":
            backup_path = f"{DB_PATH}.corrupt.{int(datetime.now().timestamp())}"
            logger.critical(f"БД повреждена ({result})! Создана копия {backup_path}")
            shutil.copy2(DB_PATH, backup_path)
    except Exception as e:
        logger.error(f"Ошибка проверки целостности БД: {e}")


@contextmanager
def get_db_connection():
    """Контекстный менеджер подключения к SQLite с режимом WAL."""
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        yield conn
        conn.commit()
    except sqlite3.DatabaseError as e:
        conn.rollback()
        logger.error(f"Ошибка БД: {e}")
        raise
    finally:
        conn.close()


def init_db():
    """Инициализация таблиц и миграции схемы БД."""
    check_db_integrity()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_blocked BOOLEAN DEFAULT FALSE,
                trial_used BOOLEAN DEFAULT FALSE,
                xui_email TEXT,
                client_uuid TEXT,
                sub_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan TEXT,
                amount INTEGER,
                method TEXT,
                status TEXT DEFAULT 'pending',
                external_id TEXT,
                xui_email TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        # Безопасные миграции со старой схемы
        for stmt in (
            "ALTER TABLE users ADD COLUMN is_blocked BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN trial_used BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN xui_email TEXT",
            "ALTER TABLE users ADD COLUMN client_uuid TEXT",
            "ALTER TABLE users ADD COLUMN sub_id TEXT",
            "ALTER TABLE users ADD COLUMN last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "ALTER TABLE payments ADD COLUMN xui_email TEXT",
        ):
            try:
                cursor.execute(stmt)
            except sqlite3.OperationalError:
                pass


def save_user(user):
    """Сохранение или обновление данных пользователя Telegram."""
    with get_db_connection() as conn:
        conn.execute('''
            INSERT INTO users (id, username, first_name, last_name, last_active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                last_active=excluded.last_active
        ''', (
            user.id,
            user.username or "",
            user.first_name or "",
            user.last_name or "",
            datetime.now().isoformat()
        ))


def get_user_info(user_id: int):
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def set_user_xui(user_id: int, xui_email: str, client_uuid: str = None, sub_id: str = None):
    with get_db_connection() as conn:
        if client_uuid and sub_id:
            conn.execute(
                "UPDATE users SET xui_email = ?, client_uuid = ?, sub_id = ? WHERE id = ?",
                (xui_email, client_uuid, sub_id, user_id)
            )
        else:
            conn.execute(
                "UPDATE users SET xui_email = ? WHERE id = ?",
                (xui_email, user_id)
            )


def has_used_trial(user_id: int) -> bool:
    row = get_user_info(user_id)
    if not row:
        return False
    return bool(row["trial_used"]) or bool(row["xui_email"])


def mark_trial_used(user_id: int):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET trial_used = TRUE WHERE id = ?",
            (user_id,)
        )


def get_users_count() -> int:
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_blocked = FALSE"
        ).fetchone()[0]


def save_payment(user_id: int, plan: str, amount: int, method: str,
                 status: str = "pending", external_id: str = None) -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO payments (user_id, plan, amount, method, status, external_id)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, plan, amount, method, status, external_id))
        return cursor.lastrowid


def update_payment_status(payment_id: int, status: str,
                          xui_email: str = None) -> bool:
    """Атомарно обновляет статус платежа из состояния 'pending'."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if xui_email:
            cursor.execute('''
                UPDATE payments
                SET status = ?, xui_email = ?, paid_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending'
            ''', (status, xui_email, payment_id))
        else:
            cursor.execute(
                "UPDATE payments SET status = ? WHERE id = ? AND status = 'pending'",
                (status, payment_id)
            )
        return cursor.rowcount > 0


def get_pending_payments(user_id: int) -> list:
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT * FROM payments WHERE user_id = ? AND status = 'pending' "
            "ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()


def get_payment(payment_id: int):
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT * FROM payments WHERE id = ?", (payment_id,)
        ).fetchone()
