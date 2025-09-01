PRAGMA foreign_keys = ON;

-- ---------- Users ----------
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    tg_user_id INTEGER NOT NULL UNIQUE,
    full_name TEXT,
    username TEXT
);

-- ---------- Services ----------
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    default_duration_min INTEGER NOT NULL DEFAULT 30,
    price INTEGER NOT NULL DEFAULT 0,
    step_min INTEGER NOT NULL DEFAULT 15,
    active INTEGER NOT NULL DEFAULT 1
);

-- ---------- Resources ----------
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY,
    service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL DEFAULT 1,
    open_time TEXT NOT NULL DEFAULT '10:00',
    close_time TEXT NOT NULL DEFAULT '18:00',
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (service_id, name)
);

-- ---------- Bookings ----------
/* Booking holds consume capacity until they expire. */
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY,
    service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    tg_user_id INTEGER NOT NULL,
    user_full_name TEXT,
    starts_at TEXT NOT NULL,   -- ISO8601 with timezone
    ends_at   TEXT NOT NULL,
    amount INTEGER NOT NULL,
    payment_method TEXT,       -- e.g., bkash|nagad|card|cash
    payment_ref TEXT,          -- txid or last4
    status TEXT NOT NULL DEFAULT 'pending', -- pending|paid|cancelled|expired
    token TEXT,                -- generated on paid
    expires_at TEXT,           -- when a pending hold expires
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------- Indexes ----------
CREATE INDEX IF NOT EXISTS idx_bookings_time
    ON bookings(resource_id, starts_at, ends_at);

CREATE INDEX IF NOT EXISTS idx_bookings_status
    ON bookings(status);

CREATE TABLE IF NOT EXISTS mutes (
  tg_user_id INTEGER PRIMARY KEY,
  until TEXT NOT NULL -- ISO8601 with timezone
);

