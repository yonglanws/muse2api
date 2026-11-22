"""0.12.0 surface tests: stored completions, Responses object fields, image/file
metadata, and explicit rejections for upstream-inexpressible parameters."""

from __future__ import annotations

import json
import time

from muse2api.services.file_store import FileStore
from test_robustness import StubDriver, _auth, _stub_client
from test_web_chat_surface import _mock_client

# --- Chat parameters -------------------------------------------------------

def test_chat_rejects_audio_output(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "speak"}],
        "modalities": ["text", "audio"]})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "modalities"

    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "speak"}],
        "audio": {"format": "wav", "voice": "alloy"}})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "audio"


def test_chat_service_tier_echo_and_validation(tmp_path):
    client = _stub_client(tmp_path, StubDriver("tiered"))
    body = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}],
        "service_tier": "auto"}).json()
    assert body["service_tier"] == "default"
    assert body["choices"][0]["logprobs"] is None

    streamed = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}],
        "service_tier": "default", "stream": True}).text
    chunks = [json.loads(line[6:]) for line in streamed.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    assert all(chunk.get("service_tier") == "default" for chunk in chunks)

    accepted = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}],
        "service_tier": "priority"})
    assert accepted.status_code == 200
    assert accepted.json()["service_tier"] == "default"

    rejected = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}],
        "service_tier": "turbo"})
    assert rejected.status_code == 400
    assert rejected.json()["error"]["param"] == "service_tier"


def test_chat_metadata_is_validated_and_echoed(tmp_path):
    client = _stub_client(tmp_path, StubDriver("meta"))
    body = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"trace": "abc"}}).json()
    assert body["metadata"] == {"trace": "abc"}

    bad = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"count": 3}})
    assert bad.status_code == 400
    assert bad.json()["error"]["param"] == "metadata"


# --- Responses object surface ---------------------------------------------

def test_responses_object_has_current_fields(tmp_path):
    client = _stub_client(tmp_path, StubDriver("fields"))
    body = client.post("/v1/responses", headers=_auth(), json={
        "input": "hello", "service_tier": "auto"}).json()
    assert body["service_tier"] == "default"
    assert body["access_programs"] is None
    assert body["background"] is False
    assert body["conversation"] is None
    assert body["completed_at"] >= body["created_at"]
    assert body["top_logprobs"] is None
    assert body["usage"]["input_tokens_details"]["cache_write_tokens"] == 0
    text = body["output"][0]["content"][0]
    assert text["logprobs"] == []

    rejected = client.post("/v1/responses", headers=_auth(), json={
        "input": "hello", "top_logprobs": 3})
    assert rejected.status_code == 400
    assert rejected.json()["error"]["param"] == "top_logprobs"


def test_responses_rejects_background_and_unknown_conversation(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    background = client.post("/v1/responses", headers=_auth(), json={
        "input": "hello", "background": True})
    assert background.status_code == 400
    assert background.json()["error"]["param"] == "background"

    conversation = client.post("/v1/responses", headers=_auth(), json={
        "input": "hello", "conversation": "conv_123"})
    assert conversation.status_code == 404
    assert conversation.json()["error"]["param"] == "conversation"


def test_previous_response_never_carries_instructions(tmp_path):
    driver = StubDriver("ok")
    client = _stub_client(tmp_path, driver)
    first = client.post("/v1/responses", headers=_auth(), json={
        "input": "one", "instructions": "TOP-SECRET-INSTRUCTION"}).json()
    second = client.post("/v1/responses", headers=_auth(), json={
        "input": "two", "previous_response_id": first["id"]})
    assert second.status_code == 200
    assert "TOP-SECRET-INSTRUCTION" not in (driver.requests[1].system_prompt or "")
    assert "TOP-SECRET-INSTRUCTION" not in driver.requests[1].prompt

    # ...even when the follow-up supplies its own instructions.
    third = client.post("/v1/responses", headers=_auth(), json={
        "input": "three", "previous_response_id": second.json()["id"],
        "instructions": "NEW-INSTRUCTION"})
    assert third.status_code == 200
    assert "NEW-INSTRUCTION" in (driver.requests[2].system_prompt or "")
    assert "TOP-SECRET-INSTRUCTION" not in (driver.requests[2].system_prompt or "")


def test_input_items_default_to_descending(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    body = client.post("/v1/responses", headers=_auth(), json={
        "input": [{"role": "user", "content": "first"},
                  {"role": "user", "content": "second"}]}).json()
    response_id = body["id"]

    default = client.get(f"/v1/responses/{response_id}/input_items",
                         headers=_auth()).json()
    assert [item["content"][0]["text"] for item in default["data"]] == [
        "second", "first"]

    ascending = client.get(f"/v1/responses/{response_id}/input_items",
                           headers=_auth(), params={"order": "asc"}).json()
    assert [item["content"][0]["text"] for item in ascending["data"]] == [
        "first", "second"]


def test_input_items_exclude_instructions(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    body = client.post("/v1/responses", headers=_auth(), json={
        "input": "hello", "instructions": "SEPARATE-INSTRUCTION"}).json()
    items = client.get(f"/v1/responses/{body['id']}/input_items",
                       headers=_auth()).json()["data"]
    assert [item["content"][0]["text"] for item in items] == ["hello"]


# --- Images ----------------------------------------------------------------

def test_image_response_echoes_generation_fields(tmp_path):
    client = _mock_client(tmp_path)
    body = client.post("/v1/images/generations", headers=_auth(), json={
        "prompt": "a red square", "size": "1024x1024", "quality": "high"}).json()
    assert body["background"] == "opaque"
    assert body["output_format"] == "png"
    assert body["size"] == "1024x1024"
    assert body["quality"] == "high"


def test_image_stream_is_accepted(tmp_path):
    """0.18.3: stream=true is served as the gpt-image-1 SSE shape (new-api
    sends it for images); it used to be a 400."""
    client = _mock_client(tmp_path)
    with client.stream("POST", "/v1/images/generations", headers=_auth(),
                       json={"prompt": "a red square", "stream": True}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "image_generation.completed" in body


# --- Files -----------------------------------------------------------------

def test_files_purpose_validation_and_expiration(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    bad = client.post("/v1/files", headers=_auth(), files={
        "file": ("x.txt", b"x", "text/plain")}, data={"purpose": "bogus"})
    assert bad.status_code == 400
    assert bad.json()["error"]["param"] == "purpose"

    created = client.post("/v1/files", headers=_auth(), files={
        "file": ("x.txt", b"x", "text/plain")}, data={
        "purpose": "batch",
        "expires_after": json.dumps({"anchor": "created_at", "seconds": 7200}),
    }).json()
    assert created["expires_at"] is not None
    assert created["expires_at"] > created["created_at"]

    too_short = client.post("/v1/files", headers=_auth(), files={
        "file": ("x.txt", b"x", "text/plain")}, data={
        "purpose": "assistants",
        "expires_after": json.dumps({"anchor": "created_at", "seconds": 10}),
    })
    assert too_short.status_code == 400
    assert too_short.json()["error"]["param"] == "expires_after.seconds"


def test_files_list_has_cursor_fields(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    ids = [client.post("/v1/files", headers=_auth(), files={
        "file": (f"{i}.txt", b"x", "text/plain")}).json()["id"] for i in range(3)]

    page = client.get("/v1/files", headers=_auth(), params={"limit": 2}).json()
    assert page["has_more"] is True
    assert page["first_id"] == ids[-1]
    assert len(page["data"]) == 2
    next_page = client.get("/v1/files", headers=_auth(),
                           params={"limit": 2, "after": page["last_id"]}).json()
    assert next_page["has_more"] is False
    assert len(next_page["data"]) == 1


# --- Models ----------------------------------------------------------------

def test_models_expose_shutdown_date_and_reject_delete(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    listing = client.get("/v1/models", headers=_auth()).json()
    assert all(model["shutdown_date"] is None for model in listing["data"])
    model_id = listing["data"][0]["id"]
    assert client.delete(f"/v1/models/{model_id}",
                         headers=_auth()).status_code == 404


# --- Videos ----------------------------------------------------------------

def test_video_ids_use_sora_prefix(tmp_path):
    client = _mock_client(tmp_path)
    created = client.post("/v1/videos", headers=_auth(), json={
        "prompt": "a paper boat", "seconds": "4"}).json()
    assert created["id"].startswith("video_")
    assert created["object"] == "video"

    status = created["status"]
    for _ in range(100):
        body = client.get(f"/v1/videos/{created['id']}", headers=_auth()).json()
        status = body["status"]
        if status in ("completed", "failed"):
            break
        time.sleep(0.02)
    assert status == "completed"

    listed = client.get("/v1/videos", headers=_auth()).json()
    assert listed["data"][0]["id"] == created["id"]
    assert client.get(f"/v1/videos/{created['id']}/content",
                      headers=_auth()).status_code == 200
    deleted = client.delete(f"/v1/videos/{created['id']}",
                            headers=_auth()).json()
    assert deleted == {"id": created["id"], "object": "video.deleted",
                       "deleted": True}


# --- ownership / security --------------------------------------------------

def test_stored_objects_are_scoped_per_api_key(tmp_path):
    client = _stub_client(tmp_path, StubDriver("scoped"))
    other = client.post("/admin/keys", headers=_auth(),
                        json={"name": "other"}).json()["api_key"]
    other_auth = {"Authorization": f"Bearer {other}"}

    file_id = client.post("/v1/files", headers=_auth(), files={
        "file": ("secret.txt", b"OWNER-ONLY", "text/plain")}).json()["id"]
    assert client.get(f"/v1/files/{file_id}",
                      headers=other_auth).status_code == 404
    assert client.get("/v1/files", headers=other_auth).json()["data"] == []

    completion = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}],
        "store": True}).json()
    assert client.get(f"/v1/chat/completions/{completion['id']}",
                      headers=other_auth).status_code == 404

    response = client.post("/v1/responses", headers=_auth(),
                           json={"input": "one"}).json()
    assert client.get(f"/v1/responses/{response['id']}",
                      headers=other_auth).status_code == 404
    chained = client.post("/v1/responses", headers=other_auth, json={
        "input": "two", "previous_response_id": response["id"]})
    assert chained.status_code == 404

    # The owner still sees everything.
    assert client.get(f"/v1/files/{file_id}", headers=_auth()).status_code == 200
    assert client.get(f"/v1/chat/completions/{completion['id']}",
                      headers=_auth()).status_code == 200
    assert client.get(f"/v1/responses/{response['id']}",
                      headers=_auth()).status_code == 200


def test_file_expiry_is_pruned_and_bytes_removed(tmp_path):
    store = FileStore(tmp_path / "files", tmp_path / "files.json")
    entry = store.create(filename="old.txt", purpose="assistants",
                         data=b"expired", mime="text/plain")
    index = json.loads((tmp_path / "files.json").read_text(encoding="utf-8"))
    backing = tmp_path / "files" / index["files"][entry["id"]]["stored"]
    assert backing.is_file()
    store._items[entry["id"]]["expires_at"] = time.time() - 1
    assert store.prune(0) == 1
    assert not backing.is_file()
    assert store.get(entry["id"]) is None


def test_expired_file_lookup_removes_backing_bytes(tmp_path):
    store = FileStore(tmp_path / "files", tmp_path / "files.json")
    entry = store.create(filename="old.txt", purpose="assistants",
                         data=b"expired", mime="text/plain")
    index = json.loads((tmp_path / "files.json").read_text(encoding="utf-8"))
    backing = tmp_path / "files" / index["files"][entry["id"]]["stored"]
    store._items[entry["id"]]["expires_at"] = time.time() - 1
    assert store.get(entry["id"]) is None
    assert not backing.is_file()


def test_file_cursor_keeps_same_timestamp_entries(tmp_path):
    store = FileStore(tmp_path / "files", tmp_path / "files.json")
    entries = [store.create(filename=f"{i}.txt", purpose="assistants",
                            data=b"x", mime="text/plain")
               for i in range(3)]
    same_tick = time.time()
    for entry in entries:
        store._items[entry["id"]]["created"] = same_tick
    page = store.list(limit=2, order="desc")
    assert len(page) == 2
    tail = store.list(limit=2, order="desc", after=page[-1]["id"])
    assert len(tail) == 1
    assert {item["id"] for item in page + tail} == {
        entry["id"] for entry in entries}


# --- Responses stream / input items ---------------------------------------

def test_responses_sse_has_event_lines(tmp_path):
    client = _stub_client(tmp_path, StubDriver("events"))
    text = client.post("/v1/responses", headers=_auth(), json={
        "input": "hello", "stream": True}).text
    assert "event: response.created" in text
    assert "event: response.output_text.delta" in text
    assert "event: response.completed" in text


def test_responses_retrieve_stream_replays_terminal_event(tmp_path):
    client = _stub_client(tmp_path, StubDriver("replay"))
    created = client.post("/v1/responses", headers=_auth(),
                          json={"input": "hello"}).json()
    text = client.get(f"/v1/responses/{created['id']}",
                      headers=_auth(), params={"stream": True}).text
    assert "event: response.created" in text
    assert "event: response.output_text.delta" in text
    assert "event: response.completed" in text
    assert '"delta": "replay"' in text


def test_input_items_skip_reasoning_items(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    body = client.post("/v1/responses", headers=_auth(), json={
        "input": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"role": "user", "content": "hello"},
        ]}).json()
    items = client.get(f"/v1/responses/{body['id']}/input_items",
                       headers=_auth()).json()["data"]
    assert [item["content"][0]["text"] for item in items] == ["hello"]


def test_prompt_template_is_rejected(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    r = client.post("/v1/responses", headers=_auth(), json={
        "input": "hello", "prompt": {"id": "pmpt_123"}})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "prompt"


# --- Responses input attachments -------------------------------------------

def test_function_call_output_list_reaches_upstream(tmp_path):
    driver = StubDriver("ok")
    client = _stub_client(tmp_path, driver)
    image = ("data:image/png;base64,"
             "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    r = client.post("/v1/responses", headers=_auth(), json={
        "input": [
            {"type": "function_call", "call_id": "call_1",
             "name": "look", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": [
                {"type": "input_text", "text": "here"},
                {"type": "input_image", "image_url": image},
            ]},
        ]})
    assert r.status_code == 200
    assert driver.requests[0].images[0].mime == "image/png"


def test_input_file_url_is_fetched(tmp_path, monkeypatch):
    driver = StubDriver("ok")
    client = _stub_client(tmp_path, driver)

    async def fake_load(ref, *, timeout=20.0):
        return b"URL-FILE", "text/plain"

    monkeypatch.setattr("muse2api.api.routes.responses.load_image_ref", fake_load)
    r = client.post("/v1/responses", headers=_auth(), json={
        "input": [{"type": "input_file", "file_url": "https://example.com/a.txt"},
                  {"role": "user", "content": "read"}]})
    assert r.status_code == 200
    assert driver.requests[0].images[0].data == b"URL-FILE"
