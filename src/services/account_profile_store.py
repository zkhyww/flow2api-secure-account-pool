"""Contained storage boundary for per-account persistent browser profiles."""

from pathlib import Path
import re
import shutil
import uuid


_PROFILE_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_CLEANUP_MARKER_PREFIX = ".cleanup-key-"


class AccountProfileStore:
    """Resolve opaque account profile keys below one fixed local root."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve(strict=False)

    def create_key(self) -> str:
        self._drain_pending_cleanup()
        return uuid.uuid4().hex

    def _drain_pending_cleanup(self) -> None:
        if not self.root.is_dir():
            return
        for marker in self.root.iterdir():
            marker_name = marker.name
            if not marker_name.startswith(_CLEANUP_MARKER_PREFIX):
                continue
            key = marker_name[len(_CLEANUP_MARKER_PREFIX):]
            if not _PROFILE_KEY_RE.fullmatch(key):
                continue
            target = self.root / key
            try:
                if target.is_symlink():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                marker.unlink(missing_ok=True)
            except Exception:
                continue

    def _validate_key(self, key: str) -> str:
        normalized = str(key or "")
        if not _PROFILE_KEY_RE.fullmatch(normalized):
            raise ValueError("invalid account profile key")
        return normalized

    def resolve(self, key: str, *, create: bool = False) -> Path:
        normalized = self._validate_key(key)
        root = self.root
        candidate = root / normalized

        if candidate.is_symlink():
            raise ValueError("account profile path is not contained")

        resolved = candidate.resolve(strict=False)
        if resolved.parent != root:
            raise ValueError("account profile path is not contained")

        if create:
            root.mkdir(parents=True, exist_ok=True)
            if candidate.is_symlink():
                raise ValueError("account profile path is not contained")
            candidate.mkdir(exist_ok=True)
            resolved = candidate.resolve(strict=True)
            if resolved.parent != root or candidate.is_symlink():
                raise ValueError("account profile path is not contained")

        if candidate.exists() and not candidate.is_dir():
            raise ValueError("account profile path is not a directory")

        return resolved

    def exists(self, key: str) -> bool:
        path = self.resolve(key)
        return path.is_dir()

    def clone_to_new_key(self, source_key: str) -> str:
        source = self.resolve(source_key)
        if not source.is_dir():
            raise ValueError("account profile source is missing")
        for child in source.rglob("*"):
            if child.is_symlink():
                raise ValueError("account profile tree contains a symlink")

        candidate_key = self.create_key()
        candidate = self.resolve(candidate_key)
        try:
            shutil.copytree(source, candidate)
            self.resolve(candidate_key)
        except Exception:
            try:
                self.remove(candidate_key)
            except Exception:
                pass
            raise
        return candidate_key

    def remove(self, key: str) -> None:
        normalized = self._validate_key(key)
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / f"{_CLEANUP_MARKER_PREFIX}{normalized}"
        marker.touch(exist_ok=True)
        self._drain_pending_cleanup()
