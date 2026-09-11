# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import random
from typing import Any, Dict

DEFAULT_PATH = os.environ.get("STATE_FILE_PATH", "state.json")


def load_state(path: str = DEFAULT_PATH) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {
            "telegram_update_offset": 0,
            "github_signals_processed_lines": 0,
            "last_signal_update_id": 0,
            "open_trades": {},
            "pending_protection": {},
            "needs_flatten": {},
            "unparsed_messages": [],
            "processed_signal_ids": [],
            "processed_command_ids": [],
            "last_successful_poll_ts": None,
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("open_trades", {})
        data.setdefault("pending_protection", {})
        data.setdefault("needs_flatten", {})
        data.setdefault("github_signals_processed_lines", 0)
        data.setdefault("last_signal_update_id", 0)
        data.setdefault("processed_signal_ids", [])
        data.setdefault("processed_command_ids", [])
        return data
    except Exception:
        corrupt = f"{path}.corrupt-{int(__import__('time').time())}"
        try:
            os.replace(path, corrupt)
        except OSError:
            pass
        return {
            "telegram_update_offset": 0,
            "github_signals_processed_lines": 0,
            "last_signal_update_id": 0,
            "open_trades": {},
            "pending_protection": {},
            "needs_flatten": {},
            "unparsed_messages": [],
            "processed_signal_ids": [],
            "processed_command_ids": [],
            "last_successful_poll_ts": None,
        }


def save_state(state: Dict[str, Any], path: str = DEFAULT_PATH) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp-{os.getpid()}-{random.randint(0, 999999)}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
