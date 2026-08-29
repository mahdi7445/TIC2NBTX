# -*- coding: utf-8 -*-
"""
ذخیره‌ی وضعیت پروژه در یک فایل JSON محلی (state.json).
این پروژه کاملاً مستقل از state.json دو ربات دیگر است - فایل و ریپازیتوری
جدا دارد، هیچ تداخلی رخ نمی‌دهد.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

DEFAULT_PATH = os.environ.get("STATE_FILE_PATH", "state.json")


def load_state(path: str = DEFAULT_PATH) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {
            "telegram_update_offset": 0,
            "open_trades": {},  # key: "SYMBOL_TF" -> trade dict
            "unparsed_messages": [],  # لاگ پیام‌هایی که پارس نشدند
        }
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict[str, Any], path: str = DEFAULT_PATH) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
