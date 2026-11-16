"""Local image transcoding for the Images API.

muse.ai returns the format it painted (usually PNG); OpenAI clients can ask for
``png``/``jpeg``/``webp`` with an optional lossy compression level. Pillow
does the conversion locally so the response bytes are exactly the requested
format instead of a renamed original.
"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

from ..errors import UpstreamRefused

_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def format_from_mime(mime: str) -> str | None:
    normalized = (mime or "").split(";", 1)[0].strip().lower()
    return {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/webp": "webp",
    }.get(normalized)


def transcode_image(data: bytes, output_format: str, compression: int | None = None
                    ) -> tuple[bytes, str]:
    """Encode ``data`` as ``output_format``; returns ``(bytes, mime)``."""
    fmt = output_format.lower()
    if fmt not in _MIME:
        raise ValueError(f"unsupported output format: {output_format}")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            target = image
            if fmt == "jpeg":
                if image.mode in ("RGBA", "LA", "PA", "P"):
                    # JPEG has no alpha: flatten onto white like OpenAI does.
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, (255, 255, 255))
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    target = background
                elif image.mode not in ("RGB", "L"):
                    target = image.convert("RGB")
            elif fmt == "webp" and image.mode not in ("RGB", "RGBA", "L", "LA"):
                target = image.convert("RGBA")
            elif fmt == "png" and image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                target = image.convert("RGBA")
            params = {}
            if fmt in ("jpeg", "webp") and compression is not None:
                params["quality"] = int(compression)
            if fmt == "jpeg":
                params["optimize"] = True
            out = io.BytesIO()
            target.save(out, format=fmt.upper(), **params)
            return out.getvalue(), _MIME[fmt]
    except (UnidentifiedImageError, OSError, ValueError,
            Image.DecompressionBombError) as exc:
        raise UpstreamRefused(
            "muse.ai returned data that is not a decodable image") from exc
