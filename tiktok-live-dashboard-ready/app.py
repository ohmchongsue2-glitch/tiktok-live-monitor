import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from flask import Flask, jsonify, render_template, request
from TikTokLive import TikTokLiveClient

DB_PATH = os.environ.get("DB_PATH", "dashboard.db")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
SUMMARY_INTERVAL = int(os.environ.get("SUMMARY_INTERVAL", "900"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

app = Flask(__name__)
poller_started = False
poller_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        is_live INTEGER,
        live_started_at TEXT,
        last_checked_at TEXT,
        last_changed_at TEXT,
        last_error TEXT
    );

    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_at TEXT NOT NULL,
        duration_seconds INTEGER,
        FOREIGN KEY(channel_id) REFERENCES channels(id)
    );
    """)
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def fmt_duration(seconds):
    seconds = max(0, int(seconds or 0))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Telegram ยังไม่ได้ตั้งค่า TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        with httpx.Client(timeout=15) as client:
            r = client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})

        if r.is_error:
            try:
                desc = r.json().get("description") or f"HTTP {r.status_code}"
            except Exception:
                desc = f"HTTP {r.status_code}"
            raise RuntimeError(f"Telegram error {r.status_code}: {desc}")
    except httpx.HTTPError as e:
        raise RuntimeError(f"Telegram network error: {type(e).__name__}") from e


def send_status_summary():
    conn = db()
    try:
        row = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN is_live = 1 AND last_error IS NULL THEN 1 ELSE 0 END) AS live_count,
                SUM(CASE WHEN is_live = 0 AND last_error IS NULL THEN 1 ELSE 0 END) AS offline_count,
                SUM(CASE WHEN is_live IS NULL OR last_error IS NOT NULL THEN 1 ELSE 0 END) AS unknown_count
            FROM channels
        """).fetchone()
    finally:
        conn.close()

    total = int(row["total"] or 0)
    live_count = int(row["live_count"] or 0)
    offline_count = int(row["offline_count"] or 0)
    unknown_count = int(row["unknown_count"] or 0)

    message = (
        "📊 สรุปสถานะ TikTok LIVE\n"
        f"🟢 กำลังไลฟ์: {live_count} ช่อง\n"
        f"⚫ ไม่ได้ไลฟ์: {offline_count} ช่อง\n"
        f"⚠️ ตรวจสถานะไม่ได้: {unknown_count} ช่อง\n"
        f"📱 ทั้งหมด: {total} ช่อง"
    )
    send_telegram(message)


async def _check_tiktoklive(username):
    client = TikTokLiveClient(unique_id=username)
    return bool(await client.is_live())


def _browser_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
        "Referer": "https://www.tiktok.com/",
    }


def _check_tiktok_live_api(username):
    url = "https://www.tiktok.com/api-live/user/room/"
    params = {"aid": "1988", "uniqueId": username}

    try:
        with httpx.Client(timeout=15, follow_redirects=True, headers=_browser_headers()) as client:
            r = client.get(url, params=params)
    except httpx.HTTPError as e:
        return "unknown", f"web-api network: {type(e).__name__}"

    if r.status_code in (403, 429):
        return "unknown", f"web-api blocked HTTP {r.status_code}"
    if r.status_code != 200:
        return "unknown", f"web-api HTTP {r.status_code}"

    try:
        payload = r.json()
    except Exception:
        return "unknown", "web-api returned non-JSON"

    data = payload.get("data")
    if not isinstance(data, dict):
        return "unknown", "web-api missing data"

    user = data.get("user")
    if isinstance(user, dict):
        room_id = user.get("roomId") or user.get("room_id")
        if room_id not in (None, "", "0", 0):
            return "live", None
        return "offline", None

    room_id = data.get("roomId") or data.get("room_id")
    if room_id not in (None, "", "0", 0):
        return "live", None

    return "unknown", "web-api could not determine status"


def _check_tiktok_live_page(username):
    url = f"https://www.tiktok.com/@{username}/live"
    try:
        with httpx.Client(timeout=15, follow_redirects=True, headers=_browser_headers()) as client:
            r = client.get(url)
    except httpx.HTTPError as e:
        return "unknown", f"live-page network: {type(e).__name__}"

    if r.status_code in (403, 429):
        return "unknown", f"live-page blocked HTTP {r.status_code}"
    if r.status_code == 404:
        return "unknown", "live-page HTTP 404"
    if r.status_code != 200:
        return "unknown", f"live-page HTTP {r.status_code}"

    text = r.text or ""
    low = text.lower()

    anti_bot = (
        "captcha",
        "verify to continue",
        "security verification",
        "access denied",
        "too many requests",
    )
    if any(x in low for x in anti_bot):
        return "unknown", "live-page anti-bot response"

    final_path = urlparse(str(r.url)).path.rstrip("/").lower()
    wanted = f"/@{username.lower()}/live"

    live_markers = (
        '"roomid":"',
        '"room_id":"',
        '"liveroom"',
        '"livestatus":2',
        '"status":2,"owner"',
    )
    if final_path == wanted and any(m in low for m in live_markers):
        return "live", None

    if final_path == f"/@{username.lower()}":
        return "offline", None

    offline_markers = (
        "live has ended",
        "isn't live",
        "is not live",
        "live ended",
    )
    if any(m in low for m in offline_markers):
        return "offline", None

    return "unknown", "live-page ambiguous response"


def check_tiktok_status(username):
    import asyncio

    errors = []

    try:
        live = asyncio.run(_check_tiktoklive(username))
        return ("live" if live else "offline"), None
    except Exception as e:
        errors.append(f"TikTokLive: {str(e)[:160]}")

    status, err = _check_tiktok_live_api(username)
    if status != "unknown":
        return status, None
    if err:
        errors.append(err)

    status, err = _check_tiktok_live_page(username)
    if status != "unknown":
        return status, None
    if err:
        errors.append(err)

    short = " | ".join(errors[-3:])
    return "unknown", f"ตรวจสถานะไม่ได้: {short[:360]}"


def check_once():
    conn = db()
    channels = conn.execute("SELECT * FROM channels ORDER BY id").fetchall()

    for ch in channels:
        username = ch["username"]
        checked = now_iso()

        try:
            status, check_error = check_tiktok_status(username)

            if status == "unknown":
                conn.execute(
                    "UPDATE channels SET last_checked_at=?, last_error=? WHERE id=?",
                    (checked, check_error or "ตรวจสถานะไม่ได้", ch["id"]),
                )
                conn.commit()
                continue

            current = status == "live"
            previous = ch["is_live"]

            if previous is None:
                conn.execute(
                    """UPDATE channels SET is_live=?, live_started_at=?,
                       last_checked_at=?, last_changed_at=?, last_error=NULL WHERE id=?""",
                    (
                        1 if current else 0,
                        checked if current else None,
                        checked,
                        checked,
                        ch["id"],
                    ),
                )
                conn.commit()
                continue

            if bool(previous) != current:
                changed = now_iso()

                if current:
                    conn.execute(
                        """UPDATE channels SET is_live=1, live_started_at=?,
                           last_checked_at=?, last_changed_at=?, last_error=NULL WHERE id=?""",
                        (changed, checked, changed, ch["id"]),
                    )
                    conn.execute(
                        "INSERT INTO events(channel_id,username,event_type,event_at) VALUES(?,?,?,?)",
                        (ch["id"], username, "START", changed),
                    )
                    conn.commit()
                    try:
                        send_telegram(f"🟢 @{username} เริ่ม LIVE แล้ว")
                    except Exception as e:
                        print(f"Telegram START failed for @{username}: {e}")
                else:
                    started = ch["live_started_at"]
                    duration = None
                    if started:
                        try:
                            start_dt = datetime.fromisoformat(started)
                            end_dt = datetime.fromisoformat(changed)
                            duration = int((end_dt - start_dt).total_seconds())
                        except Exception:
                            duration = None

                    conn.execute(
                        """UPDATE channels SET is_live=0, live_started_at=NULL,
                           last_checked_at=?, last_changed_at=?, last_error=NULL WHERE id=?""",
                        (checked, changed, ch["id"]),
                    )
                    conn.execute(
                        """INSERT INTO events(channel_id,username,event_type,event_at,duration_seconds)
                           VALUES(?,?,?,?,?)""",
                        (ch["id"], username, "STOP", changed, duration),
                    )
                    conn.commit()

                    msg = f"🔴 @{username} หยุด LIVE แล้ว"
                    if duration is not None:
                        msg += f"\n⏱ ระยะเวลา {fmt_duration(duration)}"
                    try:
                        send_telegram(msg)
                    except Exception as e:
                        print(f"Telegram STOP failed for @{username}: {e}")
            else:
                conn.execute(
                    "UPDATE channels SET last_checked_at=?, last_error=NULL WHERE id=?",
                    (checked, ch["id"]),
                )
                conn.commit()

        except Exception as e:
            conn.execute(
                "UPDATE channels SET last_checked_at=?, last_error=? WHERE id=?",
                (checked, f"internal checker error: {str(e)[:300]}", ch["id"]),
            )
            conn.commit()

    conn.close()


def poll_loop():
    while True:
        try:
            check_once()
        except Exception as e:
            print("poll loop error:", e)
        time.sleep(CHECK_INTERVAL)


def summary_loop():
    while True:
        time.sleep(SUMMARY_INTERVAL)
        try:
            send_status_summary()
        except Exception as e:
            print("summary loop error:", e)


def start_poller():
    global poller_started
    with poller_lock:
        if poller_started:
            return
        poller_started = True
        threading.Thread(target=poll_loop, daemon=True).start()
        threading.Thread(target=summary_loop, daemon=True).start()


@app.before_request
def _start():
    start_poller()


@app.route("/")
def index():
    return render_template("index.html", interval=CHECK_INTERVAL)


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/channels")
def channels():
    conn = db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM channels ORDER BY username").fetchall()]
    conn.close()
    return jsonify(rows)


@app.post("/api/channels")
def add_channel():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip().lstrip("@")
    if not username:
        return jsonify({"error": "username required"}), 400
    conn = db()
    try:
        conn.execute("INSERT INTO channels(username) VALUES(?)", (username,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "channel already exists"}), 409
    conn.close()
    return jsonify({"ok": True})


@app.delete("/api/channels/<int:channel_id>")
def delete_channel(channel_id):
    conn = db()
    conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.get("/api/events")
def events():
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT 100"
    ).fetchall()]
    conn.close()
    return jsonify(rows)


@app.post("/api/check-now")
def check_now():
    threading.Thread(target=check_once, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/test-telegram")
def test_telegram():
    try:
        send_telegram("✅ TikTok LIVE Dashboard เชื่อม Telegram สำเร็จ")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


init_db()

if __name__ == "__main__":
    start_poller()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
