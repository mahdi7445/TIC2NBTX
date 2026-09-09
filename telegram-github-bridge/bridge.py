# -*- coding: utf-8 -*-
"""Telegram <-> GitHub bridge.

Runs on GitHub Actions. It is the only component that talks to Telegram.
The Windows executor talks only to GitHub and Nobitex.
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
MAX_SIGNAL_LINES = int(os.environ.get("MAX_SIGNAL_LINES", "5000"))
OUTBOX_FILE = "outbox.jsonl"
BRIDGE_STATE_FILE = "bridge_state.json"
LOOP_MAX_SECONDS = int(os.environ.get("LOOP_MAX_SECONDS", str(5 * 3600 + 20 * 60)))
POLL_INTERVAL_SECONDS = max(2, int(os.environ.get("POLL_INTERVAL_SECONDS", "5")))


def load_state() -> dict:
    if not os.path.exists(BRIDGE_STATE_FILE):
        return {"telegram_update_offset": 0, "outbox_lines_sent": 0}
    try:
        with open(BRIDGE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"telegram_update_offset": 0, "outbox_lines_sent": 0}


def save_state(state: dict) -> None:
    tmp = BRIDGE_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, BRIDGE_STATE_FILE)


def git_commit_and_push(message: str) -> None:
    subprocess.run(["git", "config", "user.name", "telegram-github-bridge"], check=False)
    subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=False)
    subprocess.run(["git", "add", SIGNALS_FILE, OUTBOX_FILE, BRIDGE_STATE_FILE], check=False)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", message], check=True)
    # The bridge is the writer of signals.jsonl; executor may concurrently write
    # outbox.jsonl through the Contents API. Retry a push race rather than lose
    # the whole bridge cycle.
    for attempt in range(4):
        result = subprocess.run(["git", "push"], capture_output=True, text=True)
        if result.returncode == 0:
            return
        if attempt == 3:
            raise RuntimeError(result.stderr[-1000:])
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        time.sleep(1.5 * (attempt + 1))


def poll_telegram_to_signals(state: dict) -> bool:
    resp = requests.get(
        f"{TELEGRAM_API}/getUpdates",
        params={
            "offset": state.get("telegram_update_offset", 0),
            "timeout": 0,
            "allowed_updates": json.dumps(["channel_post"]),
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        log.error("Telegram getUpdates error: %s", data)
        return False

    new_lines = []
    for update in data.get("result", []):
        # Advance the offset only after inspecting the update. It is persisted
        # after the loop and committed together with signals.jsonl.
        state["telegram_update_offset"] = max(
            int(state.get("telegram_update_offset", 0)), int(update["update_id"]) + 1
        )
        post = update.get("channel_post")
        if not post or str(post.get("chat", {}).get("id")) != str(TELEGRAM_CHANNEL_ID):
            continue
        text = post.get("text") or post.get("caption")
        if not text:
            continue
        new_lines.append(json.dumps({
            "update_id": update["update_id"],
            "message_id": post.get("message_id"),
            "ts": time.time(),
            "text": text,
        }, ensure_ascii=False))

    if not new_lines:
        return bool(data.get("result"))

    existing = []
    if os.path.exists(SIGNALS_FILE):
        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            existing = f.readlines()
    combined = existing + [line + "\n" for line in new_lines]
    if len(combined) > MAX_SIGNAL_LINES:
        combined = combined[-MAX_SIGNAL_LINES:]
    tmp = SIGNALS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(combined)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SIGNALS_FILE)
    log.info("%d Telegram posts appended to signals.jsonl (retained=%d)", len(new_lines), len(combined))
    return True


def relay_outbox_to_telegram(state: dict) -> bool:
    if not TELEGRAM_ADMIN_CHAT_ID or not os.path.exists(OUTBOX_FILE):
        return False

    with open(OUTBOX_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    sent = int(state.get("outbox_lines_sent", 0))
    if sent > len(lines):
        # File was rotated/rebuilt. Do not silently skip messages.
        sent = 0
        state["outbox_lines_sent"] = 0

    changed = False
    index = sent
    for index in range(sent, len(lines)):
        line = lines[index].strip()
        if not line:
            state["outbox_lines_sent"] = index + 1
            changed = True
            continue
        try:
            entry = json.loads(line)
            text = str(entry.get("text", line))
        except json.JSONDecodeError:
            text = line

        try:
            r = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "text": text[:4000]},
                timeout=10,
            )
            r.raise_for_status()
            body = r.json()
            if not body.get("ok"):
                raise RuntimeError(str(body))
        except Exception as e:
            # IMPORTANT: do not advance the cursor. The same message will be
            # retried on the next bridge loop/job.
            log.error("outbox line %d was not delivered: %s", index, e)
            break

        state["outbox_lines_sent"] = index + 1
        changed = True

    return changed


def main() -> None:
    state = load_state()
    log.info("Telegram-GitHub bridge started")
    start = time.time()
    while time.time() - start < LOOP_MAX_SECONDS:
        changed = False
        try:
            changed |= poll_telegram_to_signals(state)
        except Exception:
            log.exception("Telegram polling failed")
        try:
            changed |= relay_outbox_to_telegram(state)
        except Exception:
            log.exception("outbox relay failed")

        save_state(state)
        if changed:
            try:
                git_commit_and_push(f"chore: sync signals/outbox @ {int(time.time())} [skip ci]")
            except Exception:
                log.exception("git commit/push failed")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
