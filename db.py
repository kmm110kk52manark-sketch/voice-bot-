"""
ماژول مشترک مدیریت مشترکین — هم ربات، هم داشبورد وب از همین فایل استفاده می‌کنند.
نیازمند یک دیتابیس PostgreSQL (مثلاً افزونه رایگان Postgres در Railway) است.
آدرس اتصال باید در متغیر محیطی DATABASE_URL باشد.
"""

import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ["DATABASE_URL"]


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    """جدول مشترکین را در صورت نبودن می‌سازد. در استارت هر دو سرویس صدا زده شود."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                telegram_id BIGINT PRIMARY KEY,
                display_name TEXT,
                expires_at DATE NOT NULL,
                added_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.commit()


def is_subscribed(telegram_id: int) -> bool:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at FROM subscribers WHERE telegram_id = %s", (telegram_id,)
        )
        row = cur.fetchone()
        return bool(row) and row[0] >= date.today()


def add_or_renew_subscriber(
    telegram_id: int, days: int, display_name: str = ""
) -> date:
    """اشتراک را می‌سازد یا تمدید می‌کند و تاریخ انقضای جدید را برمی‌گرداند.

    اگر اشتراک قبلی هنوز منقضی نشده باشد، روزهای جدید به تاریخ انقضای فعلی
    اضافه می‌شود (نه از امروز)، تا تمدید زودهنگام حق مشترک را نخورد.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at FROM subscribers WHERE telegram_id = %s", (telegram_id,)
        )
        row = cur.fetchone()
        base_date = row[0] if row and row[0] >= date.today() else date.today()
        new_expiry = base_date + timedelta(days=days)

        cur.execute(
            """
            INSERT INTO subscribers (telegram_id, display_name, expires_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id)
            DO UPDATE SET expires_at = EXCLUDED.expires_at,
                          display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), subscribers.display_name)
            """,
            (telegram_id, display_name, new_expiry),
        )
        conn.commit()
        return new_expiry


def remove_subscriber(telegram_id: int) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM subscribers WHERE telegram_id = %s", (telegram_id,))
        conn.commit()


def list_subscribers():
    """همه‌ی مشترکین را به همراه وضعیت (فعال/منقضی) برمی‌گرداند."""
    with get_connection() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(
            "SELECT telegram_id, display_name, expires_at, added_at "
            "FROM subscribers ORDER BY expires_at DESC"
        )
        rows = cur.fetchall()
        today = date.today()
        for row in rows:
            row["active"] = row["expires_at"] >= today
        return rows
