"""0.12.0 surface tests: stored completions, Responses object fields, image/file
metadata, and explicit rejections for upstream-inexpressible parameters."""

from __future__ import annotations

import json

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

    rejected = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}],
        "service_tier": "priority"})
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


def test_responses_rejects_background_and_conversation(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    background = client.post("/v1/responses", headers=_auth(), json={
        "input": "hello", "background": True})
    assert background.status_code == 400
    assert background.json()["error"]["param"] == "background"

    conversation = client.post("/v1/responses", headers=_auth(), json={
        "input": "hello", "conversation": "conv_123"})
    assert conversation.status_code == 400
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


def test_image_stream_is_rejected(tmp_path):
    client = _mock_client(tmp_path)
    r = client.post("/v1/images/generations", headers=_auth(), json={
        "prompt": "a red square", "stream": True})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "stream"


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
                         headers=_auth()).status_code == 405
