# -*- coding: utf-8 -*-
"""
کلاینت ساده‌ی GitHub Contents API.

executor.py (روی ویندوز، بدون فیلترشکن) از این برای دو کار استفاده می‌کند:
1) خواندن signals.jsonl از ریپازیتوری پل (bridge) - به‌جای اتصال مستقیم به
   تلگرام.
2) اضافه کردن خط جدید به outbox.jsonl همان ریپازیتوری - که bridge.py (روی
   GitHub Actions) آن را می‌خواند و برای ادمین در تلگرام می‌فرستد.

گیت‌هاب طبق گفته‌ی کاربر از ایران فیلتر نیست، پس این کلاینت هیچ‌وقت نباید
از پراکسی/VPN استفاده کند - درست مثل کلاینت نوبیتکس.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Optional

import requests

log = logging.getLogger("github_client")


class GithubClient:
    def __init__(self, repo: str, token: str, branch: str = "main"):
        """repo باید به‌شکل 'owner/name' باشد."""
        self.repo = repo
        self.token = token
        self.branch = branch
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.proxies = {}
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def validate_repository(self) -> None:
        """دسترسی به repository و branch تنظیم‌شده را قبل از شروع executor بررسی می‌کند."""
        if not self.repo or "/" not in self.repo or self.repo.count("/") != 1:
            raise ValueError(
                f"GITHUB_REPO نامعتبر است: {self.repo!r}. "
                "فرمت صحیح باید owner/repository باشد."
            )
        if not self.token:
            raise ValueError("GITHUB_PAT تنظیم نشده است.")

        repo_url = f"https://api.github.com/repos/{self.repo}"
        resp = self._session.get(repo_url, timeout=15)
        if resp.status_code == 404:
            raise RuntimeError(
                f"GitHub repository پیدا نشد یا توکن به آن دسترسی ندارد: {self.repo}"
            )
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"GitHub authentication/permission failed "
                f"(HTTP {resp.status_code}) for {self.repo}: {resp.text[:300]}"
            )
        resp.raise_for_status()

        data = resp.json()
        if self.branch:
            branch_url = f"{repo_url}/branches/{self.branch}"
            branch_resp = self._session.get(branch_url, timeout=15)
            if branch_resp.status_code == 404:
                raise RuntimeError(
                    f"Branch در GitHub پیدا نشد: {self.repo}@{self.branch}"
                )
            if branch_resp.status_code in (401, 403):
                raise RuntimeError(
                    f"GitHub branch access denied "
                    f"(HTTP {branch_resp.status_code}) for {self.repo}@{self.branch}: "
                    f"{branch_resp.text[:300]}"
                )
            branch_resp.raise_for_status()

        log.info("GitHub repository validated: %s@%s", self.repo, self.branch)

    def get_file(self, path: str, allow_missing: bool = False) -> tuple[str, Optional[str]]:
        """محتوای فایل و SHA فعلی‌اش را برمی‌گرداند.

        اگر allow_missing=True باشد، نبودن فایل به معنی فایل خالی است و
        ("", None) برگردانده می‌شود.
        """
        resp = self._session.get(
            f"https://api.github.com/repos/{self.repo}/contents/{path}",
            params={"ref": self.branch},
            timeout=15,
        )
        if resp.status_code == 404 and allow_missing:
            return "", None
        resp.raise_for_status()
        data = resp.json()

        # Contents API برای یک فایل باید content داشته باشد. اگر مسیر به
        # یک directory اشاره کند، ساختار پاسخ متفاوت است و نباید بی‌صدا
        # به عنوان فایل پردازش شود.
        if not isinstance(data, dict) or "content" not in data or "sha" not in data:
            raise RuntimeError(f"GitHub path is not a file: {path}")

        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]

    def append_line(self, path: str, line: str, max_retries: int = 3) -> None:
        """
        یک خط به انتهای فایل اضافه می‌کند (با retry روی تداخل sha، چون
        bridge.py هم ممکن است هم‌زمان همین فایل را commit کند).
        """
        for attempt in range(max_retries):
            try:
                content, sha = self.get_file(path)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    content, sha = "", None
                else:
                    raise

            new_content = content + (line if content.endswith("\n") or not content else "\n" + line)
            if not content:
                new_content = line
            if not new_content.endswith("\n"):
                new_content += "\n"

            body = {
                "message": f"chore: outbox entry @ {int(time.time())} [skip ci]",
                "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
                "branch": self.branch,
            }
            if sha:
                body["sha"] = sha

            resp = self._session.put(
                f"https://api.github.com/repos/{self.repo}/contents/{path}",
                json=body,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                return
            if resp.status_code == 409 and attempt < max_retries - 1:
                log.warning("تداخل sha هنگام نوشتن %s - تلاش دوباره...", path)
                time.sleep(1.5)
                continue
            resp.raise_for_status()
