"""Async client for Muse's Hatch Noise transport.

The browser drives the same channel; talking to it directly removes Chromium
from the request path and exposes the upstream's real token deltas, thread ids,
and generated-media resources.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
import posixpath
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus

from ...errors import (
    UpstreamAuthError,
    UpstreamError,
    UpstreamQuotaError,
    UpstreamTimeout,
)
from . import hatch_pb2 as pb
from .noise import MAX_PLAINTEXT_FRAME, NoiseXX, Reassembler

log = logging.getLogger(__name__)

SITE = "https://muse.ai"
SESSION_API = f"{SITE}/api/session"
WAKE_API = f"{SITE}/api/hatch/vm/wake"
TOKEN_API = f"{SITE}/api/hatch/token"
NOISE_URL = "wss://hatch.metaaivm.com/v1/noise"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
CAPABILITIES = [
    "chat_cancel",
    "delta_stream",
    "custom_reactions",
    "custom_reactions_facebook_thumbs_up_v1",
    "voice_reply_captions",
    "voice_user_live_transcript",
]
# The chat.stream capabilities list is separate and carries the web client's
# exact feature flags (bundle 21125sd0xkd-l.js, module 459544 export F).
STREAM_CAPABILITIES = [
    "hotel_results",
    "chat_cancel",
    "delta_stream",
    "custom_reactions",
    "custom_reactions_facebook_thumbs_up_v1",
]


@dataclass
class Response:
    status: int | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytearray = field(default_factory=bytearray)
    ended: bool = False
    reset: dict[str, Any] | None = None

    def json(self) -> Any:
        return json.loads(bytes(self.body)) if self.body else {}


class _NoiseHandshakeError(UpstreamError):
    """One Noise handshake attempt failed; ``connect`` may wake and retry."""

    def __init__(self, detail: str, *, status: int | None = None,
                 marker: str = "") -> None:
        super().__init__(f"Noise WebSocket connection failed ({detail})")
        self.status = status
        self.marker = marker
        # Auth rejections cannot be fixed by waking; everything else (timeouts,
        # 5xx, and the IP-registry 502) can.
        self.retryable = (
            status is None
            or status in (404, 408, 425, 429)
            or status >= 500
            or "registry" in marker
        )


class Subscription:
    """A live ``chat.subscribe`` stream for one main/thread scope."""

    def __init__(self, client: MuseProtocolClient, stream_id: int,
                 session_id: str | None, ack: dict[str, Any]) -> None:
        self.client = client
        self.stream_id = stream_id
        self.session_id = session_id
        self.ack = ack
        self.events: list[dict[str, Any]] = []
        self._offset = 0
        self._cond = asyncio.Condition()
        self._task = asyncio.create_task(self._read())
        self.closed = False

    async def _read(self) -> None:
        try:
            while not self.closed and not self.client.closing:
                response = self.client.responses.get(self.stream_id)
                if response is None:
                    await asyncio.sleep(0.2)
                    continue
                raw = bytes(response.body)
                cut = raw.rfind(b"\n", self._offset)
                if cut >= self._offset:
                    for line in raw[self._offset:cut + 1].splitlines():
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if isinstance(event, dict):
                            async with self._cond:
                                self.events.append(event)
                                self._cond.notify_all()
                    self._offset = cut + 1
                try:
                    await self.client.wait_update(self.stream_id, 0.5)
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    break
                if response.ended:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken reader is reported by cursor waits
            log.debug("protocol subscription %s stopped: %s", self.stream_id, exc)
        finally:
            async with self._cond:
                self.closed = True
                self._cond.notify_all()

    async def wait_from(self, index: int, timeout: float = 0.5) -> list[dict[str, Any]]:
        """Return events from ``index``; an empty list means "nothing new yet"."""
        async with self._cond:
            if len(self.events) <= index and not self.closed:
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
            return self.events[index:]

    def pending(self, index: int) -> list[dict[str, Any]]:
        return self.events[index:]

    def close(self) -> None:
        self.closed = True
        self._task.cancel()
        self.client.responses.pop(self.stream_id, None)


class MuseProtocolClient:
    def __init__(self, cookies: dict[str, str], *, proxy: str | None = None,
                 user_agent: str = USER_AGENT, ping_interval: float = 30.0,
                 node_id: str | None = None) -> None:
        self.cookies = {k: v for k, v in cookies.items() if v}
        self.proxy = proxy or None
        self.user_agent = user_agent
        self.ping_interval = ping_interval
        self.ws: ClientConnection | None = None
        self.send_cipher = None
        self.recv_cipher = None
        self._reassembler = Reassembler()
        self._next_stream = 1
        self.responses: dict[int, Response] = {}
        self._waiters: dict[int, set[asyncio.Future]] = {}
        self._send_lock: asyncio.Lock | None = None
        self._receiver_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            proxy=self.proxy,
            follow_redirects=False,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )
        # The node id is this client's stable device identity: follow-up turns
        # and command registrations are bound to it, so a reconnecting pooled
        # connection reuses the same id instead of minting a new one.
        self.node_id = node_id or str(uuid.uuid4())
        self.closing = False
        self.timings: dict[str, float] = {}
        self.session: dict[str, Any] = {}
        self.cookie_expires: dict[str, int] = {}

    # ------------------------------------------------------------ bootstrap
    def _mark(self, name: str) -> None:
        self.timings[name] = time.perf_counter()

    def _headers(self) -> dict[str, str]:
        return {
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v),
            "Origin": SITE,
            "Referer": f"{SITE}/thread/new",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

    def _merge_cookies(self, response: httpx.Response) -> None:
        for name, value in response.cookies.items():
            if value:
                self.cookies[name] = value
        for header in response.headers.get_list("set-cookie"):
            # httpx's jar covers normal cookies; this catches Max-Age/expiry hints.
            first = header.split(";", 1)[0]
            if "=" in first:
                name, value = first.split("=", 1)
                if value:
                    self.cookies[name.strip()] = value.strip()

    async def _http_json(self, method: str, url: str,
                         body: dict[str, Any] | None = None,
                         timeout: float = 30.0) -> dict[str, Any]:
        try:
            response = await self._http.request(
                method, url, headers=self._headers(), json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Muse HTTP request failed ({type(exc).__name__})") from None
        self._merge_cookies(response)
        if response.status_code in (401, 403):
            raise UpstreamAuthError(
                f"Muse session rejected (HTTP {response.status_code})")
        if response.status_code == 407:
            raise UpstreamError("account proxy authentication failed (HTTP 407)")
        if response.status_code == 429:
            raise UpstreamQuotaError("Muse rate limit reached (HTTP 429)")
        if response.status_code >= 400:
            raise UpstreamError(
                f"Muse HTTP {response.status_code} for {url.split('?')[0]}")
        if not response.content:
            return {}
        try:
            value = response.json()
        except ValueError:
            raise UpstreamError("Muse returned a non-JSON bootstrap response") from None
        if not isinstance(value, dict):
            raise UpstreamError("Muse returned an invalid bootstrap response")
        return value

    async def _wake(self, session: dict[str, Any], *, confirm: float = 20.0) -> dict[str, Any]:
        """Ask the VM to wake and wait until /api/session is usable again.

        muse.ai can report ``vm_state: RUNNING`` while the VM's Noise FQDN is
        missing from Meta's IP registry (the edge answers 502
        ``fail_ip_registry_not_found``). A wake call re-registers the endpoint;
        without it every reconnect fails forever even though the session looks
        healthy (observed 2026-11-16).
        """
        if not session.get("vm_id"):
            return session
        try:
            self._mark("wake_start")
            log.info("waking Muse VM %s (noise/registry recovery)", session["vm_id"])
            await self._http_json("POST", WAKE_API, {
                "vm_id": session["vm_id"], "retry_count": 0,
                "connect_attempt_id": str(uuid.uuid4()),
            })
            self._mark("wake")
        except UpstreamError:
            return session
        deadline = time.monotonic() + max(0.0, confirm)
        while True:
            await asyncio.sleep(2.0)
            session = await self._http_json("GET", f"{SESSION_API}?vm_state=live")
            self._mark("session")
            if self._usable(session) or time.monotonic() >= deadline:
                return session

    async def _noise_handshake(self, session: dict[str, Any], vm_type: str) -> None:
        self._mark("token_start")
        try:
            token = await self._http_json("POST", TOKEN_API, {
                "vmAddress": session.get("endpoint_url"),
                **({"vmName": session["vm_name"]} if session.get("vm_name") else {}),
            })
        except (UpstreamAuthError, UpstreamQuotaError):
            raise
        except UpstreamError as exc:
            # A 5xx from the bootstrap API is often the same VM-registry
            # problem; wake + retry instead of failing the request.
            raise _NoiseHandshakeError(str(exc)) from None
        auth_token = token.get("token")
        if not auth_token:
            raise UpstreamError("Muse hatch/token returned no token")
        self._mark("token")
        notary = token.get("notary_token")
        if isinstance(notary, str) and notary.startswith("delegation."):
            raise UpstreamError(
                "confidential-VM delegation endorsement is not supported")
        query = {
            "vm_id": session.get("vm_id"),
            "auth_token": auth_token,
            "app_id": "hatch-web",
            "request_id": str(uuid.uuid4()),
        }
        if isinstance(notary, str) and notary:
            query["notary_token"] = notary
        url = f"{NOISE_URL}?{urlencode(query)}"
        try:
            self.ws = await connect(
                url,
                max_size=4 * 1024 * 1024,
                max_queue=64,
                open_timeout=30,
                close_timeout=3,
                origin=SITE,
                user_agent_header=self.user_agent,
                additional_headers={
                    "Accept-Language": "en-US,en-GB;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                proxy=self.proxy,
            )
            self._mark("noise_start")
            noise = NoiseXX()
            await self.ws.send(noise.msg1())
            message2 = await asyncio.wait_for(self.ws.recv(), 30)
            if not isinstance(message2, bytes):
                raise TypeError("text WebSocket frame during Noise handshake")
            challenge = noise.read_msg2(message2)
            if challenge and "confidential" in vm_type:
                raise UpstreamError("confidential VM attestation is not supported")
            await self.ws.send(noise.msg3())
            self.send_cipher, self.recv_cipher = noise.split()
            self._mark("noise")
        except InvalidStatus as exc:
            status = getattr(exc.response, "status_code", None)
            marker = ""
            with contextlib.suppress(Exception):
                marker = exc.response.headers.get("x-hatch-resolution-status") or ""
            await self._close_ws()
            raise _NoiseHandshakeError(
                f"HTTP {status}" + (f" ({marker})" if marker else ""),
                status=status, marker=marker) from None
        except UpstreamError:
            if self.ws is not None:
                await self._close_ws()
            raise
        except Exception as exc:
            if self.ws is not None:
                await self._close_ws()
            raise _NoiseHandshakeError(type(exc).__name__) from None

    async def connect(self, *, wake_confirm: float = 20.0) -> None:
        if self.alive:
            return
        self.closing = False
        self._mark("session_start")
        session = await self._http_json("GET", f"{SESSION_API}?vm_state=live")
        self._mark("session")
        if not self._usable(session):
            if not session.get("vm_id"):
                raise UpstreamError("Muse workspace is not assigned")
            session = await self._wake(session, confirm=wake_confirm)
        if not self._usable(session):
            state = str(session.get("vm_state") or "unknown")
            raise UpstreamError(f"Muse workspace is not RUNNING (state={state})")
        self.session = session
        vm_type = str(session.get("vm_type") or "").lower()
        cvm = session.get("cvm_eligibility") or session.get("cvmEligibility") or {}
        if isinstance(cvm, dict):
            state = str(cvm.get("state") or cvm.get("status") or "").upper()
            inactive = ("", "NOT_ELIGIBLE", "INELIGIBLE", "NONE", "DISABLED", "N/A")
            if state not in inactive and (
                    "ELIGIB" in state or state in ("REQUIRED", "ENROLLED", "ENABLED")):
                raise UpstreamError(
                    f"unsupported confidential VM state={state}; protocol driver "
                    "supports standard hatch_vm only")
        if "confidential" in vm_type:
            raise UpstreamError(
                f"unsupported VM type {vm_type}; protocol driver supports "
                "standard hatch_vm only")

        retries = 2
        while True:
            try:
                await self._noise_handshake(session, vm_type)
                break
            except _NoiseHandshakeError as exc:
                if retries <= 0 or not exc.retryable:
                    raise UpstreamError(
                        f"Noise WebSocket connection failed ({exc})") from None
                retries -= 1
                log.warning("noise handshake failed (%s); waking VM and retrying",
                            exc)
                session = await self._wake(session, confirm=min(wake_confirm, 10.0))
                self.session = session
                if not self._usable(session):
                    raise UpstreamError(
                        "Noise WebSocket connection failed; VM did not become ready"
                    ) from None
        self._receiver_task = asyncio.create_task(self._receiver())
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    @staticmethod
    def _usable(session: dict[str, Any]) -> bool:
        if session.get("status") != "assigned" or not session.get("endpoint_url"):
            return False
        state = str(session.get("vm_state") or "").upper()
        return not state or state == "RUNNING"

    @property
    def alive(self) -> bool:
        return bool(
            self.ws is not None
            and not self.closing
            and self._receiver_task is not None
            and not self._receiver_task.done()
        )

    # ------------------------------------------------------------ transport
    def _notify(self, stream_id: int) -> None:
        for waiter in list(self._waiters.get(stream_id, ())):
            if not waiter.done():
                waiter.set_result(True)

    def _fail_waiters(self, exc: Exception) -> None:
        for waiters in list(self._waiters.values()):
            for waiter in list(waiters):
                if not waiter.done():
                    waiter.set_exception(exc)

    async def _receiver(self) -> None:
        try:
            assert self.ws is not None and self.recv_cipher is not None
            async for message in self.ws:
                if not isinstance(message, bytes):
                    raise TypeError("text WebSocket frame")
                frame = pb.NoiseTransportFrame.FromString(
                    self.recv_cipher.dec(b"", message))
                raw = self._reassembler.push(
                    frame.chunk_id, frame.chunk_index,
                    frame.total_chunks, bytes(frame.payload))
                if raw is None:
                    continue
                service_response = pb.ServiceResponse.FromString(raw)
                service_frame = pb.ServiceFrame.FromString(service_response.payload)
                kind = service_frame.WhichOneof("kind")
                stream_id = service_frame.stream_id
                response = self.responses.setdefault(stream_id, Response())
                if kind == "response":
                    response.status = service_frame.response.status
                    response.headers = [
                        (h.key, h.value) for h in service_frame.response.headers]
                    response.body.extend(service_frame.response.body)
                    response.ended = service_frame.response.end_body
                elif kind == "body_chunk":
                    response.body.extend(service_frame.body_chunk.data)
                    response.ended = service_frame.body_chunk.end_body
                elif kind == "reset":
                    response.reset = {
                        "code": service_frame.reset.code,
                        "reason": service_frame.reset.reason,
                    }
                    response.ended = True
                self._notify(stream_id)
            if not self.closing:
                self._fail_waiters(UpstreamError("Muse Noise connection closed"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closing:
                self._fail_waiters(
                    UpstreamError(f"Muse Noise receive failed ({type(exc).__name__})"))
        finally:
            for waiters in list(self._waiters.values()):
                for waiter in list(waiters):
                    if not waiter.done():
                        waiter.set_result(True)

    async def _heartbeat(self) -> None:
        try:
            while not self.closing:
                await asyncio.sleep(self.ping_interval)
                await self.request("POST", "/api/ping", {"presence": "active"}, 15)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def wait_update(self, stream_id: int, timeout: float = 30.0) -> None:
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(stream_id, set()).add(waiter)
        try:
            await asyncio.wait_for(waiter, timeout)
        finally:
            self._waiters.get(stream_id, set()).discard(waiter)

    async def wait_headers(self, stream_id: int, timeout: float = 30.0) -> Response:
        deadline = time.monotonic() + timeout
        while True:
            response = self.responses[stream_id]
            if response.status is not None or response.reset or response.ended:
                return response
            if time.monotonic() > deadline:
                raise UpstreamTimeout("timed out waiting for Muse response headers")
            await self.wait_update(stream_id, max(0.1, deadline - time.monotonic()))

    async def wait_end(self, stream_id: int, timeout: float = 30.0) -> Response:
        deadline = time.monotonic() + timeout
        while not self.responses[stream_id].ended:
            if time.monotonic() > deadline:
                raise UpstreamTimeout("timed out waiting for Muse response body")
            await self.wait_update(stream_id, max(0.1, deadline - time.monotonic()))
        return self.responses[stream_id]

    def _pack(self, message) -> list[bytes]:
        raw = message.SerializeToString()
        chunk_id = int.from_bytes(secrets.token_bytes(8), "big", signed=True)
        chunks = [raw[i:i + MAX_PLAINTEXT_FRAME]
                  for i in range(0, len(raw), MAX_PLAINTEXT_FRAME)] or [b""]
        frames = []
        for index, chunk in enumerate(chunks):
            frame = pb.NoiseTransportFrame(
                chunk_id=chunk_id, chunk_index=index,
                total_chunks=len(chunks), payload=chunk)
            assert self.send_cipher is not None
            frames.append(self.send_cipher.enc(b"", frame.SerializeToString()))
        return frames

    async def send_request(self, method: str, path: str,
                           body: dict[str, Any] | None = None) -> int:
        if self.ws is None:
            raise UpstreamError("Muse Noise connection is not open")
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            stream_id = self._next_stream
            self._next_stream += 1
            self.responses[stream_id] = Response()
            headers = [
                pb.Header(key="x-request-id", value=str(uuid.uuid4())),
                pb.Header(key="x-app-id", value="hatch-web"),
                pb.Header(key="Accept-Language", value="en-US"),
                pb.Header(key="Accept-Encoding", value="identity"),
            ]
            if body is not None:
                headers.append(pb.Header(key="Content-Type", value="application/json"))
            request = pb.ApplicationRequest(
                verb=method,
                path=path,
                headers=headers,
                body=json.dumps(body, ensure_ascii=False,
                                separators=(",", ":")).encode() if body is not None else b"",
                end_body=True,
            )
            frame = pb.ServiceFrame(stream_id=stream_id, request=request)
            service = pb.ServiceRequest(
                service=pb.SERVICE_DAEMON, payload=frame.SerializeToString())
            for encrypted in self._pack(service):
                await self.ws.send(encrypted)
            return stream_id

    async def request(self, method: str, path: str,
                      body: dict[str, Any] | None = None,
                      timeout: float = 40.0) -> Response:
        stream_id = await self.send_request(method, path, body)
        try:
            response = await self.wait_end(stream_id, timeout)
            if response.reset:
                raise UpstreamError(f"Muse reset the stream for {path}")
            if response.status is not None and response.status >= 400:
                raise UpstreamError(f"{path} returned HTTP {response.status}")
            return response
        finally:
            # One-shot responses (pings, acks, fs chunks, invoke replies) must
            # not accumulate: without this the dict grows forever on a
            # long-running process. Stream readers (Subscription) use
            # send_request directly and pop their own entry on close.
            self.responses.pop(stream_id, None)

    # ------------------------------------------------------------ chat
    async def subscribe(self, session_id: str | None = None, *,
                        after_stream_seq: int = 0,
                        after_chat_event_seq: int = 0) -> Subscription:
        body: dict[str, Any] = {
            "after_stream_seq": max(0, int(after_stream_seq)),
            "after_chat_event_seq": max(0, int(after_chat_event_seq)),
            "capabilities": CAPABILITIES,
        }
        if session_id:
            body["session_id"] = session_id
        self._mark("subscribe_start")
        stream_id = await self.send_request("POST", "/chat/subscribe", body)
        try:
            response = await self.wait_headers(stream_id, 30)
            if response.status != 200:
                raise UpstreamError(f"chat.subscribe returned HTTP {response.status}")
            deadline = time.monotonic() + 30
            while b"\n" not in response.body:
                if time.monotonic() > deadline:
                    raise UpstreamTimeout("chat.subscribe did not acknowledge")
                await self.wait_update(stream_id, max(0.1, deadline - time.monotonic()))
            ack = json.loads(bytes(response.body).split(b"\n", 1)[0])
            if not isinstance(ack, dict) or ack.get("status") != "ok":
                raise UpstreamError("chat.subscribe was not accepted")
        except BaseException:
            # The Subscription never exists to pop this entry on close.
            self.responses.pop(stream_id, None)
            raise
        self._mark("subscribe")
        return Subscription(self, stream_id, session_id, ack)

    # ------------------------------------------------------ web chat parity
    async def mark_seen(self, thread_id: str) -> None:
        """Mark a thread's transcript as read (web: ``chat.mark_seen``).

        Path carries the thread id; body is empty.  Called after every
        completed gateway turn so sessions never keep an unread badge in the
        owner's muse.ai UI.  Best-effort: older builds may not know the route.
        """
        response = await self.request(
            "POST", f"/api/chat/{thread_id}/seen", {}, 10)
        if response.status not in (200, 204):
            raise UpstreamError(f"chat.mark_seen returned HTTP {response.status}")

    async def chat_history(self, session_id: str | None = None, *,
                           limit: int = 100,
                           after_seq: int | None = None,
                           before_seq: int | None = None) -> dict[str, Any]:
        """Read a thread transcript (web: ``chat.history``).

        The web client pages with ``{limit, after_seq}`` for forward reads and
        the mirror shape for backward reads; the response carries
        ``chat_events`` (each event has ``seq`` and ``role``).  Returned as a
        dict for the caller to interpret; transport errors raise.
        """
        body: dict[str, Any] = {"limit": max(0, int(limit))}
        if session_id:
            body["session_id"] = session_id
        if after_seq is not None:
            body["after_seq"] = max(0, int(after_seq))
        if before_seq is not None:
            body["before_seq"] = max(0, int(before_seq))
        response = await self.request("GET", "/chat/history", body, 30)
        if response.status != 200:
            raise UpstreamError(f"chat.history returned HTTP {response.status}")
        try:
            payload = response.json()
        except ValueError:
            return {}
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            return payload["result"]
        return payload if isinstance(payload, dict) else {}

    async def chat_stream(self, session_id: str, items: list[dict[str, Any]], *,
                          node_id: str | None = None,
                          hidden_prompt: str | None = None,
                          metadata: dict[str, Any] | None = None,
                          model_id: str | None = None,
                          timezone: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "items": items,
            "node_id": node_id or self.node_id,
            "capabilities": STREAM_CAPABILITIES,
            "session_id": session_id,
        }
        if hidden_prompt:
            body["hidden_prompt"] = hidden_prompt
        if metadata:
            body["metadata"] = metadata
        if model_id:
            # The web client sends the pinned model while the account's
            # model_identity_visibility is "visible" (probe 18: auto/auto).
            body["model"] = model_id
        if timezone:
            # Upstream turns this into the per-turn [Day … TZ]
            # [client_timezone=…] developer message; the value is sticky.
            body["timezone"] = timezone
        self._mark("stream_start")
        response = await self.request("POST", "/chat/stream", body, 60)
        if response.status != 200:
            raise UpstreamError(f"chat.stream returned HTTP {response.status}")
        try:
            ack = response.json()
        except ValueError:
            ack = {}
        return ack if isinstance(ack, dict) else {}

    # ------------------------------------------------- identity / vm config
    async def get_model(self) -> dict[str, Any] | None:
        """Account model state: selected_model, available_models, visibility.

        Web parity surface (noise ``model.get``); ``None`` when the endpoint is
        unavailable so callers can fall back to plain sends."""
        try:
            response = await self.request("GET", "/model", None, 20)
        except Exception:  # noqa: BLE001 - optional surface
            return None
        if response.status != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        result = body.get("result") if isinstance(body, dict) else None
        return result if isinstance(result, dict) else None

    async def identity_get(self) -> dict[str, Any] | None:
        """Parsed account identity (name/vibe/emoji, from IDENTITY.md)."""
        try:
            response = await self.request("GET", "/identity", None, 20)
        except Exception:  # noqa: BLE001 - optional surface
            return None
        if response.status != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        result = body.get("result") if isinstance(body, dict) else None
        return result if isinstance(result, dict) else None

    async def identity_set(self, *, name: str | None = None,
                           vibe: str | None = None) -> dict[str, Any]:
        """Native identity write (``onboarding.identity.set``).

        Upstream applies ``vibe`` (dry_witty / short_concise / warm_friendly)
        by appending a ``- **Vibe:** …`` line to IDENTITY.md — probed 19c to
        propagate to fresh threads immediately. ``name`` is only honored
        during onboarding; the default Muse name is kept.
        """
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if vibe:
            body["vibe"] = vibe
        response = await self.request("POST", "/onboarding/identity", body, 20)
        if response.status != 200:
            raise UpstreamError(
                f"/onboarding/identity returned HTTP {response.status}")
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload.get("result") if isinstance(payload, dict) else {}

    async def product_improvements_get(self) -> dict[str, Any] | None:
        """Account data-training settings (noise ``product_improvements.get``).

        ``{"training_enabled": bool, "training_objection": {...}}``; ``None``
        when the surface is unavailable."""
        try:
            response = await self.request("GET", "/api/product-improvements",
                                          None, 20)
        except Exception:  # noqa: BLE001 - optional surface
            return None
        if response.status != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        result = body.get("result") if isinstance(body, dict) else None
        return result if isinstance(result, dict) else None

    async def product_improvements_set(self, *, training_enabled: bool) -> dict[str, Any]:
        """Toggle account data training (noise ``product_improvements.set``)."""
        response = await self.request("POST", "/api/product-improvements",
                                      {"training_enabled": training_enabled}, 20)
        if response.status != 200:
            raise UpstreamError(
                f"/api/product-improvements returned HTTP {response.status}")
        try:
            body = response.json()
        except ValueError:
            return {}
        return body.get("result") if isinstance(body, dict) else {}

    async def vm_config_get(self) -> dict[str, Any] | None:
        """VM agent config (llm.reasoning.* effort), noise ``config.get``."""
        try:
            response = await self.request("GET", "/config", None, 20)
        except Exception:  # noqa: BLE001 - optional surface
            return None
        if response.status != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        result = body.get("result") if isinstance(body, dict) else None
        return result if isinstance(result, dict) else None

    async def vm_config_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Update the VM agent config (noise ``config.update``)."""
        response = await self.request("PATCH", "/config", patch, 20)
        if response.status != 200:
            raise UpstreamError(f"/config returned HTTP {response.status}")
        try:
            body = response.json()
        except ValueError:
            return {}
        result = body.get("result") if isinstance(body, dict) else None
        return result if isinstance(result, dict) else {}

    async def cancel(self, session_id: str, message_id: str | None = None) -> bool:
        body: dict[str, Any] = {"session_id": session_id}
        if message_id:
            body["message_id"] = message_id
        try:
            response = await self.request("POST", "/chat/cancel", body, 10)
            value = response.json()
            return bool(value.get("cancelled")) if isinstance(value, dict) else False
        except Exception:
            return False

    # ------------------------------------------------------------ client tools
    async def register_capabilities(self, commands: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Declare ``hatch_app_commands`` for this connection.

        Commands in the ``ui.navigate.*`` namespace are injected into the
        agent's tool list; the agent calls them and the gateway receives
        ``client.invoke`` events on the thread subscription.  ``linked_nodes``
        binds the declaration to this client's stable ``node_id`` (the same id
        every ``chat.stream`` turn is sent with).
        """
        body: dict[str, Any] = {
            "client_id": f"hatch-web-tab:{self.node_id}",
            "platform": "web",
            "display_name": "Muse Web",
            "version": "0.0.0",
            "capabilities": {
                "data_sources": {},
                "device_commands": {},
                "hatch_app_commands": commands,
                "rendering": {
                    "supported_presentations": [],
                    "supported_inline_presentations": [],
                    "supported_text_entities": [],
                },
            },
            "linked_nodes": [{"node_id": self.node_id}],
        }
        response = await self.request("POST", "/client/register-capabilities", body, 30)
        if response.status != 200:
            raise UpstreamError(
                f"client.register-capabilities returned HTTP {response.status}")
        try:
            ack = response.json()
        except ValueError:
            ack = {}
        return ack if isinstance(ack, dict) else {}

    async def reply_invoke(self, invoke_id: str, ok: bool, *,
                           payload: dict[str, Any] | None = None,
                           error: dict[str, Any] | None = None) -> None:
        """Answer a ``client.invoke`` event (must happen within ``timeout_ms``)."""
        body: dict[str, Any] = {"invoke_id": invoke_id, "ok": bool(ok)}
        if payload is not None:
            body["payload_json"] = json.dumps(payload, ensure_ascii=False)
        if error is not None:
            body["error"] = error
        await self.request("POST", "/client/invoke-result", body, 15)

    # ------------------------------------------------------------ vm files
    async def read_text_file(self, path: str) -> str | None:
        """Read a small text file from the VM (``None`` when missing/unreadable)."""
        try:
            response = await self.request("POST", "/fs/read", {
                "path": path, "offset": 0, "len": 262144,
            }, 30)
        except Exception:  # noqa: BLE001 - best-effort read
            return None
        if response.status != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        result = body.get("result", body) if isinstance(body, dict) else {}
        if not isinstance(result, dict) or "data_base64" not in result:
            return None
        return base64.b64decode(result["data_base64"]).decode("utf-8", "replace")

    async def write_text_file(self, path: str, text: str) -> None:
        """Overwrite a small text file in the VM root."""
        response = await self.request("POST", "/fs/write", {
            "path": path,
            "data_base64": base64.b64encode(text.encode("utf-8")).decode(),
            "overwrite": True, "append": False, "create_parent": True,
        }, 30)
        if response.status != 200:
            raise UpstreamError(f"fs.write({path}) returned HTTP {response.status}")

    # ------------------------------------------------------------ files
    async def download_bytes(self, resource: dict[str, Any]) -> bytes:
        path = str(resource.get("path") or "")
        if path.startswith("sandbox://"):
            path = path[len("sandbox://"):]
        if not path:
            raise UpstreamError("generated resource has no sandbox path")
        output = bytearray()
        offset = 0
        while True:
            response = await self.request("POST", "/fs/read", {
                "path": path, "offset": offset, "len": 1048576,
            }, 60)
            if response.status != 200:
                raise UpstreamError(f"fs.read returned HTTP {response.status}")
            body = response.json()
            result = body.get("result", body) if isinstance(body, dict) else {}
            if not isinstance(result, dict) or "data_base64" not in result:
                raise UpstreamError("fs.read returned no data")
            chunk = base64.b64decode(result["data_base64"])
            if len(chunk) != result.get("len", len(chunk)):
                raise UpstreamError("fs.read length mismatch")
            output.extend(chunk)
            if result.get("eof"):
                break
            if not chunk:
                raise UpstreamError("fs.read returned a zero-length non-EOF chunk")
            offset += len(chunk)
        return bytes(output)

    async def upload_file_ref(self, data: bytes, mime: str, filename: str) -> dict[str, Any]:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "reference.bin"
        suffix = posixpath.splitext(safe)[1].lower() or mimetypes.guess_extension(mime) or ".bin"
        stem = posixpath.splitext(safe)[0]
        final = f"workspace/user/files/{stem}_0_{uuid.uuid4().hex[:8]}{suffix}"
        staging = f"workspace/user/files/.upload-{uuid.uuid4()}.part"
        try:
            for index, offset in enumerate(range(0, len(data), 524288)):
                chunk = data[offset:offset + 524288]
                response = await self.request("POST", "/fs/write", {
                    "path": staging,
                    "data_base64": base64.b64encode(chunk).decode(),
                    "overwrite": index == 0,
                    "append": index != 0,
                    "create_parent": index == 0,
                }, 90)
                if response.status != 200:
                    raise UpstreamError(f"fs.write returned HTTP {response.status}")
            if not data:
                response = await self.request("POST", "/fs/write", {
                    "path": staging, "data_base64": "", "overwrite": True,
                    "append": False, "create_parent": True,
                }, 90)
                if response.status != 200:
                    raise UpstreamError("empty fs.write failed")
            response = await self.request("POST", "/fs/stat", {"path": staging}, 40)
            body = response.json()
            stat = body.get("result", body) if isinstance(body, dict) else {}
            if (response.status != 200 or stat.get("kind") != "file"
                    or stat.get("size") != len(data)):
                raise UpstreamError("uploaded reference failed size verification")
            response = await self.request("POST", "/fs/rename", {
                "source": staging, "destination": final,
                "create_parent": True, "overwrite": True,
            }, 60)
            if response.status != 200:
                raise UpstreamError(f"fs.rename returned HTTP {response.status}")
            return {"type": "file_ref", "mime_type": mime, "path": final,
                    "filename": filename}
        except Exception:
            try:
                await self.request("POST", "/fs/delete", {
                    "path": staging, "recursive": False}, 30)
            except Exception:
                pass
            raise

    # ------------------------------------------------------------ teardown
    async def _close_ws(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    async def close(self) -> None:
        self.closing = True
        tasks = [t for t in (self._receiver_task, self._heartbeat_task) if t]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._receiver_task = None
        self._heartbeat_task = None
        await self._close_ws()
        try:
            await self._http.aclose()
        except Exception:
            pass
