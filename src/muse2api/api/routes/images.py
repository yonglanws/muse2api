from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import time
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from ...core.image_codec import format_from_mime, transcode_image
from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...drivers.base import ImageRequest, InputImage, MediaResult
from ...errors import InvalidRequest, NotFound
from ...services.container import Services
from ...services.ownership import visible
from ...services.tasks import Task
from ..deps import get_services, public_base, require_api_key
from ..schemas import ImageGenerationRequest

router = APIRouter(tags=["images"], dependencies=[Depends(require_api_key)])

log = logging.getLogger(__name__)

# Reference images attached to one muse.ai message.
MAX_REFERENCE_IMAGES = 4

# Field names clients and relays use for reference images. Only ``image`` is
# OpenAI-standard; the rest are seen in the wild (new-api exposes ``images``,
# Gemini-style clients use image_url / inline_data objects).
REFERENCE_FIELDS = (
    "image", "images", "image_url", "image_urls", "reference_image",
    "reference_images", "ref_image", "init_image", "input_image",
    "input_images",
)

_REF_OBJECT_KEYS = ("url", "image_url", "image_urls", "data", "b64_json",
                    "base64", "file_data", "image", "images", "content")


def _as_ref_strings(value: Any) -> list[str]:
    """Flatten a reference value into URL/data-URL/base64 strings.

    Accepts strings, lists and the object shapes relays emit, e.g.
    ``{"url": ...}``, ``{"image_url": {"url": ...}}`` or ``{"data": ...}``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        for key in _REF_OBJECT_KEYS:
            if key in value:
                return _as_ref_strings(value[key])
        raise InvalidRequest(
            "unsupported image reference object; expected url/image_url/data")
    if isinstance(value, (list, tuple)):
        refs: list[str] = []
        for item in value:
            refs.extend(_as_ref_strings(item))
        return refs
    raise InvalidRequest("unsupported image reference value")


def collect_reference_refs(body: ImageGenerationRequest) -> list[str]:
    """Every reference image on the request, from any supported field name."""
    extras = getattr(body, "model_extra", None) or {}
    refs: list[str] = []
    for name in REFERENCE_FIELDS:
        value = getattr(body, name, None)
        if value is None:
            value = extras.get(name)
        refs.extend(ref for ref in _as_ref_strings(value) if ref)
    suspicious = sorted(
        key for key in extras
        if "image" in key.lower() and key not in REFERENCE_FIELDS)
    if suspicious:
        log.warning("image request carried unrecognised field(s) %s; supported "
                    "reference fields: %s", suspicious,
                    ", ".join(REFERENCE_FIELDS))
    return refs

# Asking muse.ai for a "transparent background" makes it reply with text only, so
# ask for an easy-to-cut plain background and remove it locally instead.
_CUTOUT_HINT = "Isolated subject on a plain, uncluttered studio background."


def _image_item(r: MediaResult, fmt: str, svc: Services, base: str) -> dict:
    item: dict = {"revised_prompt": r.revised_prompt}
    if fmt == "b64_json":
        item["b64_json"] = base64.b64encode(r.data).decode()
    else:
        name = svc.media.save(r.data, r.mime, prefix="img")
        item["url"] = f"{base}/v1/media/{name}"
    return item


def _task_view(task: Task) -> dict:
    return {
        "id": task.id,
        "object": "image.task",
        "status": task.status.value,
        "progress": task.progress,
        "created_at": int(task.created_at),
        "model": task.request.get("model"),
        "result": task.result,
        "error": task.error,
    }


async def _generate(body: ImageGenerationRequest, refs: list[InputImage],
                    request: Request, svc: Services) -> dict | StreamingResponse:
    request.state.model = resolve_model(body.model, "image").id  # actual model
    if len(refs) > MAX_REFERENCE_IMAGES:
        raise InvalidRequest(f"at most {MAX_REFERENCE_IMAGES} reference images are supported")
    if body.background == "transparent" and body.output_format == "jpeg":
        raise InvalidRequest(
            "transparent backgrounds require output_format 'png' or 'webp'",
            param="output_format")
    if body.stream and body.async_:
        raise InvalidRequest(
            "stream and async cannot be combined; pick one", param="stream")
    if body.async_:
        return _submit(body, refs, request, svc)
    if body.stream:
        return await _stream(body, refs, request, svc)
    return await _run(body, refs, public_base(request), svc)


async def _stream(body: ImageGenerationRequest, refs: list[InputImage],
                  request: Request, svc: Services) -> StreamingResponse:
    """OpenAI image streaming (gpt-image-1 shape): one
    ``image_generation.completed`` event per image carrying ``b64_json``.

    muse.ai paints the whole image in one agent reply, so there are no partial
    frames; keepalive comments cover the (often 10-60 s) wait.  new-api and
    other OpenAI-compatible relays send ``stream: true`` here, so answering
    with SSE is the compatible behaviour (a 400 used to break those channels).
    """
    base = public_base(request)
    inner = body.model_copy(update={"response_format": "b64_json"})
    work = asyncio.create_task(_run(inner, refs, base, svc))

    async def events():
        try:
            while True:
                try:
                    response = await asyncio.wait_for(
                        asyncio.shield(work), timeout=10.0)
                    break
                except TimeoutError:
                    yield ": keepalive\n\n"
            fmt = response.get("output_format") or "png"
            mime = f"image/{'jpeg' if fmt == 'jpeg' else fmt}"
            for item in response.get("data") or []:
                event = {
                    "type": "image_generation.completed",
                    "b64_json": item.get("b64_json"),
                    "created_at": response.get("created") or int(time.time()),
                    "output_format": fmt,
                }
                if body.response_format == "url" and item.get("b64_json"):
                    data = base64.b64decode(item["b64_json"])
                    name = svc.media.save(data, mime, prefix="img")
                    event["url"] = f"{base}/v1/media/{name}"
                for key in ("size", "quality", "background"):
                    if response.get(key) is not None:
                        event[key] = response[key]
                yield (f"event: image_generation.completed\n"
                       f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
        except Exception as exc:  # noqa: BLE001 - headers are already sent
            payload = {"type": "error",
                       "error": {"message": str(exc), "type": "server_error"}}
            yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            if not work.done():
                work.cancel()

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _submit(body: ImageGenerationRequest, refs: list[InputImage], request: Request,
            svc: Services) -> dict:
    """Run the generation as a background task; results are stored as media URLs."""
    spec = resolve_model(body.model, "image")
    body = body.model_copy(update={"response_format": "url"})  # keep base64 out of tasks.json
    base = public_base(request)

    async def runner(progress) -> dict:
        return await _run(body, refs, base, svc)

    meta = {"model": spec.id, "prompt": body.prompt, "size": body.size, "n": body.n,
            "references": len(refs),
            "key_id": getattr(request.state, "key_id", None)}
    task = svc.tasks.submit("image", meta, runner)
    request.state.task_id = task.id
    return _task_view(task)


async def _run(body: ImageGenerationRequest, refs: list[InputImage], base: str,
               svc: Services) -> dict:
    spec = resolve_model(body.model, "image")
    transparent = body.background == "transparent"
    prompt = f"{body.prompt.rstrip('. ')}. {_CUTOUT_HINT}" if transparent else body.prompt
    req = ImageRequest(prompt=prompt, model=spec.id, size=body.size, n=body.n,
                       reference_images=refs, timeout=svc.settings.image_timeout)
    results = await svc.gateway.generate_image(req)
    if transparent:
        # After the gateway call, so the account is not held during matting.
        results = [replace(r, data=await svc.matting.remove(r.data), mime="image/png")
                   for r in results]
    converted: list[MediaResult] = []
    for result in results:
        source = format_from_mime(result.mime)
        target = body.output_format
        recompress = (body.output_compression is not None
                      and (target or source) in ("jpeg", "webp"))
        if target and (target != source or recompress):
            data, mime = transcode_image(
                result.data, target,
                body.output_compression if target in ("jpeg", "webp") else None)
            result = replace(result, data=data, mime=mime)
        elif not target and recompress and source:
            data, mime = transcode_image(
                result.data, source, body.output_compression)
            result = replace(result, data=data, mime=mime)
        converted.append(result)
    results = converted
    output_format = None
    if results:
        output_format = format_from_mime(results[0].mime)
    response = {"created": int(time.time()),
                "data": [_image_item(r, body.response_format, svc, base) for r in results],
                "background": "transparent" if transparent else "opaque"}
    if output_format:
        response["output_format"] = output_format
    if body.size:
        response["size"] = body.size
    if body.quality:
        response["quality"] = body.quality
    if body.safety_identifier or body.user:
        response["safety_identifier"] = body.safety_identifier or body.user
    return response


@router.post("/v1/images/generations", response_model=None)
async def generate_images(body: ImageGenerationRequest, request: Request,
                          svc: Services = Depends(get_services)
                          ) -> dict | StreamingResponse:
    loaded = [InputImage(*(await load_image_ref(ref)))
              for ref in collect_reference_refs(body)]
    return await _generate(body, loaded, request, svc)


async def _multipart_request(request: Request, *,
                             default_prompt: str | None = None
                             ) -> tuple[ImageGenerationRequest, list[InputImage]]:
    """Parse the shared ``image``/``image[]`` + fields multipart shape."""
    form = await request.form()
    if form.get("mask") is not None:
        raise InvalidRequest("mask is not supported; muse.ai cannot inpaint a region")
    files = [f for key in ("image", "image[]") for f in form.getlist(key)
             if isinstance(f, UploadFile)]
    if not files:
        raise InvalidRequest("at least one image file is required (field 'image' or 'image[]')")
    fields = {k: v for k, v in form.items() if isinstance(v, str) and k not in ("image", "image[]")}
    if default_prompt and not fields.get("prompt"):
        fields["prompt"] = default_prompt
    try:
        body = ImageGenerationRequest(**fields)
    except ValidationError as exc:
        raise InvalidRequest(exc.errors()[0].get("msg", "invalid request")) from exc
    refs = []
    for f in files:
        mime = f.content_type or ""
        if not mime.startswith("image/"):
            mime = mimetypes.guess_type(f.filename or "")[0] or "image/png"
        refs.append(InputImage(data=await f.read(), mime=mime))
    # Multipart forms may mix uploaded files with URL/data-URL fields.
    for ref in collect_reference_refs(body):
        refs.append(InputImage(*(await load_image_ref(ref))))
    return body, refs


@router.post("/v1/images/edits", response_model=None)
async def edit_images(request: Request,
                      svc: Services = Depends(get_services)
                      ) -> dict | StreamingResponse:
    """OpenAI-style multipart edit: ``image`` / ``image[]`` files plus a prompt.

    muse.ai has no inpainting, so the images act as references for a new image
    and ``mask`` is rejected rather than silently ignored.
    """
    body, refs = await _multipart_request(request)
    return await _generate(body, refs, request, svc)


@router.post("/v1/images/variations", response_model=None)
async def create_variation(request: Request,
                           svc: Services = Depends(get_services)
                           ) -> dict | StreamingResponse:
    """OpenAI image variations, expressed as a reference-image generation."""
    body, refs = await _multipart_request(request, default_prompt=(
        "Create a new variation of the uploaded image, preserving its subject "
        "and style while changing the details."))
    return await _generate(body, refs, request, svc)


@router.get("/v1/images/generations/{task_id}")
async def get_image_task(task_id: str, request: Request,
                         svc: Services = Depends(get_services)) -> dict:
    task = svc.tasks.get(task_id)
    if (task is None or task.kind != "image"
            or not visible(task.request.get("key_id"),
                           getattr(request.state, "key_id", None))):
        raise NotFound(f"image task '{task_id}' not found")
    request.state.model = task.request.get("model")
    return _task_view(task)
