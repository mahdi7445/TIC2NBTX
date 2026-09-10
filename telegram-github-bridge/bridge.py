# -*- coding: utf-8 -*-
"""Telegram <-> GitHub relay for the TIC2NBTX admin/control plane."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("telegram-github-bridge")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = str(os.environ["TELEGRAM_CHANNEL_ID"])
ADMIN_CHAT_ID = str(os.environ["TELEGRAM_ADMIN_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"

SIGNALS = Path("signals.jsonl")
COMMANDS = Path("commands.jsonl")
OUTBOX = Path("outbox.jsonl")
BRIDGE_STATE = Path("bridge_state.json")
MAX_LINES = int(os.environ.get("MAX_RELAY_LINES", "5000"))
RUNTIME_SECONDS = int(os.environ.get("BRIDGE_RUNTIME_SECONDS", "275"))
LONG_POLL_SECONDS = int(os.environ.get("TELEGRAM_LONG_POLL_SECONDS", "25"))

BOT_COMMANDS = [
    {"command": "start", "description": "باز کردن منوی اصلی"},
    {"command": "menu", "description": "منوی کامل مدیریت"},
    {"command": "status", "description": "وضعیت سیستم"},
    {"command": "balance", "description": "موجودی Nobitex"},
    {"command": "positions", "description": "معاملات باز"},
    {"command": "config", "description": "تنظیمات ربات"},
    {"command": "resume", "description": "فعال‌سازی ورود معاملات"},
    {"command": "pause", "description": "توقف ورود معاملات"},
    {"command": "test_api", "description": "تست اتصال Nobitex"},
    {"command": "reconcile", "description": "همگام‌سازی معاملات"},
    {"command": "protection", "description": "بررسی محافظت معاملات"},
    {"command": "logs", "description": "آخرین لاگ‌های Executor"},
    {"command": "risk", "description": "تنظیم سقف ریسک"},
    {"command": "collateral", "description": "تنظیم مارجین"},
    {"command": "maxtrades", "description": "حداکثر معاملات همزمان"},
    {"command": "closeall", "description": "بستن همه معاملات"},
]


def load_state() -> dict:
    if not BRIDGE_STATE.exists():
        return {"telegram_update_offset": 0, "outbox_lines_sent": 0}
    try:
        return json.loads(BRIDGE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"telegram_update_offset": 0, "outbox_lines_sent": 0}


def save_state(state: dict) -> None:
    tmp = BRIDGE_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(BRIDGE_STATE)


def append_lines(path: Path, new_lines: list[str]) -> None:
    if not new_lines:
        return
    old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    combined = (old + [x.rstrip("\n") for x in new_lines])[-MAX_LINES:]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(combined) + "\n", encoding="utf-8")
    tmp.replace(path)


def ensure_runtime_files() -> None:
    for path in (SIGNALS, COMMANDS, OUTBOX):
        if not path.exists():
            path.write_text("", encoding="utf-8")


def telegram_call(method: str, payload: dict | None = None) -> dict:
    r = requests.post(API + "/" + method, json=payload or {}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API error: {body}")
    return body


def send_message(text: str, inline_keyboard=None, reply_keyboard=None) -> None:
    payload = {"chat_id": ADMIN_CHAT_ID, "text": str(text)[:4000], "disable_web_page_preview": True}
    if inline_keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    elif reply_keyboard is not None:
        payload["reply_markup"] = {"keyboard": reply_keyboard, "resize_keyboard": True, "is_persistent": True}
    telegram_call("sendMessage", payload)


def answer_callback(callback_id: str) -> None:
    try:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception:
        pass


def setup_bot_commands() -> None:
    try:
        telegram_call("setMyCommands", {"commands": BOT_COMMANDS})
    except Exception:
        log.exception("Could not configure Telegram commands")


def reply_keyboard():
    return [
        ["📊 وضعیت سیستم", "💰 موجودی"],
        ["📈 معاملات باز", "⚙️ تنظیمات"],
        ["▶️ فعال‌سازی", "⏸ توقف ورود"],
        ["🧪 تست API", "🔄 همگام‌سازی"],
        ["🛡️ بررسی محافظت", "🧾 آخرین لاگ‌ها"],
        ["📌 ریسک", "💵 مارجین"],
        ["🔢 حداکثر معاملات", "🚨 بستن همه معاملات"],
        ["📋 منوی کامل"],
    ]


def inline_main_menu():
    return [
        [{"text": "📊 وضعیت سیستم", "callback_data": "cmd:/status"}, {"text": "💰 موجودی", "callback_data": "cmd:/balance"}],
        [{"text": "📈 معاملات باز", "callback_data": "cmd:/positions"}, {"text": "⚙️ تنظیمات", "callback_data": "cmd:/config"}],
        [{"text": "▶️ فعال‌سازی", "callback_data": "cmd:/resume"}, {"text": "⏸ توقف ورود", "callback_data": "cmd:/pause"}],
        [{"text": "🧪 تست API", "callback_data": "cmd:/test_api"}, {"text": "🔄 همگام‌سازی", "callback_data": "cmd:/reconcile"}],
        [{"text": "🛡️ بررسی محافظت", "callback_data": "cmd:/protection"}, {"text": "🧾 آخرین لاگ‌ها", "callback_data": "cmd:/logs"}],
        [{"text": "📌 ریسک", "callback_data": "risk_menu"}, {"text": "💵 مارجین", "callback_data": "collateral_menu"}],
        [{"text": "🔢 حداکثر معاملات", "callback_data": "maxtrades_menu"}],
        [{"text": "🚨 بستن همه معاملات", "callback_data": "closeall_confirm"}],
    ]


def append_command(update_id: int, command: str) -> None:
    append_lines(COMMANDS, [json.dumps({
        "command_id": f"tg-{update_id}",
        "update_id": update_id,
        "ts": time.time(),
        "command": command,
    }, ensure_ascii=False)])


def send_menu() -> None:
    send_message(
        "🤖 پنل مدیریت TRADE IS COOL\n\nتمام کنترل‌های Executor و Nobitex از این منو در دسترس است.",
        inline_keyboard=inline_main_menu(),
        reply_keyboard=reply_keyboard(),
    )


def handle_message(update: dict) -> bool:
    msg = update.get("message") or {}
    chat = msg.get("chat", {})
    if str(chat.get("id")) != ADMIN_CHAT_ID:
        return False
    text = (msg.get("text") or "").strip()

    if text in {"/start", "/menu", "menu", "/help", "help", "📋 منوی کامل"}:
        send_menu()
        return True

    keyboard_commands = {
        "📊 وضعیت سیستم": "/status", "💰 موجودی": "/balance",
        "📈 معاملات باز": "/positions", "⚙️ تنظیمات": "/config",
        "▶️ فعال‌سازی": "/resume", "⏸ توقف ورود": "/pause",
        "🧪 تست API": "/test_api", "🔄 همگام‌سازی": "/reconcile",
        "🛡️ بررسی محافظت": "/protection", "🧾 آخرین لاگ‌ها": "/logs",
    }
    if text in keyboard_commands:
        command = keyboard_commands[text]
        append_command(int(update["update_id"]), command)
        send_message(f"📤 فرمان {command} برای Executor ویندوز در GitHub صف شد.")
        return True

    if text == "📌 ریسک":
        send_message("🎯 سقف ریسک هر معامله", inline_keyboard=[
            [{"text": "0.25 USDT", "callback_data": "cmd:/risk 0.25"}, {"text": "0.50 USDT", "callback_data": "cmd:/risk 0.50"}],
            [{"text": "1.00 USDT", "callback_data": "cmd:/risk 1"}, {"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if text == "💵 مارجین":
        send_message("💵 سقف وجه تضمین هر معامله", inline_keyboard=[
            [{"text": "1.00", "callback_data": "cmd:/collateral 1"}, {"text": "1.25", "callback_data": "cmd:/collateral 1.25"}],
            [{"text": "1.50", "callback_data": "cmd:/collateral 1.5"}, {"text": "2.00", "callback_data": "cmd:/collateral 2"}],
            [{"text": "5.00", "callback_data": "cmd:/collateral 5"}, {"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if text == "🔢 حداکثر معاملات":
        send_message("🔢 حداکثر تعداد پوزیشن همزمان", inline_keyboard=[
            [{"text": "5", "callback_data": "cmd:/maxtrades 5"}, {"text": "10", "callback_data": "cmd:/maxtrades 10"}],
            [{"text": "15", "callback_data": "cmd:/maxtrades 15"}, {"text": "20", "callback_data": "cmd:/maxtrades 20"}],
            [{"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if text == "🚨 بستن همه معاملات":
        send_message("⚠️ تأیید نهایی\n\nآیا واقعاً می‌خواهید تمام پوزیشن‌های باز با Market بسته شوند؟", inline_keyboard=[
            [{"text": "❌ بله، همه را ببند", "callback_data": "cmd:/closeall"}],
            [{"text": "↩️ لغو", "callback_data": "menu"}],
        ])
        return True

    if text.startswith("/"):
        append_command(int(update["update_id"]), text)
        send_message(f"📤 فرمان {text} دریافت شد و برای Executor ویندوز در GitHub صف شد.")
        return True

    send_menu()
    return True


def handle_callback(update: dict) -> bool:
    q = update.get("callback_query") or {}
    msg = q.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != ADMIN_CHAT_ID:
        return False
    answer_callback(str(q.get("id", "")))
    data = str(q.get("data", ""))
    uid = int(update["update_id"])

    if data == "menu":
        send_menu()
        return True

    if data == "risk_menu":
        send_message("🎯 سقف ریسک هر معامله", inline_keyboard=[
            [{"text": "0.25 USDT", "callback_data": "cmd:/risk 0.25"}, {"text": "0.50 USDT", "callback_data": "cmd:/risk 0.50"}],
            [{"text": "1.00 USDT", "callback_data": "cmd:/risk 1"}, {"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if data == "collateral_menu":
        send_message("💵 سقف وجه تضمین هر معامله", inline_keyboard=[
            [{"text": "1.00", "callback_data": "cmd:/collateral 1"}, {"text": "1.25", "callback_data": "cmd:/collateral 1.25"}],
            [{"text": "1.50", "callback_data": "cmd:/collateral 1.5"}, {"text": "2.00", "callback_data": "cmd:/collateral 2"}],
            [{"text": "5.00", "callback_data": "cmd:/collateral 5"}, {"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if data == "maxtrades_menu":
        send_message("🔢 حداکثر تعداد پوزیشن همزمان", inline_keyboard=[
            [{"text": "5", "callback_data": "cmd:/maxtrades 5"}, {"text": "10", "callback_data": "cmd:/maxtrades 10"}],
            [{"text": "15", "callback_data": "cmd:/maxtrades 15"}, {"text": "20", "callback_data": "cmd:/maxtrades 20"}],
            [{"text": "↩️ منو", "callback_data": "menu"}],
        ])
        return True

    if data == "closeall_confirm":
        send_message("⚠️ تأیید نهایی\n\nآیا واقعاً می‌خواهید تمام پوزیشن‌های باز با Market بسته شوند؟", inline_keyboard=[
            [{"text": "❌ بله، همه را ببند", "callback_data": "cmd:/closeall"}],
            [{"text": "↩️ لغو", "callback_data": "menu"}],
        ])
        return True

    if data.startswith("cmd:"):
        command = data[4:].strip()
        append_command(uid, command)
        send_message(f"📤 فرمان {command} برای Executor ویندوز در GitHub صف شد.")
        return True

    return False


def poll_telegram(state: dict) -> bool:
    offset = int(state.get("telegram_update_offset", 0))
    r = requests.get(API + "/getUpdates", params={
        "offset": offset,
        "timeout": LONG_POLL_SECONDS,
        "allowed_updates": json.dumps(["channel_post", "message", "callback_query"]),
    }, timeout=LONG_POLL_SECONDS + 10)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(str(body))

    changed = False
    signal_lines: list[str] = []
    for update in body.get("result", []):
        uid = int(update.get("update_id", 0))
        state["telegram_update_offset"] = max(int(state.get("telegram_update_offset", 0)), uid + 1)
        post = update.get("channel_post") or {}
        if post and str(post.get("chat", {}).get("id")) == CHANNEL_ID:
            text = post.get("text") or post.get("caption")
            if text:
                message_id = post.get("message_id")
                signal_lines.append(json.dumps({
                    "update_id": uid,
                    "message_id": message_id,
                    "signal_id": f"{CHANNEL_ID}:{message_id}" if message_id else str(uid),
                    "ts": time.time(),
                    "text": text,
                }, ensure_ascii=False))
                changed = True
        if update.get("message"):
            changed |= handle_message(update)
        if update.get("callback_query"):
            changed |= handle_callback(update)

    if signal_lines:
        append_lines(SIGNALS, signal_lines)
        log.info("Relayed %d Telegram channel posts to signals.jsonl", len(signal_lines))
    return changed or bool(body.get("result"))


def relay_outbox(state: dict) -> bool:
    if not OUTBOX.exists():
        return False
    lines = OUTBOX.read_text(encoding="utf-8").splitlines()
    sent = int(state.get("outbox_lines_sent", 0))
    if sent > len(lines):
        sent = 0
    changed = False
    for idx in range(sent, len(lines)):
        line = lines[idx].strip()
        if not line:
            state["outbox_lines_sent"] = idx + 1
            changed = True
            continue
        try:
            text = str(json.loads(line).get("text", line))
        except Exception:
            text = line
        try:
            send_message(text)
        except Exception as e:
            log.error("outbox delivery failed at line %s: %s", idx, e)
            break
        state["outbox_lines_sent"] = idx + 1
        changed = True
    return changed


def git_sync_and_push() -> None:
    ensure_runtime_files()
    subprocess.run(["git", "config", "user.name", "telegram-github-bridge"], check=True)
    subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "signals.jsonl", "commands.jsonl", "outbox.jsonl", "bridge_state.json"], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", f"chore: telegram relay @ {int(time.time())} [skip ci]"], check=True)
    for attempt in range(5):
        r = subprocess.run(["git", "push", "origin", "HEAD:main"], capture_output=True, text=True)
        if r.returncode == 0:
            return
        if attempt == 4:
            raise RuntimeError(r.stderr[-1500:])
        subprocess.run(["git", "fetch", "origin", "main"], check=False)
        subprocess.run(["git", "rebase", "origin/main"], check=False)
        time.sleep(1 + attempt)


def main() -> None:
    ensure_runtime_files()
    setup_bot_commands()
    state = load_state()
    start = time.time()
    changed_any = False
    log.info("Telegram-GitHub relay started for %.0fs", RUNTIME_SECONDS)
    while time.time() - start < RUNTIME_SECONDS:
        try:
            changed_any |= poll_telegram(state)
        except Exception:
            log.exception("Telegram polling failed")
        try:
            changed_any |= relay_outbox(state)
        except Exception:
            log.exception("outbox relay failed")
        save_state(state)
        time.sleep(0.5)
    if changed_any:
        git_sync_and_push()
    log.info("Telegram-GitHub relay finished")


if __name__ == "__main__":
    main()
