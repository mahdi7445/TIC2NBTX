# -*- coding: utf-8 -*-
"""
پل تلگرام <-> گیت‌هاب.

این اسکریپت روی GitHub Actions اجرا می‌شود (نه روی کامپیوتر ویندوزی) چون
باید مستقیم به تلگرام برسد - و سرورهای GitHub Actions در ایران فیلتر
نیستند، پس نیازی به فیلترشکن ندارند.

دو کار می‌کند:
1) پیام‌های تازه‌ی کانال مشترک تلگرام را می‌خواند و در signals.jsonl
   (در همین ریپازیتوری) اضافه می‌کند - executor.py روی ویندوز این فایل را
   می‌خواند، بدون این‌که خودش مستقیم به تلگرام وصل شود.
2) هشدارهای ادمین را که executor.py (روی ویندوز) در outbox.jsonl نوشته،
   می‌خواند و برای شما در تلگرام می‌فرستد - چون فرستادن پیام تلگرام هم به
   همان دلیل باید از اینجا انجام شود، نه از کامپیوتر ویندوزی.

هیچ کلید نوبیتکسی اینجا وجود ندارد و نباید اضافه شود - این پروژه فقط
واسطه‌ی پیام است.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bridge")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

SIGNALS_FILE = "signals.jsonl"
OUTBOX_FILE = "outbox.jsonl"
BRIDGE_STATE_FILE = "bridge_state.json"

LOOP_MAX_SECONDS = int(os.environ.get("LOOP_MAX_SECONDS", str(5 * 3600 + 20 * 60)))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))


def load_state() -> dict:
    if not os.path.exists(BRIDGE_STATE_FILE):
        return {"telegram_update_offset": 0, "outbox_lines_sent": 0}
    with open(BRIDGE_STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(BRIDGE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def git_commit_and_push(message: str) -> None:
    subprocess.run(["git", "config", "user.name", "telegram-github-bridge"], check=False)
    subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=False)
    subprocess.run(["git", "add", SIGNALS_FILE, OUTBOX_FILE, BRIDGE_STATE_FILE], check=False)
    result = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if result.returncode == 0:
        return  # چیزی تغییر نکرده - نیازی به commit نیست
    subprocess.run(["git", "commit", "-m", message], check=True)
    subprocess.run(["git", "push"], check=True)


def poll_telegram_to_signals(state: dict) -> bool:
    """پیام‌های تازه‌ی کانال را می‌خواند و به signals.jsonl اضافه می‌کند."""
    resp = requests.get(
        f"{TELEGRAM_API}/getUpdates",
        params={
            "offset": state.get("telegram_update_offset", 0),
            "timeout": 0,
            "allowed_updates": '["channel_post"]',
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        log.error("خطای Telegram getUpdates: %s", data)
        return False

    changed = False
    new_lines = []
    for update in data.get("result", []):
        state["telegram_update_offset"] = update["update_id"] + 1
        post = update.get("channel_post")
        if not post:
            continue
        chat_id = str(post.get("chat", {}).get("id"))
        if chat_id != str(TELEGRAM_CHANNEL_ID):
            continue
        text = post.get("text") or post.get("caption")
        if not text:
            continue
        new_lines.append(json.dumps({
            "update_id": update["update_id"],
            "ts": time.time(),
            "text": text,
        }, ensure_ascii=False))
        changed = True

    if new_lines:
        with open(SIGNALS_FILE, "a", encoding="utf-8") as f:
            for line in new_lines:
                f.write(line + "\n")
        log.info("%d پیام تازه به signals.jsonl اضافه شد.", len(new_lines))

    return changed


def relay_outbox_to_telegram(state: dict) -> bool:
    """هشدارهایی که executor.py در outbox.jsonl نوشته را به ادمین می‌فرستد."""
    if not TELEGRAM_ADMIN_CHAT_ID or not os.path.exists(OUTBOX_FILE):
        return False

    with open(OUTBOX_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    already_sent = state.get("outbox_lines_sent", 0)
    new_lines = lines[already_sent:]
    if not new_lines:
        return False

    for line in new_lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            text = entry.get("text", line)
        except json.JSONDecodeError:
            text = line
        try:
            requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "text": text[:4000]},
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001
            log.error("ارسال پیام outbox به تلگرام ناموفق بود: %s", e)

    state["outbox_lines_sent"] = len(lines)
    log.info("%d پیام outbox به ادمین فرستاده شد.", len(new_lines))
    return True


def main() -> None:
    state = load_state()
    log.info("پل تلگرام-گیت‌هاب شروع شد.")

    start = time.time()
    while time.time() - start < LOOP_MAX_SECONDS:
        changed = False
        try:
            changed |= poll_telegram_to_signals(state)
        except Exception:
            log.exception("خطا در خواندن تلگرام")
        try:
            changed |= relay_outbox_to_telegram(state)
        except Exception:
            log.exception("خطا در ارسال outbox")

        save_state(state)
        if changed:
            try:
                git_commit_and_push(f"chore: sync signals/outbox @ {int(time.time())} [skip ci]")
            except Exception:
                log.exception("خطا در commit/push")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
