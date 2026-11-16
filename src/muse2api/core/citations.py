"""Citation-marker normalization for muse.ai replies.

Muse's model cites web sources inline as ``【{url_id}†L{line}】`` (browser
pages), ``【post-{post_id}】`` (social posts) — the web UI renders these as
source chips. A pure OpenAI-style API never emits them; the structured
citations already travel separately (``message_done.resources`` →
``annotations``). ``citation_markers`` selects what the gateway does with the
inline markers:

* ``native``   — pass through untouched (matches the muse.ai web transcript);
* ``footnote`` — each distinct marker becomes ``[1]``, ``[2]``, … in order of
  first appearance (readable footnotes; the URLs stay in annotations);
* ``strip``    — remove the markers entirely.

``MarkerFilter`` works on streamed deltas: a marker can arrive split across
many ``text_append`` chunks, so a potential ``【…`` prefix is held back until
it resolves into a full marker or proves too long to be one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OPEN = "【"
CLOSE = "】"
# Anything longer than this cannot be a citation marker; release it verbatim.
MAX_MARKER = 80
# A marker is a citation when its payload matches one of these shapes.
CITATION_SHAPES = ("†", "post-")


@dataclass
class MarkerFilter:
    """Streaming normalizer for one reply (one driver turn)."""

    mode: str = "native"
    _pending: str = ""
    _numbers: dict[str, int] = field(default_factory=dict)

    def feed(self, text: str) -> str:
        """Consume one delta and return the text safe to emit now."""
        if self.mode == "native" or not text:
            return text
        buffer = self._pending + text
        self._pending = ""
        out: list[str] = []
        while buffer:
            start = buffer.find(OPEN)
            if start < 0:
                out.append(self._emit_plain(buffer))
                buffer = ""
                break
            if start > 0:
                out.append(self._emit_plain(buffer[:start]))
                buffer = buffer[start:]
            end = buffer.find(CLOSE)
            if end < 0:
                if len(buffer) > MAX_MARKER:
                    # Not a marker after all; keep the bracket and move on.
                    out.append(self._emit_plain(buffer[:MAX_MARKER]))
                    buffer = buffer[MAX_MARKER:]
                    continue
                self._pending = buffer
                break
            payload = buffer[len(OPEN):end]
            buffer = buffer[end + len(CLOSE):]
            if len(payload) <= MAX_MARKER and any(shape in payload for shape in CITATION_SHAPES):
                out.append(self._replace(payload))
            else:
                out.append(self._emit_plain(OPEN + payload + CLOSE))
        return "".join(out)

    def flush(self) -> str:
        """Return whatever was held back (call once after the last delta)."""
        text, self._pending = self._pending, ""
        return self._emit_plain(text) if text else ""

    # ------------------------------------------------------------ helpers
    def _emit_plain(self, text: str) -> str:
        return text

    def _replace(self, payload: str) -> str:
        if self.mode == "strip":
            return ""
        key = payload.split("†", 1)[0].strip()
        if key not in self._numbers:
            self._numbers[key] = len(self._numbers) + 1
        return f"[{self._numbers[key]}]"


def normalize(text: str, mode: str) -> str:
    """One-shot normalization for a complete reply."""
    if mode == "native" or not text:
        return text
    filt = MarkerFilter(mode=mode)
    return filt.feed(text) + filt.flush()
