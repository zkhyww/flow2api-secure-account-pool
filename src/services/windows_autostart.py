"""Windows Task Scheduler integration for the local Flow2API checkout.

The HTTP layer may only choose enabled/disabled. Task identity, executable,
arguments, working directory, trigger, and retry policy are all server-owned.
"""

from __future__ import annotations

import json
import ntpath
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol, Sequence


TASK_NAME = "Flow2API-Local-Account-Pool"
STATUS_VALUES = {"enabled", "disabled", "error", "unsupported"}
_QUERY_TIMEOUT_SECONDS = 20
_WRITE_TIMEOUT_SECONDS = 30


class ScheduledTaskBackendError(RuntimeError):
    """Raised when the OS scheduled-task boundary cannot complete safely."""


@dataclass(frozen=True)
class TaskSpec:
    task_name: str
    execute: Path
    arguments: str
    working_directory: Path
    start_when_available: bool = True
    restart_count: int = 3


class ScheduledTaskBackend(Protocol):
    def inspect(self, task_name: str) -> Mapping[str, object]: ...

    def register(self, spec: TaskSpec) -> None: ...

    def unregister(self, task_name: str) -> None: ...


def _powershell_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _default_runner(args: Sequence[str], *, timeout: int):
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        list(args),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=creation_flags,
    )


class PowerShellScheduledTaskBackend:
    """Minimal fixed-template adapter around Windows ScheduledTasks cmdlets."""

    def __init__(
        self,
        *,
        runner: Callable[..., object] = _default_runner,
    ) -> None:
        self._runner = runner

    def _invoke(self, script: str, *, timeout: int) -> str:
        try:
            completed = self._runner(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ScheduledTaskBackendError(type(exc).__name__) from exc

        returncode = int(getattr(completed, "returncode", 1))
        if returncode != 0:
            raise ScheduledTaskBackendError(f"exit_{returncode}")
        return str(getattr(completed, "stdout", "") or "")

    @staticmethod
    def _require_fixed_task_name(task_name: str) -> None:
        if task_name != TASK_NAME:
            raise ScheduledTaskBackendError("invalid_task_name")

    def inspect(self, task_name: str) -> Mapping[str, object]:
        self._require_fixed_task_name(task_name)
        task_literal = _powershell_quote(TASK_NAME)
        script = f"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName {task_literal} -ErrorAction SilentlyContinue
if ($null -eq $task) {{
    [pscustomobject]@{{ exists = $false }} | ConvertTo-Json -Compress
    exit 0
}}
$action = @($task.Actions)[0]
$trigger = @($task.Triggers)[0]
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$triggerType = if ($trigger -and $trigger.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger') {{ 'logon' }} else {{ [string]$trigger.CimClass.CimClassName }}
[pscustomobject]@{{
    exists = $true
    enabled = ($task.State -ne 'Disabled')
    execute = [string]$action.Execute
    arguments = [string]$action.Arguments
    working_directory = [string]$action.WorkingDirectory
    trigger_type = $triggerType
    trigger_user = [string]$trigger.UserId
    principal_user = [string]$task.Principal.UserId
    current_user = [string]$currentUser
    start_when_available = [bool]$task.Settings.StartWhenAvailable
    restart_count = [int]$task.Settings.RestartCount
}} | ConvertTo-Json -Compress
""".strip()
        raw = self._invoke(script, timeout=_QUERY_TIMEOUT_SECONDS).strip()
        if not raw:
            raise ScheduledTaskBackendError("empty_status_payload")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScheduledTaskBackendError("invalid_status_payload") from exc
        if not isinstance(payload, dict):
            raise ScheduledTaskBackendError("invalid_status_payload")
        return payload

    def register(self, spec: TaskSpec) -> None:
        self._require_fixed_task_name(spec.task_name)
        execute = _powershell_quote(spec.execute)
        arguments = _powershell_quote(spec.arguments)
        working_directory = _powershell_quote(spec.working_directory)
        task_name = _powershell_quote(TASK_NAME)
        restart_count = int(spec.restart_count)
        if restart_count != 3 or not spec.start_when_available:
            raise ScheduledTaskBackendError("invalid_task_spec")

        script = f"""
$ErrorActionPreference = 'Stop'
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute {execute} -Argument {arguments} -WorkingDirectory {working_directory}
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount {restart_count} -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName {task_name} -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
""".strip()
        self._invoke(script, timeout=_WRITE_TIMEOUT_SECONDS)

    def unregister(self, task_name: str) -> None:
        self._require_fixed_task_name(task_name)
        task_literal = _powershell_quote(TASK_NAME)
        script = f"""
$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName {task_literal} -Confirm:$false
""".strip()
        self._invoke(script, timeout=_WRITE_TIMEOUT_SECONDS)


def _normalized_windows_path(value: object) -> str:
    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    return ntpath.normcase(ntpath.normpath(text))


def _same_windows_identity(observed: object, current_user: object) -> bool:
    observed_text = str(observed or "").strip().casefold()
    current_text = str(current_user or "").strip().casefold()
    if not observed_text or not current_text:
        return False
    if observed_text == current_text:
        return True
    if "\\" not in observed_text and "\\" in current_text:
        current_leaf = current_text.rsplit("\\", 1)[1]
        return bool(current_leaf) and observed_text == current_leaf
    return False


class WindowsAutostartManager:
    def __init__(
        self,
        *,
        repo_root: Optional[Path] = None,
        platform_name: Optional[str] = None,
        backend: Optional[ScheduledTaskBackend] = None,
    ) -> None:
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[2]).resolve()
        self.platform_name = platform_name or os.name
        self.backend = backend or PowerShellScheduledTaskBackend()
        self.spec = TaskSpec(
            task_name=TASK_NAME,
            execute=self.repo_root / "venv" / "Scripts" / "python.exe",
            arguments="main.py",
            working_directory=self.repo_root,
        )

    @staticmethod
    def _status(status: str, reason: str) -> dict:
        if status not in STATUS_VALUES:
            raise ValueError("invalid autostart status")
        return {"status": status, "reason": reason}

    def _unsupported(self) -> dict:
        return self._status("unsupported", "仅 Windows 支持登录后自动启动")

    def _inspect(self) -> Mapping[str, object]:
        return self.backend.inspect(TASK_NAME)

    def _matches_fixed_template(self, snapshot: Mapping[str, object]) -> bool:
        if not bool(snapshot.get("exists")) or not bool(snapshot.get("enabled")):
            return False
        if _normalized_windows_path(snapshot.get("execute")) != _normalized_windows_path(self.spec.execute):
            return False
        if str(snapshot.get("arguments") or "").strip() != self.spec.arguments:
            return False
        if _normalized_windows_path(snapshot.get("working_directory")) != _normalized_windows_path(self.spec.working_directory):
            return False
        if str(snapshot.get("trigger_type") or "").strip().casefold() != "logon":
            return False
        current_user = snapshot.get("current_user")
        if not current_user:
            return False
        if not _same_windows_identity(snapshot.get("trigger_user"), current_user):
            return False
        if not _same_windows_identity(snapshot.get("principal_user"), current_user):
            return False
        if not bool(snapshot.get("start_when_available")):
            return False
        try:
            restart_count = int(snapshot.get("restart_count") or 0)
        except (TypeError, ValueError):
            return False
        return restart_count == self.spec.restart_count

    def _render_snapshot(self, snapshot: Mapping[str, object]) -> dict:
        if self._matches_fixed_template(snapshot):
            return self._status("enabled", "Windows 计划任务已启用并符合当前仓库固定模板")
        if not bool(snapshot.get("exists")):
            return self._status("disabled", "Windows 计划任务未启用")
        return self._status("disabled", "Windows 计划任务存在，但未启用或需要按当前仓库固定模板修复")

    def get_status(self) -> dict:
        if self.platform_name != "nt":
            return self._unsupported()
        try:
            snapshot = self._inspect()
        except Exception:
            return self._status("error", "读取 Windows 计划任务状态失败")
        return self._render_snapshot(snapshot)

    def set_enabled(self, enabled: bool) -> dict:
        if self.platform_name != "nt":
            return self._unsupported()

        try:
            before_snapshot = self._inspect()
        except Exception:
            return self._status("error", "读取 Windows 计划任务状态失败，未执行变更")

        before = self._render_snapshot(before_snapshot)
        if enabled and before["status"] == "enabled":
            return before
        if not enabled and not bool(before_snapshot.get("exists")):
            return before

        operation_label = "启用" if enabled else "停用"
        try:
            if enabled:
                self.backend.register(self.spec)
            else:
                self.backend.unregister(TASK_NAME)
        except Exception:
            try:
                fresh = self._render_snapshot(self._inspect())
            except Exception:
                fresh = before
            return self._status(
                fresh["status"],
                f"{operation_label}失败；页面已保持当前可确认状态",
            )

        try:
            return self._render_snapshot(self._inspect())
        except Exception:
            return self._status("error", f"{operation_label}后无法确认 Windows 计划任务状态")


_manager: Optional[WindowsAutostartManager] = None


def get_windows_autostart_manager() -> WindowsAutostartManager:
    global _manager
    if _manager is None:
        _manager = WindowsAutostartManager()
    return _manager
