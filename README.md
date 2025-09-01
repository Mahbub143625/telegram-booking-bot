# Telegram Booking Bot – Advanced (Date Selector + Multi-Service/Resource + Payment Verify)

Features
- Inline calendar date selector
- Multiple services & resources (capacity-aware)
- Pending holds with expiry; admin verification in a private group
- Double-booking prevention (transactional)
- Timezone aware (Asia/Dhaka default)
- GitHub → Render Free deploy (long-polling)

## Setup
```bash
python -V    # 3.10+
pip install -r requirements.txt
cp .env.example .env  # fill BOT_TOKEN, ADMIN_GROUP_ID
