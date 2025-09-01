# db.py
import os, sqlite3, json
from contextlib import contextmanager
from datetime import datetime, timedelta
from utils import TZ

DB_PATH = os.getenv("DB_PATH", "booking.db")

@contextmanager
def conn_ctx():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()

def now_tz() -> datetime:
    return datetime.now(TZ)

# ---------- Schema & seed helpers ----------
def init_db():
    with conn_ctx() as conn:
        c = conn.cursor()
        # Base tables (your prior schema)
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            tg_user_id INTEGER NOT NULL UNIQUE,
            full_name TEXT, username TEXT
        );
        CREATE TABLE IF NOT EXISTS services(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            default_duration_min INTEGER NOT NULL DEFAULT 30,
            price INTEGER NOT NULL DEFAULT 0,
            step_min INTEGER NOT NULL DEFAULT 15,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS resources(
            id INTEGER PRIMARY KEY,
            service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            capacity INTEGER NOT NULL DEFAULT 1,
            open_time TEXT NOT NULL DEFAULT '10:00',
            close_time TEXT NOT NULL DEFAULT '18:00',
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(service_id, name)
        );
        CREATE TABLE IF NOT EXISTS bookings(
            id INTEGER PRIMARY KEY,
            service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
            resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
            tg_user_id INTEGER NOT NULL,
            user_full_name TEXT,
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            amount INTEGER NOT NULL,
            payment_method TEXT,
            payment_ref TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            token TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_bookings_time ON bookings(resource_id, starts_at, ends_at);
        CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);
        """)
        # Key/Value store for welcome, reply sessions, etc.
        c.execute("""CREATE TABLE IF NOT EXISTS kv_store(
            k TEXT PRIMARY KEY, v TEXT NOT NULL
        );""")
        # Auto Q/A bank (multiple entries allowed)
        c.execute("""CREATE TABLE IF NOT EXISTS auto_qa(
            id INTEGER PRIMARY KEY,
            patterns_json TEXT NOT NULL, -- ["hi","hello"]
            answer TEXT NOT NULL
        );""")
        conn.commit()

# ---------- KV helpers ----------
def get_kv(key: str, default=None):
    with conn_ctx() as conn:
        row = conn.execute("SELECT v FROM kv_store WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

def set_kv(key: str, value):
    with conn_ctx() as conn:
        conn.execute("INSERT INTO kv_store(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (key, json.dumps(value)))
        conn.commit()

# ---------- Users ----------
def upsert_user(tg_id: int, full_name: str, username: str|None):
    with conn_ctx() as conn:
        conn.execute("""
            INSERT INTO users(tg_user_id, full_name, username)
            VALUES(?,?,?)
            ON CONFLICT(tg_user_id) DO UPDATE SET full_name=excluded.full_name, username=excluded.username
        """, (tg_id, full_name, username))
        conn.commit()

# ---------- Catalog ----------
def list_services():
    with conn_ctx() as conn:
        return conn.execute("SELECT id,name,default_duration_min,price,step_min FROM services WHERE active=1 ORDER BY id").fetchall()

def get_service(svc_id: int):
    with conn_ctx() as conn:
        return conn.execute("SELECT id,name,default_duration_min,price,step_min FROM services WHERE id=?", (svc_id,)).fetchone()

def list_resources(svc_id: int):
    with conn_ctx() as conn:
        return conn.execute("""
            SELECT id,name,capacity,open_time,close_time FROM resources
            WHERE active=1 AND service_id=?
            ORDER BY id
        """,(svc_id,)).fetchall()

def get_resource(res_id: int):
    with conn_ctx() as conn:
        return conn.execute("""
            SELECT id,service_id,name,capacity,open_time,close_time FROM resources WHERE id=?
        """,(res_id,)).fetchone()

# ---------- Availability & Booking ----------
def count_overlapping(res_id: int, start_iso: str, end_iso: str) -> int:
    with conn_ctx() as conn:
        # paid or pending (unexpired) consume capacity
        return conn.execute("""
            SELECT COUNT(*) FROM bookings
            WHERE resource_id=? AND status IN ('paid','pending')
              AND starts_at < ? AND ends_at > ?
              AND (status='paid' OR (status='pending' AND (expires_at IS NULL OR expires_at > datetime('now'))))
        """,(res_id, end_iso, start_iso)).fetchone()[0]

def create_pending_booking(tg_user_id: int, user_full_name: str,
                           service_id: int, resource_id: int,
                           starts_at_iso: str, ends_at_iso: str,
                           amount: int, payment_method: str, payment_ref: str|None):
    hold_minutes = int(os.getenv("HOLD_MINUTES", "10"))
    expires_at = now_tz() + timedelta(minutes=hold_minutes)
    expires_at_iso = expires_at.isoformat()

    with conn_ctx() as conn:
        try:
            conn.execute("""
                INSERT INTO bookings(service_id,resource_id,tg_user_id,user_full_name,
                 starts_at,ends_at,amount,payment_method,payment_ref,status,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,'pending',?)
            """, (service_id, resource_id, tg_user_id, user_full_name,
                  starts_at_iso, ends_at_iso, amount, payment_method, payment_ref, expires_at_iso))
            bid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            return int(bid)
        except sqlite3.IntegrityError:
            conn.rollback()
            return None

def mark_paid(booking_id: int, token: str) -> bool:
    with conn_ctx() as conn:
        row = conn.execute("SELECT status FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row: return False
        if row[0] == 'paid': return True
        if row[0] in ('cancelled','expired'): return False
        conn.execute("UPDATE bookings SET status='paid', token=?, expires_at=NULL WHERE id=?",
                     (token, booking_id))
        conn.commit()
        return True

def cancel_booking(booking_id: int) -> bool:
    with conn_ctx() as conn:
        row = conn.execute("SELECT status FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row: return False
        if row[0] == 'cancelled': return True
        conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
        conn.commit()
        return True

def get_booking(booking_id: int):
    with conn_ctx() as conn:
        return conn.execute("""
        SELECT b.id, b.service_id, s.name, b.resource_id, r.name, b.tg_user_id, b.user_full_name,
               b.starts_at, b.ends_at, b.amount, b.payment_method, b.payment_ref, b.status, b.token
        FROM bookings b
        JOIN services s ON s.id=b.service_id
        JOIN resources r ON r.id=b.resource_id
        WHERE b.id=?
        """,(booking_id,)).fetchone()

def list_bookings(offset=0, limit=15):
    with conn_ctx() as conn:
        return conn.execute("""
        SELECT b.id, s.name, r.name, b.starts_at, b.ends_at, b.status, COALESCE(b.token,'-'), b.amount
        FROM bookings b
        JOIN services s ON s.id=b.service_id
        JOIN resources r ON r.id=b.resource_id
        ORDER BY b.starts_at DESC
        LIMIT ? OFFSET ?
        """,(limit, offset)).fetchall()

def user_bookings(tg_user_id: int, limit=10):
    with conn_ctx() as conn:
        return conn.execute("""
        SELECT b.id, s.name, r.name, b.starts_at, b.ends_at, b.status, COALESCE(b.token,'-')
        FROM bookings b
        JOIN services s ON s.id=b.service_id
        JOIN resources r ON r.id=b.resource_id
        WHERE b.tg_user_id=?
        ORDER BY b.starts_at DESC
        LIMIT ?
        """,(tg_user_id, limit)).fetchall()

# ---------- Auto Q/A ----------
def add_autoqa(patterns: list[str], answer: str):
    patterns = [p.strip().lower() for p in patterns if p.strip()]
    with conn_ctx() as conn:
        conn.execute("INSERT INTO auto_qa(patterns_json,answer) VALUES(?,?)",
                     (json.dumps(patterns), answer.strip()))
        conn.commit()

def all_autoqa():
    with conn_ctx() as conn:
        rows = conn.execute("SELECT id, patterns_json, answer FROM auto_qa").fetchall()
        out = []
        for i, pj, ans in rows:
            try:
                pat = json.loads(pj)
            except Exception:
                pat = []
            out.append((i, pat, ans))
        return out

def clear_autoqa():  # not exposed as command, but handy to keep
    with conn_ctx() as conn:
        conn.execute("DELETE FROM auto_qa")
        conn.commit()
