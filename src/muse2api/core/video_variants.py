"""Local ffmpeg rendering for Sora-shaped video variants.

muse.ai produces one MP4 per turn; OpenAI's Sora API also exposes a thumbnail
and a spritesheet. Both are derived locally from the stored MP4 and cached next
to the source media file.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..errors import FeatureNotImplemented, InvalidRequest

VARIANTS = frozenset({"thumbnail", "spritesheet"})

_THUMBNAIL_ARGS = [
    "-v", "error", "-ss", "0", "-i", "{src}", "-frames:v", "1",
    "-f", "image2pipe", "-vcodec", "png", "pipe:1",
]
_SPRITESHEET_ARGS = [
    "-v", "error", "-i", "{src}",
    "-vf",
    ("fps=1,scale=320:-1:force_original_aspect_ratio=decrease,"
     "tile=5x5:padding=4:margin=4:color=black"),
    "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
]


def render_variant(video: Path, variant: str) -> tuple[bytes, str]:
    """Return ``(png_bytes, mime)`` for a thumbnail or spritesheet."""
    if variant not in VARIANTS:
        raise InvalidRequest(f"variant '{variant}' is not supported",
                             param="variant", code="unsupported_variant")
    if shutil.which("ffmpeg") is None:
        raise FeatureNotImplemented(
            "ffmpeg is required for video thumbnail/spritesheet variants")
    args = (_THUMBNAIL_ARGS if variant == "thumbnail"
            else _SPRITESHEET_ARGS)
    command = ["ffmpeg", *(arg.format(src=video) for arg in args)]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=45,
                              check=False)
    except subprocess.TimeoutExpired:
        raise InvalidRequest("video variant generation timed out",
                             param="variant", code="unsupported_variant") from None
    if proc.returncode != 0 or not proc.stdout:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise InvalidRequest(
            f"could not render video {variant}: {message[:200]}",
            param="variant", code="unsupported_variant")
    return proc.stdout, "image/png"
