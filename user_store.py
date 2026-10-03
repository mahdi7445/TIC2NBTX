"""Encrypted multi-user credential vault + subscription expiry for the
multi-tenant Nobitex Auto Executor.

SECURITY MODEL - read this before touching anything here:
  - A user's Nobitex API key/secret are the most sensitive data this whole
    system ever handles - far more sensitive than the signals/commands the
    rest of the bot relays. They are NEVER written to GitHub, NEVER logged,
    and NEVER echoed back into any Telegram message once stored. They live
    ONLY inside this one local, encrypted file next to state.json, on the
    same machine that runs the executor.
  - USER_VAULT_MASTER_KEY (a Fernet key) must exist only in the local
    environment/.env of that machine. It is never committed, never logged,
    never pasted into a chat. Losing it means every stored user key becomes
    permanently undecryptable (by design - there is no recovery back door).
    Rotate it with re_encrypt_vault(), not by hand-editing the file.
  - A key is only ever accepted with Nobitex's own WITHDRAW permission
    absent (validate_key_permissions() checks this against Nobitex itself
    before connect_keys() ever stores anything). This is a best-effort check
    against Nobitex's documented /apikeys/list response (permissions is a
    comma-separated string of READ/TRADE/WITHDRAW/DEPOSIT/ADDRESS_BOOK/OTP -
    see apidocs.nobitex.ir). If that response ever looks different than
    expected, this fails CLOSED (rejects the key) rather than guessing it's
    safe - never relax that toward "assume OK if unsure".
  - Expiring a subscription (sweep_expired) only blocks NEW trades for that
    user. It never force-closes an already-open, already-protected position -
    that is a deliberate, separate, explicit action (admin or the user), the
    same way pausing new entries for the admin's own account never touches
    existing trades.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("user_store")

VAULT_PATH = Path(os.environ.get("USER_VAULT_PATH", "users_vault.json"))

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_EXPIRED = "expired"
STATUS_PENDING = "pending_connect"  # has paid/been granted days; waiting for the user to submit their own keys
STATUS_NEW = "new"                  # self-registered through /start; no days yet (must pay or be granted days)
STATUS_BLOCKED = "blocked"          # admin refused/blocked this person; the bot ignores everything but a notice


def _master_key() -> bytes:
    raw = os.environ.get("USER_VAULT_MASTER_KEY", "")
    if not raw:
        raise RuntimeError(
            "USER_VAULT_MASTER_KEY تنظیم نشده؛ بدون این کلید هیچ کلید کاربری رمزگشایی/رمزنگاری نمی‌شود. "
            "یک‌بار با generate_master_key() بسازید و در .env همین کامپیوتر نگه دارید (هرگز در گیت یا تلگرام نه)."
        )
    return raw.encode() if isinstance(raw, str) else raw


def _fernet() -> Fernet:
    return Fernet(_master_key())


def encrypt_field(plaintext: str) -> str:
    """Encrypt one piece of secret text (a submitted API key) with the same
    master key the vault uses. Used by the onboarding relay path (bridge.py,
    via a subprocess or its own copy of this function) so a user's key is
    never written to the public GitHub repo in plaintext, even transiently."""
    return _fernet().encrypt(plaintext.strip().encode()).decode()


def decrypt_field(ciphertext: str) -> str:
    """Inverse of encrypt_field(). Raises RuntimeError (not InvalidToken) on
    a bad/stale master key, matching get_decrypted_keys()'s error shape."""
    try:
        return _fernet().decrypt(ciphertext.strip().encode()).decode()
    except InvalidToken as e:
        raise RuntimeError(
            "رمزگشایی این مقدار شکست خورد؛ احتمالاً USER_VAULT_MASTER_KEY بین bridge و executor یکسان نیست."
        ) from e


def generate_master_key() -> str:
    """Run once, interactively, to create USER_VAULT_MASTER_KEY. The result
    goes in the local .env only - never anywhere else."""
    return Fernet.generate_key().decode()


class CapExceeded(ValueError):
    """A subscriber asked for a value above the admin's ceiling (the UI then
    offers to send the request to the admin)."""
    def __init__(self, field: str, name_fa: str, value: str, cap: str):
        super().__init__(f"{name_fa} = {value} از سقف مجاز ({cap}) بالاتر است.")
        self.field, self.name_fa, self.value, self.cap = field, name_fa, value, cap


@dataclass
class UserRecord:
    user_id: str
    display_name: str = ""
    status: str = STATUS_PENDING
    added_at: float = 0.0
    expires_at: float = 0.0
    pending_days: float = 0.0   # entitlement chosen at /adduser time; becomes real (expires_at) only once they connect
    connected_at: Optional[float] = None
    public_key_enc: Optional[str] = None   # Fernet ciphertext, base64 text - never plaintext at rest
    private_key_enc: Optional[str] = None
    risk_usdt: str = "1"
    max_collateral_usdt: str = "1.25"
    leverage: str = "5"
    max_open_trades: int = 10
    # Ceilings the admin set for this user (/setuserrisk). The user may tune
    # their own effective values (/myrisk) anywhere at or below these, never
    # above - so raising risk/leverage always needs the admin.
    cap_risk_usdt: str = "1"
    cap_collateral_usdt: str = "1.25"
    cap_leverage: str = "5"
    cap_max_open_trades: int = 10
    cap_request: Optional[dict] = None       # a pending "please raise my ceiling" request awaiting the admin
    paused: bool = False                     # user-chosen "no NEW entries" switch (/mypause)
    auto_exit: bool = True                   # follow channel exit events (breakeven/stop/...) automatically
    terms_accepted_at: Optional[float] = None
    username: str = ""                       # Telegram @username (informational)
    daily_loss_usdt: str = "0"               # 0 = off; otherwise no NEW entries after today's realized loss reaches this
    daily_report: bool = False               # send a short daily summary
    last_report_day: str = ""
    lead_notified: bool = False              # admin was told about this self-registered person
    notify_chat_id: Optional[str] = None
    expiry_notice_sent: bool = False
    last_error: Optional[str] = None

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "UserRecord":
        names = {f.name for f in fields(UserRecord)}
        rec = UserRecord(**{k: v for k, v in d.items() if k in names})
        if "cap_max_open_trades" not in d:
            # Records written before this field existed: the ceiling is whatever the admin had set.
            rec.cap_max_open_trades = int(d.get("max_open_trades", 10) or 10)
        return rec

    def days_left(self) -> float:
        return max(0.0, (self.expires_at - time.time()) / 86400)

    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE and self.expires_at > time.time()


class UserVault:
    def __init__(self, path: Path = VAULT_PATH):
        self.path = path
        self._users: Dict[str, UserRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            # Refuse to silently start empty on a corrupt vault - that would look
            # exactly like "all users vanished" and risks re-onboarding (and
            # re-storing) keys for users who are still validly connected.
            log.exception("failed to read user vault %s - refusing to start", self.path)
            raise
        for uid, d in raw.items():
            self._users[str(uid)] = UserRecord.from_json(d)

    def _save(self) -> None:
        data = {uid: rec.to_json() for uid, rec in self._users.items()}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # atomic within the same filesystem

    # ---------------- admin operations ----------------
    def add_pending(self, user_id: str, days: float, display_name: str = "") -> UserRecord:
        """Registers the entitlement (days) but does NOT start the clock yet -
        expires_at is only set once they actually connect_keys(), so time
        spent waiting to onboard is never deducted from what they paid for.
        Calling this again for an existing PENDING user just updates the
        days they'll get once connected (e.g. admin corrected a typo)."""
        user_id = str(user_id)
        now = time.time()
        rec = self._users.get(user_id) or UserRecord(user_id=user_id)
        if display_name:
            rec.display_name = display_name
        rec.status = STATUS_PENDING
        rec.added_at = now
        rec.pending_days = float(days)
        rec.expiry_notice_sent = False
        rec.last_error = None
        self._users[user_id] = rec
        self._save()
        return rec

    def connect_keys(self, user_id: str, public_key: str, private_key: str) -> UserRecord:
        user_id = str(user_id)
        rec = self._users.get(user_id)
        if not rec:
            raise KeyError(f"کاربر {user_id} در صف onboarding نیست؛ اول ادمین باید /adduser بزند.")
        f = _fernet()
        rec.public_key_enc = f.encrypt(public_key.strip().encode()).decode()
        rec.private_key_enc = f.encrypt(private_key.strip().encode()).decode()
        if rec.status == STATUS_PENDING:
            # First-ever connect: the clock starts now, not at /adduser time.
            rec.expires_at = time.time() + rec.pending_days * 86400
            rec.status = STATUS_ACTIVE if rec.expires_at > time.time() else STATUS_EXPIRED
        # Reconnecting (rotating keys on an already-active/expired account)
        # never touches expires_at - only /extenduser or a confirmed payment does.
        rec.connected_at = time.time()
        rec.last_error = None
        self._save()
        return rec

    def get_decrypted_keys(self, user_id: str) -> Tuple[str, str]:
        rec = self._users[str(user_id)]
        if not rec.public_key_enc or not rec.private_key_enc:
            raise ValueError("این کاربر هنوز کلیدی ثبت نکرده است.")
        f = _fernet()
        try:
            pub = f.decrypt(rec.public_key_enc.encode()).decode()
            priv = f.decrypt(rec.private_key_enc.encode()).decode()
        except InvalidToken as e:
            raise RuntimeError(
                "رمزگشایی کلید این کاربر شکست خورد؛ احتمالاً USER_VAULT_MASTER_KEY عوض شده است."
            ) from e
        return pub, priv

    def extend(self, user_id: str, days: float) -> UserRecord:
        rec = self._users[str(user_id)]
        if rec.status in (STATUS_PENDING, STATUS_NEW) or (rec.status == STATUS_BLOCKED and not rec.public_key_enc):
            # Not connected yet: the clock has not started, so the extra days
            # simply join the entitlement that starts at connection time.
            rec.pending_days += float(days)
            rec.status = STATUS_PENDING
            rec.added_at = rec.added_at or time.time()
            self._save()
            return rec
        base = rec.expires_at if rec.expires_at > time.time() else time.time()
        rec.expires_at = base + float(days) * 86400
        if rec.status == STATUS_EXPIRED and rec.public_key_enc:
            rec.status = STATUS_ACTIVE
        rec.expiry_notice_sent = False
        self._save()
        return rec

    def suspend(self, user_id: str) -> UserRecord:
        rec = self._users[str(user_id)]
        rec.status = STATUS_SUSPENDED
        self._save()
        return rec

    def resume(self, user_id: str) -> UserRecord:
        rec = self._users[str(user_id)]
        if rec.expires_at <= time.time():
            raise ValueError("اشتراک این کاربر منقضی شده؛ اول با /extenduser تمدید کنید.")
        if not rec.public_key_enc:
            raise ValueError("این کاربر هنوز کلیدی متصل نکرده است.")
        rec.status = STATUS_ACTIVE
        self._save()
        return rec

    def register_lead(self, user_id: str, display_name: str = "", username: str = "") -> UserRecord:
        """Someone pressed /start for the first time. Creates a NEW record (no
        days, no key) - they cannot trade until they pay or the admin grants days."""
        user_id = str(user_id)
        rec = self._users.get(user_id)
        if rec is None:
            rec = UserRecord(user_id=user_id, status=STATUS_NEW, added_at=time.time())
            rec.notify_chat_id = user_id
            self._users[user_id] = rec
        if display_name and not rec.display_name:
            rec.display_name = display_name
        if username:
            rec.username = username
        self._save()
        return rec

    def block(self, user_id: str) -> UserRecord:
        rec = self._users[str(user_id)]
        rec.status = STATUS_BLOCKED
        self._save()
        return rec

    def unblock(self, user_id: str) -> UserRecord:
        """Back to whatever they were: connected + time left -> active/expired,
        days waiting -> pending_connect, otherwise new."""
        rec = self._users[str(user_id)]
        if rec.public_key_enc:
            rec.status = STATUS_ACTIVE if rec.expires_at > time.time() else STATUS_EXPIRED
        elif rec.pending_days > 0:
            rec.status = STATUS_PENDING
        else:
            rec.status = STATUS_NEW
        self._save()
        return rec

    def set_field(self, user_id: str, **fields) -> UserRecord:
        rec = self._users[str(user_id)]
        for k, v in fields.items():
            if hasattr(rec, k):
                setattr(rec, k, v)
        self._save()
        return rec

    def user_set_values(self, user_id: str, *, risk_usdt=None, max_collateral_usdt=None, leverage=None,
                        max_open_trades=None) -> UserRecord:
        """One-field-at-a-time version of user_set_risk() for the button UI. Every
        value must be positive and (except max_open_trades) at or below the
        admin's ceiling. Raises ValueError with a Persian message; nothing
        changes on failure."""
        rec = self._users[str(user_id)]
        new = {}
        try:
            if risk_usdt is not None:
                new["risk_usdt"] = (float(risk_usdt), float(rec.cap_risk_usdt), "ریسک", str(risk_usdt))
            if max_collateral_usdt is not None:
                new["max_collateral_usdt"] = (float(max_collateral_usdt), float(rec.cap_collateral_usdt), "سقف وثیقه", str(max_collateral_usdt))
            if leverage is not None:
                new["leverage"] = (float(leverage), float(rec.cap_leverage), "اهرم", str(leverage))
            if max_open_trades is not None:
                mo = int(float(max_open_trades))
                if mo < 1:
                    raise ValueError("حداکثر معاملات همزمان باید حداقل ۱ باشد.")
                if mo > int(rec.cap_max_open_trades):
                    raise CapExceeded("max_open_trades", "حداکثر معاملات", str(mo), str(rec.cap_max_open_trades))
        except ValueError as e:
            if str(e).startswith("حداکثر"):
                raise
            raise ValueError("مقدار باید عدد باشد.")
        for name, (v, cap, fa, raw) in new.items():
            if v <= 0:
                raise ValueError(f"{fa} باید بزرگ‌تر از صفر باشد.")
            if v > cap:
                raise CapExceeded(name, fa, raw, f"{cap:g}")
        for name, (v, cap, fa, raw) in new.items():
            setattr(rec, name, str(raw))
        if max_open_trades is not None:
            rec.max_open_trades = int(float(max_open_trades))
        self._save()
        return rec

    def remove(self, user_id: str) -> None:
        # Full, irreversible wipe (including the encrypted keys) - distinct
        # from suspend(), which is reversible and keeps the keys stored.
        self._users.pop(str(user_id), None)
        self._save()

    def set_risk(self, user_id: str, *, risk_usdt=None, max_collateral_usdt=None,
                 leverage=None, max_open_trades=None) -> UserRecord:
        rec = self._users[str(user_id)]
        if risk_usdt is not None:
            rec.risk_usdt = rec.cap_risk_usdt = str(risk_usdt)
        if max_collateral_usdt is not None:
            rec.max_collateral_usdt = rec.cap_collateral_usdt = str(max_collateral_usdt)
        if leverage is not None:
            rec.leverage = rec.cap_leverage = str(leverage)
        if max_open_trades is not None:
            rec.max_open_trades = rec.cap_max_open_trades = int(max_open_trades)
        self._save()
        return rec

    def user_set_risk(self, user_id: str, risk_usdt: str, max_collateral_usdt: str, leverage: str) -> UserRecord:
        """The user's OWN adjustment: each value must be positive and no
        higher than the admin's ceiling for them. Raises ValueError with a
        Persian message otherwise; nothing is changed on failure."""
        rec = self._users[str(user_id)]
        try:
            new = (float(risk_usdt), float(max_collateral_usdt), float(leverage))
            caps = (float(rec.cap_risk_usdt), float(rec.cap_collateral_usdt), float(rec.cap_leverage))
        except ValueError:
            raise ValueError("مقادیر باید عدد باشند.")
        if any(v <= 0 for v in new):
            raise ValueError("همه‌ی مقادیر باید بزرگ‌تر از صفر باشند.")
        names = ("ریسک", "سقف وثیقه", "اهرم")
        for v, c, n in zip(new, caps, names):
            if v > c:
                raise ValueError(f"{n} نمی‌تواند بیشتر از سقف تعیین‌شده توسط ادمین ({c:g}) باشد.")
        rec.risk_usdt, rec.max_collateral_usdt, rec.leverage = str(risk_usdt), str(max_collateral_usdt), str(leverage)
        self._save()
        return rec

    def set_auto_exit(self, user_id: str, value: bool) -> UserRecord:
        rec = self._users[str(user_id)]
        rec.auto_exit = bool(value)
        self._save()
        return rec

    def set_paused(self, user_id: str, paused: bool) -> UserRecord:
        rec = self._users[str(user_id)]
        rec.paused = bool(paused)
        self._save()
        return rec

    def accept_terms(self, user_id: str) -> UserRecord:
        rec = self._users[str(user_id)]
        rec.terms_accepted_at = time.time()
        self._save()
        return rec

    def disconnect(self, user_id: str) -> UserRecord:
        """User (or admin) removes the stored API key but keeps the account
        and the remaining subscription time: status goes back to
        pending_connect with the remaining days as pending_days, and the
        clock restarts only when they connect again."""
        rec = self._users[str(user_id)]
        remaining = rec.days_left() if rec.status in (STATUS_ACTIVE, STATUS_SUSPENDED) else rec.pending_days
        rec.public_key_enc = None
        rec.private_key_enc = None
        rec.connected_at = None
        rec.pending_days = float(remaining)
        rec.status = STATUS_PENDING if rec.pending_days > 0.0001 else STATUS_NEW
        rec.expires_at = 0.0
        self._save()
        return rec

    # ---------------- queries ----------------
    def get(self, user_id: str) -> Optional[UserRecord]:
        return self._users.get(str(user_id))

    def all(self) -> List[UserRecord]:
        return list(self._users.values())

    def active_users(self) -> List[UserRecord]:
        """Users eligible for the trading loop to open NEW trades for them
        right now. Does not reflect whether they have existing open trades -
        those keep running under sweep_expired's rule regardless."""
        return [r for r in self._users.values() if r.is_active()]

    def sweep_expired(self) -> List[UserRecord]:
        """Call once per main-loop tick. Flips ACTIVE -> EXPIRED for anyone
        whose time is up (blocking new entries only - never touches already-
        open trades). Returns users that flipped on THIS call, so the caller
        notifies each of them exactly once."""
        now = time.time()
        newly = []
        for rec in self._users.values():
            if rec.status == STATUS_ACTIVE and rec.expires_at <= now:
                rec.status = STATUS_EXPIRED
                newly.append(rec)
        if newly:
            self._save()
        return newly

    def due_soon(self, within_seconds: float = 86400) -> List[UserRecord]:
        """Active users whose subscription ends within `within_seconds` who
        have not yet been warned. Caller notifies once, then calls
        mark_notice_sent()."""
        now = time.time()
        return [r for r in self._users.values()
                if r.status == STATUS_ACTIVE and not r.expiry_notice_sent
                and now < r.expires_at <= now + within_seconds]

    def mark_notice_sent(self, user_id: str) -> None:
        rec = self._users.get(str(user_id))
        if rec:
            rec.expiry_notice_sent = True
            self._save()

    def set_last_error(self, user_id: str, message: Optional[str]) -> None:
        rec = self._users.get(str(user_id))
        if rec:
            rec.last_error = message
            self._save()


PRICING_PATH = Path(os.environ.get("PRICING_PATH", "pricing.json"))
PAYMENTS_PATH = Path(os.environ.get("PAYMENTS_PATH", "payments.json"))

STATUS_PENDING_PAY = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"


class PricingConfig:
    """Admin-defined price list, per subscription length in days, in EITHER
    or BOTH currencies (Toman bank transfer / USDT crypto transfer) - a tier
    only needs one of the two set to be offered. No prices are built in -
    Mahdi sets his own via /setpricetoman and /setpriceusdt, since he knows
    his own market; nothing here guesses or hardcodes a figure."""

    def __init__(self, path: Path = PRICING_PATH):
        self.path = path
        self._tiers: Dict[int, Dict[str, Optional[float]]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in raw.items():
                self._tiers[int(k)] = {"toman": v.get("toman"), "usdt": v.get("usdt")}
        except Exception:
            log.exception("failed to read pricing file %s", self.path)
            raise

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({str(k): v for k, v in self._tiers.items()}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        tmp.replace(self.path)

    def set_price(self, days: int, *, toman: Optional[int] = None, usdt: Optional[float] = None) -> None:
        if days <= 0:
            raise ValueError("مدت باید مثبت باشد.")
        cur = self._tiers.get(int(days), {"toman": None, "usdt": None})
        if toman is not None:
            if toman < 0:
                raise ValueError("مبلغ تومانی نمی‌تواند منفی باشد.")
            cur["toman"] = int(toman)
        if usdt is not None:
            if usdt < 0:
                raise ValueError("مبلغ تتری نمی‌تواند منفی باشد.")
            cur["usdt"] = float(usdt)
        self._tiers[int(days)] = cur
        self._save()

    def remove_price(self, days: int) -> None:
        self._tiers.pop(int(days), None)
        self._save()

    def all(self) -> Dict[int, Dict[str, Optional[float]]]:
        return dict(sorted(self._tiers.items()))

    def price_for(self, days: int) -> Optional[Dict[str, Optional[float]]]:
        return self._tiers.get(int(days))


@dataclass
class PaymentRecord:
    payment_id: str
    user_id: str
    days: int
    currency: str            # "toman" | "usdt"
    amount: float
    status: str = STATUS_PENDING_PAY
    note: str = ""            # tracking code / bank ref / tx hash the user provided
    requested_at: float = 0.0
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    reject_reason: Optional[str] = None
    # Discount code bookkeeping (empty/0 for a normal full-price payment).
    code: str = ""
    list_amount: float = 0.0          # price before the discount
    discount_percent: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "PaymentRecord":
        names = {f.name for f in fields(PaymentRecord)}
        return PaymentRecord(**{k: v for k, v in d.items() if k in names})


class PaymentLedger:
    """Manual payment tracking for BOTH rails - a Toman bank-card transfer or
    a USDT crypto transfer. Neither is an automated payment gateway: the user
    pays by hand (bank transfer or on-chain transfer) and reports a
    reference (bank tracking code, or a tx hash for USDT), the admin checks
    it against their own bank statement / block explorer, and confirming
    applies the day-extension. Every record is dated (requested_at /
    decided_at) - a subscription is only ever extended at the moment an
    admin explicitly confirms a payment, never automatically and never on an
    unconfirmed promise to pay."""

    def __init__(self, path: Path = PAYMENTS_PATH):
        self.path = path
        self._payments: Dict[str, PaymentRecord] = {}
        self._next = 1
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("failed to read payments file %s", self.path)
            raise
        for pid, d in raw.get("payments", {}).items():
            self._payments[pid] = PaymentRecord.from_json(d)
        self._next = int(raw.get("next", 1))

    def _save(self) -> None:
        data = {"next": self._next, "payments": {pid: p.to_json() for pid, p in self._payments.items()}}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def request(self, user_id: str, days: int, currency: str, amount: float, note: str = "",
                code: str = "", list_amount: float = 0.0, discount_percent: float = 0.0) -> PaymentRecord:
        if currency not in ("toman", "usdt"):
            raise ValueError("ارز پرداخت باید toman یا usdt باشد.")
        pid = f"P{self._next}"
        self._next += 1
        rec = PaymentRecord(payment_id=pid, user_id=str(user_id), days=int(days), currency=currency,
                             amount=float(amount), note=note, requested_at=time.time(),
                             code=str(code or ""), list_amount=float(list_amount or 0.0),
                             discount_percent=float(discount_percent or 0.0))
        self._payments[pid] = rec
        self._save()
        return rec

    def get(self, payment_id: str) -> Optional[PaymentRecord]:
        return self._payments.get(payment_id)

    def set_note(self, payment_id: str, note: str) -> PaymentRecord:
        rec = self._payments[payment_id]
        if rec.status != STATUS_PENDING_PAY:
            raise ValueError(f"این پرداخت قبلاً {rec.status} شده؛ دیگر قابل ویرایش نیست.")
        rec.note = note
        self._save()
        return rec

    def pending(self) -> List[PaymentRecord]:
        return [p for p in self._payments.values() if p.status == STATUS_PENDING_PAY]

    def uses_of_code(self, code: str, user_id: Optional[str] = None) -> int:
        """How many payments hold this discount code. A pending or confirmed payment
        occupies a use; a rejected/cancelled one gives it back - so the count can
        never drift (it is derived from the ledger, not kept as a separate counter)."""
        code = str(code or "")
        n = 0
        for p in self._payments.values():
            if p.code != code or p.status == STATUS_REJECTED:
                continue
            if user_id is not None and p.user_id != str(user_id):
                continue
            n += 1
        return n

    def for_user(self, user_id: str) -> List[PaymentRecord]:
        return sorted((p for p in self._payments.values() if p.user_id == str(user_id)),
                      key=lambda p: p.requested_at)

    def confirm(self, payment_id: str, decided_by: str = "admin") -> PaymentRecord:
        rec = self._payments[payment_id]
        if rec.status != STATUS_PENDING_PAY:
            raise ValueError(f"این پرداخت قبلاً {rec.status} شده؛ دوباره قابل تأیید نیست.")
        rec.status = STATUS_CONFIRMED
        rec.decided_at = time.time()
        rec.decided_by = decided_by
        self._save()
        return rec

    def cancel_by_user(self, payment_id: str, user_id: str) -> PaymentRecord:
        """A subscriber withdraws their own still-unpaid request (no receipt sent yet)."""
        rec = self._payments[payment_id]
        if rec.user_id != str(user_id):
            raise KeyError(payment_id)
        if rec.status != STATUS_PENDING_PAY or rec.note:
            raise ValueError("این درخواست دیگر قابل لغو نیست.")
        rec.status = STATUS_REJECTED
        rec.decided_at = time.time()
        rec.decided_by = str(user_id)
        rec.reject_reason = "لغو توسط کاربر"
        self._save()
        return rec

    def reject(self, payment_id: str, reason: str = "", decided_by: str = "admin") -> PaymentRecord:
        rec = self._payments[payment_id]
        if rec.status != STATUS_PENDING_PAY:
            raise ValueError(f"این پرداخت قبلاً {rec.status} شده؛ دوباره قابل رد نیست.")
        rec.status = STATUS_REJECTED
        rec.decided_at = time.time()
        rec.decided_by = decided_by
        rec.reject_reason = reason
        self._save()
        return rec


DISCOUNTS_PATH = Path(os.environ.get("DISCOUNTS_PATH", "discounts.json"))
_FA_TO_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_CODE_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def normalize_code(raw: str) -> str:
    """Canonical form of a discount code: upper-case ASCII letters/digits/_/-, Persian
    digits converted, spaces dropped. Returns '' when the text is not a usable code."""
    t = str(raw or "").translate(_FA_TO_EN).strip().upper().replace(" ", "")
    if not (3 <= len(t) <= 32) or any(ch not in _CODE_OK for ch in t):
        return ""
    return t


def apply_discount(base: float, percent: float, currency: str) -> float:
    """Price after `percent` off. Toman is rounded to the nearest 1,000 (to look like a
    real price), USDT to 2 decimals. Never below the smallest unit, never above base."""
    from decimal import Decimal, ROUND_HALF_UP
    b = Decimal(str(base))
    out = b * (Decimal(100) - Decimal(str(percent))) / Decimal(100)
    if currency == "toman":
        if b >= 100000:
            out = (out / 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP) * 1000
        else:
            out = out.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        out = max(out, Decimal(1))
    else:
        out = max(out.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), Decimal("0.01"))
    return float(min(out, b))


@dataclass
class DiscountCode:
    code: str
    percent: float
    plans: List[int]                 # subscription lengths (days) it applies to; [] = every plan
    max_uses: int = 0                # 0 = unlimited
    expires_at: Optional[float] = None
    active: bool = True
    created_at: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "DiscountCode":
        names = {f.name for f in fields(DiscountCode)}
        return DiscountCode(**{k: v for k, v in d.items() if k in names})

    def applies_to(self, days: int) -> bool:
        return not self.plans or int(days) in [int(x) for x in self.plans]


class DiscountStore:
    """Admin-created discount codes + which code each subscriber has entered.
    Local file only (like pricing/payments). A code is entered by the subscriber BEFORE
    picking a plan; every plan it covers is then shown at the reduced price, and the
    payment request records the code + original price so the admin sees exactly what
    was applied. Usage is derived from the payment ledger (see uses_of_code)."""

    def __init__(self, path: Path = DISCOUNTS_PATH):
        self.path = path
        self._codes: Dict[str, DiscountCode] = {}
        self._applied: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("failed to read discounts file %s", self.path)
            raise
        for c, d in (raw.get("codes") or {}).items():
            self._codes[c] = DiscountCode.from_json(d)
        self._applied = {str(k): str(v) for k, v in (raw.get("applied") or {}).items()}

    def _save(self) -> None:
        data = {"codes": {c: d.to_json() for c, d in self._codes.items()}, "applied": self._applied}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ---- admin side ----
    def create(self, code: str, percent: float, plans: Optional[List[int]] = None, max_uses: int = 0,
               expire_days: float = 0) -> DiscountCode:
        c = normalize_code(code)
        if not c:
            raise ValueError("کد باید ۳ تا ۳۲ کاراکتر و فقط شامل حروف انگلیسی، عدد، _ یا - باشد (مثلاً EID20).")
        if c in self._codes:
            raise ValueError(f"کد {c} از قبل وجود دارد؛ اول حذفش کنید یا نام دیگری بگذارید.")
        pct = float(percent)
        if not (0 < pct < 100):
            raise ValueError("درصد تخفیف باید بین ۱ تا ۹۹ باشد.")
        plans = sorted({int(x) for x in (plans or [])})
        if any(x <= 0 for x in plans):
            raise ValueError("مدت اشتراک‌ها باید مثبت باشد.")
        if int(max_uses) < 0:
            raise ValueError("سقف تعداد استفاده نمی‌تواند منفی باشد.")
        if float(expire_days) < 0:
            raise ValueError("مهلت انقضا نمی‌تواند منفی باشد.")
        rec = DiscountCode(code=c, percent=pct, plans=plans, max_uses=int(max_uses),
                           expires_at=(time.time() + float(expire_days) * 86400) if float(expire_days) > 0 else None,
                           active=True, created_at=time.time())
        self._codes[c] = rec
        self._save()
        return rec

    def delete(self, code: str) -> bool:
        c = normalize_code(code)
        if c not in self._codes:
            return False
        self._codes.pop(c)
        self._applied = {u: x for u, x in self._applied.items() if x != c}
        self._save()
        return True

    def set_active(self, code: str, active: bool) -> DiscountCode:
        c = normalize_code(code)
        if c not in self._codes:
            raise KeyError(code)
        self._codes[c].active = bool(active)
        self._save()
        return self._codes[c]

    def all(self) -> List[DiscountCode]:
        return sorted(self._codes.values(), key=lambda d: d.created_at)

    def get(self, code: str) -> Optional[DiscountCode]:
        return self._codes.get(normalize_code(code))

    # ---- subscriber side ----
    def check(self, code: str, user_id: str, ledger=None, days: Optional[int] = None) -> Tuple[Optional[DiscountCode], str]:
        """(DiscountCode, '') when usable by this user right now, else (None, persian reason)."""
        rec = self.get(code)
        if rec is None or not rec.active:
            return None, "این کد تخفیف معتبر نیست."
        if rec.expires_at is not None and time.time() > rec.expires_at:
            return None, "مهلت این کد تخفیف تمام شده است."
        if ledger is not None:
            if ledger.uses_of_code(rec.code, user_id) > 0:
                return None, "شما قبلاً از این کد تخفیف استفاده کرده‌اید."
            if rec.max_uses > 0 and ledger.uses_of_code(rec.code) >= rec.max_uses:
                return None, "ظرفیت استفاده از این کد تخفیف تکمیل شده است."
        if days is not None and not rec.applies_to(days):
            return None, "این کد روی این مدت اشتراک اعمال نمی‌شود."
        return rec, ""

    def set_user_code(self, user_id: str, code: str) -> None:
        c = normalize_code(code)
        if c not in self._codes:
            raise KeyError(code)
        self._applied[str(user_id)] = c
        self._save()

    def clear_user_code(self, user_id: str) -> None:
        if str(user_id) in self._applied:
            self._applied.pop(str(user_id))
            self._save()

    def user_code(self, user_id: str) -> Optional[str]:
        return self._applied.get(str(user_id))


PAYMENT_INFO_PATH = Path(os.environ.get("PAYMENT_INFO_PATH", "payment_info.json"))


class PaymentInfo:
    """Admin's payment receiving details shown to users when they request a
    subscription - a bank card for Toman, and/or a wallet address for USDT.
    Kept local-only (like the vault), never in control.json, since
    control.json syncs through the public GitHub repo and neither a card
    number nor necessarily-public-but-still-not-git-history-worthy wallet
    address needs to sit in git history."""

    def __init__(self, path: Path = PAYMENT_INFO_PATH):
        self.path = path
        self.card_number = ""
        self.card_holder = ""
        self.usdt_address = ""
        self.usdt_network = "TRC20"
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.card_number = raw.get("card_number", "")
            self.card_holder = raw.get("card_holder", "")
            self.usdt_address = raw.get("usdt_address", "")
            self.usdt_network = raw.get("usdt_network", "TRC20")
        except Exception:
            log.exception("failed to read payment info file %s", self.path)
            raise

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "card_number": self.card_number, "card_holder": self.card_holder,
            "usdt_address": self.usdt_address, "usdt_network": self.usdt_network,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def set_card(self, card_number: str, card_holder: str) -> None:
        self.card_number = card_number.strip()
        self.card_holder = card_holder.strip()
        self._save()

    def set_usdt(self, address: str, network: str = "TRC20") -> None:
        self.usdt_address = address.strip()
        self.usdt_network = (network or "TRC20").strip()
        self._save()

    def is_card_set(self) -> bool:
        return bool(self.card_number)

    def is_usdt_set(self) -> bool:
        return bool(self.usdt_address)


def validate_key_permissions(nx) -> Tuple[bool, str]:
    """Check a freshly-built NobitexClient's key against Nobitex's own
    /apikeys/list before it is ever stored. Rejects (fail CLOSED) unless the
    response clearly shows this exact key with WITHDRAW absent and TRADE
    present. Any unexpected shape, ambiguity (can't tell which listed key is
    this one), or API error is treated as "cannot verify" -> rejected; this
    never assumes a key is safe just because verification failed to prove
    otherwise. Returns (ok, detail_message_in_persian)."""
    try:
        resp = nx.list_api_keys()
    except Exception as e:
        return False, f"استعلام مجوزهای کلید از نوبیتکس ممکن نشد: {e}"
    keys = resp.get("apiKeys") or resp.get("keys") or resp.get("result") or []
    if isinstance(resp, list):
        keys = resp
    if not isinstance(keys, list) or not keys:
        return False, "پاسخ نوبیتکس برای لیست کلیدها قابل تشخیص نبود؛ برای امنیت، کلید پذیرفته نشد."
    this_pub = getattr(nx.cfg, "public_key", None) or getattr(nx.config, "public_key", None)
    matches = [k for k in keys if isinstance(k, dict) and str(k.get("key", "")) == str(this_pub)]
    if len(matches) != 1:
        matches = keys if len(keys) == 1 else []
    if len(matches) != 1:
        return False, "نمی‌توان مطمئن شد کدام کلید در لیست همین کلید ارسالی است؛ برای امنیت، کلید پذیرفته نشد."
    perms = str(matches[0].get("permissions", "")).upper()
    perm_set = {p.strip() for p in perms.split(",") if p.strip()}
    if "WITHDRAW" in perm_set:
        return False, "این کلید مجوز WITHDRAW (برداشت) دارد؛ به‌هیچ‌وجه پذیرفته نمی‌شود. کلیدی فقط با READ+TRADE بسازید."
    if "TRADE" not in perm_set:
        return False, f"این کلید مجوز TRADE ندارد (مجوزهای فعلی: {perms or 'نامشخص'})؛ بدون TRADE معامله‌ای قابل انجام نیست."
    return True, f"مجوزهای کلید تأیید شد: {perms} (بدون WITHDRAW)."
