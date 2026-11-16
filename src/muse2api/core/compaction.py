"""Gateway-local conversation compaction payloads.

OpenAI's compaction items carry an opaque ``encrypted_content`` blob. The
gateway cannot reproduce OpenAI's model state, so it stores a self-contained
summary in the same envelope; feeding the item back restores that summary as a
system message. Clients only need to treat the value as opaque.
"""

from __future__ import annotations

import base64
import json


def encode_compaction(summary: str) -> str:
    payload = json.dumps({"v": 1, "summary": summary},
                         ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_compaction(value: str) -> str | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    summary = payload.get("summary")
    return summary if isinstance(summary, str) else None
