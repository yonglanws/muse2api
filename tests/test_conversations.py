"""OpenAI Conversations API and Responses conversation= integration."""

from __future__ import annotations

from test_robustness import StubDriver, _auth, _stub_client


def _create(client, **body) -> dict:
    return client.post("/v1/conversations", headers=_auth(), json=body).json()


def test_conversation_crud_and_items(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    created = _create(client, metadata={"trace": "c1"},
                      items=[{"role": "user", "content": "hello"}])
    assert created["object"] == "conversation"
    assert created["metadata"] == {"trace": "c1"}
    conversation_id = created["id"]

    assert client.get(f"/v1/conversations/{conversation_id}",
                      headers=_auth()).json()["id"] == conversation_id
    updated = client.post(f"/v1/conversations/{conversation_id}", headers=_auth(),
                          json={"metadata": {"trace": "c2"}}).json()
    assert updated["metadata"] == {"trace": "c2"}

    page = client.get(f"/v1/conversations/{conversation_id}/items",
                      headers=_auth()).json()
    assert page["object"] == "list"
    assert page["has_more"] is False
    assert len(page["data"]) == 1
    item = page["data"][0]
    assert item["type"] == "message"
    assert item["role"] == "user"
    assert item["status"] == "completed"
    assert item["id"].startswith("msg_")
    assert item["content"] == [{"type": "input_text", "text": "hello"}]

    fetched = client.get(
        f"/v1/conversations/{conversation_id}/items/{item['id']}",
        headers=_auth()).json()
    assert fetched == item

    added = client.post(
        f"/v1/conversations/{conversation_id}/items", headers=_auth(),
        json={"items": [{"role": "assistant", "content": "hi there"}]}).json()
    assert added["object"] == "list"
    assert added["data"][0]["content"][0]["type"] == "output_text"
    assert added["data"][0]["content"][0]["annotations"] == []

    deleted_item = client.delete(
        f"/v1/conversations/{conversation_id}/items/{item['id']}",
        headers=_auth()).json()
    assert deleted_item["object"] == "conversation"

    deleted = client.delete(f"/v1/conversations/{conversation_id}",
                            headers=_auth()).json()
    assert deleted == {"id": conversation_id, "object": "conversation.deleted",
                       "deleted": True}
    assert client.get(f"/v1/conversations/{conversation_id}",
                      headers=_auth()).status_code == 404


def test_conversation_items_pagination(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    conversation = _create(client, items=[
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ])
    cid = conversation["id"]
    page = client.get(f"/v1/conversations/{cid}/items", headers=_auth(),
                      params={"limit": 2}).json()
    assert page["has_more"] is True
    assert len(page["data"]) == 2
    assert page["data"][0]["content"][0]["text"] == "three"
    tail = client.get(f"/v1/conversations/{cid}/items", headers=_auth(),
                      params={"limit": 2, "after": page["last_id"]}).json()
    assert tail["has_more"] is False
    assert len(tail["data"]) == 1
    assert tail["data"][0]["content"][0]["text"] == "one"
    bad = client.get(f"/v1/conversations/{cid}/items", headers=_auth(),
                     params={"after": "item_missing"})
    assert bad.status_code == 400


def test_conversations_are_scoped_per_key(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    other = client.post("/admin/keys", headers=_auth(),
                        json={"name": "conv-other"}).json()["api_key"]
    other_auth = {"Authorization": f"Bearer {other}"}
    cid = _create(client, items=[{"role": "user", "content": "secret"}])["id"]
    assert client.get(f"/v1/conversations/{cid}",
                      headers=other_auth).status_code == 404
    assert client.get(f"/v1/conversations/{cid}/items",
                      headers=other_auth).status_code == 404
    assert client.delete(f"/v1/conversations/{cid}",
                         headers=other_auth).status_code == 404
    assert client.get(f"/v1/conversations/{cid}",
                      headers=_auth()).status_code == 200


def test_responses_conversation_appends_turns(tmp_path):
    driver = StubDriver("assistant reply")
    client = _stub_client(tmp_path, driver)
    cid = _create(client)["id"]

    first = client.post("/v1/responses", headers=_auth(), json={
        "model": "muse-chat", "input": "hello", "conversation": cid})
    assert first.status_code == 200
    assert first.json()["status"] == "completed"
    assert first.json()["conversation"]["id"] == cid

    page = client.get(f"/v1/conversations/{cid}/items", headers=_auth()).json()
    assert len(page["data"]) == 2
    roles = {item["role"] for item in page["data"] if item["type"] == "message"}
    assert roles == {"user", "assistant"}

    second = client.post("/v1/responses", headers=_auth(), json={
        "model": "muse-chat", "input": "again",
        "conversation": {"id": cid}})
    assert second.status_code == 200
    # The second planner prompt must contain both the earlier user turn and
    # the earlier assistant reply, proving conversation items were prepended.
    prompt = driver.requests[1].prompt
    assert "hello" in prompt and "assistant reply" in prompt and "again" in prompt
    page = client.get(f"/v1/conversations/{cid}/items", headers=_auth()).json()
    assert len(page["data"]) == 4

    conflict = client.post("/v1/responses", headers=_auth(), json={
        "model": "muse-chat", "input": "x", "conversation": cid,
        "previous_response_id": "resp_other"})
    assert conflict.status_code == 400
    assert conflict.json()["error"]["param"] == "conversation"


def test_responses_conversation_streaming_and_store_false(tmp_path):
    client = _stub_client(tmp_path, StubDriver("streamed reply"))
    cid = _create(client)["id"]
    streamed = client.post("/v1/responses", headers=_auth(), json={
        "model": "muse-chat", "input": "stream me", "conversation": cid,
        "stream": True})
    assert streamed.status_code == 200
    assert "event: response.completed" in streamed.text
    page = client.get(f"/v1/conversations/{cid}/items", headers=_auth()).json()
    assert len(page["data"]) == 2

    cid2 = _create(client)["id"]
    client.post("/v1/responses", headers=_auth(), json={
        "model": "muse-chat", "input": "do not store", "conversation": cid2,
        "store": False})
    page = client.get(f"/v1/conversations/{cid2}/items", headers=_auth()).json()
    assert len(page["data"]) == 2


def test_input_tokens_counts_conversation_history(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    cid = _create(client, items=[{"role": "user", "content": "hello"}])["id"]
    counted = client.post("/v1/responses/input_tokens", headers=_auth(), json={
        "model": "muse-chat", "input": "again", "conversation": cid})
    assert counted.status_code == 200
    assert counted.json()["input_tokens"] > 0


def test_responses_conversation_not_found(tmp_path):
    client = _stub_client(tmp_path, StubDriver("ok"))
    response = client.post("/v1/responses", headers=_auth(), json={
        "model": "muse-chat", "input": "x", "conversation": "conv_missing"})
    assert response.status_code == 404
    assert response.json()["error"]["param"] == "conversation"
