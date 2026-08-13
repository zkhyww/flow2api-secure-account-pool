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
    landscape_id: str,
    portrait_id: str,
) -> List[Dict[str, Any]]:
    mappings: List[Dict[str, Any]] = []
    for aspect_ratio, model_id in (
        ("16:9", landscape_id),
        ("9:16", portrait_id),
    ):
        _require_model(model_config, model_id, "video")
        mappings.append(
            {
                "parameters": {
                    "aspect_ratio": aspect_ratio,
                    "duration_seconds": "8",
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
    landscape_id: str,
    portrait_id: str,
    display_name: str,
    description: str,
    supports_images: bool,
    catalog_order: int,
    default_duration: str = "8",
    additional_duration_models: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    _require_model(model_config, landscape_id, "video")
    landscape_config = model_config[landscape_id]
    duration_options = [_option("8", "8 秒", "validated")]
    compatibility_map = _video_mapping(
        model_config,
        landscape_id,
        portrait_id,
    )
    for duration, duration_metadata in (additional_duration_models or {}).items():
        validation_status = str(duration_metadata["validation_status"])
        if validation_status not in _SELECTABLE_STATES:
            raise ValueError(f"Invalid duration state: {validation_status}")
        duration_label = f"{duration} 秒"
        if validation_status == "membership_required":
            duration_label += "（需要高级会员）"
        elif validation_status == "hidden":
            duration_label += "（待真实验证）"
        duration_options.append(
            _option(duration, duration_label, validation_status)
        )
        for aspect_ratio, mapped_model_id in duration_metadata["models"].items():
            _require_model(model_config, mapped_model_id, "video")
            compatibility_map.append(
                {
                    "parameters": {
                        "aspect_ratio": aspect_ratio,
                        "duration_seconds": duration,
                    },
                    "model_id": mapped_model_id,
                    "validation_status": validation_status,
                }
            )

    return {
        "id": landscape_id,
        "capability_id": capability_id,
        "display_name": display_name,
        "description": description,
        "model_type": "video",
        "validation_status": "validated",
        "catalog_order": catalog_order,
        "supports_images": supports_images,
        "min_images": (
            int(landscape_config.get("minImages", 0) or 0)
            if supports_images
            else 0
        ),
        "max_images": (
            int(landscape_config.get("maxImages", 0) or 0)
            if supports_images
            else 0
        ),
        "default_parameters": {
            "aspect_ratio": "16:9",
            "duration_seconds": default_duration,
        },
        "options": {
            "aspect_ratio": [
                _option(value, label) for value, label in _VIDEO_ASPECTS
            ],
            "duration_seconds": duration_options,
        },
        "compatibility_map": compatibility_map,
        "actions": [dict(_EXTEND_ACTION)],
    }


def build_public_model_catalog(
    model_config: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Return the six current public capabilities without removing old aliases."""

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
            landscape_id="omni",
            portrait_id="omni_portrait",
            display_name="Omni Flash",
            description="适合通用的 8 秒或 10 秒文生视频任务；实际速度与权限以账号为准。",
            supports_images=False,
            catalog_order=2,
            default_duration="10",
            additional_duration_models={
                "10": {
                    "validation_status": "validated",
                    "models": {
                        "16:9": "omni_10s",
                        "9:16": "omni_portrait_10s",
                    },
                }
            },
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-lite",
            landscape_id="veo_3_1_t2v_lite_landscape_8s",
            portrait_id="veo_3_1_t2v_lite_portrait_8s",
            display_name="Veo 3.1 Lite",
            description="适合轻量、快速迭代的 8 秒文生视频任务。",
            supports_images=False,
            catalog_order=3,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-fast",
            landscape_id="veo_3_1_t2v_fast_landscape_8s",
            portrait_id="veo_3_1_t2v_fast_portrait_8s",
            display_name="Veo 3.1 Fast",
            description="适合优先快速迭代的 8 秒文生视频任务。",
            supports_images=False,
            catalog_order=4,
        ),
        _video_capability(
            model_config,
            capability_id="veo-3.1-quality",
            landscape_id="veo_3_1_t2v_landscape_8s",
            portrait_id="veo_3_1_t2v_portrait_8s",
            display_name="Veo 3.1 Quality",
            description="适合更重视画面质量的 8 秒文生视频任务。",
            supports_images=False,
            catalog_order=5,
        ),
    ]
    for entry in catalog:
        if entry["validation_status"] not in _SELECTABLE_STATES:
            raise ValueError(f"Invalid capability state: {entry['validation_status']}")
    return catalog
