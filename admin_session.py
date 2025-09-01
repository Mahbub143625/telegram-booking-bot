# admin_session.py
# Simple in-memory per-admin reply session with max reply limit
KEY = "reply_sessions"

def _get_store(chat_data):
    if KEY not in chat_data:
        chat_data[KEY] = {}
    return chat_data[KEY]

def start(chat_data, admin_id: int, target_uid: int, max_replies: int = 3):
    store = _get_store(chat_data)
    store[admin_id] = {"target": target_uid, "left": max_replies}

def stop(chat_data, admin_id: int):
    store = _get_store(chat_data)
    store.pop(admin_id, None)

def is_active(chat_data, admin_id: int) -> bool:
    store = _get_store(chat_data)
    s = store.get(admin_id)
    return bool(s and s.get("left", 0) > 0)

def target(chat_data, admin_id: int):
    store = _get_store(chat_data)
    s = store.get(admin_id)
    return s.get("target") if s else None

def record_send_and_check(chat_data, admin_id: int) -> bool:
    """Return False if session should be closed (limit hit)."""
    store = _get_store(chat_data)
    s = store.get(admin_id)
    if not s: return False
    s["left"] = max(0, s.get("left", 0) - 1)
    if s["left"] <= 0:
        store.pop(admin_id, None)
        return False
    return True
