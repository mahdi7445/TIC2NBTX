# -*- coding: utf-8 -*-
"""Direct Telegram control plane for the Windows executor.

This removes the long-running GitHub Actions dependency. The Windows machine
polls Telegram directly, writes channel posts to GitHub, and receives admin
commands/callback buttons. Secrets remain local in .env.
"""
from __future__ import annotations
import json, logging, threading, time
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional
import requests

log = logging.getLogger("telegram_controller")

class TelegramController:
    def __init__(self, token: str, channel_id: str, admin_chat_id: str,
                 github, on_command: Callable[[str], str], signals_file="signals.jsonl"):
        self.token = token
        self.channel_id = str(channel_id)
        self.admin_chat_id = str(admin_chat_id)
        self.github = github
        self.on_command = on_command
        self.signals_file = signals_file
        self.api = f"https://api.telegram.org/bot{token}"
        self.offset = 0
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.started = False

    def start(self):
        if self.started:
            return
        self.started = True
        self.thread = threading.Thread(target=self._loop, name="telegram-control", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def send(self, text: str, keyboard=None):
        payload = {"chat_id": self.admin_chat_id, "text": str(text)[:4000], "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            r = requests.post(self.api + "/sendMessage", json=payload, timeout=15)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(str(data))
        except Exception:
            log.exception("Telegram send failed")

    def menu(self):
        return [
            [{"text":"📊 وضعیت سیستم","callback_data":"status"},{"text":"💰 موجودی","callback_data":"balance"}],
            [{"text":"📈 معاملات باز","callback_data":"positions"},{"text":"⚙️ تنظیمات","callback_data":"config"}],
            [{"text":"▶️ فعال‌سازی","callback_data":"resume"},{"text":"⏸ توقف ورود","callback_data":"pause"}],
            [{"text":"🧪 تست API","callback_data":"test_api"},{"text":"🔄 همگام‌سازی","callback_data":"reconcile"}],
            [{"text":"📌 ریسک","callback_data":"risk_menu"},{"text":"💵 مارجین هر معامله","callback_data":"collateral_menu"}],
            [{"text":"🔢 حداکثر معاملات","callback_data":"maxtrades_menu"}],
            [{"text":"🚨 بستن همه معاملات","callback_data":"closeall_confirm"}],
        ]

    def _answer_callback(self, callback_id: str):
        try: requests.post(self.api+"/answerCallbackQuery", json={"callback_query_id":callback_id}, timeout=10)
        except Exception: pass

    def _edit_or_send(self, chat_id, text, keyboard=None):
        self.send(text, keyboard if str(chat_id)==self.admin_chat_id else None)

    def _handle_admin_text(self, text: str):
        t=text.strip()
        if t in ("/start","/menu","menu","/help","help"):
            self.send("🤖 پنل کنترل TRADE IS COOL\n\nتمام کنترل‌های اجرایی از همین منو قابل انجام است.", self.menu()); return
        if t.startswith("/risk "):
            self.send(self.on_command(t)); return
        if t.startswith("/collateral "):
            self.send(self.on_command(t)); return
        if t.startswith("/maxtrades "):
            self.send(self.on_command(t)); return
        if t.startswith("/"):
            self.send(self.on_command(t)); return
        self.send("دستور نامعتبر است. /menu را بزنید.", self.menu())

    def _handle_callback(self, q):
        self._answer_callback(q.get("id",""))
        data=str(q.get("data",""));
        if data == "closeall_confirm":
            self.send("⚠️ تأیید نهایی: همه پوزیشن‌های باز با Market بسته شوند؟", [[
                {"text":"❌ بله، همه را ببند","callback_data":"closeall"},
                {"text":"↩️ لغو","callback_data":"menu"}
            ]]); return
        if data == "risk_menu":
            self.send("🎯 سقف ریسک هر معامله\nاین عدد سقف ضرر برنامه‌ریزی‌شده تا SL است؛ سیستم در صورت کمبود مارجین آن را خودکار کمتر می‌کند.", [
                [{"text":"0.25 USDT","callback_data":"risk:0.25"},{"text":"0.50 USDT","callback_data":"risk:0.50"}],
                [{"text":"1.00 USDT","callback_data":"risk:1.00"}],
            ]); return
        if data == "collateral_menu":
            self.send("💵 سقف وجه تضمین هر معامله", [
                [{"text":"1.00 USDT","callback_data":"collateral:1"},{"text":"1.25 USDT","callback_data":"collateral:1.25"}],
                [{"text":"1.50 USDT","callback_data":"collateral:1.5"},{"text":"2.00 USDT","callback_data":"collateral:2"}],
                [{"text":"5.00 USDT","callback_data":"collateral:5"}],
            ]); return
        if data == "maxtrades_menu":
            self.send("🔢 حداکثر تعداد پوزیشن همزمان", [
                [{"text":"5","callback_data":"maxtrades:5"},{"text":"10","callback_data":"maxtrades:10"}],
                [{"text":"15","callback_data":"maxtrades:15"},{"text":"20","callback_data":"maxtrades:20"}],
            ]); return
        if data == "menu": self.send("منوی اصلی", self.menu()); return
        if data.startswith("risk:") or data.startswith("collateral:") or data.startswith("maxtrades:"):
            self.send(self.on_command("/"+data.replace(":"," "))); self.send("منوی اصلی", self.menu()); return
        if data in {"status","balance","positions","config","pause","resume","test_api","reconcile","closeall"}:
            self.send(self.on_command("/"+data));
            if data in {"status","config","pause","resume","test_api","reconcile"}: self.send("منوی اصلی", self.menu())

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                r=requests.get(self.api+"/getUpdates", params={"offset":self.offset,"timeout":20,
                    "allowed_updates":json.dumps(["channel_post","message","callback_query"])}, timeout=30)
                r.raise_for_status(); data=r.json()
                if not data.get("ok"):
                    time.sleep(3); continue
                for u in data.get("result",[]):
                    self.offset=max(self.offset,int(u.get("update_id",0))+1)
                    post=u.get("channel_post")
                    if post and str(post.get("chat",{}).get("id"))==self.channel_id:
                        text=post.get("text") or post.get("caption")
                        if text:
                            line=json.dumps({"update_id":u["update_id"],"message_id":post.get("message_id"),"signal_id":f"{self.channel_id}:{post.get('message_id')}" if post.get('message_id') else str(u["update_id"]),"ts":time.time(),"text":text},ensure_ascii=False)
                            self.github.append_line(self.signals_file,line)
                            self.send("📥 سیگنال کانال در صف اجرای ویندوز ثبت شد.\nMessage ID: %s" % post.get("message_id"))
                    msg=u.get("message")
                    if msg and str(msg.get("chat",{}).get("id"))==self.admin_chat_id:
                        self._handle_admin_text(msg.get("text") or "")
                    q=u.get("callback_query")
                    if q and str(q.get("message",{}).get("chat",{}).get("id"))==self.admin_chat_id:
                        self._handle_callback(q)
            except Exception:
                log.exception("Telegram polling failed")
                time.sleep(5)
