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

import base64
import json
import logging
import os
import re
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


# The highest leverage a subscriber may ever pick on their own (self-service), whatever ceiling is stored.
USER_MAX_LEVERAGE = 10.0


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
    leverage: str = "10"
    max_open_trades: int = 10
    # Ceilings the admin set for this user (/setuserrisk). The user may tune
    # their own effective values (/myrisk) anywhere at or below these, never
    # above - so raising risk/leverage always needs the admin.
    cap_risk_usdt: str = "1"
    cap_collateral_usdt: str = "1.25"
    cap_leverage: str = "10"
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
    referred_by: Optional[str] = None        # user id of the person who invited this user (set once, on first /start)
    ref_rewarded: bool = False               # the inviter already got the reward for this user's first confirmed payment
    ref_count: int = 0                       # how many invited people have paid (rewards earned)
    ref_days_earned: float = 0.0             # total subscription days earned through referrals
    trial_granted: bool = False              # the free trial was given (only ever once per user)
    trial_days_given: float = 0.0            # how many free days that trial was
    trial_days_override: Optional[float] = None   # admin's per-user trial length (None = use the global default)
    has_paid: bool = False                   # at least one payment of this user was confirmed
    # --- connection / onboarding facts the admin sees in the user panel (never contains a key) ---
    key_perms: str = ""                      # permissions Nobitex reported for the key ("" = could not be listed)
    key_perms_verified: bool = False         # True only when Nobitex itself listed the key's permissions
    key_checked_at: Optional[float] = None   # last successful connection check
    key_check_note: str = ""                 # short note about the last check (also failures, for diagnosis)
    budget_usdt: str = ""                    # the margin budget the user told the bot during guided setup
    settings_confirmed_at: Optional[float] = None   # the user confirmed the guided settings
    balance_seen: str = ""                   # last margin USDT balance the bot read
    balance_seen_at: Optional[float] = None
    last_seen_at: Optional[float] = None     # last time the user pressed anything

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "UserRecord":
        names = {f.name for f in fields(UserRecord)}
        rec = UserRecord(**{k: v for k, v in d.items() if k in names})
        if "auto_exit" not in d:
            rec.auto_exit = True                     # automatic channel exits are ON unless the user switched them off
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
        migrated = False
        for uid, d in raw.items():
            rec = UserRecord.from_json(d)
            # r34: the subscriber leverage ceiling is now the maximum possible (10x). Only the old untouched default
            # ceiling (5) is raised; any other value the admin chose on purpose is left exactly as it was.
            if str(rec.cap_leverage).strip() in ("5", "5.0"):
                rec.cap_leverage = "10"
                migrated = True
            # r35: new default leverage is 10x; people who have not connected yet (so never tuned anything) get it too.
            if not rec.public_key_enc and str(rec.leverage).strip() in ("5", "5.0"):
                rec.leverage = "10"
                migrated = True
            self._users[str(uid)] = rec
        if migrated:
            try:
                self._save()
            except Exception:
                log.exception("could not persist leverage-ceiling migration")

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
                new["leverage"] = (float(leverage), min(float(rec.cap_leverage), USER_MAX_LEVERAGE), "اهرم", str(leverage))
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

    def record_key_check(self, user_id: str, *, perms: str, verified: bool, note: str) -> None:
        rec = self._users.get(str(user_id))
        if not rec:
            return
        rec.key_perms, rec.key_perms_verified, rec.key_check_note = perms, bool(verified), note[:200]
        rec.key_checked_at = time.time()
        self._save()

    def touch(self, user_id: str, balance: Optional[str] = None) -> None:
        """Remember when the user was last active (and the last balance read). Saves at most once a minute."""
        rec = self._users.get(str(user_id))
        if not rec:
            return
        now = time.time()
        changed = False
        if not rec.last_seen_at or now - rec.last_seen_at > 60:
            rec.last_seen_at = now
            changed = True
        if balance is not None and (rec.balance_seen != balance):
            rec.balance_seen, rec.balance_seen_at = balance, now
            changed = True
        if changed:
            self._save()

    def apply_guided_settings(self, user_id: str, *, budget: str, risk: str, collateral: str, leverage: str,
                              max_open: int, daily: str) -> UserRecord:
        """The user CONFIRMED the budget-based plan in the guided setup. Values are applied and the user's ceilings are
        raised to at least those values (never lowered), so a confirmed plan is never silently cut down by the old
        tiny default ceilings. Leverage can never exceed USER_MAX_LEVERAGE."""
        rec = self._users[str(user_id)]
        lev = min(float(leverage), USER_MAX_LEVERAGE)
        rec.risk_usdt, rec.max_collateral_usdt, rec.leverage, rec.max_open_trades = str(risk), str(collateral), f"{lev:g}", int(max_open)
        rec.cap_risk_usdt = f"{max(float(rec.cap_risk_usdt), float(risk)):g}"
        rec.cap_collateral_usdt = f"{max(float(rec.cap_collateral_usdt), float(collateral)):g}"
        rec.cap_leverage = f"{max(min(float(rec.cap_leverage), USER_MAX_LEVERAGE), lev):g}"
        rec.cap_max_open_trades = max(int(rec.cap_max_open_trades), int(max_open))
        rec.daily_loss_usdt = str(daily)
        rec.budget_usdt = str(budget)
        rec.settings_confirmed_at = time.time()
        self._save()
        return rec

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
            caps = (float(rec.cap_risk_usdt), float(rec.cap_collateral_usdt), min(float(rec.cap_leverage), USER_MAX_LEVERAGE))
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

    def set_referrer(self, user_id: str, referrer_id: str) -> bool:
        """Remember who invited this user. Only once, never self, and only for an existing different user."""
        user_id, referrer_id = str(user_id), str(referrer_id)
        rec = self._users.get(user_id)
        ref = self._users.get(referrer_id)
        if rec is None or ref is None or user_id == referrer_id or rec.referred_by:
            return False
        if rec.public_key_enc or rec.expires_at or rec.pending_days:
            return False                                  # an existing customer cannot be "invited" afterwards
        if ref.referred_by == user_id:
            return False                                  # no A<->B loops
        rec.referred_by = referrer_id
        self._save()
        return True

    def claim_referral_reward(self, user_id: str) -> Optional[str]:
        """Called when this user's FIRST payment is confirmed. Returns the inviter's id exactly once (idempotent)."""
        rec = self._users.get(str(user_id))
        if rec is None or not rec.referred_by or rec.ref_rewarded:
            return None
        if rec.referred_by not in self._users or rec.referred_by == rec.user_id:
            return None
        rec.ref_rewarded = True
        self._save()
        return rec.referred_by

    def add_referral_credit(self, referrer_id: str, days: float) -> UserRecord:
        rec = self._users[str(referrer_id)]
        rec.ref_count += 1
        rec.ref_days_earned += float(days)
        self._save()
        return self.extend(referrer_id, days)

    def trial_eligible(self, user_id: str) -> bool:
        """A trial is only for someone who never had any entitlement: not given before, never paid, no days from the
        admin, not connected, not blocked."""
        rec = self._users.get(str(user_id))
        return bool(rec and not rec.trial_granted and not rec.has_paid and rec.status == STATUS_NEW
                    and not rec.pending_days and not rec.expires_at and not rec.public_key_enc)

    def grant_trial(self, user_id: str, days: float) -> Optional[UserRecord]:
        """Gives the one-time free trial (days start counting when the exchange key is connected, like any
        entitlement). Returns None when the user is not eligible or days <= 0."""
        days = float(days)
        if days <= 0 or not self.trial_eligible(user_id):
            return None
        rec = self._users[str(user_id)]
        rec.trial_granted = True
        rec.trial_days_given = days
        self._save()
        return self.extend(user_id, days)

    def revoke_trial(self, user_id: str) -> UserRecord:
        """Takes the (not yet started) free-trial days back, e.g. because this exchange account already had a trial.
        trial_granted stays True so it can never be handed out again."""
        rec = self._users[str(user_id)]
        rec.pending_days = max(0.0, rec.pending_days - float(rec.trial_days_given or 0))
        rec.trial_days_given = 0.0
        if rec.status == STATUS_PENDING and rec.pending_days <= 0.0001:
            rec.pending_days = 0.0
            rec.status = STATUS_NEW
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


ACCOUNT_REGISTRY_PATH = Path(os.environ.get("ACCOUNT_REGISTRY_PATH", "account_registry.json"))


def account_fingerprint(nx) -> Optional[str]:
    """A stable, non-reversible identifier of the Nobitex ACCOUNT behind an API key (a person can create many keys for
    one account, so the key itself cannot be used). It is derived from the account's own margin-USDT wallet id.
    Returns None when the exchange response carries no usable id (the caller then cannot enforce uniqueness)."""
    import hashlib
    try:
        data = nx.get_wallets("margin", "usdt")
    except Exception:
        return None
    for w in (data or {}).get("wallets", []) or []:
        if str(w.get("currency", "")).lower() != "usdt":
            continue
        ident = w.get("id") or w.get("user")
        if ident not in (None, ""):
            salt = os.environ.get("USER_VAULT_MASTER_KEY", "") + "|nbx-account|"
            return hashlib.sha256((salt + str(ident)).encode()).hexdigest()
    return None


class AccountRegistry:
    """Which subscriber (Telegram id) first connected which Nobitex account. Used so one exchange account gets the free
    trial only once, even through several Telegram accounts or several API keys."""

    def __init__(self, path: Path = ACCOUNT_REGISTRY_PATH):
        self.path = path
        self._d: Dict[str, str] = {}
        if path.exists():
            try:
                self._d = {str(k): str(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
            except Exception:
                log.exception("failed to read account registry %s", path)

    def owner(self, fp: str) -> Optional[str]:
        return self._d.get(fp)

    def register(self, fp: str, user_id: str) -> None:
        if fp and fp not in self._d:
            self._d[fp] = str(user_id)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._d), encoding="utf-8")
            tmp.replace(self.path)


TRIAL_PATH = Path(os.environ.get("TRIAL_PATH", "trial_config.json"))


class TrialConfig:
    """Admin-set length (days) of the free trial every new subscriber gets once. 0 = trial switched off."""

    def __init__(self, path: Path = TRIAL_PATH):
        self.path = path
        self.days = 0.0
        if path.exists():
            try:
                self.days = max(0.0, float(json.loads(path.read_text(encoding="utf-8")).get("days", 0)))
            except Exception:
                log.exception("failed to read trial config %s", path)

    def set_days(self, days: float) -> None:
        if days < 0 or days > 365:
            raise ValueError("تعداد روز باید بین ۰ تا ۳۶۵ باشد.")
        self.days = float(days)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"days": self.days}), encoding="utf-8")
        tmp.replace(self.path)


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



# --------------------------------------------------------------------------- key hygiene + friendly errors
AUTH_FAIL_TAG = "نوبیتکس این کلید را نشناخت"
_INVISIBLE = {ord(c): None for c in "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\ufeff\u00a0\u2066\u2067\u2068\u2069"}


def clean_key_text(value: str) -> str:
    """Telegram copy/paste can add invisible direction marks, quotes or brackets; none of them belong in a key."""
    v = str(value or "").translate(_INVISIBLE)
    v = re.sub(r"\s+", "", v)
    return v.strip("\"'`“”‘’<>()[]{}،,;:")


def _b64_32(x: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
        return len(raw) == 32
    except Exception:
        return False


def key_format_problem(pub: str, priv: str) -> Optional[str]:
    """Persian explanation when the two pasted strings cannot possibly be a Nobitex key pair (None = looks fine)."""
    for label, x in (("کلید عمومی (API Key)", pub), ("کلید خصوصی (Secret Key)", priv)):
        if not x:
            return f"{label} خالی است."
        if not re.fullmatch(r"[A-Za-z0-9_\-]{20,100}={0,2}", x):
            return (f"{label} شامل کاراکتر نامعتبر است. کلید فقط از حروف و عدد انگلیسی (و _ - =) ساخته شده؛ "
                    "آن را دوباره از نوبیتکس «کپی» کنید و بدون هیچ متن اضافه بفرستید.")
    if pub == priv:
        return "هر دو بار یک مقدار فرستاده شد. اول «کلید عمومی» و بعد «کلید خصوصی (Secret)» را بفرستید؛ این دو متفاوت‌اند."
    if not _b64_32(priv):
        return ("کلید خصوصی کامل یا درست نیست (معمولاً ۴۴ کاراکتر و آخرش «=» است). "
                "مطمئن شوید کلید خصوصی (Secret) را کامل و بدون کم‌وزیاد کپی کرده‌اید؛ اگر آن را از دست داده‌اید، کلید جدید بسازید.")
    return None


def explain_key_error(e: Exception) -> str:
    """Turn a raw Nobitex failure into a calm, actionable message (never a raw JSON dump)."""
    code = str(getattr(e, "code", "") or "")
    status = getattr(e, "http_status", None)
    msg = str(getattr(e, "message", "") or e)
    if code in ("NetworkError", "TooManyRequests", "Throttled", "RateLimit") or (status is not None and (status == 429 or status >= 500)) \
            or code.startswith("HTTP5"):
        return ("ارتباط با نوبیتکس الان برقرار نشد یا نوبیتکس موقتاً شلوغ است؛ این مشکل از کلید شما نیست.\n"
                "۱–۲ دقیقه بعد دوباره «🔁 دوباره وارد می‌کنم» را بزنید.")
    if status in (401, 403) or code in ("HTTP401", "HTTP403") or "اعتبارسنجی" in msg or "Authentication" in msg:
        ip = os.environ.get("BOT_SERVER_IP", "").strip()
        ip_line = (f"   • یا آدرس IP سرور ربات را در لیست IP مجاز کلید بگذارید: {ip}\n" if ip else
                   "   • یا آدرس IP سرور ربات را از «🆘 پشتیبانی» بگیرید و در لیست IP مجاز کلید بگذارید.\n")
        return (f"{AUTH_FAIL_TAG} (احراز هویت رد شد). معمولاً یکی از این‌هاست:\n"
                "1️⃣ کلید ناقص یا اشتباه کپی شده؛ «کلید عمومی (Key)» و «کلید خصوصی (Secret)» را دوباره از همان صفحه‌ی ساخت کلید کپی کنید "
                "(کلید خصوصی فقط یک‌بار نمایش داده می‌شود؛ اگر گم شد کلید جدید بسازید).\n"
                "2️⃣ برای کلید «محدودیت IP» گذاشته‌اید:\n"
                "   • هنگام ساخت کلید، بخش IP مجاز را خالی بگذارید،\n" + ip_line +
                "3️⃣ کلید منقضی یا حذف شده یا غیرفعال است؛ یک کلید تازه با دسترسی «خواندن + معامله» بسازید و «محدودیت زمانی/تاریخ انقضا» و «محدودیت IP» را خالی بگذارید.")
    shown = re.sub(r"\s+", " ", msg).strip()[:140]
    return (f"نوبیتکس درخواست بررسی کلید را رد کرد ({code or 'خطای نامشخص'}{': ' + shown if shown else ''}).\n"
            "مطمئن شوید «معاملات تعهدی (Margin)» در حساب نوبیتکس فعال و احراز هویت کامل است؛ اگر درست بود چند لحظه بعد دوباره امتحان کنید یا با پشتیبانی تماس بگیرید.")


def is_auth_error(e: Exception) -> bool:
    code = str(getattr(e, "code", "") or "")
    status = getattr(e, "http_status", None)
    msg = str(getattr(e, "message", "") or e)
    return status in (401, 403) or code in ("HTTP401", "HTTP403") or "اعتبارسنجی" in msg or "Authentication" in msg


def verify_connection(nx) -> dict:
    """Decide whether a freshly submitted key may be connected.

    1) AUTHENTICATE with a plain READ call (margin wallet) - the very call the bot itself relies on every day, so it
       proves the key pair works for real. Any failure here is a real, explainable failure.
    2) Then, best effort, ask Nobitex to list the key's permissions (/apikeys/list). If Nobitex lists them, a key with
       WITHDRAW (or without TRADE) is refused exactly as before. If that management endpoint does not answer to API-key
       authentication (it may be web-session only), the key is still connected - the bot never calls any withdraw
       endpoint - but it is recorded as 'permissions not verified' so the user is reminded and the admin can see it.
    Returns {ok, auth_failed, verified, perms, detail, balance, raw}. Never raises."""
    out = {"ok": False, "auth_failed": False, "verified": False, "perms": "", "detail": "", "balance": None, "raw": ""}
    try:
        out["balance"] = nx.get_margin_usdt_balance()
    except Exception as e:
        log.warning("key authentication probe failed: %s", e)
        out.update(auth_failed=is_auth_error(e), detail=explain_key_error(e),
                   raw=f"{getattr(e, 'code', type(e).__name__)}: {str(getattr(e, 'message', e))[:200]}")
        return out
    out["ok"] = True
    try:
        resp = nx.list_api_keys()
    except Exception as e:
        out["detail"] = "کلید با موفقیت احراز هویت شد (فهرست مجوزها از نوبیتکس قابل‌خواندن نبود)."
        out["raw"] = f"apikeys/list: {getattr(e, 'code', type(e).__name__)}"
        return out
    keys = resp.get("apiKeys") or resp.get("keys") or resp.get("result") or [] if isinstance(resp, dict) else resp
    this_pub = getattr(nx.cfg, "public_key", None)
    matches = [k for k in keys if isinstance(k, dict) and str(k.get("key", "")) == str(this_pub)] if isinstance(keys, list) else []
    if len(matches) != 1 and isinstance(keys, list) and len(keys) == 1 and isinstance(keys[0], dict):
        matches = keys
    if len(matches) != 1:
        out["detail"] = "کلید احراز هویت شد (مجوزهای آن در پاسخ نوبیتکس قابل‌تشخیص نبود)."
        out["raw"] = "apikeys/list: ambiguous"
        return out
    perms = str(matches[0].get("permissions", "")).upper()
    perm_set = {p.strip() for p in perms.split(",") if p.strip()}
    out["perms"] = perms
    if "WITHDRAW" in perm_set:
        out.update(ok=False, detail="این کلید مجوز WITHDRAW (برداشت) دارد؛ به‌هیچ‌وجه پذیرفته نمی‌شود. کلیدی فقط با «خواندن» و «معامله» (READ+TRADE) بسازید.")
        return out
    if "TRADE" not in perm_set:
        out.update(ok=False, detail=f"این کلید مجوز «معامله» (TRADE) ندارد (مجوزهای فعلی: {perms or 'نامشخص'})؛ بدون آن معامله‌ای انجام نمی‌شود. کلید تازه با READ+TRADE بسازید.")
        return out
    out.update(verified=True, detail=f"مجوزهای کلید تأیید شد: {perms} (بدون WITHDRAW).")
    return out


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
        log.warning("api key verification failed: %s", e)
        return False, explain_key_error(e)
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
