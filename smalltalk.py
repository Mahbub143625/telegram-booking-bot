# smalltalk.py
# Very small fallback replies. We normalize in bot.
_RESP = {
    "hi": "Hi 👋",
    "hello": "Hello 👋",
    "hey": "Hey there!",
    "kemon acho": "ভালো আছি, আপনাকে কিভাবে সাহায্য করতে পারি?",
    "ki obostha": "ভালই তো! কিভাবে সাহায্য করতে পারি?",
    "ki khobor": "সব ভাল! 🙂 কী জানতে চান?",
}

def maybe_auto_reply(norm_text: str):
    # exact match or startswith for simple cases
    for k, v in _RESP.items():
        if norm_text == k or norm_text.startswith(k + " "):
            return v
    return None
