"""Risk guard notifier: ntfy push + Resend email over httpx (spec 2026-11-08 §5.3)."""

import json
from datetime import date

import httpx
import pytest

from xenon.api.services.risk_guard.notify import Notifier, NotifyConfig

CFG = NotifyConfig(
    ntfy_url="https://ntfy.example/xenon-risk",
    ntfy_token="tk_test",
    resend_token="re_test",
    email_to=("me@example.com",),
    email_from="Xenon <risk@example.com>",
    email_max_per_day=2,
)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_sends_push_and_email():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"id": "x"})

    async with _client(handler) as c:
        r = await Notifier(CFG, c).send("CRASH SPX -3.1%", "body text", priority=5, tags=("rotating_light",))
    assert (r.push, r.email) == ("sent", "sent")
    push = next(q for q in seen if q.url.host == "ntfy.example")
    assert push.headers["X-Title"] == "CRASH SPX -3.1%"
    assert push.headers["X-Priority"] == "5"
    assert push.headers["X-Tags"] == "rotating_light"
    assert push.headers["Authorization"] == "Bearer tk_test"
    assert push.content == b"body text"
    mail = next(q for q in seen if q.url.host == "api.resend.com")
    assert str(mail.url) == "https://api.resend.com/emails"
    assert mail.headers["Authorization"] == "Bearer re_test"
    assert json.loads(mail.content) == {
        "from": "Xenon <risk@example.com>",
        "to": ["me@example.com"],
        "subject": "CRASH SPX -3.1%",
        "text": "body text",
    }


@pytest.mark.asyncio
async def test_daily_email_cap_then_push_only():
    async with _client(lambda req: httpx.Response(200, json={})) as c:
        n = Notifier(CFG, c)
        d = date(2026, 11, 9)
        assert (await n.send("a", "b", 4, today=d)).email == "sent"
        assert (await n.send("a", "b", 4, today=d)).email == "sent"
        r = await n.send("a", "b", 4, today=d)
        assert (r.push, r.email) == ("sent", "capped")
        assert (await n.send("a", "b", 4, today=date(2026, 11, 10))).email == "sent"


@pytest.mark.asyncio
async def test_failures_never_raise_and_hide_tokens(caplog):
    def handler(req):
        if req.url.host == "ntfy.example":
            raise httpx.ConnectError("boom")
        return httpx.Response(429, text="rate limited")

    async with _client(handler) as c:
        r = await Notifier(CFG, c).send("t", "b", 3)
    assert (r.push, r.email) == ("failed", "failed")
    assert "tk_test" not in caplog.text and "re_test" not in caplog.text


@pytest.mark.asyncio
async def test_redirect_is_not_sent():
    async with _client(lambda req: httpx.Response(301, headers={"Location": "https://elsewhere"})) as c:
        r = await Notifier(CFG, c).send("t", "b", 3)
    assert (r.push, r.email) == ("failed", "failed")


@pytest.mark.asyncio
async def test_unexpected_error_in_one_channel_keeps_the_other():
    def handler(req):
        if req.url.host == "ntfy.example":
            raise RuntimeError("bug")
        return httpx.Response(200)

    async with _client(handler) as c:
        r = await Notifier(CFG, c).send("t", "b", 3, tags=("tag-é",))
    assert (r.push, r.email) == ("failed", "sent")


@pytest.mark.asyncio
async def test_failed_email_does_not_use_cap():
    async with _client(lambda req: httpx.Response(500)) as c:
        n = Notifier(CFG, c)
        for _ in range(3):
            assert (await n.send("t", "b", 3, today=date(2026, 11, 9))).email == "failed"


@pytest.mark.asyncio
async def test_unconfigured_channels_are_off():
    async with _client(lambda req: pytest.fail("no request expected")) as c:
        r = await Notifier(NotifyConfig(), c).send("t", "b", 3)
    assert (r.push, r.email) == ("off", "off")


@pytest.mark.asyncio
async def test_non_ascii_title_is_header_safe():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200)

    async with _client(handler) as c:
        await Notifier(CFG, c).send("SPX ↓ 3% — crash", "bé", 5)
    push = next(q for q in seen if q.url.host == "ntfy.example")
    assert push.headers["X-Title"] == "SPX ? 3% ? crash"
    assert push.content == "bé".encode()


def test_config_from_env():
    env = {
        "XENON_RISK_NTFY_URL": "https://ntfy.sh/t",
        "XENON_RISK_NTFY_TOKEN": "tk",
        "XENON_RISK_RESEND_TOKEN": "re",
        "XENON_RISK_EMAIL_TO": "a@x.com, b@x.com",
        "XENON_RISK_EMAIL_FROM": "r@x.com",
        "XENON_RISK_EMAIL_MAX_PER_DAY": "50",
    }
    cfg = NotifyConfig.from_env(env)
    assert cfg.email_to == ("a@x.com", "b@x.com") and cfg.email_max_per_day == 50
    assert NotifyConfig.from_env({}).email_max_per_day == 100
    assert NotifyConfig.from_env({"XENON_RISK_EMAIL_MAX_PER_DAY": "x"}).email_max_per_day == 100
