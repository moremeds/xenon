"""Push (ntfy) + email (Resend) for risk alerts (spec §5.3). No DB dependency.

ntfy: POST the body to the topic URL with ``X-Title`` / ``X-Priority`` (1-5) /
``X-Tags`` and ``Authorization: Bearer`` (docs.ntfy.sh/publish).
Resend: POST https://api.resend.com/emails, Bearer token, JSON
``from`` / ``to`` / ``subject`` / ``text`` (resend.com/docs/api-reference/emails/send-email).

A send never raises. Past the daily email cap only the push goes out. Logs carry
the status, never a token.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Mapping

import httpx

from xenon.utils import market_calendar

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT_S = 5.0
# ponytail: the Resend plan quota is unverified (spec §10); set the env to the real daily limit.
DEFAULT_EMAIL_MAX_PER_DAY = 100


@dataclass(frozen=True)
class NotifyConfig:
    ntfy_url: str | None = None
    ntfy_token: str | None = None
    resend_token: str | None = None
    email_to: tuple[str, ...] = ()
    email_from: str | None = None
    email_max_per_day: int = DEFAULT_EMAIL_MAX_PER_DAY

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> NotifyConfig:
        try:
            cap = int(env.get("XENON_RISK_EMAIL_MAX_PER_DAY", DEFAULT_EMAIL_MAX_PER_DAY))
        except ValueError:
            cap = DEFAULT_EMAIL_MAX_PER_DAY
        return cls(
            ntfy_url=env.get("XENON_RISK_NTFY_URL") or None,
            ntfy_token=env.get("XENON_RISK_NTFY_TOKEN") or None,
            resend_token=env.get("XENON_RISK_RESEND_TOKEN") or None,
            email_to=tuple(a.strip() for a in env.get("XENON_RISK_EMAIL_TO", "").split(",") if a.strip()),
            email_from=env.get("XENON_RISK_EMAIL_FROM") or None,
            email_max_per_day=cap,
        )

    @property
    def push_on(self) -> bool:
        return bool(self.ntfy_url)

    @property
    def email_on(self) -> bool:
        return bool(self.resend_token and self.email_to and self.email_from)


@dataclass(frozen=True)
class SendResult:
    push: str  # sent | failed | off
    email: str  # sent | failed | off | capped


class Notifier:
    def __init__(self, cfg: NotifyConfig, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = cfg
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_S)
        self._email_day: date | None = None
        self._emails_sent = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(
        self, title: str, body: str, priority: int, tags: tuple[str, ...] = (), today: date | None = None
    ) -> SendResult:
        today = today or market_calendar.current_session_date_et()
        if self._email_day != today:
            self._email_day, self._emails_sent = today, 0
        email_task = None
        if not self.cfg.email_on:
            email = "off"
        elif self._emails_sent >= self.cfg.email_max_per_day:
            email = "capped"
        else:
            email, email_task = "", self._email(title, body)
        push_task = self._push(title, body, priority, tags) if self.cfg.push_on else None
        results = await asyncio.gather(*(t for t in (push_task, email_task) if t is not None))
        push = results.pop(0) if push_task is not None else "off"
        if email_task is not None:
            email = results.pop(0)
            if email == "sent":
                self._emails_sent += 1
        return SendResult(push, email)

    async def _push(self, title: str, body: str, priority: int, tags: tuple[str, ...]) -> str:
        headers = {"X-Title": title.encode("ascii", "replace").decode(), "X-Priority": str(priority)}
        if tags:
            headers["X-Tags"] = ",".join(tags)
        if self.cfg.ntfy_token:
            headers["Authorization"] = f"Bearer {self.cfg.ntfy_token}"
        return await self._post("ntfy", self.cfg.ntfy_url, content=body.encode(), headers=headers)

    async def _email(self, title: str, body: str) -> str:
        payload = {"from": self.cfg.email_from, "to": list(self.cfg.email_to), "subject": title, "text": body}
        headers = {"Authorization": f"Bearer {self.cfg.resend_token}"}
        return await self._post("resend", RESEND_URL, json=payload, headers=headers)

    async def _post(self, channel: str, url: str, **kw) -> str:
        try:
            resp = await self._client.post(url, timeout=TIMEOUT_S, **kw)
        except httpx.HTTPError as exc:
            logger.error("risk_guard %s send failed: %s", channel, type(exc).__name__)
            return "failed"
        if resp.status_code >= 400:
            logger.error("risk_guard %s send failed: HTTP %s", channel, resp.status_code)
            return "failed"
        return "sent"
