"""0.13.0 surface tests: OpenAI Uploads API and image format transcoding."""

from __future__ import annotations

import base64
import hashlib
import io

from PIL import Image

from test_robustness import StubDriver, _auth, _stub_client
from test_web_chat_surface import _mock_client


def _upload(client, payload: bytes, *, purpose: str = "assistants"):
    return client.post("/v1/uploads", headers=_auth(), json={
        "bytes": len(payload), "filename": "payload.bin",
        "mime_type": "application/octet-stream", "purpose": purpose,
    }).json()


def _part(client, upload_id: str, data: bytes) -> dict:
    return client.post(
        f"/v1/uploads/{upload_id}/parts", headers=_auth(),
        files={"data": ("part.bin", data, "application/octet-stream")},
    ).json()


def test_upload_roundtrip_becomes_a_file(tmp_path):
    client = _stub_client(tmp_path, StubDriver("unused"))
    payload = b"hello multipart upload"
    upload = _upload(client, payload)
    assert upload["object"] == "upload"
    assert upload["status"] == "pending"
    assert upload["expires_at"] > upload["created_at"]

    first = _part(client, upload["id"], payload[:5])
    second = _part(client, upload["id"], payload[5:])
    assert first["object"] == "upload.part"
    assert first["upload_id"] == upload["id"]

    completed = client.post(
        f"/v1/uploads/{upload['id']}/complete", headers=_auth(),
        json={"part_ids": [first["id"], second["id"]],
              "md5": hashlib.md5(payload).hexdigest()},
    )
    assert completed.status_code == 200
    body = completed.json()
    assert body["status"] == "completed"
    file_id = body["file"]["id"]
    assert body["file"]["filename"] == "payload.bin"
    content = client.get(f"/v1/files/{file_id}/content", headers=_auth())
    assert content.content == payload


def test_upload_part_order_and_size_validation(tmp_path):
    client = _stub_client(tmp_path, StubDriver("unused"))
    payload = b"abcdefghij"
    upload = _upload(client, payload)
    first = _part(client, upload["id"], payload[:4])
    second = _part(client, upload["id"], payload[4:])
    # Ordered list controls the assembled bytes.
    reversed_body = client.post(
        f"/v1/uploads/{upload['id']}/complete", headers=_auth(),
        json={"part_ids": [second["id"], first["id"]]}).json()
    reversed_payload = client.get(
        f"/v1/files/{reversed_body['file']['id']}/content", headers=_auth()).content
    assert reversed_payload == payload[4:] + payload[:4]

    short = _upload(client, payload)
    short_part = _part(client, short["id"], payload[:3])
    mismatch = client.post(f"/v1/uploads/{short['id']}/complete",
                           headers=_auth(),
                           json={"part_ids": [short_part["id"]]})
    assert mismatch.status_code == 400
    assert mismatch.json()["error"]["code"] == "upload_size_mismatch"

    bad_md5 = _upload(client, b"x")
    part = _part(client, bad_md5["id"], b"x")
    rejected = client.post(f"/v1/uploads/{bad_md5['id']}/complete",
                           headers=_auth(), json={"part_ids": [part["id"]],
                                                  "md5": "0" * 32})
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "md5_mismatch"


def test_upload_cancel_and_owner_isolation(tmp_path):
    client = _stub_client(tmp_path, StubDriver("unused"))
    other = client.post("/admin/keys", headers=_auth(),
                        json={"name": "upload-other"}).json()["api_key"]
    other_auth = {"Authorization": f"Bearer {other}"}

    upload = _upload(client, b"secret")
    part = _part(client, upload["id"], b"secret")
    assert client.post(f"/v1/uploads/{upload['id']}/cancel",
                       headers=other_auth).status_code == 404
    assert client.post(f"/v1/uploads/{upload['id']}/complete",
                       headers=other_auth,
                       json={"part_ids": [part["id"]]}).status_code == 404

    cancelled = client.post(f"/v1/uploads/{upload['id']}/cancel",
                            headers=_auth()).json()
    assert cancelled["status"] == "cancelled"
    late_part = client.post(
        f"/v1/uploads/{upload['id']}/parts", headers=_auth(),
        files={"data": ("late.bin", b"x", "application/octet-stream")})
    assert late_part.status_code == 400
    assert late_part.json()["error"]["code"] == "upload_not_pending"


def test_upload_validation_and_missing_ids(tmp_path):
    client = _stub_client(tmp_path, StubDriver("unused"))
    bad_purpose = client.post("/v1/uploads", headers=_auth(), json={
        "bytes": 1, "filename": "x", "mime_type": "text/plain",
        "purpose": "bogus"})
    assert bad_purpose.status_code == 400
    assert bad_purpose.json()["error"]["param"] == "purpose"

    zero = client.post("/v1/uploads", headers=_auth(), json={
        "bytes": 0, "filename": "x", "mime_type": "text/plain",
        "purpose": "assistants"})
    assert zero.status_code == 400
    assert zero.json()["error"]["param"] == "bytes"

    assert client.post("/v1/uploads/upload_missing/cancel",
                       headers=_auth()).status_code == 404
    assert client.post("/v1/uploads/upload_missing/parts", headers=_auth(),
                       files={"data": ("x.bin", b"x",
                                       "application/octet-stream")}).status_code == 404


def _image_bytes(fmt: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (16, 16), (200, 30, 40, 128)).save(out, fmt)
    return out.getvalue()


def test_image_output_format_and_compression(tmp_path):
    client = _mock_client(tmp_path)
    body = client.post("/v1/images/generations", headers=_auth(), json={
        "model": "muse-image", "prompt": "paint", "response_format": "b64_json",
        "output_format": "jpeg", "output_compression": 25,
    }).json()
    assert body["output_format"] == "jpeg"
    data = base64.b64decode(body["data"][0]["b64_json"])
    decoded = Image.open(io.BytesIO(data))
    assert decoded.format == "JPEG"
    assert decoded.size == (64, 64)

    webp = client.post("/v1/images/generations", headers=_auth(), json={
        "model": "muse-image", "prompt": "paint", "response_format": "b64_json",
        "output_format": "webp",
    }).json()
    assert webp["output_format"] == "webp"
    assert Image.open(io.BytesIO(base64.b64decode(
        webp["data"][0]["b64_json"]))).format == "WEBP"

    plain = client.post("/v1/images/generations", headers=_auth(), json={
        "model": "muse-image", "prompt": "paint", "response_format": "b64_json",
    }).json()
    assert plain["output_format"] == "png"


def test_image_transparent_jpeg_is_rejected_before_generation(tmp_path):
    client = _mock_client(tmp_path)
    response = client.post("/v1/images/generations", headers=_auth(), json={
        "model": "muse-image", "prompt": "paint",
        "background": "transparent", "output_format": "jpeg",
    })
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "output_format"


def test_image_codec_flattens_alpha_for_jpeg():
    from muse2api.core.image_codec import transcode_image

    data, mime = transcode_image(_image_bytes("PNG"), "jpeg", 80)
    assert mime == "image/jpeg"
    decoded = Image.open(io.BytesIO(data))
    assert decoded.format == "JPEG"
    assert decoded.mode == "RGB"
