"""Public capability catalog backed by the existing compatible model identifiers."""

from typing import Any, Dict, List, Mapping, Optional


_SELECTABLE_STATES = {"validated", "membership_required", "hidden"}
_IMAGE_ASPECTS = (
    ("16:9", "16:9", "landscape"),
    ("9:16", "9:16", "portrait"),
    ("1:1", "1:1", "square"),
    ("4:3", "4:3", "four-three"),
    ("3:4", "3:4", "three-four"),
)
_IMAGE_RESOLUTIONS = (
    ("1K", "1K（可用）", ""),
    ("2K", "2K（可用）", "-2k"),
    ("4K", "4K（需要高级会员）", "-4k"),
)
_VIDEO_ASPECTS = (
    ("16:9", "16:9"),
    ("9:16", "9:16"),
)
_EXTEND_ACTION = {
    "id": "extend",
    "label": "续写此视频",
    "description": "成功视频后的续写动作；Veo 3.1 的 8 秒视频使用 Lite 续写入口。",
    "validation_status": "validated",
    "model_map": {
        "16:9": "veo_3_1_extend",
        "9:16": "veo_3_1_extend_portrait",
    },
}


def _option(
    value: str,
    label: str,
    validation_status: Optional[str] = None,
) -> Dict[str, str]:
    option = {"value": value, "label": label}
    if validation_status:
        option["validation_status"] = validation_status
    return option


def _require_model(
    model_config: Mapping[str, Mapping[str, Any]],
    model_id: str,
    model_type: str,
) -> Mapping[str, Any]:
    config = model_config.get(model_id)
    if not config or config.get("type") != model_type:
        raise ValueError(f"Public catalog mapping is not callable: {model_id}")
    return config


def _image_mapping(
    model_config: Mapping[str, Mapping[str, Any]],
    prefix: str,
    capability_id: str,
) -> List[Dict[str, Any]]:
    mappings: List[Dict[str, Any]] = []
    for aspect_value, _label, aspect_suffix in _IMAGE_ASPECTS:
        for resolution_value, _resolution_label, resolution_suffix in _IMAGE_RESOLUTIONS:
            model_id = f"{prefix}-{aspect_suffix}{resolution_suffix}"
            _require_model(model_config, model_id, "image")
            validation_status = (
                "membership_required"
                if resolution_value == "4K"
                else "validated"
            )
            mappings.append(
                {
                    "parameters": {
                        "aspect_ratio": aspect_value,
                        "resolution": resolution_value,
                    },
                    "model_id": model_id,
                    "validation_status": validation_status,
                }
            )
    return mappings


def _video_mapping(
    model_config: Mapping[str, Mapping[str, Any]],
    model_map: Mapping[str, Mapping[str, str]],
) -> List[Dict[str, Any]]:
    mappings: List[Dict[str, Any]] = []
    for duration, aspect_models in model_map.items():
        for aspect_ratio in ("16:9", "9:16"):
            model_id = str(aspect_models[aspect_ratio])
            _require_model(model_config, model_id, "video")
            mappings.append(
                {
                    "parameters": {
                        "aspect_ratio": aspect_ratio,
                        "duration_seconds": str(duration),
                    },
                    "model_id": model_id,
                    "validation_status": "validated",
                }
            )
    return mappings


def _image_capability(
    model_config: Mapping[str, Mapping[str, Any]],
    *,
    capability_id: str,
    model_id: str,
    prefix: str,
    display_name: str,
    description: str,
    validation_status: str,
    catalog_order: int,
) -> Dict[str, Any]:
    _require_model(model_config, model_id, "image")
    return {
        "id": model_id,
        "capability_id": capability_id,
        "display_name": display_name,
        "description": description,
        "model_type": "image",
        "validation_status": validation_status,
        "catalog_order": catalog_order,
        "supports_images": True,
        "min_images": 0,
        "max_images": 5,
        "default_parameters": {
            "aspect_ratio": "16:9",
            "resolution": "1K",
        },
        "options": {
            "aspect_ratio": [
                _option(value, label) for value, label, _suffix in _IMAGE_ASPECTS
            ],
            "resolution": [
                _option(value, label) for value, label, _suffix in _IMAGE_RESOLUTIONS
            ],
        },
        "compatibility_map": _image_mapping(model_config, prefix, capability_id),
        "actions": [],
    }


def _video_capability(
    model_config: Mapping[str, Mapping[str, Any]],
    *,
    capability_id: str,
    primary_model_id: str,
    display_name: str,
    description: str,
    generation_mode: str,
    generation_mode_label: str,
    image_semantics: str,
    min_images: int,
    max_images: int,
    model_map: Mapping[str, Mapping[str, str]],
    usage_guide: str,
    unsupported_notes: List[str],
    catalog_order: int,
    default_duration: str = "8",
) -> Dict[str, Any]:
    _require_model(model_config, primary_model_id, "video")
    if min_images < 0 or max_images < min_images:
        raise ValueError(f"Invalid image bounds for {capability_id}")
    if default_duration not in model_map:
        raise ValueError(f"Invalid default duration for {capability_id}")

    duration_options = [
        _option(str(duration), f"{duration} 秒", "validated")
        for duration in model_map
    ]
    compatibility_map = _video_mapping(model_config, model_map)

    return {
        "id": capability_id,
        "capability_id": capability_id,
        "display_name": display_name,
        "description": description,
        "model_type": "video",
        "validation_status": "validated",
        "catalog_order": catalog_order,
        "generation_mode": generation_mode,
        "generation_modes": [
            {"id": generation_mode, "label": generation_mode_label}
        ],
        "supports_images": max_images > 0,
        "min_images": min_images,
        "max_images": max_images,
        "image_semantics": image_semantics,
        "usage_guide": usage_guide,
        "unsupported_notes": list(unsupported_notes),
        "default_parameters": {
            "aspect_ratio": "16:9",
            "duration_seconds": default_duration,
            "resolution": "native",
        },
        "options": {
            "aspect_ratio": [
                _option(value, label) for value, label in _VIDEO_ASPECTS
            ],
            "duration_seconds": duration_options,
            "resolution": [
                _option(
                    "native",
                    "原生清晰度（影策/巨天请选择 720P）",
                    "validated",
                )
            ],
        },
        "compatibility_map": compatibility_map,
        "actions": [dict(_EXTEND_ACTION)],
    }


def build_public_model_catalog(
    model_config: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Return the canonical public capabilities without removing old aliases."""

    catalog = [
        _image_capability(
            model_config,
            capability_id="nano-banana-2",
            model_id="gemini-3.1-flash-image-landscape",
            prefix="gemini-3.1-flash-image",
            display_name="Nano Banana 2",
            description="适合日常快速出图与参考图编辑；1K/2K 已验证，4K 需要高级会员。",
            validation_status="validated",
            catalog_order=0,
        ),
        _image_capability(
            model_config,
            capability_id="nano-banana-pro",
            model_id="gemini-3.0-pro-image-landscape",
            prefix="gemini-3.0-pro-image",
            display_name="Gemini 3 Pro Image / Nano Banana Pro",
            description="适合更重视细节与质量的图片任务；1K/2K 已验证，4K 需要高级会员。",
            validation_status="validated",
            catalog_order=1,
        ),
        _video_capability(
            model_config,
            capability_id="omni-flash",
            primary_model_id="omni",
            display_name="Omni Flash · 文生视频",
            description="Omni Flash 的 8 秒或 10 秒纯文本视频入口。",
            generation_mode="text_to_video",
            generation_mode_label="文生视频",
            image_semantics="不使用图片",
            min_images=0,
            max_images=0,
            model_map={
                "8": {"16:9": "omni", "9:16": "omni_portrait"},
                "10": {"16:9": "omni_10s", "9:16": "omni_portrait_10s"},
            },
            usage_guide="只输入文字；8/10 秒均可，影策/巨天清晰度请选择 720P（按上游原生输出处理）。",
            unsupported_notes=[
                "此入口不接收图片；Omni 10 秒参考图不开放；1080P/4K/2160P/480P 未在本轮验证并明确拒绝。"
            ],
            catalog_order=2,
            default_duration="10",
        ),
        _video_capability(
            model_config,
            capability_id="omni-flash-references",
            primary_model_id="omni",
            display_name="Omni Flash · 参考图生视频",
            description="Omni Flash 的 8 秒 Reference Images 入口。",
            generation_mode="references_to_video",
            generation_mode_label="参考图生视频",
            image_semantics="1–3 张人物或素材参考图",
            min_images=1,
            max_images=3,
            model_map={
                "8": {"16:9": "omni", "9:16": "omni_portrait"},
            },
            usage_guide="上传 1–3 张参考图并选择 8 秒；影策/巨天清晰度请选择 720P（按上游原生输出处理）。",
            unsupported_notes=[
                "10 秒参考图尚未完成本轮真实验证，因此不开放；1080P/4K/2160P/480P 同样明确拒绝。"
            ],
            catalog_order=3,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-lite",
            primary_model_id="veo_3_1_t2v_lite_landscape_8s",
            display_name="Veo 3.1 Lite · 文生视频",
            description="Veo 3.1 Lite 的 8 秒纯文本视频入口。",
            generation_mode="text_to_video",
            generation_mode_label="文生视频",
            image_semantics="不使用图片",
            min_images=0,
            max_images=0,
            model_map={
                "8": {
                    "16:9": "veo_3_1_t2v_lite_landscape_8s",
                    "9:16": "veo_3_1_t2v_lite_portrait_8s",
                }
            },
            usage_guide="只输入文字并选择 8 秒；如需图片，请改选 Lite 首帧或首尾帧能力。",
            unsupported_notes=[
                "此入口不接收图片；1080P/4K/2160P/480P 未在本轮验证并明确拒绝。"
            ],
            catalog_order=4,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-lite-first-frame",
            primary_model_id="veo_3_1_i2v_lite_landscape_8s",
            display_name="Veo 3.1 Lite · 首帧生视频",
            description="Veo 3.1 Lite 的 8 秒单首帧视频入口。",
            generation_mode="first_frame_to_video",
            generation_mode_label="首帧生视频",
            image_semantics="恰好 1 张首帧图片",
            min_images=1,
            max_images=1,
            model_map={
                "8": {
                    "16:9": "veo_3_1_i2v_lite_landscape_8s",
                    "9:16": "veo_3_1_i2v_lite_portrait_8s",
                }
            },
            usage_guide="上传恰好 1 张首帧并选择 8 秒；两张首尾帧请改选 Lite 首尾帧能力。",
            unsupported_notes=[
                "只接受 1 张首帧；References 不从此入口推断；1080P/4K/2160P/480P 明确拒绝。"
            ],
            catalog_order=5,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-lite-first-last",
            primary_model_id="veo_3_1_interpolation_lite_landscape_8s",
            display_name="Veo 3.1 Lite · 首尾帧生视频",
            description="Veo 3.1 Lite 的 8 秒首尾帧插值入口。",
            generation_mode="first_last_frame_to_video",
            generation_mode_label="首尾帧生视频",
            image_semantics="恰好 2 张图片：第 1 张首帧，第 2 张尾帧",
            min_images=2,
            max_images=2,
            model_map={
                "8": {
                    "16:9": "veo_3_1_interpolation_lite_landscape_8s",
                    "9:16": "veo_3_1_interpolation_lite_portrait_8s",
                }
            },
            usage_guide="按首帧、尾帧顺序上传恰好 2 张图片并选择 8 秒。",
            unsupported_notes=[
                "只接受 2 张首尾帧；References 不从此入口推断；1080P/4K/2160P/480P 明确拒绝。"
            ],
            catalog_order=6,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-fast",
            primary_model_id="veo_3_1_t2v_fast_landscape_8s",
            display_name="Veo 3.1 Fast · 文生视频",
            description="Veo 3.1 Fast 的 8 秒纯文本视频入口。",
            generation_mode="text_to_video",
            generation_mode_label="文生视频",
            image_semantics="不使用图片",
            min_images=0,
            max_images=0,
            model_map={
                "8": {
                    "16:9": "veo_3_1_t2v_fast_landscape_8s",
                    "9:16": "veo_3_1_t2v_fast_portrait_8s",
                }
            },
            usage_guide="只输入文字并选择 8 秒；图片输入请选择对应的 Fast 首帧、首尾帧或 References 能力。",
            unsupported_notes=[
                "此入口不接收图片；1080P/4K/2160P/480P 未在本轮验证并明确拒绝。"
            ],
            catalog_order=7,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-fast-first-frame",
            primary_model_id="veo_3_1_i2v_s_fast_landscape_8s_fl",
            display_name="Veo 3.1 Fast · 首帧生视频",
            description="Veo 3.1 Fast 的 8 秒单首帧视频入口。",
            generation_mode="first_frame_to_video",
            generation_mode_label="首帧生视频",
            image_semantics="恰好 1 张首帧图片",
            min_images=1,
            max_images=1,
            model_map={
                "8": {
                    "16:9": "veo_3_1_i2v_s_fast_landscape_8s_fl",
                    "9:16": "veo_3_1_i2v_s_fast_portrait_8s_fl",
                }
            },
            usage_guide="上传恰好 1 张首帧并选择 8 秒；不要用图片张数去猜 References。",
            unsupported_notes=[
                "只接受 1 张首帧；References 请显式选择 Fast References；1080P/4K/2160P/480P 明确拒绝。"
            ],
            catalog_order=8,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-fast-first-last",
            primary_model_id="veo_3_1_i2v_s_fast_landscape_8s_fl",
            display_name="Veo 3.1 Fast · 首尾帧生视频",
            description="Veo 3.1 Fast 的 8 秒首尾帧视频入口。",
            generation_mode="first_last_frame_to_video",
            generation_mode_label="首尾帧生视频",
            image_semantics="恰好 2 张图片：第 1 张首帧，第 2 张尾帧",
            min_images=2,
            max_images=2,
            model_map={
                "8": {
                    "16:9": "veo_3_1_i2v_s_fast_landscape_8s_fl",
                    "9:16": "veo_3_1_i2v_s_fast_portrait_8s_fl",
                }
            },
            usage_guide="按首帧、尾帧顺序上传恰好 2 张图片并选择 8 秒。",
            unsupported_notes=[
                "只接受 2 张首尾帧；References 请显式选择 Fast References；1080P/4K/2160P/480P 明确拒绝。"
            ],
            catalog_order=9,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-fast-references",
            primary_model_id="veo_3_1_r2v_fast_landscape",
            display_name="Veo 3.1 Fast · References",
            description="Veo 3.1 Fast 的 8 秒 Ingredients/References 多参考图入口。",
            generation_mode="references_to_video",
            generation_mode_label="参考图生视频",
            image_semantics="1–3 张 Ingredients/References 参考图",
            min_images=1,
            max_images=3,
            model_map={
                "8": {
                    "16:9": "veo_3_1_r2v_fast_landscape",
                    "9:16": "veo_3_1_r2v_fast_portrait",
                }
            },
            usage_guide="上传 1–3 张 Ingredients/References 参考图并选择 8 秒。",
            unsupported_notes=[
                "仅 Fast 公开此 References 能力；Quality 不开放 References；1080P/4K/2160P/480P 明确拒绝。"
            ],
            catalog_order=10,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-quality",
            primary_model_id="veo_3_1_t2v_landscape_8s",
            display_name="Veo 3.1 Quality · 文生视频",
            description="Veo 3.1 Quality 的 8 秒纯文本视频入口。",
            generation_mode="text_to_video",
            generation_mode_label="文生视频",
            image_semantics="不使用图片",
            min_images=0,
            max_images=0,
            model_map={
                "8": {
                    "16:9": "veo_3_1_t2v_landscape_8s",
                    "9:16": "veo_3_1_t2v_portrait_8s",
                }
            },
            usage_guide="只输入文字并选择 8 秒；Quality 图片输入请改选首帧或首尾帧能力。",
            unsupported_notes=[
                "Veo Quality 不开放 Ingredients/References；1080P/4K/2160P/480P 未在本轮验证并明确拒绝。"
            ],
            catalog_order=11,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-quality-first-frame",
            primary_model_id="veo_3_1_i2v_s_landscape_8s",
            display_name="Veo 3.1 Quality · 首帧生视频",
            description="Veo 3.1 Quality 的 8 秒单首帧视频入口。",
            generation_mode="first_frame_to_video",
            generation_mode_label="首帧生视频",
            image_semantics="恰好 1 张首帧图片",
            min_images=1,
            max_images=1,
            model_map={
                "8": {
                    "16:9": "veo_3_1_i2v_s_landscape_8s",
                    "9:16": "veo_3_1_i2v_s_portrait_8s",
                }
            },
            usage_guide="上传恰好 1 张首帧并选择 8 秒；Quality 不提供 References 入口。",
            unsupported_notes=[
                "Veo Quality 不开放 Ingredients/References；此入口只接受 1 张首帧；1080P/4K/2160P/480P 明确拒绝。"
            ],
            catalog_order=12,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-quality-first-last",
            primary_model_id="veo_3_1_i2v_s_landscape_8s",
            display_name="Veo 3.1 Quality · 首尾帧生视频",
            description="Veo 3.1 Quality 的 8 秒首尾帧视频入口。",
            generation_mode="first_last_frame_to_video",
            generation_mode_label="首尾帧生视频",
            image_semantics="恰好 2 张图片：第 1 张首帧，第 2 张尾帧",
            min_images=2,
            max_images=2,
            model_map={
                "8": {
                    "16:9": "veo_3_1_i2v_s_landscape_8s",
                    "9:16": "veo_3_1_i2v_s_portrait_8s",
                }
            },
            usage_guide="按首帧、尾帧顺序上传恰好 2 张图片并选择 8 秒；Quality 不提供 References 入口。",
            unsupported_notes=[
                "Veo Quality 不开放 Ingredients/References；此入口只接受 2 张首尾帧；1080P/4K/2160P/480P 明确拒绝。"
            ],
            catalog_order=13,
        ),
    ]
    for entry in catalog:
        if entry["validation_status"] not in _SELECTABLE_STATES:
            raise ValueError(f"Invalid capability state: {entry['validation_status']}")
    return catalog
