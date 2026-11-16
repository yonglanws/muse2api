"""Token counting for OpenAI-shaped usage envelopes.

muse.ai does not expose per-turn token counts (``message_done`` metadata has
model/timestamps only, and the JS bundle contains no usage counters). The next
best thing is a real BPE tokenizer instead of ``len(text) // 4``: we use the
same ``o200k_base`` vocabulary as current OpenAI text models, which is accurate
for English/code and far closer for CJK (where len/4 underestimates by ~3x).
The encoding is pre-downloaded into the image at build time, so no request
touches the network; if tiktoken or its cache is unavailable the old heuristic
is used as a fallback.
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from typing import Any

log = logging.getLogger(__name__)

_ENCODING = "o200k_base"
_lock = threading.Lock()
_encoder: Any = None
_failed = False


def _get_encoder():
    global _encoder, _failed
    if _encoder is not None or _failed:
        return _encoder
    with _lock:
        if _encoder is not None or _failed:
            return _encoder
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding(_ENCODING)
        except Exception as exc:  # noqa: BLE001 - counting must never fail a reply
            _failed = True
            log.info("tiktoken %s unavailable (%s); using the len/4 fallback",
                     _ENCODING, exc)
    return _encoder


def count_tokens(text: str) -> int:
    if not text:
        return 0
    encoder = _get_encoder()
    if encoder is not None:
        try:
            return max(1, len(encoder.encode(text, disallowed_special=())))
        except Exception:  # noqa: BLE001
            pass
    return max(1, len(text) // 4)


@lru_cache(maxsize=1)
def tokenizer_name() -> str:
    return _ENCODING if _get_encoder() is not None else "heuristic(len/4)"


def truncate_tokens(text: str, max_tokens: int) -> str:
    """Cut ``text`` at a token boundary, keeping at most ``max_tokens`` tokens."""
    if not text or max_tokens <= 0:
        return ""
    encoder = _get_encoder()
    if encoder is None:
        return text[: max_tokens * 4]
    try:
        tokens = encoder.encode(text, disallowed_special=())
        if len(tokens) <= max_tokens:
            return text
        return encoder.decode(tokens[:max_tokens])
    except Exception:  # noqa: BLE001
        return text[: max_tokens * 4]
