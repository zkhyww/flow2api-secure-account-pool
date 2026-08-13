"""Double-click launcher for the local Flow2API checkout."""

from __future__ import annotations

import socket
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable


HEALTH_URL = "http://127.0.0.1:8000/health"
MANAGE_URL = "http://127.0.0.1:8000/manage"
STARTUP_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 1.0
REPO_ROOT = Path(__file__).resolve().parent


def _health_ready(url: str = HEALTH_URL) -> bool:
    if url != HEALTH_URL:
        return False
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2.0) as response:
            return int(response.status) == 200
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
        return False


def _show_error(message: str) -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showerror("Flow2API", message, parent=root)
    finally:
        root.destroy()


def run_launcher(
    *,
    health_probe: Callable[[str], bool] = _health_ready,
    popen: Callable[..., object] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    open_browser: Callable[[str], object] = webbrowser.open,
    show_error: Callable[[str], None] = _show_error,
    startup_timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
) -> int:
    repo = REPO_ROOT
    python_exe = repo / "venv" / "Scripts" / "python.exe"
    main_py = repo / "main.py"

    if health_probe(HEALTH_URL):
        open_browser(MANAGE_URL)
        return 0

    if not python_exe.is_file():
        show_error("无法启动 Flow2API：未找到仓库虚拟环境。")
        return 1
    if not main_py.is_file():
        show_error("无法启动 Flow2API：未找到 main.py。")
        return 1

    try:
        popen(
            [str(python_exe), str(main_py)],
            cwd=str(repo),
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        show_error("无法启动 Flow2API 本地服务。")
        return 1

    timeout_seconds = max(0.0, float(startup_timeout_seconds))
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if health_probe(HEALTH_URL):
            open_browser(MANAGE_URL)
            return 0
        sleep(POLL_INTERVAL_SECONDS)

    show_error("Flow2API 未能在限定时间内就绪，请检查本地环境。")
    return 1


def main() -> int:
    return run_launcher()


if __name__ == "__main__":
    main()
