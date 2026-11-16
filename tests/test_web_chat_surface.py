"""0.11.0 web-chat surface tests.

These lock in the parity work that makes the proxy behave like a pure API for
what muse.ai's web chat really does:

* generated artifacts (images/videos/documents) are re-hosted in the reply,
* web sources become standard ``url_citation`` annotations,
* the OpenAI Files API resolves ``file_id`` references,
* Responses supports retrieve/delete/cancel/input_items/input_tokens,
* modern request parameters (max_completion_tokens, parallel_tool_calls,
  legacy functions, web_search_options) have real semantics.
"""

from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from muse2api.app import create_app
from muse2api.config import Settings
from muse2api.core.tokens import count_tokens
from muse2api.drivers.mock import MockDriver
from test_robustness import StubDriver, _auth, _stub_client

_TWO_TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "get_time",
        "parameters": {"type": "object", "properties": {"zone": {"type": "string"}}}}},
]


def _mock_client(tmp_path) -> TestClient:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data",
                        api_key="test-key", admin_key="test-key", driver="mock")
    return TestClient(create_app(settings, MockDriver(delay=0)))


def _sse_events(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            payload = line[6:]
            if payload != "[DONE]":
                out.append(json.loads(payload))
    return out


# --- generated chat artifacts & citations ---------------------------------

def test_chat_generated_media_is_rehosted_and_annotated(tmp_path):
    client = _mock_client(tmp_path)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi [[mock:extras]]"}]})
    message = r.json()["choices"][0]["message"]
    assert "![mock image](http://testserver/v1/media/chat_" in message["content"]
    url = re.search(r"\((http://testserver/v1/media/[^)]+)\)", message["content"]).group(1)
    media = client.get(url.split("testserver", 1)[1])
    assert media.status_code == 200 and media.content.startswith(b"\x89PNG")
    annotation = message["annotations"][0]
    assert annotation["type"] == "url_citation"
    assert annotation["url_citation"]["url"] == "https://example.com/source"


def test_chat_stream_replays_media_and_annotations(tmp_path):
    client = _mock_client(tmp_path)
    with client.stream("POST", "/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi [[mock:extras]]"}],
        "stream": True,
    }) as r:
        events = _sse_events(r.read().decode())
    text = "".join(e["choices"][0]["delta"].get("content", "")
                   for e in events if e.get("choices"))
    assert "![mock image](http://testserver/v1/media/chat_" in text
    finish = [e for e in events if e.get("choices")
              and e["choices"][0].get("finish_reason") == "stop"][0]
    assert finish["choices"][0]["delta"]["annotations"][0]["url_citation"]["url"] == (
        "https://example.com/source")


def test_responses_media_and_annotations(tmp_path):
    client = _mock_client(tmp_path)
    r = client.post("/v1/responses", headers=_auth(), json={
        "input": "hello [[mock:extras]]", "metadata": {"trace": "abc"}})
    body = r.json()
    assert body["metadata"] == {"trace": "abc"}
    part = body["output"][0]["content"][0]
    assert "![mock image](http://testserver/v1/media/chat_" in part["text"]
    assert part["annotations"][0]["type"] == "url_citation"
    assert part["annotations"][0]["url"] == "https://example.com/source"


def test_responses_stream_emits_annotation_events(tmp_path):
    client = _mock_client(tmp_path)
    with client.stream("POST", "/v1/responses", headers=_auth(), json={
        "input": "hello [[mock:extras]]", "stream": True}) as r:
        events = _sse_events(r.read().decode())
    kinds = [e.get("type") for e in events]
    assert "response.output_text.annotation.added" in kinds
    completed = [e for e in events if e.get("type") == "response.completed"][0]
    part = completed["response"]["output"][0]["content"][0]
    assert part["annotations"][0]["url"] == "https://example.com/source"
    assert "![mock image](" in part["text"]


# --- Files API -------------------------------------------------------------

def test_files_api_roundtrip(tmp_path):
    client = _mock_client(tmp_path)
    created = client.post("/v1/files", headers=_auth(), files={
        "file": ("note.txt", b"FILE-SECRET", "text/plain")},
        data={"purpose": "assistants"}).json()
    file_id = created["id"]
    assert created["object"] == "file" and created["bytes"] == 11
    assert created["purpose"] == "assistants"

    assert [f["id"] for f in client.get("/v1/files", headers=_auth()).json()["data"]] == [
        file_id]
    assert client.get(f"/v1/files/{file_id}", headers=_auth()).json()["filename"] == "note.txt"
    content = client.get(f"/v1/files/{file_id}/content", headers=_auth())
    assert content.status_code == 200 and content.content == b"FILE-SECRET"
    assert client.delete(f"/v1/files/{file_id}", headers=_auth()).json()["deleted"] is True
    assert client.get(f"/v1/files/{file_id}", headers=_auth()).status_code == 404


def test_file_id_is_resolved_for_chat(tmp_path):
    driver = StubDriver("ok")
    client = _stub_client(tmp_path, driver)
    file_id = client.post("/v1/files", headers=_auth(), files={
        "file": ("secret.txt", b"XYZZY-99", "text/plain")}).json()["id"]
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": [
            {"type": "file", "file": {"file_id": file_id}}]}]})
    assert r.status_code == 200
    assert driver.requests[0].images[0].mime == "text/plain"
    assert driver.requests[0].images[0].data == b"XYZZY-99"


def test_file_id_is_resolved_for_responses(tmp_path):
    driver = StubDriver("ok")
    client = _stub_client(tmp_path, driver)
    file_id = client.post("/v1/files", headers=_auth(), files={
        "file": ("secret.txt", b"RESP-SECRET", "text/plain")}).json()["id"]
    r = client.post("/v1/responses", headers=_auth(), json={
        "input": [{"type": "input_file", "file_id": file_id},
                  {"role": "user", "content": "read it"}]})
    assert r.status_code == 200
    assert driver.requests[0].images[0].data == b"RESP-SECRET"


def test_file_id_with_system_prompt_keeps_attachment(tmp_path):
    """The prompt bridge used to drop file/audio parts when a system message
    was present (only ``image_url`` was extracted)."""
    driver = StubDriver("ok")
    client = _stub_client(tmp_path, driver)
    file_id = client.post("/v1/files", headers=_auth(), files={
        "file": ("secret.txt", b"SYS-SECRET", "text/plain")}).json()["id"]
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": [
                {"type": "text", "text": "read it"},
                {"type": "file", "file": {"file_id": file_id}}]}]})
    assert r.status_code == 200
    assert driver.requests[0].images[0].data == b"SYS-SECRET"


def test_unknown_file_id_returns_404(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": [
            {"type": "file", "file": {"file_id": "file-123"}}]}]})
    assert r.status_code == 404
    assert "file-123" in r.json()["error"]["message"]


# --- Responses state surface ----------------------------------------------

def test_responses_retrieve_input_items_and_delete(tmp_path):
    client = _mock_client(tmp_path)
    body = client.post("/v1/responses", headers=_auth(),
                       json={"input": "remember this"}).json()
    response_id = body["id"]
    assert client.get(f"/v1/responses/{response_id}", headers=_auth()).json()["id"] == (
        response_id)
    items = client.get(f"/v1/responses/{response_id}/input_items",
                       headers=_auth()).json()
    assert items["data"][0]["content"][0]["text"] == "remember this"
    assert client.delete(f"/v1/responses/{response_id}",
                         headers=_auth()).json()["deleted"] is True
    assert client.get(f"/v1/responses/{response_id}",
                      headers=_auth()).status_code == 404


def test_responses_input_tokens(tmp_path):
    client = _mock_client(tmp_path)
    r = client.post("/v1/responses/input_tokens", headers=_auth(),
                    json={"input": "count these words please"})
    assert r.status_code == 200
    assert r.json()["object"] == "response.input_tokens"
    assert r.json()["input_tokens"] > 0


def test_responses_accepts_native_function_tool_shape(tmp_path):
    """The official SDK sends Responses tools as top-level fields, not nested
    under ``function`` like Chat Completions."""
    reply = json.dumps({"tool": "get_weather", "arguments": {"city": "Paris"}})
    driver = StubDriver(reply)
    client = _stub_client(tmp_path, driver, tool_channel="planner", tool_retries=0)
    r = client.post("/v1/responses", headers=_auth(), json={
        "input": "weather in Paris?", "tools": [{
            "type": "function", "name": "get_weather",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}}}}]})
    assert r.status_code == 200
    calls = [item for item in r.json()["output"] if item["type"] == "function_call"]
    assert calls[0]["name"] == "get_weather"


def test_responses_web_search_tool_adds_a_directive(tmp_path):
    driver = StubDriver("today's news")
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/responses", headers=_auth(), json={
        "input": "latest news", "tools": [{"type": "web_search"}]})
    assert r.status_code == 200
    assert "[[WEB SEARCH]]" in driver.requests[0].prompt


def test_responses_cancel_requires_an_inflight_turn(tmp_path):
    client = _mock_client(tmp_path)
    body = client.post("/v1/responses", headers=_auth(), json={"input": "hi"}).json()
    r = client.post(f"/v1/responses/{body['id']}/cancel", headers=_auth())
    assert r.status_code == 400


# --- models ----------------------------------------------------------------

def test_model_retrieve(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.get("/v1/models/muse-chat", headers=_auth())
    assert r.status_code == 200 and r.json()["id"] == "muse-chat"
    missing = client.get("/v1/models/gpt-4o", headers=_auth())
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"


def test_image_variations_endpoint(tmp_path):
    client = _mock_client(tmp_path)
    r = client.post("/v1/images/variations", headers=_auth(),
                    data={"response_format": "b64_json"},
                    files={"image": ("a.png", b"\x89PNG-data", "image/png")})
    assert r.status_code == 200
    assert r.json()["data"][0]["b64_json"]


def test_media_suffix_renders_videos_and_files(tmp_path):
    from muse2api.api.routes.chat import _media_suffix
    from muse2api.core.media import MediaStore
    from muse2api.drivers.base import GeneratedMedia

    class _Svc:
        media = MediaStore(tmp_path / "media")

    suffix = _media_suffix([
        GeneratedMedia(data=b"v", mime="video/mp4", kind="video", label="clip"),
        GeneratedMedia(data=b"f", mime="application/pdf", kind="file",
                       label="report.pdf"),
    ], _Svc(), "http://gw")
    assert suffix.startswith("\n\n")
    assert "[clip](http://gw/v1/media/chat_" in suffix
    assert "[report.pdf](http://gw/v1/media/chat_" in suffix
    assert "![clip]" not in suffix


# --- modern chat parameters ------------------------------------------------

def test_max_completion_tokens_caps_and_marks_length(tmp_path):
    client = _stub_client(tmp_path, StubDriver("abcdefghij klmnopqrst"))
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 1})
    content = r.json()["choices"][0]["message"]["content"]
    assert count_tokens(content) <= 1
    assert content != "abcdefghij klmnopqrst"
    assert r.json()["choices"][0]["finish_reason"] == "length"


def test_legacy_functions_become_tools(tmp_path):
    driver = StubDriver("plain reply")
    client = _stub_client(tmp_path, driver, tool_channel="planner", tool_retries=0)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "weather?"}],
        "functions": [{"name": "get_weather",
                       "parameters": {"type": "object",
                                      "properties": {"city": {"type": "string"}}}}],
        "function_call": "auto"})
    assert r.status_code == 200
    assert driver.requests[0].tools[0]["function"]["name"] == "get_weather"


def test_legacy_functions_response_shape(tmp_path):
    driver = StubDriver('{"tool": "get_weather", "arguments": {"city": "Paris"}}')
    client = _stub_client(tmp_path, driver, tool_channel="planner", tool_retries=0)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "weather?"}],
        "functions": [{"name": "get_weather",
                       "parameters": {"type": "object",
                                      "properties": {"city": {"type": "string"}}}}]})
    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "function_call"
    assert choice["message"]["function_call"]["name"] == "get_weather"
    assert "tool_calls" not in choice["message"]


def test_parallel_tool_calls_false_keeps_one_call(tmp_path):
    reply = json.dumps({"calls": [
        {"tool": "get_weather", "arguments": {"city": "Paris"}},
        {"tool": "get_time", "arguments": {"zone": "UTC"}}]})
    driver = StubDriver(reply)
    client = _stub_client(tmp_path, driver, tool_channel="planner", tool_retries=0)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "both"}],
        "tools": _TWO_TOOLS, "tool_choice": "required",
        "parallel_tool_calls": False})
    calls = r.json()["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1


def test_web_search_options_adds_a_directive(tmp_path):
    driver = StubDriver("answer")
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "latest news"}],
        "web_search_options": {"search_context_size": "high"}})
    assert r.status_code == 200
    assert "[[WEB SEARCH]]" in driver.requests[0].prompt


def test_usage_chunk_semantics(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hello world"))
    with client.stream("POST", "/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True,
        "stream_options": {"include_usage": True}}) as r:
        events = _sse_events(r.read().decode())
    assert all(e["usage"] is None for e in events if e["choices"])
    assert events[-1]["choices"] == [] and events[-1]["usage"]["total_tokens"] > 0


def test_logprobs_is_rejected_like_a_model_without_support(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}], "logprobs": True})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "logprobs"
