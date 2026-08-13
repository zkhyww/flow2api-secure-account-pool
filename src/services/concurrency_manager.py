"""Concurrency manager for token-based rate limiting"""
import asyncio
import math
import time
from typing import Dict, Optional
from ..core.logger import debug_logger


DEFAULT_LEARNED_LIMIT = 3


class ConcurrencyManager:
    """Manages concurrent request limits for each token"""

    def __init__(self, clock=None):
        """Initialize concurrency manager"""
        self._clock = clock or time.monotonic
        # token_id -> max concurrency limit (only stores >0 values)
        self._image_limits: Dict[int, int] = {}
        self._video_limits: Dict[int, int] = {}
        # token_id -> learned safe concurrency when no explicit user limit exists
        self._image_learning_limits: Dict[int, int] = {}
        self._video_learning_limits: Dict[int, int] = {}
        self._image_cooldown_until: Dict[int, float] = {}
        self._video_cooldown_until: Dict[int, float] = {}
        # token_id -> current in-flight requests
        self._image_inflight: Dict[int, int] = {}
        self._video_inflight: Dict[int, int] = {}
        self._lock = asyncio.Lock()  # Protect concurrent access

    async def initialize(self, tokens: list):
        """
        Initialize concurrency counters from token list

        Args:
            tokens: List of Token objects with image_concurrency and video_concurrency fields
        """
        async with self._lock:
            self._image_limits.clear()
            self._video_limits.clear()
            self._image_learning_limits.clear()
            self._video_learning_limits.clear()
            self._image_cooldown_until.clear()
            self._video_cooldown_until.clear()

            # 初始化时重置 in-flight，避免重启后带入脏状态
            self._image_inflight.clear()
            self._video_inflight.clear()

            for token in tokens:
                self._image_inflight[token.id] = 0
                self._video_inflight[token.id] = 0

                if token.image_concurrency and token.image_concurrency > 0:
                    self._image_limits[token.id] = token.image_concurrency
                self._image_learning_limits[token.id] = min(
                    token.image_concurrency if token.image_concurrency and token.image_concurrency > 0 else DEFAULT_LEARNED_LIMIT,
                    DEFAULT_LEARNED_LIMIT,
                )
                if token.video_concurrency and token.video_concurrency > 0:
                    self._video_limits[token.id] = token.video_concurrency
                self._video_learning_limits[token.id] = min(
                    token.video_concurrency if token.video_concurrency and token.video_concurrency > 0 else DEFAULT_LEARNED_LIMIT,
                    DEFAULT_LEARNED_LIMIT,
                )

            debug_logger.log_info(f"Concurrency manager initialized with {len(tokens)} tokens")

    def _validate_media_type(self, media_type: str):
        if media_type not in ("image", "video"):
            raise ValueError(f"unsupported media_type: {media_type}")

    async def can_use_image(self, token_id: int) -> bool:
        """
        Check if token can be used for image generation

        Args:
            token_id: Token ID

        Returns:
            True if token has available image concurrency, False if concurrency is 0
        """
        async with self._lock:
            if self._in_cooldown("image", token_id):
                return False
            limit = self._effective_limit("image", token_id)
            inflight = self._image_inflight.get(token_id, 0)
            if inflight >= limit:
                debug_logger.log_info(
                    f"Token {token_id} image concurrency exhausted (inflight: {inflight}/{limit})"
                )
                return False

            return True

    def _effective_limit(self, media_type: str, token_id: int) -> int:
        if media_type == "video":
            learned = self._video_learning_limits.get(token_id, DEFAULT_LEARNED_LIMIT)
            ceiling = self._video_limits.get(token_id)
        elif media_type == "image":
            learned = self._image_learning_limits.get(token_id, DEFAULT_LEARNED_LIMIT)
            ceiling = self._image_limits.get(token_id)
        else:
            raise ValueError(f"unsupported media_type: {media_type}")
        return min(learned, ceiling) if ceiling is not None else learned

    def _cooldown_until(self, media_type: str, token_id: int) -> float:
        return (self._video_cooldown_until if media_type == "video" else self._image_cooldown_until).get(token_id, 0)

    def _in_cooldown(self, media_type: str, token_id: int) -> bool:
        return self._clock() < self._cooldown_until(media_type, token_id)

    async def can_use_video(self, token_id: int) -> bool:
        async with self._lock:
            if self._in_cooldown("video", token_id):
                return False
            limit = self._effective_limit("video", token_id)
            return self._video_inflight.get(token_id, 0) < limit

    async def acquire_image(self, token_id: int) -> bool:
        """
        Acquire image concurrency slot

        Args:
            token_id: Token ID

        Returns:
            True if acquired, False if not available
        """
        async with self._lock:
            if self._in_cooldown("image", token_id):
                return False
            limit = self._effective_limit("image", token_id)
            inflight = self._image_inflight.get(token_id, 0)

            if inflight >= limit:
                return False

            new_inflight = inflight + 1
            self._image_inflight[token_id] = new_inflight
            debug_logger.log_info(f"Token {token_id} acquired image slot (inflight: {new_inflight}/{limit})")
            return True

    async def wait_acquire_image(self, token_id: int, timeout_seconds: float) -> tuple[bool, int]:
        """等待获取图片硬并发槽位，避免请求在短暂竞争下直接失败。"""
        wait_started = time.monotonic()
        timeout_seconds = max(1.0, float(timeout_seconds or 1.0))
        deadline = wait_started + timeout_seconds

        while True:
            if await self.acquire_image(token_id):
                waited_ms = int((time.monotonic() - wait_started) * 1000)
                return True, waited_ms

            if time.monotonic() >= deadline:
                waited_ms = int((time.monotonic() - wait_started) * 1000)
                return False, waited_ms

            await asyncio.sleep(0.05)

    async def wait_acquire_video(self, token_id: int, timeout_seconds: float) -> tuple[bool, int]:
        """等待获取视频硬并发槽位，避免请求在短暂竞争下直接失败。"""
        wait_started = time.monotonic()
        timeout_seconds = max(1.0, float(timeout_seconds or 1.0))
        deadline = wait_started + timeout_seconds

        while True:
            if await self.acquire_video(token_id):
                waited_ms = int((time.monotonic() - wait_started) * 1000)
                return True, waited_ms

            if time.monotonic() >= deadline:
                waited_ms = int((time.monotonic() - wait_started) * 1000)
                return False, waited_ms

            await asyncio.sleep(0.05)

    async def acquire_video(self, token_id: int) -> bool:
        async with self._lock:
            if self._in_cooldown("video", token_id):
                return False
            limit = self._effective_limit("video", token_id)
            inflight = self._video_inflight.get(token_id, 0)
            if inflight >= limit:
                return False
            self._video_inflight[token_id] = inflight + 1
            return True

    async def release_image(self, token_id: int):
        """
        Release image concurrency slot

        Args:
            token_id: Token ID
        """
        async with self._lock:
            inflight = self._image_inflight.get(token_id, 0)
            if inflight <= 0:
                self._image_inflight[token_id] = 0
                debug_logger.log_warning(f"Token {token_id} release_image called with inflight=0")
                return

            new_inflight = inflight - 1
            self._image_inflight[token_id] = new_inflight
            limit = self._effective_limit("image", token_id)
            debug_logger.log_info(f"Token {token_id} released image slot (inflight: {new_inflight}/{limit})")

    async def release_video(self, token_id: int):
        """
        Release video concurrency slot

        Args:
            token_id: Token ID
        """
        async with self._lock:
            inflight = self._video_inflight.get(token_id, 0)
            if inflight <= 0:
                self._video_inflight[token_id] = 0
                debug_logger.log_warning(f"Token {token_id} release_video called with inflight=0")
                return

            new_inflight = inflight - 1
            self._video_inflight[token_id] = new_inflight
            limit = self._effective_limit("video", token_id)
            debug_logger.log_info(f"Token {token_id} released video slot (inflight: {new_inflight}/{limit})")

    async def record_success(self, token_id: int, media_type: str):
        self._validate_media_type(media_type)
        async with self._lock:
            if media_type == "video":
                value = self._video_learning_limits.get(token_id, DEFAULT_LEARNED_LIMIT) + 1
                ceiling = self._video_limits.get(token_id)
                self._video_learning_limits[token_id] = min(value, ceiling) if ceiling is not None else value
            else:
                value = self._image_learning_limits.get(token_id, DEFAULT_LEARNED_LIMIT) + 1
                ceiling = self._image_limits.get(token_id)
                self._image_learning_limits[token_id] = min(value, ceiling) if ceiling is not None else value

    async def record_rate_limit(self, token_id: int, media_type: str, cooldown_seconds: float = 60):
        self._validate_media_type(media_type)
        async with self._lock:
            reduced = max(1, self._effective_limit(media_type, token_id) // 2)
            if media_type == "video":
                self._video_learning_limits[token_id] = reduced
                self._video_cooldown_until[token_id] = self._clock() + cooldown_seconds
            else:
                self._image_learning_limits[token_id] = reduced
                self._image_cooldown_until[token_id] = self._clock() + cooldown_seconds

    async def get_image_remaining(self, token_id: int) -> Optional[int]:
        """
        Get remaining image concurrency for token

        Args:
            token_id: Token ID

        Returns:
            Remaining count or None if no limit
        """
        async with self._lock:
            if self._in_cooldown("image", token_id):
                return 0
            limit = self._effective_limit("image", token_id)
            inflight = self._image_inflight.get(token_id, 0)
            return max(0, limit - inflight)

    async def get_video_remaining(self, token_id: int) -> Optional[int]:
        """
        Get remaining video concurrency for token

        Args:
            token_id: Token ID

        Returns:
            Remaining count or None if no limit
        """
        async with self._lock:
            if self._in_cooldown("video", token_id):
                return 0
            limit = self._effective_limit("video", token_id)
            inflight = self._video_inflight.get(token_id, 0)
            return max(0, limit - inflight)

    async def get_image_inflight(self, token_id: int) -> int:
        """Get current in-flight image request count for token"""
        async with self._lock:
            return self._image_inflight.get(token_id, 0)

    async def get_video_inflight(self, token_id: int) -> int:
        """Get current in-flight video request count for token"""
        async with self._lock:
            return self._video_inflight.get(token_id, 0)

    async def get_observability_snapshot(self, token_ids: list[int]) -> Dict[int, dict]:
        """Copy public concurrency state for many accounts under one lock."""
        normalized_ids: list[int] = []
        seen: set[int] = set()
        for value in token_ids or []:
            try:
                token_id = int(value)
            except (TypeError, ValueError):
                continue
            if token_id in seen:
                continue
            seen.add(token_id)
            normalized_ids.append(token_id)

        async with self._lock:
            now_value = float(self._clock())
            snapshot: Dict[int, dict] = {}
            for token_id in normalized_ids:
                known = any(
                    token_id in mapping
                    for mapping in (
                        self._image_limits,
                        self._video_limits,
                        self._image_learning_limits,
                        self._video_learning_limits,
                        self._image_inflight,
                        self._video_inflight,
                    )
                )
                image_remaining = max(
                    0.0,
                    float(self._image_cooldown_until.get(token_id, 0) or 0) - now_value,
                )
                video_remaining = max(
                    0.0,
                    float(self._video_cooldown_until.get(token_id, 0) or 0) - now_value,
                )
                snapshot[token_id] = {
                    "image_learned_limit": self._effective_limit("image", token_id) if known else None,
                    "image_inflight": max(0, int(self._image_inflight.get(token_id, 0) or 0)),
                    "image_cooldown_reason": "429_rate_limit" if image_remaining > 0 else None,
                    "image_cooldown_remaining_seconds": int(math.ceil(image_remaining)),
                    "video_learned_limit": self._effective_limit("video", token_id) if known else None,
                    "video_inflight": max(0, int(self._video_inflight.get(token_id, 0) or 0)),
                    "video_cooldown_reason": "429_rate_limit" if video_remaining > 0 else None,
                    "video_cooldown_remaining_seconds": int(math.ceil(video_remaining)),
                }
            return snapshot

    async def reset_token(self, token_id: int, image_concurrency: int = -1, video_concurrency: int = -1):
        """
        Reset concurrency counters for a token

        Args:
            token_id: Token ID
            image_concurrency: New image concurrency limit (-1 for no limit)
            video_concurrency: New video concurrency limit (-1 for no limit)
        """
        async with self._lock:
            if image_concurrency > 0:
                self._image_limits[token_id] = image_concurrency
            else:
                self._image_limits.pop(token_id, None)
            self._image_learning_limits[token_id] = DEFAULT_LEARNED_LIMIT

            if video_concurrency > 0:
                self._video_limits[token_id] = video_concurrency
            else:
                self._video_limits.pop(token_id, None)
            self._video_learning_limits[token_id] = DEFAULT_LEARNED_LIMIT
            self._image_cooldown_until.pop(token_id, None)
            self._video_cooldown_until.pop(token_id, None)

            # 重置时确保存在 in-flight 计数字段
            self._image_inflight.setdefault(token_id, 0)
            self._video_inflight.setdefault(token_id, 0)

            debug_logger.log_info(f"Token {token_id} concurrency reset (image: {image_concurrency}, video: {video_concurrency})")

    async def remove_token(self, token_id: int):
        """Remove all concurrency state for a deleted token."""
        async with self._lock:
            self._image_limits.pop(token_id, None)
            self._video_limits.pop(token_id, None)
            self._image_learning_limits.pop(token_id, None)
            self._video_learning_limits.pop(token_id, None)
            self._image_cooldown_until.pop(token_id, None)
            self._video_cooldown_until.pop(token_id, None)
            self._image_inflight.pop(token_id, None)
            self._video_inflight.pop(token_id, None)
            debug_logger.log_info(f"Token {token_id} concurrency state removed")
