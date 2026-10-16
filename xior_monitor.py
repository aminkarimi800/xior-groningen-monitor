# -*- coding: utf-8 -*-
"""
Xior Groningen availability monitor.

Watches https://www.xiorstudenthousing.eu/netherlands/groningen/ and notifies you
as soon as one of the watched residences is no longer "Fully booked".

The site is behind Cloudflare, so a real browser (Playwright) is used to load the page:
Microsoft Edge on Windows, Playwright's bundled Chromium elsewhere (e.g. GitHub Actions).

Usage:
    python xior_monitor.py            # run forever, checking every N minutes
    python xior_monitor.py --once     # single check (GitHub Actions / cron / Task Scheduler)
    python xior_monitor.py --test     # send a test notification and exit

Secrets can come from config.json or from environment variables
(BALE_BOT_TOKEN, BALE_CHAT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "monitor.log"

URL = "https://www.xiorstudenthousing.eu/netherlands/groningen/"
BOOKED_TEXT = "fully booked"
FAILS_BEFORE_ALERT = 6
IS_WINDOWS = sys.platform == "win32"
TEHRAN = timezone(timedelta(hours=3, minutes=30))

DEFAULT_CONFIG = {
    # Exact residence names as shown on the page ("Zernike Tower - Short Stay" is NOT included)
    "watch": ["Oosterhamrikkade", "Eendrachtskade", "Zernike Tower"],
    "interval_minutes": 5,
    "headless": True,
    # Windows popup + sound (ignored on Linux)
    "windows_notification": True,
    # Optional: Telegram bot  -> https://t.me/BotFather
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    # Optional: Bale bot (same API as Telegram) -> https://ble.ir/botfather
    "bale_bot_token": "",
    "bale_chat_id": "",
}
ENV_KEYS = ["bale_bot_token", "bale_chat_id", "telegram_bot_token", "telegram_chat_id"]


def now_str():
    return datetime.now(TEHRAN).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{now_str()}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    if CONFIG_FILE.exists():
        user_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    elif os.environ.get("GITHUB_ACTIONS"):
        user_cfg = {}
    else:
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"Created default config: {CONFIG_FILE}")
        user_cfg = {}
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(user_cfg)
    for key in ENV_KEYS:
        if os.environ.get(key.upper()):
            cfg[key] = os.environ[key.upper()].strip()
    return cfg


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- scraping

def fetch_statuses(headless=True):
    """Return {residence name: status label} for every residence card on the page.
    An empty label means the card has no status badge (i.e. bookable)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge" if IS_WINDOWS else None,
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            # The site blocks the default headless user agent ("HeadlessChrome")
            probe = browser.new_page()
            user_agent = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            probe.close()

            page = browser.new_page(
                locale="en-US",
                user_agent=user_agent,
                viewport={"width": 1366, "height": 900},
            )
            page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
            # Cloudflare "Checking your browser..." page redirects by itself
            try:
                page.wait_for_selector(".filtered-homes-col h3", state="attached", timeout=90_000)
            except Exception:
                raise RuntimeError(f"Residence list not found (page title: {page.title()!r})")
            return page.evaluate(
                """() => {
                    const out = {};
                    document.querySelectorAll('.filtered-homes-col').forEach(col => {
                        const h = col.querySelector('h3');
                        if (!h) return;
                        const badge = col.querySelector('.discount-bar');
                        out[h.textContent.trim()] = badge ? badge.innerText.replace(/\\s+/g, ' ').trim() : '';
                    });
                    return out;
                }"""
            )
        finally:
            browser.close()


def is_booked(label):
    return BOOKED_TEXT in (label or "").lower()


# ---------------------------------------------------------------- notifications

def notify_windows(title, message):
    try:
        import ctypes
        import threading
        import winsound

        for _ in range(3):
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            time.sleep(0.4)
        # Non-blocking, always-on-top message box
        threading.Thread(
            target=ctypes.windll.user32.MessageBoxW,
            args=(0, message, title, 0x40 | 0x40000 | 0x1000),  # INFO | TOPMOST | SYSTEMMODAL
            daemon=False,
        ).start()
    except Exception as e:
        log(f"Windows notification failed: {e}")


def notify_bot(api_base, token, chat_id, text):
    url = f"{api_base}/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
        body = json.loads(r.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(body)


def notify(cfg, title, message):
    """Send to every configured channel. Returns True if at least one succeeded."""
    log(f"NOTIFY: {title} | {message}")
    text = f"{title}\n\n{message}"
    sent = False
    bots = [
        ("Bale", "https://tapi.bale.ai", cfg.get("bale_bot_token"), cfg.get("bale_chat_id")),
        ("Telegram", "https://api.telegram.org", cfg.get("telegram_bot_token"), cfg.get("telegram_chat_id")),
    ]
    for name, api_base, token, chat_id in bots:
        if not (token and chat_id):
            continue
        try:
            notify_bot(api_base, token, chat_id, text)
            log(f"{name}: sent")
            sent = True
        except Exception as e:
            log(f"{name} failed: {e}")
    if IS_WINDOWS and cfg.get("windows_notification", True):
        notify_windows(title, message)
        sent = True
    if not sent:
        log("WARNING: no notification channel delivered this message")
    return sent


# ---------------------------------------------------------------- main logic

def check_once(cfg):
    statuses = fetch_statuses(cfg.get("headless", True))
    state = load_state()
    residences = state.setdefault("residences", {})
    summary = []

    for name in cfg["watch"]:
        if name not in statuses:
            summary.append(f"{name}: NOT FOUND")
            continue
        label = statuses[name]
        booked = is_booked(label)
        summary.append(f"{name}: {label or '(no badge)'}")

        prev = residences.get(name)
        was_booked = prev is None or prev.get("booked", True)

        if was_booked and not booked:
            notify(
                cfg,
                f"🏠 Xior: {name} is AVAILABLE!",
                f"'{name}' is no longer fully booked.\nStatus now: {label or '(no badge - bookable)'}\n\n{URL}",
            )
        elif prev is not None and not was_booked and booked:
            notify(cfg, f"Xior: {name} fully booked again", f"'{name}' is fully booked again.\n{URL}")

        if prev is None or prev.get("booked") != booked or prev.get("label") != label:
            residences[name] = {"booked": booked, "label": label, "since": now_str()}

    missing = [n for n in cfg["watch"] if n not in statuses]
    if missing and len(missing) == len(cfg["watch"]):
        raise RuntimeError(f"None of the watched residences found on page (found: {list(statuses)})")
    if missing:
        log(f"WARNING: not found on page: {missing} (found: {list(statuses)})")

    state["failures"] = 0
    save_state(state)
    log("Check OK -> " + " | ".join(summary))


def record_failure(cfg, error):
    """Count consecutive failures in state.json; alert once when the threshold is reached."""
    state = load_state()
    state["failures"] = state.get("failures", 0) + 1
    log(f"Check failed ({state['failures']}): {error}")
    if state["failures"] == FAILS_BEFORE_ALERT:
        notify(cfg, "⚠️ Xior monitor - problem", f"{FAILS_BEFORE_ALERT} checks in a row failed.\nLast error: {error}")
    save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="check one time and exit")
    ap.add_argument("--test", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()

    cfg = load_config()

    if args.test:
        ok = notify(cfg, "✅ Xior monitor - test", "If you see this, notifications work.")
        sys.exit(0 if ok else 1)

    if args.once:
        try:
            check_once(cfg)
        except Exception as e:
            # exit 0 on purpose: a temporary site/Cloudflare hiccup should not mark the
            # scheduled run as failed (GitHub emails on every failed run). After
            # FAILS_BEFORE_ALERT failures in a row you get a notification instead.
            record_failure(cfg, e)
        return

    log(f"Monitoring {cfg['watch']} every ~{cfg['interval_minutes']} min. Ctrl+C to stop.")
    while True:
        try:
            check_once(cfg)
        except Exception as e:
            record_failure(cfg, e)
        # small random jitter so requests are not perfectly periodic
        time.sleep(cfg["interval_minutes"] * 60 + random.randint(0, 60))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
