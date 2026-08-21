# TikTok LIVE Monitor + Telegram

พร้อมใช้งานสำหรับติดตามหลายช่อง TikTok:
- Dashboard แสดง LIVE / OFFLINE
- เพิ่มและลบช่องจากหน้าเว็บ
- Telegram แจ้งเฉพาะตอนเริ่ม LIVE / หยุด LIVE
- เก็บเวลาเริ่ม-หยุดและระยะเวลา
- เช็กอัตโนมัติทุก 60 วินาที
- มี Health Check สำหรับ deploy
- SQLite พร้อม persistent storage

## 1) สร้าง Telegram Bot
1. เปิด Telegram และคุยกับ `@BotFather`
2. ส่ง `/newbot`
3. เก็บ `Bot Token`
4. เข้า bot ที่สร้างแล้วกด Start และส่งข้อความ 1 ครั้ง
5. เปิด:
   `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`
6. หา `message.chat.id` นั่นคือ `TELEGRAM_CHAT_ID`

อย่าแชร์ Bot Token ต่อสาธารณะ

## 2) วิธีง่าย: Render
โปรเจกต์มี `render.yaml` แล้ว

ตั้ง Secret ตอนสร้างบริการ:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

ค่าที่เตรียมไว้:
- `CHECK_INTERVAL=60`
- `DB_PATH=/var/data/dashboard.db`
- Persistent disk 1 GB
- Health check `/health`

หมายเหตุ: แผนที่รองรับ persistent disk อาจมีค่าใช้จ่าย

## 3) Railway
โปรเจกต์มี Dockerfile และ `railway.json`

ใน Variables ใส่:
```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
CHECK_INTERVAL=60
DB_PATH=/app/data/dashboard.db
```

จากนั้นเพิ่ม Volume mount ที่:
```text
/app/data
```

แล้ว Generate Domain ใน Networking

## 4) รันบน Windows/เครื่องตัวเอง
ติดตั้ง Python 3.10+ แล้ว:

```bash
pip install -r requirements.txt
```

PowerShell:
```powershell
$env:TELEGRAM_BOT_TOKEN="ใส่-token"
$env:TELEGRAM_CHAT_ID="ใส่-chat-id"
$env:CHECK_INTERVAL="60"
python app.py
```

เปิด:
`http://localhost:8000`

## การแจ้งเตือน
เมื่อเพิ่มช่องครั้งแรก ระบบจะใช้ผลเช็กครั้งแรกเป็น baseline และไม่ยิงข้อความย้อนหลัง

หลังจากนั้น:
- OFFLINE → LIVE: `🟢 @username เริ่ม LIVE แล้ว`
- LIVE → OFFLINE: `🔴 @username หยุด LIVE แล้ว` พร้อมระยะเวลา

## ข้อควรรู้
ตัวตรวจ TikTok ใช้ TikTokLive ซึ่งเป็น unofficial integration หาก TikTok เปลี่ยนระบบภายใน อาจต้องอัปเดตไลบรารีหรือวิธีตรวจสถานะในอนาคต
