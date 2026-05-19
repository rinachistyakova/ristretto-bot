import sqlite3
from datetime import datetime


# ---------- INIT ----------
def init_db():
    conn = sqlite3.connect("ristretto.db")
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        name TEXT,
        active INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS meetings (
        user1 INTEGER,
        user2 INTEGER,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


# ---------- USERS ----------
def add_user(user_id, username, name):
    conn = sqlite3.connect("ristretto.db")
    cur = conn.cursor()

    cur.execute("""
    INSERT OR IGNORE INTO users VALUES (?, ?, ?, 1)
    """, (user_id, username, name))

    conn.commit()
    conn.close()


def set_active(user_id, active):
    conn = sqlite3.connect("ristretto.db")
    cur = conn.cursor()

    cur.execute("""
    UPDATE users SET active=?
    WHERE user_id=?
    """, (1 if active else 0, user_id))

    conn.commit()
    conn.close()


def get_users(active_only=False):
    conn = sqlite3.connect("ristretto.db")
    cur = conn.cursor()

    if active_only:
        cur.execute("SELECT user_id, username, name FROM users WHERE active=1")
    else:
        cur.execute("SELECT user_id, username, name FROM users")

    rows = cur.fetchall()
    conn.close()
    return rows


def delete_user(user_id):
    conn = sqlite3.connect("ristretto.db")
    cur = conn.cursor()

    cur.execute("DELETE FROM users WHERE user_id=?", (user_id,))

    conn.commit()
    conn.close()


# ---------- MEETINGS ----------
def save_meeting(user1, user2):
    conn = sqlite3.connect("ristretto.db")
    cur = conn.cursor()

    u1, u2 = sorted([user1, user2])
    now = datetime.now().isoformat()

    cur.execute("""
    INSERT INTO meetings VALUES (?, ?, ?)
    """, (u1, u2, now))

    conn.commit()
    conn.close()


def last_meeting_days_ago(user1, user2):
    conn = sqlite3.connect("ristretto.db")
    cur = conn.cursor()

    u1, u2 = sorted([user1, user2])

    cur.execute("""
    SELECT created_at FROM meetings
    WHERE user1=? AND user2=?
    ORDER BY created_at DESC
    LIMIT 1
    """, (u1, u2))

    row = cur.fetchone()
    conn.close()

    if not row:
        return 999

    last = datetime.fromisoformat(row[0])
    return (datetime.now() - last).days


# ---------- ANALYTICS ----------
def get_stats():
    conn = sqlite3.connect("ristretto.db")
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    users = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM users WHERE active=1")
    active = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM meetings")
    meetings = cur.fetchone()[0]

    cur.execute("SELECT AVG(active) FROM users")
    avg_inactivity = cur.fetchone()[0] or 0

    conn.close()

    return {
        "users": users,
        "active": active,
        "meetings": meetings,
        "avg_inactivity": round(avg_inactivity, 2)
    }