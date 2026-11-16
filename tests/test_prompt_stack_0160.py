"""0.16.0 system-prompt stack: citation markers, wire parity, native channels.

Covers the reverse-engineered web-parity surface (probes 18/19, 2026-11-17):
the streaming citation-marker filter, the chat.stream model/timezone envelope,
and the driver's identity / vm-config admin passthroughs.
"""
from __future__ import annotations

import asyncio

from muse2api.accounts.model import Account
from muse2api.config import Settings
from muse2api.core.citations import MarkerFilter, normalize
from muse2api.drivers.base import ChatRequest
from muse2api.drivers.protocol.driver import ProtocolDriver, _Conn


# ---------------------------------------------------------------- citations
def test_marker_filter_footnote_numbers_distinct_ids():
    filt = MarkerFilter(mode="footnote")
    assert filt.feed("看这个【16348836503601069257†L9】很好") == "看这个[1]很好"
    assert filt.feed("再看【999†L1-L3】和【16348836503601069257†L2】") == "再看[2]和[1]"


def test_marker_filter_handles_markers_split_across_deltas():
    filt = MarkerFilter(mode="footnote")
    first = filt.feed("前文【16")
    assert first == "前文"
    second = filt.feed("348†L")
    assert second == ""
    third = filt.feed("9】后文")
    assert third == "[1]后文"
    assert filt.flush() == ""


def test_marker_filter_flush_releases_unterminated_prefix():
    filt = MarkerFilter(mode="strip")
    assert filt.feed("答案【未闭合") == "答案"
    assert filt.flush() == "【未闭合"


def test_marker_filter_keeps_non_citation_brackets():
    filt = MarkerFilter(mode="strip")
    # 【系统指令】 has no citation shape († or post-) and must survive.
    assert filt.feed("【系统指令】保持") == "【系统指令】保持"


def test_marker_filter_post_markers_and_social_citations():
    filt = MarkerFilter(mode="footnote")
    assert filt.feed("【post-abc123】") == "[1]"
    assert filt.feed("【post-abc123】") == "[1]"


def test_marker_filter_releases_overlong_prefix_verbatim():
    filt = MarkerFilter(mode="strip")
    text = "【" + "x" * 90 + "更多"
    # No 】 in sight: the bracket + first chunk release verbatim in 80-char
    # steps and the remainder (no fresh 【) is plain text too.
    assert filt.feed(text) == text
    assert filt.flush() == ""


def test_marker_filter_native_passthrough():
    filt = MarkerFilter(mode="native")
    raw = "【1†L2】原样"
    assert filt.feed(raw) == raw
    assert filt.flush() == ""


def test_normalize_one_shot():
    assert normalize("a【7†L1】b【8†L2】c", "strip") == "abc"
    assert normalize("a【7†L1】b【8†L2】c", "footnote") == "a[1]b[2]c"
    assert normalize("a【7†L1】b", "native") == "a【7†L1】b"
    assert normalize("没有标记", "strip") == "没有标记"


# --------------------------------------------------------------- wire parity
class _ParityClient:
    """FakeClient that records the chat.stream envelope kwargs."""

    alive = True
    node_id = "node-parity-1"

    def __init__(self, reply: str = "ok") -> None:
        self.calls: list[dict] = []
        self._events: list[dict] = []
        self._reply = reply

    async def chat_stream(self, session_id: str, items: list[dict], *,
                          node_id: str | None = None, hidden_prompt: str | None = None,
                          metadata: dict | None = None, model_id: str | None = None,
                          timezone: str | None = None) -> dict:
        self.calls.append({"session_id": session_id, "items": items,
                           "node_id": node_id, "model_id": model_id,
                           "timezone": timezone})
        message_id = "m1"
        self._events.extend([
            {"event": "delta.message_start", "seq": 1, "payload": {
                "chat_context": {"device_id": node_id}, "message_id": message_id}},
            {"event": "delta.text_append", "seq": 2, "payload": {
                "message_id": message_id, "text": self._reply,
                "session_id": session_id}},
            {"event": "delta.message_done", "seq": 3, "payload": {
                "chat_context": {"device_id": node_id}, "status": "completed"}},
        ])
        return {"session_id": session_id, "message_id": message_id}

    async def subscribe(self, session_id: str, *, after_stream_seq: int = 0,
                        **kwargs):
        return _ParitySub(self)

    async def cancel(self, session_id: str, message_id: str | None = None) -> bool:
        return True

    async def register_capabilities(self, commands: dict) -> dict:
        return {"accepted_hatch_app_commands": len(commands), "rejected_ids": []}

    async def reply_invoke(self, invoke_id: str, ok: bool, **kwargs) -> None:
        return None


class _ParitySub:
    def __init__(self, client: _ParityClient) -> None:
        self.client = client
        self.closed = False

    @property
    def events(self) -> list[dict]:
        return self.client._events

    async def wait_from(self, index: int, timeout: float = 0.5) -> list[dict]:
        await asyncio.sleep(0)
        return self.events[index:]

    def close(self) -> None:
        self.closed = True


def _parity_driver(settings: Settings, client: _ParityClient,
                   model_info: dict | None) -> ProtocolDriver:
    driver = ProtocolDriver(settings)
    driver._fake_conn = _Conn(client=client, model_info=model_info)

    async def fake_acquire_conn(account: Account, state, preferred) -> _Conn:
        return driver._fake_conn

    driver._acquire_conn = fake_acquire_conn  # type: ignore[method-assign]
    return driver


def _chat(prompt: str) -> ChatRequest:
    return ChatRequest(prompt=prompt, model="muse-chat", conversation_hint=None,
                       turns=[("user", prompt)])


async def test_wire_parity_sends_selected_model_and_timezone():
    client = _ParityClient()
    driver = _parity_driver(
        Settings(chat_wire_parity=True, timezone="Asia/Shanghai"), client,
        {"selected_model": "auto/auto", "model_identity_visibility": "visible"})
    text = "".join([d async for d in driver.chat_stream(Account(id="a"), _chat("hi"))])
    assert text == "ok"
    assert client.calls[0]["model_id"] == "auto/auto"
    assert client.calls[0]["timezone"] == "Asia/Shanghai"


async def test_wire_parity_skips_model_when_identity_hidden():
    client = _ParityClient()
    driver = _parity_driver(
        Settings(chat_wire_parity=True, timezone="UTC"), client,
        {"selected_model": "auto/auto", "model_identity_visibility": "hidden"})
    await anext(driver.chat_stream(Account(id="a"), _chat("hi")))
    assert client.calls[0]["model_id"] is None
    assert client.calls[0]["timezone"] == "UTC"


async def test_wire_parity_off_keeps_plain_envelope():
    client = _ParityClient()
    driver = _parity_driver(
        Settings(chat_wire_parity=False, timezone="UTC"), client,
        {"selected_model": "auto/auto", "model_identity_visibility": "visible"})
    await anext(driver.chat_stream(Account(id="a"), _chat("hi")))
    assert client.calls[0]["model_id"] is None
    assert client.calls[0]["timezone"] is None


async def test_citation_footnote_mode_filters_reply():
    client = _ParityClient(reply="来源【123†L9】完")
    driver = _parity_driver(
        Settings(citation_markers="footnote"), client, None)
    text = "".join([d async for d in driver.chat_stream(Account(id="a"), _chat("hi"))])
    assert text == "来源[1]完"


async def test_citation_native_mode_keeps_markers():
    client = _ParityClient(reply="来源【123†L9】完")
    driver = _parity_driver(Settings(), client, None)
    text = "".join([d async for d in driver.chat_stream(Account(id="a"), _chat("hi"))])
    assert text == "来源【123†L9】完"


# ------------------------------------------------------------ admin channels
async def test_identity_and_vm_config_passthrough():
    class _AdminClient(_ParityClient):
        def __init__(self) -> None:
            super().__init__()
            self.identity_payload = {"name": "Muse", "name_set": False,
                                     "vibe": None, "path": "IDENTITY.md"}
            self.config_payload = {"llm": {"reasoning": {
                "root_agent": {"effort": "high"}}}}

        async def identity_get(self):
            return dict(self.identity_payload)

        async def identity_set(self, *, name=None, vibe=None):
            if vibe:
                self.identity_payload["vibe"] = vibe
            return {"vibe": vibe}

        async def vm_config_get(self):
            return dict(self.config_payload)

        async def vm_config_patch(self, patch):
            self.config_payload["patched"] = patch
            return dict(self.config_payload)

    client = _AdminClient()
    driver = _parity_driver(Settings(), client, None)
    account = Account(id="acc_admin")
    got = await driver.identity_get(account)
    assert got["name"] == "Muse"
    await driver.identity_set(account, vibe="dry_witty")
    assert client.identity_payload["vibe"] == "dry_witty"
    cfg = await driver.vm_config_get(account)
    assert cfg["llm"]["reasoning"]["root_agent"]["effort"] == "high"
    await driver.vm_config_patch(account, {"llm": {"reasoning": {
        "root_agent": {"effort": "medium"}}}})
    assert client.config_payload["patched"]["llm"]["reasoning"]["root_agent"]["effort"] == "medium"
