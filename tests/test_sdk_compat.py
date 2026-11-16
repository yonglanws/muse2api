"""Regression tests that drive the API through the official ``openai`` SDK.

The `/v1/responses` endpoint (C1) was verified by hand against openai 3.37.0;
these tests lock that in so a future change cannot silently break SDK
compatibility. The `/v1/images/edits` endpoint (C2) is covered the same way.
Requires the ``openai`` package (declared in the ``dev`` extra).
"""

from __future__ import annotations

import httpx
from openai import AsyncOpenAI


def _sdk_client(client, token: str) -> tuple[AsyncOpenAI, httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=client.app)
    http_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    sdk = AsyncOpenAI(base_url="http://testserver/v1", api_key=token, http_client=http_client)
    return sdk, http_client


async def test_sdk_responses_non_streaming(client, auth):
    token = auth["Authorization"].split(" ", 1)[1]
    sdk, http_client = _sdk_client(client, token)
    try:
        resp = await sdk.responses.create(model="muse-chat", input="sdk non-stream hello")
        assert resp.object == "response"
        assert resp.status == "completed"
        assert "sdk non-stream hello" in resp.output_text
        assert resp.usage.total_tokens > 0
        # Unknown models are rejected like the real API (0.10.0 parity).
        try:
            await sdk.responses.create(model="anything-at-all", input="x")
            raise AssertionError("expected NotFoundError")
        except Exception as exc:
            assert type(exc).__name__ == "NotFoundError"
    finally:
        await http_client.aclose()


async def test_sdk_images_edit(client, auth):
    token = auth["Authorization"].split(" ", 1)[1]
    sdk, http_client = _sdk_client(client, token)
    try:
        resp = await sdk.images.edit(
            image=("a.png", b"\x89PNG-sdk", "image/png"),
            prompt="sdk edit hello",
            response_format="b64_json",
        )
        assert resp.data and resp.data[0].b64_json
    finally:
        await http_client.aclose()


async def test_sdk_responses_streaming(client, auth):
    token = auth["Authorization"].split(" ", 1)[1]
    sdk, http_client = _sdk_client(client, token)
    try:
        async with sdk.responses.stream(model="muse-chat", input="sdk stream hello") as stream:
            async for _event in stream:
                pass
            final = await stream.get_final_response()
        assert final.status == "completed"
        assert "sdk stream hello" in final.output_text
    finally:
        await http_client.aclose()


async def test_sdk_videos_roundtrip(client, auth):
    """The official videos client always sends multipart; it must poll to
    ``completed``, list, download, remix and delete against our task store."""
    token = auth["Authorization"].split(" ", 1)[1]
    sdk, http_client = _sdk_client(client, token)
    try:
        created = await sdk.videos.create(
            prompt="sdk waves", model="sora-2", seconds="4", size="1280x720")
        assert created.object == "video"
        assert created.model == "sora-2"
        assert created.seconds == "4"
        assert created.size == "1280x720"

        done = await sdk.videos.poll(created.id)
        assert done.status == "completed"

        content = await sdk.videos.download_content(created.id)
        assert content.content.startswith(b"\x00\x00\x00\x18ftyp")

        page = await sdk.videos.list(limit=5)
        assert any(video.id == created.id for video in page.data)

        remixed = await sdk.videos.remix(created.id, prompt="sdk remix")
        assert remixed.remixed_from_video_id == created.id
        assert remixed.id != created.id

        deleted = await sdk.videos.delete(created.id)
        assert deleted.deleted is True and deleted.object == "video.deleted"
    finally:
        await http_client.aclose()


async def test_sdk_videos_input_reference_and_unsupported_edits(client, auth):
    from openai import APIStatusError

    token = auth["Authorization"].split(" ", 1)[1]
    sdk, http_client = _sdk_client(client, token)
    try:
        created = await sdk.videos.create(
            prompt="sdk first frame",
            input_reference=("ref.png", b"\x89PNG-sdk", "image/png"))
        assert created.object == "video"
        try:
            await sdk.videos.edit(prompt="edit it",
                                  video=("v.mp4", b"\x00\x00\x00\x18ftyp", "video/mp4"))
            raise AssertionError("expected 501")
        except APIStatusError as exc:
            assert exc.status_code == 501
    finally:
        await http_client.aclose()
