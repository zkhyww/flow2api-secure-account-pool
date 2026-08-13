"""Count-only privacy gate for a prepared clean delivery tree.

The scanner never prints matched values. Known runtime/credential paths are
rejected by path name before file contents are opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Dict


_IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "env",
    "node_modules",
    "venv",
}
_FORBIDDEN_DIRECTORY_NAMES = {
    "browser_data",
    "browser_data_rt",
    "data",
    "tmp",
}
_FORBIDDEN_EXACT_PATHS = {
    ".env",
    ".env.local",
    "config/setting.toml",
    "config/setting_warp.toml",
    "logs.txt",
}
_FORBIDDEN_SUFFIXES = {
    ".db",
    ".har",
    ".log",
    ".sqlite",
    ".sqlite3",
}
_TEXT_SUFFIXES = {
    ".bat",
    ".cfg",
    ".cmd",
    ".conf",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".pyw",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_TEXT_FILENAMES = {
    ".dockerignore",
    ".gitignore",
    "Dockerfile",
}
_SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"sk-[0-9A-Za-z_-]{20,}"),
    re.compile(r"ghp_[0-9A-Za-z]{20,}"),
    re.compile(r"github_pat_[0-9A-Za-z_]{20,}"),
    re.compile(r"Bearer\s+(?!<your_api_key>)[0-9A-Za-z._-]{8,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_PUBLIC_SECRET_ALLOWLIST = {
    "src/services/flow_client.py": {
        "a6d65898ecdb9c54e13467be96a82330b2639db112af9a2c0c6a94f36c62494c",
    },
}
_TEST_SYNTHETIC_SECRET_MARKERS = (
    "fixture",
    "synthetic",
    "placeholder",
    "test-",
)


def _relative_key(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix().casefold()


def _is_text_candidate(path: Path) -> bool:
    return path.name in _TEXT_FILENAMES or path.suffix.casefold() in _TEXT_SUFFIXES


def _is_allowed_secret_match(relative_key: str, matched_value: str) -> bool:
    digest = hashlib.sha256(matched_value.encode("utf-8")).hexdigest()
    if digest in _PUBLIC_SECRET_ALLOWLIST.get(relative_key, set()):
        return True
    if relative_key.startswith("tests/"):
        lowered = matched_value.casefold()
        return any(marker in lowered for marker in _TEST_SYNTHETIC_SECRET_MARKERS)
    return False


def scan_delivery_tree(root: Path) -> Dict[str, int]:
    """Return count-only findings for a prepared delivery directory."""

    root = Path(root).resolve()
    result = {
        "files_scanned": 0,
        "forbidden_path_count": 0,
        "secret_pattern_count": 0,
    }
    if not root.is_dir():
        raise ValueError("delivery_root_not_directory")

    for current_root, directory_names, file_names in os.walk(root):
        current = Path(current_root)
        kept_directories = []
        for directory_name in directory_names:
            lowered = directory_name.casefold()
            if lowered in _IGNORED_DIRECTORY_NAMES:
                continue
            if lowered in _FORBIDDEN_DIRECTORY_NAMES:
                result["forbidden_path_count"] += 1
                continue
            kept_directories.append(directory_name)
        directory_names[:] = kept_directories

        for file_name in file_names:
            path = current / file_name
            relative_key = _relative_key(path, root)
            if (
                relative_key in _FORBIDDEN_EXACT_PATHS
                or path.suffix.casefold() in _FORBIDDEN_SUFFIXES
            ):
                result["forbidden_path_count"] += 1
                continue
            if not _is_text_candidate(path):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                result["forbidden_path_count"] += 1
                continue
            result["files_scanned"] += 1
            for pattern in _SECRET_PATTERNS:
                for match in pattern.finditer(text):
                    if _is_allowed_secret_match(relative_key, match.group(0)):
                        continue
                    result["secret_pattern_count"] += 1

    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan a prepared Flow2API delivery tree without printing secret values."
    )
    parser.add_argument("root", type=Path, help="Prepared clean delivery directory")
    args = parser.parse_args(argv)
    result = scan_delivery_tree(args.root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["forbidden_path_count"] or result["secret_pattern_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
