"""Yingce compatibility API."""

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from ..core.auth import verify_api_key_flexible
from ..core.model_resolver import resolve_model_name
from ..core.public_model_catalog import build_public_model_catalog
from ..services.compat_video_tasks import (
    CompatVideoTask,
    CompatVideoTaskRegistry,
    VideoTaskCapacityError,
    VideoTaskIdempotencyConflict,
)
from ..services.generation_handler import MODEL_CONFIG, GenerationHandler


router = APIRouter()
generation_handler: Optional[GenerationHandler] = None
video_tasks = CompatVideoTaskRegistry()
_background_video_tasks: set[asyncio.Task] = set()
_background_video_tasks_by_id: Dict[str, asyncio.Task] = {}
MAX_UPLOAD_BYTES = max(1, int(os.getenv("FLOW2API_YINGCE_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))))
MAX_INLINE_MEDIA_BYTES = max(1, int(os.getenv("FLOW2API_YINGCE_MAX_INLINE_MEDIA_BYTES", str(64 * 1024 * 1024))))
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
MAX_MULTIPART_REQUEST_BYTES = max(
    1,
    int(
        os.getenv(
            "FLOW2API_YINGCE_MAX_MULTIPART_REQUEST_BYTES",
            str(max(MAX_UPLOAD_BYTES, 64 * 1024 * 1024) + _MULTIPART_OVERHEAD_BYTES),
        )
    ),
)
_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\((.*?)\)", re.DOTALL)
VIDEO_HTML_RE = re.compile(r"<video[^>]+src=['\"](.*?)['\"]", re.IGNORECASE)
DATA_IMAGE_RE = re.compile(r"^data:image/[^;]+;base64,(?P<data>.+)$", re.DOTALL)
DATA_VIDEO_RE = re.compile(r"^data:video/[^;]+;base64,(?P<data>.+)$", re.DOTALL)
PUBLIC_GENERATION_ERROR_CODES = {
    "recaptcha",
    "model_access_denied",
    "membership_tier",
    "quota_exhausted",
    "content_policy",
    "authentication",
    "rate_limited",
    "upstream_5xx",
    "upstream_error",
    "submission_uncertain",
    "media_empty",
    "generation_failed",
}


class MediaTooLargeError(ValueError):
    pass


class VideoMediaMaterializationError(ValueError):
    def __init__(self, error_class: str):
        super().__init__(error_class)
        self.error_class = error_class


class ImageGenerationRequest(BaseModel):
    model: str
    prompt: str
    n: int = 1
    size: Optional[str] = None
    quality: Optional[str] = None


def set_generation_handler(handler: GenerationHandler) -> None:
    global generation_handler
    generation_handler = handler


def _ensure_generation_handler() -> GenerationHandler:
    if generation_handler is None:
        raise HTTPException(status_code=500, detail="Generation handler not initialized")
    return generation_handler


def _cancel_background_video_task(task_id: str) -> None:
    task = _background_video_tasks_by_id.get(task_id)
    if task is None or task.done():
        return
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if task.get_loop() is current_loop:
        task.cancel()


def _bind_video_task_expiry_hook() -> None:
    video_tasks.set_active_expiry_hook(_cancel_background_video_task)


def _stable_error(code: str, status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error" if status_code < 500 else "server_error",
                "code": code,
            }
        },
    )


class YingceMultipartBodyLimitMiddleware:
    _LIMITED_PATHS = frozenset({"/v1/images/edits", "/v1/videos"})

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or str(scope.get("method") or "").upper() != "POST"
            or scope.get("path") not in self._LIMITED_PATHS
        ):
            await self.app(scope, receive, send)
            return

        limit = max(1, int(MAX_MULTIPART_REQUEST_BYTES))
        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except (TypeError, ValueError):
                declared_size = -1
            if declared_size > limit:
                response = _stable_error(
                    "media_too_large",
                    413,
                    "Multipart request body is too large",
                )
                await response(scope, receive, send)
                return

        buffered_messages = []
        received_bytes = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                buffered_messages.append(message)
                break
            body = message.get("body", b"") or b""
            received_bytes += len(body)
            if received_bytes > limit:
                response = _stable_error(
                    "media_too_large",
                    413,
                    "Multipart request body is too large",
                )
                await response(scope, receive, send)
                return
            buffered_messages.append(message)
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(buffered_messages):
                message = buffered_messages[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)


def _public_generation_error_code(value: Any) -> str:
    code = str(value or "").strip()
    return code if code in PUBLIC_GENERATION_ERROR_CODES else "generation_failed"


def _handler_error_status(error: Dict[str, Any]) -> int:
    try:
        status_code = int(error.get("status_code") or 502)
    except (TypeError, ValueError):
        return 502
    return status_code if 400 <= status_code <= 599 else 502


def _decode_base64_limited(encoded: str, *, limit: int) -> bytes:
    value = str(encoded or "")
    max_encoded = ((max(1, int(limit)) + 2) // 3) * 4 + 4
    if len(value) > max_encoded:
        raise MediaTooLargeError("media_too_large")
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError("invalid_base64_media") from exc
    if len(decoded) > max(1, int(limit)):
        raise MediaTooLargeError("media_too_large")
    return decoded


async def _read_upload_limited(upload: UploadFile, *, limit: int) -> bytes:
    max_bytes = max(1, int(limit))
    data = bytearray()
    while True:
        chunk = await upload.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise MediaTooLargeError("media_too_large")
    return bytes(data)


def _resolve_image_model(model: str, size: Optional[str], quality: Optional[str], images=None) -> str:
    request_like = SimpleNamespace(
        __pydantic_extra__={"size": size, "quality": quality}
    )
    resolved = resolve_model_name(
        model,
        request=request_like,
        model_config=MODEL_CONFIG,
        images=images,
    )
    model_entry = MODEL_CONFIG.get(resolved)
    if not model_entry or model_entry.get("type") != "image":
        raise ValueError("unsupported_image_model")
    return resolved


def _parse_handler_payload(chunk: str) -> Dict[str, Any]:
    payload = json.loads(chunk)
    return payload if isinstance(payload, dict) else {}


def _extract_completion_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "")


async def _image_result_from_content(
    content: str,
    handler: GenerationHandler,
) -> Optional[Dict[str, str]]:
    match = IMAGE_MARKDOWN_RE.search(content)
    if not match:
        return None
    uri = match.group(1).strip()
    data_match = DATA_IMAGE_RE.match(uri)
    if data_match:
        encoded = data_match.group("data")
        try:
            _decode_base64_limited(encoded, limit=MAX_INLINE_MEDIA_BYTES)
        except MediaTooLargeError:
            raise
        except Exception:
            return None
        return {"b64_json": encoded}

    filename = _local_cache_filename(uri, handler)
    if filename is None and (uri.startswith("http://") or uri.startswith("https://")):
        if handler.file_cache.is_safe_remote_media_passthrough_url(uri):
            return {"url": uri}
        return None
    file_path = _safe_cache_path(filename or "", handler)
    if file_path is None:
        return None
    try:
        encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    except Exception:
        return None
    return {"b64_json": encoded}


def _video_aspect_ratio(size: Optional[str]) -> Optional[str]:
    value = str(size or "").strip().lower()
    if not value:
        return None
    if value in {"16:9", "landscape"}:
        return "16:9"
    if value in {"9:16", "portrait"}:
        return "9:16"
    match = re.fullmatch(r"(\d{2,5})\s*[xX]\s*(\d{2,5})", value)
    if not match:
        raise ValueError("invalid_video_size")
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError("invalid_video_size")
    return "9:16" if height > width else "16:9"


def _resolve_video_model(
    model: str,
    size: Optional[str],
    seconds: Optional[int],
) -> tuple[str, int]:
    requested = str(model or "").strip()
    entry = None
    matched_mapping = None
    for candidate in build_public_model_catalog(MODEL_CONFIG):
        if candidate.get("model_type") != "video":
            continue
        mappings = candidate.get("compatibility_map", [])
        matched_mapping = next(
            (item for item in mappings if item.get("model_id") == requested),
            None,
        )
        if requested in {candidate.get("id"), candidate.get("capability_id")} or matched_mapping:
            entry = candidate
            break
    if entry is None:
        raise ValueError("unsupported_video_model")

    aspect_ratio = _video_aspect_ratio(size)
    if aspect_ratio is None:
        if matched_mapping and requested != entry.get("id"):
            aspect_ratio = str(matched_mapping["parameters"]["aspect_ratio"])
        else:
            aspect_ratio = str(entry["default_parameters"]["aspect_ratio"])

    if seconds is None:
        if matched_mapping and requested != entry.get("id"):
            duration = int(matched_mapping["parameters"]["duration_seconds"])
        else:
            duration = int(entry["default_parameters"]["duration_seconds"])
    else:
        duration = int(seconds)

    selected = next(
        (
            item
            for item in entry.get("compatibility_map", [])
            if str(item["parameters"].get("aspect_ratio")) == aspect_ratio
            and int(item["parameters"].get("duration_seconds") or 0) == duration
            and item.get("validation_status") != "hidden"
        ),
        None,
    )
    if selected is None:
        raise ValueError("unsupported_video_parameters")
    return str(selected["model_id"]), duration


def _apply_video_resolution(resolved_model: str, resolution_name: Optional[str]) -> str:
    value = str(resolution_name or "").strip().lower()
    if not value:
        return resolved_model
    if value not in {"4k", "1080p"}:
        raise ValueError("unsupported_video_resolution")

    candidates = [f"{resolved_model}_{value}"]
    if resolved_model.endswith("_landscape_8s"):
        base = resolved_model[: -len("_landscape_8s")]
        candidates.append(f"{base}_{value}")
    elif resolved_model.endswith("_portrait_8s"):
        base = resolved_model[: -len("_portrait_8s")]
        candidates.append(f"{base}_portrait_{value}")

    for candidate in candidates:
        config = MODEL_CONFIG.get(candidate)
        if config and config.get("type") == "video" and config.get("upsample"):
            return candidate
    raise ValueError("unsupported_video_resolution")


def _video_request_fingerprint(
    *,
    model: str,
    resolved_model: str,
    prompt: str,
    seconds: int,
    size: Optional[str],
    resolution_name: Optional[str],
    preset: Optional[str],
    reference_images: list[bytes],
) -> str:
    payload = {
        "model": model,
        "resolved_model": resolved_model,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "seconds": seconds,
        "size": size or "",
        "resolution_name": resolution_name or "",
        "preset": preset or "",
        "reference_sha256": [hashlib.sha256(item).hexdigest() for item in reference_images],
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _video_task_payload(task: CompatVideoTask, request: Request) -> Dict[str, Any]:
    content_url = None
    if task.status == "completed":
        content_url = f"/v1/videos/{task.id}/content"
    error = None
    if task.status == "failed":
        error = {
            "code": task.error_code or "generation_failed",
            "message": task.error_message or "Video generation failed",
        }
    return {
        "id": task.id,
        "object": "video",
        "model": task.model,
        "status": task.status,
        "progress": task.progress,
        "created_at": task.created_at,
        "completed_at": task.completed_at,
        "expires_at": task.expires_at,
        "size": task.size,
        "seconds": task.seconds,
        "error": error,
        "url": content_url,
    }


def _safe_cache_path(filename: str, handler: GenerationHandler) -> Optional[Path]:
    candidate_name = str(filename or "")
    if not candidate_name or Path(candidate_name).name != candidate_name:
        return None
    if "/" in candidate_name or "\\" in candidate_name or candidate_name in {".", ".."}:
        return None
    cache_dir = Path(handler.file_cache.cache_dir).resolve()
    candidate = (cache_dir / candidate_name).resolve()
    if candidate.parent != cache_dir or not candidate.is_file():
        return None
    return candidate


def _local_cache_filename(uri: str, handler: GenerationHandler) -> Optional[str]:
    path = urlparse(uri).path
    marker = "/tmp/"
    if marker not in path:
        return None
    filename = path.rsplit(marker, 1)[1]
    return filename if _safe_cache_path(filename, handler) is not None else None


def _classify_video_media_download_error(exc: Exception) -> str:
    code = str(exc)
    if code == "remote_media_proxy_unsupported":
        return "media_proxy_unsupported"
    if code == "remote_media_proxy_unavailable":
        return "media_proxy_unavailable"
    if code == "remote_media_proxy_connect_failed":
        return "media_proxy_connect_failed"
    if code == "remote_media_target_rejected":
        return "media_target_rejected"
    if code == "remote_media_dns_failed":
        return "media_dns_failed"
    if code == "remote_media_dns_non_public_rejected":
        return "media_dns_non_public_rejected"
    if code == "remote_media_dns_no_public_address":
        return "media_dns_no_public_address"
    if code == "remote_media_pinned_dns_failed":
        return "media_pinned_dns_failed"
    if code == "remote_media_connect_failed":
        return "media_connect_failed"
    if code in {"remote_media_tls_failed", "remote_media_proxy_tls_failed"}:
        return "media_tls_failed"
    if code == "remote_media_empty_download":
        return "media_empty_download"
    if code in {
        "remote_media_http_failed",
        "remote_media_redirect_limit",
        "remote_media_redirect_invalid",
    }:
        return "media_http_failed"
    if code == "remote_media_download_failed":
        return "media_download_failed"
    if code == "remote_media_too_large":
        return "media_too_large"
    return "media_download_failed"


def _video_media_error_message(error_class: str) -> str:
    if error_class == "media_marker_missing":
        return "Video generation returned no media marker"
    if error_class == "media_proxy_unsupported":
        return "Video media proxy is unsupported"
    if error_class == "media_proxy_unavailable":
        return "Video media proxy is unavailable"
    if error_class == "media_proxy_connect_failed":
        return "Video media proxy connection failed"
    if error_class == "media_target_rejected":
        return "Video media target is not permitted"
    if error_class == "media_dns_failed":
        return "Video media DNS lookup failed"
    if error_class == "media_dns_non_public_rejected":
        return "Video media DNS target is not public"
    if error_class == "media_dns_no_public_address":
        return "Video media DNS returned no public address"
    if error_class == "media_pinned_dns_failed":
        return "Video media pinned resolution failed"
    if error_class == "media_connect_failed":
        return "Video media connection failed"
    if error_class == "media_tls_failed":
        return "Video media TLS verification failed"
    if error_class == "media_http_failed":
        return "Video media HTTP request failed"
    if error_class == "media_empty_download":
        return "Video media download was empty"
    if error_class == "media_download_failed":
        return "Video media download failed"
    if error_class == "media_cache_missing":
        return "Video media cache is unavailable"
    if error_class == "media_decode_failed":
        return "Video media decoding failed"
    if error_class == "media_too_large":
        return "Video media is too large"
    return "Video media is unavailable"


async def _materialize_video_content(
    content: str,
    handler: GenerationHandler,
) -> str:
    match = VIDEO_HTML_RE.search(content)
    if not match:
        raise VideoMediaMaterializationError("media_marker_missing")
    uri = match.group(1).strip()
    local_filename = _local_cache_filename(uri, handler)
    if local_filename is not None:
        return local_filename
    data_match = DATA_VIDEO_RE.match(uri)
    if data_match:
        encoded = data_match.group("data")
        try:
            _decode_base64_limited(encoded, limit=MAX_INLINE_MEDIA_BYTES)
        except MediaTooLargeError as exc:
            raise VideoMediaMaterializationError("media_too_large") from exc
        except Exception as exc:
            raise VideoMediaMaterializationError("media_decode_failed") from exc
        try:
            filename = await handler.file_cache.cache_base64_video(encoded)
        except Exception as exc:
            raise VideoMediaMaterializationError("media_cache_missing") from exc
        if _safe_cache_path(filename, handler) is None:
            raise VideoMediaMaterializationError("media_cache_missing")
        return filename
    if uri.startswith("http://") or uri.startswith("https://"):
        try:
            filename = await handler.file_cache.download_and_cache(
                uri,
                "video",
                log_source_url=False,
                require_direct_connection=True,
            )
        except Exception as exc:
            raise VideoMediaMaterializationError(
                _classify_video_media_download_error(exc)
            ) from exc
        if _safe_cache_path(filename, handler) is None:
            raise VideoMediaMaterializationError("media_cache_missing")
        return filename
    raise VideoMediaMaterializationError("media_marker_missing")


def _classify_video_generation_exception(
    exc: Exception,
    handler: GenerationHandler,
) -> str:
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return "submission_uncertain"
    status_code = int(getattr(exc, "status_code", 0) or 0)
    error_code = str(getattr(exc, "error_code", "") or "").strip()
    if not status_code and not error_code:
        return "generation_failed"
    return _public_generation_error_code(
        handler.classify_failure(
            stage="submit",
            status_code=status_code,
            error_code=error_code,
            has_media=False,
        )
    )


async def _run_video_task(
    task_id: str,
    *,
    resolved_model: str,
    prompt: str,
    images: Optional[list[bytes]],
    base_url_override: str,
) -> None:
    handler = _ensure_generation_handler()
    await video_tasks.update(task_id, status="in_progress", progress=10)
    payload: Dict[str, Any] = {}
    try:
        async for chunk in handler.handle_generation(
            model=resolved_model,
            prompt=prompt,
            images=images or None,
            stream=False,
            base_url_override=base_url_override,
        ):
            payload = _parse_handler_payload(chunk)
    except Exception as exc:
        await video_tasks.update(
            task_id,
            status="failed",
            progress=100,
            error_code=_classify_video_generation_exception(exc, handler),
            error_message="Video generation failed",
        )
        return

    if isinstance(payload.get("error"), dict):
        await video_tasks.update(
            task_id,
            status="failed",
            progress=100,
            error_code=_public_generation_error_code(payload["error"].get("code")),
            error_message="Video generation failed",
        )
        return
    try:
        filename = await _materialize_video_content(
            _extract_completion_content(payload), handler
        )
    except VideoMediaMaterializationError as exc:
        await video_tasks.update(
            task_id,
            status="failed",
            progress=100,
            error_code=exc.error_class,
            error_message=_video_media_error_message(exc.error_class),
        )
        return
    current = await video_tasks.get(task_id)
    if current is None or current.status not in {"queued", "in_progress"}:
        return
    await video_tasks.update(
        task_id,
        status="completed",
        progress=100,
        filename=filename,
    )


async def _run_video_task_guarded(task_id: str, **kwargs) -> None:
    try:
        await _run_video_task(task_id, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception:
        try:
            await video_tasks.update(
                task_id,
                status="failed",
                progress=100,
                error_code="generation_failed",
                error_message="Video generation failed",
            )
        except Exception:
            pass
    finally:
        current_task = asyncio.current_task()
        if current_task is not None:
            _background_video_tasks.discard(current_task)
            if _background_video_tasks_by_id.get(task_id) is current_task:
                _background_video_tasks_by_id.pop(task_id, None)


def _consume_background_video_task(
    task: asyncio.Task,
    task_id: Optional[str] = None,
) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    finally:
        _background_video_tasks.discard(task)
        if task_id is not None and _background_video_tasks_by_id.get(task_id) is task:
            _background_video_tasks_by_id.pop(task_id, None)


async def shutdown_background_video_tasks(
    timeout: float = 5,
    *,
    timeout_seconds: Optional[float] = None,
) -> None:
    wait_timeout = max(0.0, float(timeout_seconds if timeout_seconds is not None else timeout))
    current_loop = asyncio.get_running_loop()
    current_loop_tasks: list[asyncio.Task] = []

    for task in tuple(_background_video_tasks):
        if task.get_loop() is not current_loop:
            _background_video_tasks.discard(task)
            for task_id, mapped_task in tuple(_background_video_tasks_by_id.items()):
                if mapped_task is task:
                    _background_video_tasks_by_id.pop(task_id, None)
            continue
        if task.done():
            _consume_background_video_task(task)
            continue
        task.cancel()
        current_loop_tasks.append(task)

    if current_loop_tasks:
        done, pending = await asyncio.wait(current_loop_tasks, timeout=wait_timeout)
        for task in done:
            _consume_background_video_task(task)
        for task in pending:
            _background_video_tasks.discard(task)

    _background_video_tasks.clear()
    _background_video_tasks_by_id.clear()


@router.post("/v1/images/generations")
async def create_image_generation(
    body: ImageGenerationRequest,
    request: Request,
    _: str = Depends(verify_api_key_flexible),
):
    if body.n != 1:
        return _stable_error("n_not_supported", 400, "Only n=1 is supported")
    if not body.prompt.strip():
        return _stable_error("invalid_prompt", 400, "Prompt cannot be empty")
    try:
        resolved_model = _resolve_image_model(
            body.model.strip(), body.size, body.quality
        )
    except ValueError:
        return _stable_error("unsupported_model", 400, "Unsupported image model")

    handler = _ensure_generation_handler()
    payload: Dict[str, Any] = {}
    try:
        async for chunk in handler.handle_generation(
            model=resolved_model,
            prompt=body.prompt,
            images=None,
            stream=False,
            base_url_override=str(request.base_url).rstrip("/"),
        ):
            payload = _parse_handler_payload(chunk)
    except Exception:
        return _stable_error("generation_failed", 502, "Image generation failed")

    error = payload.get("error")
    if isinstance(error, dict):
        code = _public_generation_error_code(error.get("code"))
        status_code = _handler_error_status(error)
        return _stable_error(code, status_code, "Image generation failed")

    try:
        result = await _image_result_from_content(
            _extract_completion_content(payload), handler
        )
    except MediaTooLargeError:
        return _stable_error("media_too_large", 502, "Generated image is too large")
    if result is None:
        return _stable_error("media_empty", 502, "Image generation returned no media")
    return {"created": int(time.time()), "data": [result]}


@router.post("/v1/images/edits")
async def create_image_edit(
    request: Request,
    model: str = Form(...),
    prompt: str = Form(...),
    image: UploadFile = File(...),
    mask: Optional[UploadFile] = File(None),
    size: Optional[str] = Form(None),
    quality: Optional[str] = Form(None),
    _: str = Depends(verify_api_key_flexible),
):
    if mask is not None:
        return _stable_error("mask_not_supported", 400, "Mask input is not supported")
    if not prompt.strip():
        return _stable_error("invalid_prompt", 400, "Prompt cannot be empty")
    if not str(image.content_type or "").lower().startswith("image/"):
        return _stable_error("invalid_image", 400, "Reference image must be an image")
    try:
        reference_bytes = await _read_upload_limited(image, limit=MAX_UPLOAD_BYTES)
    except MediaTooLargeError:
        return _stable_error("media_too_large", 413, "Reference image is too large")
    if not reference_bytes:
        return _stable_error("invalid_image", 400, "Reference image cannot be empty")
    try:
        resolved_model = _resolve_image_model(
            model.strip(), size, quality, images=[reference_bytes]
        )
    except ValueError:
        return _stable_error("unsupported_model", 400, "Unsupported image model")

    handler = _ensure_generation_handler()
    payload: Dict[str, Any] = {}
    try:
        async for chunk in handler.handle_generation(
            model=resolved_model,
            prompt=prompt,
            images=[reference_bytes],
            stream=False,
            base_url_override=str(request.base_url).rstrip("/"),
        ):
            payload = _parse_handler_payload(chunk)
    except Exception:
        return _stable_error("generation_failed", 502, "Image edit failed")

    error = payload.get("error")
    if isinstance(error, dict):
        code = _public_generation_error_code(error.get("code"))
        status_code = _handler_error_status(error)
        return _stable_error(code, status_code, "Image edit failed")
    try:
        result = await _image_result_from_content(
            _extract_completion_content(payload), handler
        )
    except MediaTooLargeError:
        return _stable_error("media_too_large", 502, "Generated image is too large")
    if result is None:
        return _stable_error("media_empty", 502, "Image edit returned no media")
    return {"created": int(time.time()), "data": [result]}


@router.post("/v1/videos")
async def create_video(
    request: Request,
    model: str = Form(...),
    prompt: str = Form(...),
    seconds: Optional[int] = Form(None),
    size: Optional[str] = Form(None),
    resolution_name: Optional[str] = Form(None),
    preset: Optional[str] = Form(None),
    input_reference: Optional[list[UploadFile]] = File(None, alias="input_reference[]"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    _: str = Depends(verify_api_key_flexible),
):
    _bind_video_task_expiry_hook()
    if not prompt.strip():
        return _stable_error("invalid_prompt", 400, "Prompt cannot be empty")
    try:
        resolved_model, duration = _resolve_video_model(model, size, seconds)
    except (TypeError, ValueError):
        return _stable_error("unsupported_model", 400, "Unsupported video request")

    try:
        resolved_model = _apply_video_resolution(resolved_model, resolution_name)
    except ValueError:
        return _stable_error("unsupported_resolution", 400, "Unsupported video resolution")

    reference_images = []
    for upload in input_reference or []:
        if not str(upload.content_type or "").lower().startswith("image/"):
            return _stable_error("invalid_reference", 400, "Reference must be an image")
        try:
            reference_bytes = await _read_upload_limited(upload, limit=MAX_UPLOAD_BYTES)
        except MediaTooLargeError:
            return _stable_error("media_too_large", 413, "Reference image is too large")
        if not reference_bytes:
            return _stable_error("invalid_reference", 400, "Reference image cannot be empty")
        reference_images.append(reference_bytes)
    if reference_images and not MODEL_CONFIG[resolved_model].get("supports_images", False):
        return _stable_error("reference_not_supported", 400, "Selected video model does not support references")

    reused = False
    try:
        normalized_idempotency = str(idempotency_key or "").strip()
        if normalized_idempotency:
            idempotency_digest = hashlib.sha256(
                normalized_idempotency.encode("utf-8")
            ).hexdigest()
            request_fingerprint = _video_request_fingerprint(
                model=model.strip(),
                resolved_model=resolved_model,
                prompt=prompt,
                seconds=duration,
                size=size,
                resolution_name=resolution_name,
                preset=preset,
                reference_images=reference_images,
            )
            task, reused = await video_tasks.create_idempotent(
                model=model.strip(),
                size=size,
                seconds=duration,
                idempotency_digest=idempotency_digest,
                request_fingerprint=request_fingerprint,
            )
        else:
            task = await video_tasks.create(
                model=model.strip(),
                size=size,
                seconds=duration,
            )
    except VideoTaskIdempotencyConflict:
        return _stable_error("idempotency_conflict", 409, "Idempotency-Key conflicts with an existing request")
    except VideoTaskCapacityError:
        return _stable_error("video_task_capacity", 503, "Video task capacity reached")

    if reused:
        return _video_task_payload(task, request)

    background_task = asyncio.create_task(
        _run_video_task_guarded(
            task.id,
            resolved_model=resolved_model,
            prompt=prompt,
            images=reference_images or None,
            base_url_override=str(request.base_url).rstrip("/"),
        )
    )
    _background_video_tasks.add(background_task)
    _background_video_tasks_by_id[task.id] = background_task
    background_task.add_done_callback(
        lambda completed_task, task_id=task.id: _consume_background_video_task(
            completed_task,
            task_id,
        )
    )
    return _video_task_payload(task, request)


@router.get("/v1/videos/{task_id}")
async def get_video(
    task_id: str,
    request: Request,
    _: str = Depends(verify_api_key_flexible),
):
    _bind_video_task_expiry_hook()
    task = await video_tasks.get(task_id)
    if task is None:
        return _stable_error("video_not_found", 404, "Video task not found")
    return _video_task_payload(task, request)


@router.get("/v1/videos/{task_id}/content")
async def get_video_content(
    task_id: str,
    _: str = Depends(verify_api_key_flexible),
):
    _bind_video_task_expiry_hook()
    task = await video_tasks.get(task_id)
    if task is None:
        return _stable_error("video_not_found", 404, "Video task not found")
    if task.status == "failed":
        return _stable_error(
            task.error_code or "generation_failed",
            409,
            task.error_message or "Video generation failed",
        )
    if task.status != "completed":
        return _stable_error("video_not_ready", 409, "Video content is not ready")
    handler = _ensure_generation_handler()
    file_path = _safe_cache_path(task.filename or "", handler)
    if file_path is None:
        return _stable_error("video_content_missing", 404, "Video content not found")
    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return FileResponse(str(file_path), media_type=media_type)
