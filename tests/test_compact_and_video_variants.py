"""0.15.0 surface tests: Responses compact and Sora video variants."""

from __future__ import annotations

import time

from muse2api.drivers.mock import _solid_png
from test_robustness import StubDriver, _auth, _stub_client
from test_web_chat_surface import _mock_client


def test_responses_compact_roundtrip(tmp_path):
    driver = StubDriver("The user's code word is BANANA.")
    client = _stub_client(tmp_path, driver)
    compacted = client.post("/v1/responses/compact", headers=_auth(), json={
        "model": "muse-chat", "input": "Remember the code word BANANA."})
    assert compacted.status_code == 200
    body = compacted.json()
    assert body["object"] == "response.compaction"
    assert body["usage"]["total_tokens"] > 0
    item = body["output"][0]
    assert item["type"] == "compaction"
    assert item["encrypted_content"]

    follow = client.post("/v1/responses", headers=_auth(), json={
        "model": "muse-chat",
        "input": [item, {"role": "user", "content": "What is the code word?"}]})
    assert follow.status_code == 200
    assert "Compacted conversation state" in driver.requests[-1].prompt
    assert "BANANA" in driver.requests[-1].prompt


def test_responses_compact_rejects_bad_item(tmp_path):
    client = _stub_client(tmp_path, StubDriver("summary"))
    bad = client.post("/v1/responses", headers=_auth(), json={
        "model": "muse-chat",
        "input": [{"type": "compaction", "id": "cmp_x",
                   "encrypted_content": "not-base64"}]})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_compaction"


def _wait_video(client, video_id: str) -> dict:
    for _ in range(100):
        body = client.get(f"/v1/videos/{video_id}", headers=_auth()).json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.02)
    return body


def test_video_variants_are_rendered_and_cached(tmp_path, monkeypatch):
    import muse2api.api.routes.videos as videos

    calls: list[str] = []

    def fake_render(path, variant):
        calls.append(variant)
        return _solid_png(8, 8, (10, 20, 30)), "image/png"

    monkeypatch.setattr(videos, "render_variant", fake_render)
    client = _mock_client(tmp_path)
    created = client.post("/v1/videos", headers=_auth(), json={
        "prompt": "variant test", "seconds": "4"}).json()
    _wait_video(client, created["id"])

    for variant in ("thumbnail", "spritesheet"):
        response = client.get(f"/v1/videos/{created['id']}/content",
                              headers=_auth(), params={"variant": variant})
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG")
    assert calls == ["thumbnail", "spritesheet"]

    # The generated PNG is cached next to the source; a repeat does not rerun ffmpeg.
    again = client.get(f"/v1/videos/{created['id']}/content", headers=_auth(),
                       params={"variant": "thumbnail"})
    assert again.status_code == 200
    assert calls == ["thumbnail", "spritesheet"]

    bad = client.get(f"/v1/videos/{created['id']}/content", headers=_auth(),
                     params={"variant": "gif"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "unsupported_variant"

    deleted = client.delete(f"/v1/videos/{created['id']}", headers=_auth()).json()
    assert deleted["deleted"] is True
