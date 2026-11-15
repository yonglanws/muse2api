"""Concurrency fixes of 0.8.0: same-hint parallel turns, connection pool,
response-entry leak, debounced persist and clock-resolution-proof LRU."""

from __future__ import annotations

import asyncio

import pytest

from muse2api.accounts.model import Account
from muse2api.accounts.pool import AccountPool
from muse2api.config import Settings
from muse2api.drivers.protocol.client import MuseProtocolClient, Response
from muse2api.drivers.protocol.driver import ProtocolDriver, _AccountState, _Conn
from test_protocol_driver import _chat, _driver_with_fake


# ------------------------------------------------------------ thread race
async def test_parallel_same_hint_turns_share_the_thread():
    """A second request on the same conversation while the first is in flight
    used to replace the thread and close its subscription; now it waits on the
    thread lock and continues the same muse session."""
    driver, client = _driver_with_fake()
    account = Account(id="acc_test")

    async def consume(gen):
        return [d async for d in gen]

    r1, r2 = await asyncio.gather(
        consume(driver.chat_stream(
            account, _chat("first", [("user", "first")], hint="c"))),
        consume(driver.chat_stream(
            account, _chat("full", [("user", "first"), ("assistant", "reply:first"),
                                    ("user", "second")], hint="c"))),
    )
    assert r1[0].endswith("first")
    assert r2[0].endswith("second")
    # the second turn followed up on the SAME session instead of replacing it
    assert client.calls[1]["session_id"] == client.calls[0]["session_id"]
    assert client.calls[1]["items"][-1]["text"] == "second"


async def test_followup_turns_stick_to_the_threads_connection():
    driver, _client = _driver_with_fake()
    seen: list = []

    async def spy(account, state, preferred):
        seen.append(preferred)
        return driver._fake_conn

    driver._acquire_conn = spy  # type: ignore[method-assign]
    account = Account(id="acc_test")

    async def consume(gen):
        return [d async for d in gen]

    await consume(driver.chat_stream(
        account, _chat("one", [("user", "one")], hint="c")))
    await consume(driver.chat_stream(
        account, _chat("full", [("user", "one"), ("assistant", "reply:one"),
                                ("user", "two")], hint="c")))
    # fresh turns spread (no preference), follow-ups stay on their connection
    assert seen[0] is None
    assert seen[1] is driver._fake_conn


async def test_prune_never_evicts_an_in_flight_thread():
    driver = ProtocolDriver(Settings())
    state = _AccountState()
    busy = driver._new_thread()
    busy.system_fp = "p"
    idle = driver._new_thread()
    state.threads["busy"] = busy
    state.threads["idle"] = idle

    async with busy.lock:
        await driver._prune(state)
        assert "busy" in state.threads and "idle" in state.threads
        # over the limit only the unlocked thread goes
        state.threads["x"] = driver._new_thread()
        state.threads["y"] = driver._new_thread()
        driver.settings.protocol_max_threads = 2
        await driver._prune(state)
        assert "busy" in state.threads
        assert len(state.threads) == 2


# ------------------------------------------------------------ connections
def test_choose_conn_prefers_idle_and_respects_cap():
    driver = ProtocolDriver(Settings(protocol_connections=2))
    account = Account(id="a", cookies={"hatch_sess": "x"})
    state = _AccountState()

    first = driver._choose_conn(account, state)
    assert len(state.conns) == 1 and first.client is not None
    first.cookies_generation = state.cookies_generation  # as after connect

    first.inflight = 2  # busy: the next pick creates the second connection
    second = driver._choose_conn(account, state)
    assert second is not first and len(state.conns) == 2
    second.cookies_generation = state.cookies_generation

    first.inflight = 0
    assert driver._choose_conn(account, state) is first  # idle beats busy

    capped = ProtocolDriver(Settings(protocol_connections=1))
    lone_state = _AccountState()
    lone = capped._choose_conn(account, lone_state)
    lone.cookies_generation = lone_state.cookies_generation
    lone.inflight = 9
    assert capped._choose_conn(account, lone_state) is lone
    assert len(lone_state.conns) == 1


def test_choose_conn_reuses_a_dead_connection_at_the_cap():
    driver = ProtocolDriver(Settings(protocol_connections=1))
    account = Account(id="a", cookies={"hatch_sess": "x"})
    state = _AccountState()
    dead = _Conn(client=None)
    state.conns.append(dead)
    assert driver._choose_conn(account, state) is dead


def test_invalidate_account_rebuilds_connections():
    driver = ProtocolDriver(Settings(protocol_connections=2))
    account = Account(id="a", cookies={"hatch_sess": "x"})
    state = driver._state(account.id)
    conn = driver._choose_conn(account, state)
    conn.cookies_generation = state.cookies_generation
    driver.invalidate_account(account.id)
    assert state.cookies_generation == 1
    assert conn.cookies_generation != state.cookies_generation  # stale now


async def test_acquire_conn_counts_inflight_and_release_lowers_it():
    driver = ProtocolDriver(Settings(protocol_connections=1))
    account = Account(id="a", cookies={"hatch_sess": "x"})
    state = driver._state(account.id)

    async def fake_ensure(acc, st, conn):
        return conn.client

    driver._ensure_conn = fake_ensure  # type: ignore[method-assign]
    conn = await driver._acquire_conn(account, state, None)
    assert conn.inflight == 1
    driver._release_conn(conn)
    assert conn.inflight == 0


# ------------------------------------------------------------ response leak
async def test_request_pops_its_response_entry():
    client = MuseProtocolClient({})

    async def fake_send(method, path, body=None):
        return 42

    async def fake_wait(stream_id, timeout):
        return Response(status=200, ended=True)

    client.send_request = fake_send  # type: ignore[method-assign]
    client.wait_end = fake_wait  # type: ignore[method-assign]
    response = await client.request("POST", "/api/ping", {})
    assert response.status == 200
    assert 42 not in client.responses  # one-shot entries do not accumulate


async def test_failed_request_still_pops_its_response_entry():
    client = MuseProtocolClient({})

    async def fake_send(method, path, body=None):
        return 7

    async def boom(stream_id, timeout):
        raise RuntimeError("connection died")

    client.send_request = fake_send  # type: ignore[method-assign]
    client.wait_end = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await client.request("POST", "/api/ping", {})
    assert 7 not in client.responses


async def test_failed_subscribe_pops_its_response_entry():
    from muse2api.errors import UpstreamTimeout

    client = MuseProtocolClient({})

    async def fake_send(method, path, body=None):
        return 9

    async def boom(stream_id, timeout):
        raise UpstreamTimeout("no ack")

    client.send_request = fake_send  # type: ignore[method-assign]
    client.wait_headers = boom  # type: ignore[method-assign]
    with pytest.raises(UpstreamTimeout):
        await client.subscribe("session")
    assert 9 not in client.responses


# ------------------------------------------------------------ pool hot path
class _CountingStore:
    def __init__(self) -> None:
        self.saves = 0
        self.event = asyncio.Event()

    async def load(self) -> list[Account]:
        return []

    async def save(self, accounts: list[Account]) -> None:
        self.saves += 1
        self.event.set()


async def test_release_bookkeeping_writes_are_debounced():
    store = _CountingStore()
    pool = AccountPool(store, acquire_timeout=0.2)
    await pool.upsert(Account(id="a", cookies={"hatch_sess": "x"}))
    assert store.saves == 1  # admin upsert persists immediately
    store.event.clear()
    for _ in range(5):
        acc = await pool.acquire()
        await pool.release(acc)
    assert store.saves == 1  # request bookkeeping has not written yet
    await asyncio.wait_for(store.event.wait(), 5)
    assert store.saves == 2  # five releases coalesced into one write


async def test_lru_alternates_even_with_a_frozen_clock(monkeypatch):
    import time as real_time

    class FrozenTime:
        def time(self) -> float:
            return 1000.0

        def __getattr__(self, name):
            return getattr(real_time, name)

    monkeypatch.setattr("muse2api.accounts.pool.time", FrozenTime())
    store = _CountingStore()
    pool = AccountPool(store, acquire_timeout=0.2)
    await pool.upsert(Account(id="a0", cookies={"hatch_sess": "x"}))
    await pool.upsert(Account(id="a1", cookies={"hatch_sess": "x"}))
    chosen: list[str] = []
    for _ in range(6):
        acc = await pool.acquire()
        chosen.append(acc.id)
        await pool.release(acc)
    assert chosen == ["a0", "a1", "a0", "a1", "a0", "a1"]
