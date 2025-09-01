import os
from db import init_db, conn_ctx

# Initialize schema
init_db()

# Defaults from .env (or fallbacks)
svc_name = os.environ.get("DEFAULT_SERVICE_NAME", "Consultation")
dur = int(os.environ.get("DEFAULT_SERVICE_DURATION_MIN", "30"))
price = int(os.environ.get("DEFAULT_SERVICE_PRICE", "500"))
step = int(os.environ.get("DEFAULT_SERVICE_STEP_MIN", "15"))

r1 = os.environ.get("DEFAULT_RESOURCE_1", "Room A")
r1_cap = int(os.environ.get("DEFAULT_RESOURCE_1_CAPACITY", "1"))
r1_open = os.environ.get("DEFAULT_RESOURCE_1_OPEN", "10:00")
r1_close = os.environ.get("DEFAULT_RESOURCE_1_CLOSE", "18:00")

r2 = os.environ.get("DEFAULT_RESOURCE_2", "Room B")
r2_cap = int(os.environ.get("DEFAULT_RESOURCE_2_CAPACITY", "2"))
r2_open = os.environ.get("DEFAULT_RESOURCE_2_OPEN", "10:00")
r2_close = os.environ.get("DEFAULT_RESOURCE_2_CLOSE", "18:00")

with conn_ctx() as conn:
    # Service
    conn.execute(
        "INSERT OR IGNORE INTO services(name, default_duration_min, price, step_min, active) "
        "VALUES (?,?,?,?,1)",
        (svc_name, dur, price, step),
    )
    sid = conn.execute(
        "SELECT id FROM services WHERE name = ?",
        (svc_name,),
    ).fetchone()[0]

    # Resources
    conn.execute(
        "INSERT OR IGNORE INTO resources(service_id, name, capacity, open_time, close_time, active) "
        "VALUES (?,?,?,?,?,1)",
        (sid, r1, r1_cap, r1_open, r1_close),
    )
    conn.execute(
        "INSERT OR IGNORE INTO resources(service_id, name, capacity, open_time, close_time, active) "
        "VALUES (?,?,?,?,?,1)",
        (sid, r2, r2_cap, r2_open, r2_close),
    )
    conn.commit()

print("Seeded default service and resources.")
