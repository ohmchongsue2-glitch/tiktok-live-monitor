import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import httpx
from flask import Flask, jsonify, render_template, request
from TikTokLive import TikTokLiveClient

DB_PATH = os.environ.get("DB_PATH", "dashboard.db")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
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
        print("Telegram not configured:", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    with httpx.Client(timeout=15) as client:
        r = client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
        r.raise_for_status()


async def check_tiktok_live(username):
    client = TikTokLiveClient(unique_id=username)
    return bool(await client.is_live())


def check_once():
    import asyncio
    conn = db()
    channels = conn.execute("SELECT * FROM channels ORDER BY id").fetchall()

    for ch in channels:
        username = ch["username"]
        checked = now_iso()
        try:
            current = asyncio.run(check_tiktok_live(username))
            previous = ch["is_live"]

            # First successful observation establishes baseline without notification.
            if previous is None:
                conn.execute(
                    """UPDATE channels SET is_live=?, last_checked_at=?,
                       last_changed_at=?, last_error=NULL WHERE id=?""",
                    (1 if current else 0, checked, checked, ch["id"]),
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
                    send_telegram(f"🟢 @{username} เริ่ม LIVE แล้ว")
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
                    send_telegram(msg)
            else:
                conn.execute(
                    "UPDATE channels SET last_checked_at=?, last_error=NULL WHERE id=?",
                    (checked, ch["id"]),
                )
                conn.commit()

        except Exception as e:
            conn.execute(
                "UPDATE channels SET last_checked_at=?, last_error=? WHERE id=?",
                (checked, str(e)[:500], ch["id"]),
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


def start_poller():
    global poller_started
    with poller_lock:
        if poller_started:
            return
        poller_started = True
        threading.Thread(target=poll_loop, daemon=True).start()


@app.before_request
def _start():
    start_poller()


@app.route("/")
def index():
    return render_template("index.html", interval=CHECK_INTERVAL)


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



@app.get("/health")
def health():
    return jsonify({"ok": True})

init_db()

if __name__ == "__main__":
    start_poller()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
