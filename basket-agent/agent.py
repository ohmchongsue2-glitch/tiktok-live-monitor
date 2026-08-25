import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

CONFIG_PATH = Path(__file__).with_name("config.json")
TEMPLATE_SCORE_RE = re.compile(r"template\s*score\s*[=:]\s*([01](?:\.\d+)?)", re.I)


def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit("ไม่พบ config.json กรุณาคัดลอก config.example.json เป็น config.json แล้วใส่ค่า")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["dashboard_url"] = cfg["dashboard_url"].rstrip("/")
    cfg.setdefault("interval_seconds", 90)
    cfg.setdefault("headless", False)
    cfg.setdefault("only_live", True)
    cfg.setdefault("template_threshold", 0.85)
    return cfg


def api_headers(cfg):
    return {"X-Agent-Key": str(cfg["agent_key"]), "Content-Type": "application/json"}


def fetch_channels(cfg):
    r = requests.get(cfg["dashboard_url"] + "/api/agent/channels", headers=api_headers(cfg), timeout=20)
    r.raise_for_status()
    rows = r.json()
    if cfg.get("only_live", True):
        rows = [x for x in rows if x.get("is_live") == 1]
    return rows


def push_result(cfg, username, status, error=None):
    # Compatibility fix for the visual/template detector used on some PCs.
    # If it reports e.g. "template score=0.92" while status is unknown,
    # convert it to HAS when the score reaches the configured threshold.
    if status == "unknown" and error:
        m = TEMPLATE_SCORE_RE.search(str(error))
        if m:
            score = float(m.group(1))
            threshold = float(cfg.get("template_threshold", 0.85))
            if score >= threshold:
                status = "has"
                error = f"template score={score:.2f} >= {threshold:.2f}"

    payload = {"username": username, "status": status, "error": error}
    r = requests.post(cfg["dashboard_url"] + "/api/agent/basket-result", headers=api_headers(cfg), json=payload, timeout=20)
    r.raise_for_status()


def detect_basket(page, username):
    url = f"https://www.tiktok.com/@{username}/live"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(5000)
    except PlaywrightTimeoutError:
        return "unknown", "เปิดหน้า TikTok timeout"

    current = page.url.lower()
    html = page.content().lower()
    text = page.locator("body").inner_text(timeout=5000).lower()
    combined = html + "\n" + text

    anti = ["captcha", "verify to continue", "security verification", "access denied", "too many requests"]
    if any(x in combined for x in anti):
        return "unknown", "TikTok anti-bot / verification"

    selectors = [
        '[data-e2e*="product"]', '[data-e2e*="shop"]', '[data-e2e*="commerce"]',
        'a[href*="/product/"]', 'a[href*="shop.tiktok.com"]',
        '[class*="Product"]', '[class*="product"]', '[class*="Shopping"]', '[class*="shopping"]'
    ]
    for sel in selectors:
        try:
            if page.locator(sel).count() > 0:
                return "has", None
        except Exception:
            pass

    markers = [
        "product_list", "productlist", "product_anchor", "productanchor",
        "shopping_bag", "shoppingbag", "live_product", "liveproduct",
        "commerce_info", "commerceinfo", "shop_live", "product_id", "productid"
    ]
    if any(m in combined for m in markers):
        return "has", None

    if f"/@{username.lower()}/live" in current:
        return "none", None

    return "unknown", f"TikTok redirect ไป {page.url[:120]}"


def main():
    cfg = load_config()
    profile = str(Path(__file__).with_name("chrome-profile"))
    print("Basket Agent เริ่มทำงาน")
    print("Dashboard:", cfg["dashboard_url"])
    print("Template threshold:", cfg.get("template_threshold", 0.85))

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            profile,
            headless=bool(cfg.get("headless", False)),
            viewport={"width": 1280, "height": 900},
            locale="th-TH",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        while True:
            try:
                channels = fetch_channels(cfg)
                print(f"พบ {len(channels)} ช่องที่จะตรวจ")
                for ch in channels:
                    username = ch["username"]
                    try:
                        status, error = detect_basket(page, username)
                        push_result(cfg, username, status, error)
                        print(f"@{username}: {status}" + (f" | {error}" if error else ""))
                    except Exception as e:
                        err = f"agent error: {type(e).__name__}: {str(e)[:180]}"
                        try:
                            push_result(cfg, username, "unknown", err)
                        except Exception:
                            pass
                        print(f"@{username}: ERROR {err}")
                    time.sleep(2)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print("รอบนี้ผิดพลาด:", type(e).__name__, str(e)[:250])

            time.sleep(int(cfg.get("interval_seconds", 90)))

        context.close()


if __name__ == "__main__":
    main()
