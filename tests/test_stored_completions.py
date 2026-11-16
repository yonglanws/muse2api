"""Stored Chat Completions: ``store: true`` → list/retrieve/update/delete/messages."""

from __future__ import annotations

import json

from test_robustness import StubDriver, _auth, _stub_client


def _create(client, content="hi", **extra):
    return client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": content}], **extra})


def test_store_true_roundtrip(tmp_path):
    client = _stub_client(tmp_path, StubDriver("stored reply"))
    created = _create(client, "remember this", store=True,
                      metadata={"trace": "abc"}).json()
    completion_id = created["id"]

    retrieved = client.get(f"/v1/chat/completions/{completion_id}",
                           headers=_auth())
    assert retrieved.status_code == 200
    body = retrieved.json()
    assert body["id"] == completion_id
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "stored reply"
    assert body["metadata"] == {"trace": "abc"}
    assert body["service_tier"] == "default"
    assert body["usage"]["total_tokens"] > 0

    messages = client.get(f"/v1/chat/completions/{completion_id}/messages",
                          headers=_auth()).json()
    assert messages["object"] == "list"
    assert messages["has_more"] is False
    assert messages["first_id"] == f"{completion_id}-0"
    assert messages["data"] == [{
        "id": f"{completion_id}-0", "role": "user", "content": "remember this",
        "name": None, "content_parts": None,
    }]

    updated = client.post(f"/v1/chat/completions/{completion_id}", headers=_auth(),
                          json={"metadata": {"trace": "xyz"}}).json()
    assert updated["metadata"] == {"trace": "xyz"}
    assert client.get(f"/v1/chat/completions/{completion_id}",
                      headers=_auth()).json()["metadata"] == {"trace": "xyz"}

    listed = client.get("/v1/chat/completions", headers=_auth()).json()
    assert [item["id"] for item in listed["data"]] == [completion_id]
    assert listed["first_id"] == listed["last_id"] == completion_id
    # metadata filtering is part of the stored-completions list endpoint
    assert client.get("/v1/chat/completions", headers=_auth(),
                      params={"metadata[trace]": "xyz"}).json()["data"]
    assert not client.get("/v1/chat/completions", headers=_auth(),
                          params={"metadata[trace]": "nope"}).json()["data"]

    deleted = client.delete(f"/v1/chat/completions/{completion_id}",
                            headers=_auth()).json()
    assert deleted == {"id": completion_id, "object": "chat.completion.deleted",
                       "deleted": True}
    assert client.get(f"/v1/chat/completions/{completion_id}",
                      headers=_auth()).status_code == 404


def test_store_defaults_to_false(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ephemeral"))
    created = _create(client, "no store").json()
    assert client.get(f"/v1/chat/completions/{created['id']}",
                      headers=_auth()).status_code == 404
    assert client.get("/v1/chat/completions", headers=_auth()).json()["data"] == []


def test_stored_stream_completion(tmp_path):
    client = _stub_client(tmp_path, StubDriver("streamed answer"))
    with client.stream("POST", "/v1/chat/completions", headers=_auth(), json={
            "messages": [{"role": "user", "content": "stream?"}],
            "stream": True, "store": True}) as response:
        events = [json.loads(line[6:]) for line in response.iter_lines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
    completion_id = next(e["id"] for e in events if e.get("id"))

    stored = client.get(f"/v1/chat/completions/{completion_id}", headers=_auth()).json()
    assert stored["choices"][0]["message"]["content"] == "streamed answer"
    assert stored["choices"][0]["finish_reason"] == "stop"
    assert stored["usage"]["completion_tokens"] > 0


def test_list_pagination_and_filters(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    ids = [_create(client, f"m{i}", store=True, metadata={"n": str(i)}).json()["id"]
           for i in range(3)]

    first = client.get("/v1/chat/completions", headers=_auth(),
                       params={"limit": 2}).json()
    assert [c["id"] for c in first["data"]] == ids[:2]
    assert first["has_more"] is True
    assert first["last_id"] == ids[1]

    second = client.get("/v1/chat/completions", headers=_auth(),
                        params={"limit": 2, "after": first["last_id"]}).json()
    assert [c["id"] for c in second["data"]] == ids[2:]
    assert second["has_more"] is False

    desc = client.get("/v1/chat/completions", headers=_auth(),
                      params={"order": "desc"}).json()
    assert [c["id"] for c in desc["data"]] == list(reversed(ids))

    filtered = client.get("/v1/chat/completions", headers=_auth(),
                          params={"model": "muse-fast"}).json()["data"]
    assert filtered == [] or all(c["model"] for c in filtered)


def test_stored_completion_bad_pagination(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    completion_id = _create(client, "x", store=True).json()["id"]
    for params in ({"order": "sideways"}, {"limit": 0}, {"limit": "many"},
                   {"after": "chatcmpl-nope"}):
        r = client.get("/v1/chat/completions", headers=_auth(), params=params)
        assert r.status_code == 400
    r = client.get(f"/v1/chat/completions/{completion_id}/messages",
                   headers=_auth(), params={"after": "chatcmpl-nope"})
    assert r.status_code == 400


def test_stored_messages_keep_multimodal_parts(tmp_path):
    client = _stub_client(tmp_path, StubDriver("seen"))
    image = ("data:image/png;base64,"
             "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    created = client.post("/v1/chat/completions", headers=_auth(), json={
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": image}},
        ]}], "store": True}).json()

    messages = client.get(f"/v1/chat/completions/{created['id']}/messages",
                          headers=_auth()).json()["data"]
    assert messages[0]["content"] == "describe"
    assert messages[0]["content_parts"][0] == {"type": "text", "text": "describe"}
    assert messages[0]["content_parts"][1]["type"] == "image_url"


def test_stored_completion_unknown_id_404(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    assert client.get("/v1/chat/completions/chatcmpl-missing",
                      headers=_auth()).status_code == 404
    assert client.delete("/v1/chat/completions/chatcmpl-missing",
                         headers=_auth()).status_code == 404
    assert client.post("/v1/chat/completions/chatcmpl-missing", headers=_auth(),
                       json={"metadata": {}}).status_code == 404
    assert client.get("/v1/chat/completions/chatcmpl-missing/messages",
                      headers=_auth()).status_code == 404
