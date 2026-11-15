"""Round-3 (0.10.0) parity tests — the error/header/multi-choice/Responses gaps.

Every test here maps to one E-defect in docs/superpowers/specs/2026-11-16-v0100-design.md:
E1 404/405 envelopes, E2 ``param`` field, E3 OpenAI response headers,
E4 streaming n>1 (stop per choice), E5 Responses state + function calling,
E6 unknown model, E7 stream_options without stream, E9 audio parts, E10 max_tokens.
"""

from __future__ import annotations

import base64
import json

from test_robustness import StubDriver, _auth, _stub_client

_TOOL_JSON = '{"tool": "get_weather", "arguments": {"city": "Paris"}}'


def _tool_def() -> dict:
    return {"type": "function", "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}


# --- E1: routing errors use the OpenAI envelope ----------------------------

def test_unknown_path_returns_openai_envelope(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.get("/v1/definitely-not-a-thing", headers=_auth())
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["message"] == "Invalid URL (GET /v1/definitely-not-a-thing)"
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["param"] is None and body["error"]["code"] is None


def test_wrong_method_returns_openai_envelope(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.get("/v1/chat/completions", headers=_auth())
    assert r.status_code == 405
    body = r.json()
    assert body["error"]["message"].startswith(
        "Not allowed to GET on /v1/chat/completions.")
    assert body["error"]["type"] == "invalid_request_error"


# --- E2: every error body carries ``param`` --------------------------------

def test_validation_error_carries_param(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/chat/completions", headers=_auth(), json={"messages": []})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "messages"


def test_invalid_request_carries_param(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/chat/completions", headers=_auth(),
                    json={"messages": [{"role": "user", "content": "hi"}],
                          "stream": False, "stream_options": {"include_usage": True}})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["param"] == "stream_options"
    assert "stream" in body["error"]["message"]


# --- E3: OpenAI response headers -------------------------------------------

def test_openai_response_headers(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/chat/completions", headers=_auth(),
                    json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.headers["openai-version"] == "2020-10-01"
    rid = r.headers["x-request-id"]
    assert len(rid) == 32 and all(c in "0123456789abcdef" for c in rid)
    assert int(r.headers["openai-processing-ms"]) >= 0


# --- E4: stop sequences apply per choice when streaming n>1 -----------------

def test_stream_n_gt_1_stop_per_choice(tmp_path):
    driver = StubDriver("hello")
    client = _stub_client(tmp_path, driver)
    with client.stream("POST", "/v1/chat/completions", headers=_auth(), json={
            "messages": [{"role": "user", "content": "hi"}], "n": 2, "stream": True,
            "stop": ["ll"]}) as resp:
        lines = [ln[6:] for ln in resp.iter_lines() if ln.startswith("data: ")
                 and ln != "data: [DONE]"]
    chunks = [json.loads(ln) for ln in lines if ln]
    content: dict[int, str] = {}
    finishes = []
    for c in chunks:
        if not c["choices"]:
            continue
        choice = c["choices"][0]
        if choice["delta"].get("content"):
            content[choice["index"]] = content.get(choice["index"], "") + \
                choice["delta"]["content"]
        if choice["finish_reason"]:
            finishes.append((choice["index"], choice["finish_reason"]))
    assert content == {0: "he", 1: "he"}
    assert sorted(finishes) == [(0, "stop"), (1, "stop")]


# --- E5a: previous_response_id chains ---------------------------------------

def test_previous_response_id_continues_the_chain(tmp_path):
    driver = StubDriver("okay")
    client = _stub_client(tmp_path, driver)
    r1 = client.post("/v1/responses", headers=_auth(),
                     json={"input": "remember the word banana", "user": "u1"})
    assert r1.status_code == 200
    resp_id = r1.json()["id"]
    r2 = client.post("/v1/responses", headers=_auth(),
                     json={"input": "what word?", "previous_response_id": resp_id})
    assert r2.status_code == 200
    assert r2.json()["previous_response_id"] == resp_id
    # The second request re-sends the stored history plus the new input.
    assert "banana" in driver.requests[1].prompt and "what word?" in driver.requests[1].prompt
    # Same conversation hint → the driver can keep reusing the upstream thread.
    assert driver.requests[0].conversation_hint == driver.requests[1].conversation_hint


def test_previous_response_id_replaces_stored_instructions(tmp_path):
    driver = StubDriver("okay")
    client = _stub_client(tmp_path, driver)
    r1 = client.post("/v1/responses", headers=_auth(),
                     json={"instructions": "be terse", "input": "hi"})
    resp_id = r1.json()["id"]
    r2 = client.post("/v1/responses", headers=_auth(),
                     json={"instructions": "be loud", "input": "hi again",
                           "previous_response_id": resp_id})
    assert r2.status_code == 200
    assert "be loud" in driver.requests[1].prompt
    assert "be terse" not in driver.requests[1].prompt


def test_previous_response_id_unknown(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/responses", headers=_auth(),
                    json={"input": "hi", "previous_response_id": "resp_missing"})
    assert r.status_code == 404
    body = r.json()
    assert "resp_missing" in body["error"]["message"]
    assert body["error"]["param"] == "previous_response_id"


def test_store_false_is_not_stored(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/responses", headers=_auth(),
                    json={"input": "x", "store": False})
    assert r.status_code == 200
    assert r.json()["store"] is False
    resp_id = r.json()["id"]
    r2 = client.post("/v1/responses", headers=_auth(),
                     json={"input": "again?", "previous_response_id": resp_id})
    assert r2.status_code == 404


# --- E5b: function calling ---------------------------------------------------

def test_responses_function_call_output_item(tmp_path):
    driver = StubDriver(_TOOL_JSON)
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/responses", headers=_auth(),
                    json={"input": "weather in Paris?", "tools": [_tool_def()]})
    assert r.status_code == 200
    body = r.json()
    calls = [item for item in body["output"] if item["type"] == "function_call"]
    assert len(calls) == 1
    assert calls[0]["name"] == "get_weather"
    assert json.loads(calls[0]["arguments"]) == {"city": "Paris"}
    assert calls[0]["call_id"].startswith("call_")


def test_responses_function_call_output_input(tmp_path):
    driver = StubDriver("It is sunny.")
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/responses", headers=_auth(), json={
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "get_weather",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1",
             "output": "sunny, 26C"},
            {"role": "user", "content": "summarize"}],
        "tools": [_tool_def()]})
    assert r.status_code == 200
    # The tool result travels to the model inside the built prompt.
    assert "sunny, 26C" in driver.requests[0].prompt


def test_responses_function_call_stream_events(tmp_path):
    driver = StubDriver(_TOOL_JSON)
    client = _stub_client(tmp_path, driver)
    with client.stream("POST", "/v1/responses", headers=_auth(), json={
            "input": "weather?", "tools": [_tool_def()], "stream": True}) as resp:
        lines = [ln[6:] for ln in resp.iter_lines() if ln.startswith("data: ")
                 and ln != "data: [DONE]"]
    events = [json.loads(ln) for ln in lines if ln]
    types = [e["type"] for e in events]
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types
    assert types[-1] == "response.completed"
    item = next(e for e in events
                if e["type"] == "response.output_item.done"
                and e["item"]["type"] == "function_call")
    assert item["item"]["name"] == "get_weather"


# --- E5d: response object completeness ---------------------------------------

def test_response_object_fields(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/responses", headers=_auth(),
                    json={"input": "hi", "instructions": "be nice",
                          "tools": [_tool_def()]})
    body = r.json()
    assert body["instructions"] == "be nice"
    assert body["store"] is True
    assert body["tools"] == [_tool_def()]
    assert body["error"] is None
    assert body["incomplete_details"] is None
    assert body["previous_response_id"] is None
    assert body["metadata"] == {}
    assert body["text"]["format"] == {"type": "text"}


# --- E6: unknown models -------------------------------------------------------

def test_unknown_model_is_rejected_like_the_real_api(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/chat/completions", headers=_auth(),
                    json={"model": "definitely-missing",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404
    body = r.json()["error"]
    assert body["code"] == "model_not_found"
    assert body["param"] == "model"
    assert "'definitely-missing'" in body["message"]


def test_new_aliases_resolve(tmp_path):
    driver = StubDriver("hi")
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/chat/completions", headers=_auth(),
                    json={"model": "gpt-3.5-turbo",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    r = client.post("/v1/chat/completions", headers=_auth(),
                    json={"model": "o4-mini",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


# --- E9: unsupported modalities are rejected, not dropped ---------------------

def test_audio_part_becomes_an_attachment(tmp_path):
    # Probe 17: muse.ai accepts audio attachments (file_ref) — the agent even
    # offers transcription. 0.10.0 therefore supports input_audio parts.
    driver = StubDriver("hi")
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "listen"},
            {"type": "input_audio", "input_audio": {"data": "c2VjcmV0", "format": "wav"}}]}]})
    assert r.status_code == 200
    assert len(driver.requests[0].images) == 1
    assert driver.requests[0].images[0].mime == "audio/wav"


def test_file_part_becomes_an_attachment(tmp_path):
    driver = StubDriver("hi")
    client = _stub_client(tmp_path, driver)
    payload = base64.b64encode(b"secret: XYZZY-42").decode()
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": [
            {"type": "file", "file": {"file_data":
                                      f"data:application/pdf;base64,{payload}"}}]}]})
    assert r.status_code == 200
    assert len(driver.requests[0].images) == 1
    assert driver.requests[0].images[0].mime == "application/pdf"
    assert b"XYZZY-42" in driver.requests[0].images[0].data


def test_responses_input_file_part_becomes_an_attachment(tmp_path):
    driver = StubDriver("hi")
    client = _stub_client(tmp_path, driver)
    payload = base64.b64encode(b"note body").decode()
    r = client.post("/v1/responses", headers=_auth(), json={
        "input": [{"type": "input_file", "file_data":
                   f"data:text/plain;base64,{payload}", "filename": "note.txt"},
                  {"role": "user", "content": "read it"}]})
    assert r.status_code == 200
    assert driver.requests[0].images[0].mime == "text/plain"


def test_file_id_is_rejected_with_clear_error(tmp_path):
    client = _stub_client(tmp_path, StubDriver("hi"))
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": [
            {"type": "file", "file": {"file_id": "file-123"}}]}]})
    assert r.status_code == 400
    assert "file_id" in r.json()["error"]["message"]


# --- E10: max_tokens truncates with finish_reason length ----------------------

_LONG = "abcdefghij klmnopqrst"


def test_max_tokens_truncates_non_stream(tmp_path):
    driver = StubDriver(_LONG)
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": "hi"}], "max_tokens": 2})
    body = r.json()
    choice = body["choices"][0]
    assert len(choice["message"]["content"]) <= 8
    assert choice["finish_reason"] == "length"


def test_max_tokens_truncates_stream(tmp_path):
    driver = StubDriver(_LONG)
    client = _stub_client(tmp_path, driver)
    with client.stream("POST", "/v1/chat/completions", headers=_auth(), json={
            "messages": [{"role": "user", "content": "hi"}], "max_tokens": 2,
            "stream": True}) as resp:
        lines = [ln[6:] for ln in resp.iter_lines() if ln.startswith("data: ")
                 and ln != "data: [DONE]"]
    chunks = [json.loads(ln) for ln in lines if ln]
    content = ""
    finish = None
    for c in chunks:
        if not c["choices"]:
            continue
        choice = c["choices"][0]
        content += choice["delta"].get("content") or ""
        if choice["finish_reason"]:
            finish = choice["finish_reason"]
    assert content == _LONG[:8]
    assert finish == "length"


def test_responses_max_output_tokens_marks_incomplete(tmp_path):
    driver = StubDriver(_LONG)
    client = _stub_client(tmp_path, driver)
    r = client.post("/v1/responses", headers=_auth(), json={
        "input": "hi", "max_output_tokens": 2})
    body = r.json()
    assert body["status"] == "incomplete"
    assert body["incomplete_details"] == {"reason": "max_output_tokens"}
    assert len(body["output"][0]["content"][0]["text"]) <= 8
    assert body["usage"]["output_tokens"] <= 2
