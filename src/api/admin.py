"""Admin API routes"""
import asyncio
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any
import secrets
import time
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse
from curl_cffi.requests import AsyncSession
from ..core.auth import AuthManager, verify_api_key_header
from ..core.database import Database
from ..core.config import config, get_yescaptcha_min_score, normalize_yescaptcha_task_type
from ..core.models import Token
from ..core.browser_runtime_status import (
    fail_runtime_prepare,
    finish_runtime_prepare,
    get_runtime_status,
    progress_runtime_prepare,
    start_runtime_prepare,
)
from ..core.monitoring import build_public_health_snapshot
from ..services.token_manager import TokenManager
from ..services.proxy_manager import ProxyManager
from ..services.concurrency_manager import ConcurrencyManager
from ..services.windows_autostart import get_windows_autostart_manager

try:
    import httpx
except ImportError:
    httpx = None

router = APIRouter()

# Dependency injection
token_manager: TokenManager = None
proxy_manager: ProxyManager = None
db: Database = None
concurrency_manager: Optional[ConcurrencyManager] = None
captcha_runtime_prepare_tasks: Dict[str, asyncio.Task] = {}
account_onboarding_service = None
account_profile_store = None

# Store active admin session tokens (in production, use Redis or database)
active_admin_tokens = set()
ADMIN_SESSION_COOKIE_NAME = "admin_session"
SUPPORTED_API_CAPTCHA_METHODS = {"yescaptcha", "capmonster", "ezcaptcha", "capsolver"}


def _mask_token(token: Optional[str]) -> str:
    if not token:
        return ""
    if len(token) <= 24:
        return token
    return f"{token[:18]}...{token[-8:]}"


def _truncate_text(text: Any, limit: int = 240) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit - 3]}..."


def _extract_error_summary(payload: Any) -> str:
    """从响应体里提取用户可读的错误摘要。"""
    if payload is None:
        return ""

    if isinstance(payload, str):
        raw = payload.strip()
        if not raw:
            return ""
        try:
            return _extract_error_summary(json.loads(raw))
        except Exception:
            return _truncate_text(raw)

    if isinstance(payload, dict):
        for key in ("error_summary", "error_message", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_text(value)

        error_value = payload.get("error")
        if isinstance(error_value, dict):
            for key in ("message", "detail", "reason", "code"):
                value = error_value.get(key)
                if isinstance(value, str) and value.strip():
                    return _truncate_text(value)
        elif isinstance(error_value, str) and error_value.strip():
            return _truncate_text(error_value)

        for nested_key in ("response", "data"):
            nested = payload.get(nested_key)
            if isinstance(nested, (dict, list, str)):
                summary = _extract_error_summary(nested)
                if summary:
                    return summary

        return ""

    if isinstance(payload, list):
        for item in payload:
            summary = _extract_error_summary(item)
            if summary:
                return summary
        return ""

    return _truncate_text(payload)


def _guess_client_hints_from_user_agent(user_agent: str) -> Dict[str, str]:
    """根据 UA 补全常见的 sec-ch-* 头。"""
    ua = (user_agent or "").strip()
    if not ua:
        return {}

    headers: Dict[str, str] = {}
    major_match = re.search(r"(?:Chrome|Chromium|Edg|EdgA|EdgiOS)/(\d+)", ua)
    is_mobile = any(token in ua for token in ("Android", "iPhone", "iPad", "Mobile"))
    headers["sec-ch-ua-mobile"] = "?1" if is_mobile else "?0"

    if "Windows" in ua:
        headers["sec-ch-ua-platform"] = '"Windows"'
    elif "Macintosh" in ua or "Mac OS X" in ua:
        headers["sec-ch-ua-platform"] = '"macOS"'
    elif "Android" in ua:
        headers["sec-ch-ua-platform"] = '"Android"'
    elif "iPhone" in ua or "iPad" in ua:
        headers["sec-ch-ua-platform"] = '"iOS"'
    elif "Linux" in ua:
        headers["sec-ch-ua-platform"] = '"Linux"'

    if major_match:
        major = major_match.group(1)
        if "Edg/" in ua:
            headers["sec-ch-ua"] = (
                f'"Not:A-Brand";v="99", "Microsoft Edge";v="{major}", "Chromium";v="{major}"'
            )
        else:
            headers["sec-ch-ua"] = (
                f'"Not:A-Brand";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"'
            )

    return headers


def _validate_browser_proxy_url_local(proxy_url: str) -> tuple[bool, Optional[str]]:
    if not proxy_url:
        return True, None
    normalized = proxy_url.strip()
    if not re.match(r"^(http|https|socks5h?|socks5)://", normalized):
        normalized = f"http://{normalized}"
    if not re.match(r"^(socks5h?|socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$", normalized):
        return False, "代理格式错误"
    return True, None


def _normalize_runtime_method(method: Optional[str]) -> str:
    normalized = (method or "").strip().lower()
    if normalized not in {"browser", "personal"}:
        raise HTTPException(status_code=400, detail="Invalid runtime method")
    return normalized


async def _prepare_captcha_runtime(method: str):
    runtime_method = _normalize_runtime_method(method)
    try:
        if runtime_method == "browser":
            start_runtime_prepare(
                runtime_method,
                "已开始准备有头浏览器打码运行环境，安装进度将自动显示。",
            )
        else:
            start_runtime_prepare(
                runtime_method,
                "已开始准备内置浏览器打码运行环境，安装进度将自动显示。",
            )

        if runtime_method == "browser":
            module = await asyncio.to_thread(importlib.import_module, "src.services.browser_captcha")
            service_cls = getattr(module, "BrowserCaptchaService")
            service = await service_cls.get_instance(db)
            if hasattr(service, "reload_browser_count"):
                await service.reload_browser_count()
            if hasattr(service, "warmup_browser_slots"):
                await service.warmup_browser_slots()
            finish_runtime_prepare(runtime_method, "Chromium 浏览器环境已就绪，可以开始使用有头浏览器打码。")
            return

        module = await asyncio.to_thread(importlib.import_module, "src.services.browser_captcha_personal")
        service_cls = getattr(module, "BrowserCaptchaService")
        service = await service_cls.get_instance(db)
        await service.reload_config()
        finish_runtime_prepare(runtime_method, "内置浏览器环境已就绪，可以开始使用 personal 打码。")
    except HTTPException:
        raise
    except Exception as e:
        fail_runtime_prepare(runtime_method, f"浏览器环境准备失败: {type(e).__name__}: {e}")
    finally:
        captcha_runtime_prepare_tasks.pop(runtime_method, None)


def _schedule_captcha_runtime_prepare(method: str) -> bool:
    runtime_method = _normalize_runtime_method(method)
    task = captcha_runtime_prepare_tasks.get(runtime_method)
    if task and not task.done():
        progress_runtime_prepare(runtime_method, "浏览器环境准备任务仍在进行中，请稍候...")
        return False

    captcha_runtime_prepare_tasks[runtime_method] = asyncio.create_task(
        _prepare_captcha_runtime(runtime_method)
    )
    return True


def _guess_impersonate_from_user_agent(user_agent: str) -> str:
    """从 UA 选择可用的 curl_cffi 浏览器指纹版本。"""
    ua = (user_agent or "").strip()
    major_match = re.search(r"(?:Chrome|Chromium|Edg|EdgA|EdgiOS)/(\d+)", ua)
    if not major_match:
        return "chrome120"

    try:
        major = int(major_match.group(1))
    except Exception:
        return "chrome120"

    if major >= 124:
        return "chrome124"
    if major >= 120:
        return "chrome120"
    return "chrome120"


def _build_proxy_map(proxy_url: str) -> Optional[Dict[str, str]]:
    normalized = (proxy_url or "").strip()
    if not normalized:
        return None
    return {"http": normalized, "https": normalized}


def _normalize_http_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        raise RuntimeError("远程打码服务地址未配置")

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("远程打码服务地址格式错误，必须是 http(s)://host[:port]")

    return normalized


def _get_remote_browser_client_config() -> tuple[str, str, int]:
    base_url = _normalize_http_base_url(config.remote_browser_base_url)
    api_key = (config.remote_browser_api_key or "").strip()
    if not api_key:
        raise RuntimeError("远程打码服务 API Key 未配置")
    timeout = max(5, int(config.remote_browser_timeout or 60))
    return base_url, api_key, timeout


def _build_remote_browser_http_timeout(read_timeout: float) -> Any:
    read_value = max(3.0, float(read_timeout))
    write_value = min(10.0, max(3.0, read_value))
    if httpx is None:
        return read_value
    return httpx.Timeout(
        connect=2.5,
        read=read_value,
        write=write_value,
        pool=2.5,
    )


def _parse_json_response_text(text: str) -> Optional[Any]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


async def _stdlib_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
) -> tuple[int, Optional[Any], str]:
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json")
    request_method = (method or "GET").upper()
    request_data: Optional[bytes] = None

    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
        if request_method != "GET":
            request_data = json.dumps(payload).encode("utf-8")

    def do_request() -> tuple[int, str]:
        request = urllib.request.Request(
            url=url,
            data=request_data,
            headers=req_headers,
            method=request_method,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=max(1.0, float(timeout))) as response:
                status_code = int(getattr(response, "status", 0) or response.getcode() or 0)
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return status_code, body.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            charset = exc.headers.get_content_charset() if exc.headers else None
            return int(getattr(exc, "code", 0) or 0), body.decode(charset or "utf-8", errors="replace")

    try:
        status_code, text = await asyncio.to_thread(do_request)
    except Exception as e:
        raise RuntimeError(f"远程打码服务请求失败: {e}") from e

    return status_code, _parse_json_response_text(text), text


async def _sync_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
) -> tuple[int, Optional[Any], str]:
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json")
    request_method = (method or "GET").upper()
    request_kwargs: Dict[str, Any] = {
        "headers": req_headers,
        "timeout": _build_remote_browser_http_timeout(timeout),
    }

    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
        if request_method != "GET":
            request_kwargs["json"] = payload

    if httpx is None:
        return await _stdlib_json_http_request(
            method=method,
            url=url,
            headers=req_headers,
            payload=payload,
            timeout=timeout,
        )

    try:
        # remote_browser 控制面是服务间 JSON API，使用 httpx 避免 curl_cffi 在当前
        # Windows + impersonate 场景下 POST body 丢失导致 FastAPI 直接判定 body 缺失。
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as session:
            response = await session.request(
                method=request_method,
                url=url,
                **request_kwargs,
            )
    except Exception as e:
        raise RuntimeError(f"远程打码服务请求失败: {e}") from e

    status_code = int(getattr(response, "status_code", 0) or 0)
    text = response.text or ""
    parsed = _parse_json_response_text(text)

    return status_code, parsed, text


async def _resolve_score_test_verify_proxy(
    captcha_method: str,
    browser_proxy_enabled: bool,
    browser_proxy_url: str
) -> tuple[Optional[Dict[str, str]], bool, str, str]:
    """
    选择 score-test 的 verify 请求代理，优先与浏览器打码代理保持一致。
    返回: (proxies, used, source, proxy_url)
    """
    # 浏览器打码模式优先使用 browser_proxy，确保与取 token 出口一致
    if captcha_method in {"browser", "personal"} and browser_proxy_enabled and browser_proxy_url:
        proxy_map = _build_proxy_map(browser_proxy_url)
        if proxy_map:
            return proxy_map, True, "captcha_browser_proxy", browser_proxy_url

    # 退回请求代理配置
    try:
        if proxy_manager:
            proxy_cfg = await proxy_manager.get_proxy_config()
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                proxy_map = _build_proxy_map(proxy_cfg.proxy_url)
                if proxy_map:
                    return proxy_map, True, "request_proxy", proxy_cfg.proxy_url
    except Exception:
        pass

    return None, False, "none", ""


async def _solve_recaptcha_with_api_service(
    method: str,
    website_url: str,
    website_key: str,
    action: str,
    enterprise: bool = False
) -> Optional[str]:
    """使用当前配置的第三方打码服务获取 token。"""
    if method == "yescaptcha":
        client_key = config.yescaptcha_api_key
        base_url = config.yescaptcha_base_url
        task_type = config.yescaptcha_task_type
        min_score = get_yescaptcha_min_score(task_type)
    elif method == "capmonster":
        client_key = config.capmonster_api_key
        base_url = config.capmonster_base_url
        task_type = "RecaptchaV3TaskProxyless"
        min_score = None
    elif method == "ezcaptcha":
        client_key = config.ezcaptcha_api_key
        base_url = config.ezcaptcha_base_url
        task_type = "ReCaptchaV3TaskProxylessS9"
        min_score = None
    elif method == "capsolver":
        client_key = config.capsolver_api_key
        base_url = config.capsolver_base_url
        task_type = "ReCaptchaV3EnterpriseTaskProxyLess" if enterprise else "ReCaptchaV3TaskProxyLess"
        min_score = None
    else:
        raise RuntimeError(f"不支持的打码方式: {method}")

    if not client_key:
        raise RuntimeError(f"{method} API Key 未配置")

    task: Dict[str, Any] = {
        "websiteURL": website_url,
        "websiteKey": website_key,
        "type": task_type,
        "pageAction": action,
    }
    if min_score is not None:
        task["minScore"] = min_score

    if enterprise and method == "capsolver":
        task["isEnterprise"] = True

    create_url = f"{base_url.rstrip('/')}/createTask"
    get_url = f"{base_url.rstrip('/')}/getTaskResult"

    # 获取代理配置
    proxies = None
    try:
        if proxy_manager:
            proxy_cfg = await proxy_manager.get_proxy_config()
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                proxies = {"http": proxy_cfg.proxy_url, "https": proxy_cfg.proxy_url}
    except Exception:
        pass

    async with AsyncSession() as session:
        create_resp = await session.post(
            create_url,
            json={"clientKey": client_key, "task": task},
            impersonate="chrome120",
            timeout=30,
            proxies=proxies
        )
        create_json = create_resp.json()
        task_id = create_json.get("taskId")

        if not task_id:
            error_desc = create_json.get("errorDescription") or create_json.get("errorMessage") or str(create_json)
            raise RuntimeError(f"{method} createTask 失败: {error_desc}")

        for _ in range(40):
            poll_resp = await session.post(
                get_url,
                json={"clientKey": client_key, "taskId": task_id},
                impersonate="chrome120",
                timeout=30,
                proxies=proxies
            )
            poll_json = poll_resp.json()
            if poll_json.get("status") == "ready":
                solution = poll_json.get("solution", {}) or {}
                token = solution.get("gRecaptchaResponse") or solution.get("token")
                if token:
                    return token
                raise RuntimeError(f"{method} 返回结果缺少 token: {poll_json}")

            if poll_json.get("errorId") not in (None, 0):
                error_desc = poll_json.get("errorDescription") or poll_json.get("errorMessage") or str(poll_json)
                raise RuntimeError(f"{method} getTaskResult 失败: {error_desc}")

            await asyncio.sleep(3)

    raise RuntimeError(f"{method} 获取 token 超时")


async def _score_test_with_remote_browser_service(
    website_url: str,
    website_key: str,
    verify_url: str,
    action: str,
    enterprise: bool = False,
) -> Dict[str, Any]:
    """调用远程有头打码服务执行页面内打码+分数校验。"""
    base_url, api_key, timeout = _get_remote_browser_client_config()
    endpoint = f"{base_url}/api/v1/custom-score"
    request_payload = {
        "website_url": website_url,
        "website_key": website_key,
        "verify_url": verify_url,
        "action": action,
        "enterprise": enterprise,
    }

    status_code, response_payload, response_text = await _sync_json_http_request(
        method="POST",
        url=endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        payload=request_payload,
        timeout=timeout,
    )

    if status_code >= 400:
        detail = ""
        if isinstance(response_payload, dict):
            detail = response_payload.get("detail") or response_payload.get("message") or str(response_payload)
        if not detail:
            detail = (response_text or "").strip()
        raise RuntimeError(f"远程打码服务请求失败 (HTTP {status_code}): {detail or '未知错误'}")

    if not isinstance(response_payload, dict):
        raise RuntimeError("远程打码服务返回格式错误")
    return response_payload


def _get_account_profile_store():
    global account_profile_store
    if account_profile_store is None:
        from ..services.account_profile_store import AccountProfileStore

        root = Path(__file__).resolve().parents[2] / "data" / "account_profiles"
        account_profile_store = AccountProfileStore(root)
    return account_profile_store


def set_dependencies(tm: TokenManager, pm: ProxyManager, database: Database, cm: Optional[ConcurrencyManager] = None):
    """Set service instances"""
    global token_manager, proxy_manager, db, concurrency_manager
    token_manager = tm
    proxy_manager = pm
    db = database
    concurrency_manager = cm
    from ..services.extension_pairing import configure_extension_pairing_storage
    configure_extension_pairing_storage(database)


async def _validate_flow_access_token(access_token: str) -> Dict[str, Any]:
    """Require Flow itself to accept a newly exchanged access token."""
    try:
        credits = await token_manager.flow_client.get_credits(access_token)
    except Exception:
        raise ValueError("flow_authorization_invalid") from None
    if not isinstance(credits, dict):
        raise ValueError("flow_authorization_invalid")
    return credits


async def _persist_native_onboarding_result(
    result: Dict[str, Any],
    *,
    account_profile_key: str,
) -> str:
    """Persist one private browser capture through the existing TokenManager."""
    if not isinstance(result, dict):
        raise ValueError("invalid onboarding result")
    session_token = str(result.get("st") or "").strip()
    google_cookies = str(result.get("google_cookies") or "").strip()
    profile_key = str(account_profile_key or "").strip()
    if not session_token or not google_cookies or not profile_key:
        raise ValueError("incomplete onboarding result")

    converted = await token_manager.flow_client.st_to_at(session_token)
    access_token = str(converted.get("access_token") or "").strip()
    user = converted.get("user") if isinstance(converted.get("user"), dict) else {}
    email = str(user.get("email") or "").strip()
    if not access_token or not email:
        raise ValueError("incomplete onboarding identity")
    await _validate_flow_access_token(access_token)

    at_expires = None
    expires = converted.get("expires")
    if expires:
        try:
            at_expires = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            at_expires = None

    existing = await db.get_token_by_email(email)
    if existing is None:
        added = await token_manager.add_token(
            st=session_token,
            remark="Added from native browser login",
            protocol_mode="protocol",
            google_cookies=google_cookies,
            account_profile_key="",
        )
        await db.update_token(added.id, account_profile_key=profile_key)
        return "success"

    clear_validation_cache = getattr(token_manager, "_clear_at_validation_cache", None)
    if callable(clear_validation_cache):
        clear_validation_cache(existing.id)
    await db.update_token(
        existing.id,
        st=session_token,
        at=access_token,
        at_expires=at_expires,
        protocol_mode="protocol",
        google_cookies=google_cookies,
        account_profile_key=profile_key,
        auth_state="ok",
        auth_failure_count=0,
        auth_next_retry_at=None,
        last_auth_error_class="",
    )
    return "updated"


async def _persist_native_reauth_result(
    token_id: int,
    result: Dict[str, Any],
    *,
    account_profile_key: str,
) -> str:
    """Persist an explicit re-login only when the captured identity matches the target account."""
    existing = await db.get_token(token_id)
    if existing is None:
        raise ValueError("token_not_found")
    if not isinstance(result, dict):
        raise ValueError("invalid_reauth_result")

    session_token = str(result.get("st") or "").strip()
    google_cookies = str(result.get("google_cookies") or "").strip()
    profile_key = str(account_profile_key or "").strip()
    if not session_token or not google_cookies or not profile_key:
        raise ValueError("incomplete_reauth_result")

    converted = await token_manager.flow_client.st_to_at(session_token)
    access_token = str(converted.get("access_token") or "").strip()
    user = converted.get("user") if isinstance(converted.get("user"), dict) else {}
    recovered_email = str(user.get("email") or "").strip()
    expected_email = str(getattr(existing, "email", "") or "").strip()
    if not access_token or not recovered_email:
        raise ValueError("incomplete_reauth_identity")
    if recovered_email.casefold() != expected_email.casefold():
        await token_manager._mark_auth_failure(
            token_id,
            "identity_mismatch",
            interactive=True,
        )
        raise ValueError("identity_mismatch")

    try:
        await _validate_flow_access_token(access_token)
    except ValueError:
        clear_validation_cache = getattr(token_manager, "_clear_at_validation_cache", None)
        if callable(clear_validation_cache):
            clear_validation_cache(token_id)
        await token_manager._mark_auth_failure(
            token_id,
            "oauth_callback_missing",
            interactive=False,
        )
        raise

    at_expires = None
    expires = converted.get("expires")
    if expires:
        try:
            at_expires = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            at_expires = None

    clear_validation_cache = getattr(token_manager, "_clear_at_validation_cache", None)
    if callable(clear_validation_cache):
        clear_validation_cache(token_id)
    await db.commit_account_reauth(
        token_id,
        st=session_token,
        at=access_token,
        at_expires=at_expires,
        google_cookies=google_cookies,
        account_profile_key=profile_key,
    )
    return "success"


async def _run_account_reauth(token_id: int) -> Dict[str, Any]:
    """Run one explicit headed re-login from an isolated copy of the target profile."""
    existing = await db.get_token(token_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Account not found")

    profile_store = _get_account_profile_store()
    existing_key = str(getattr(existing, "account_profile_key", "") or "").strip()
    candidate_key = ""

    def cleanup_candidate() -> None:
        if not candidate_key:
            return
        try:
            profile_store.remove(candidate_key)
        except Exception:
            pass

    try:
        if existing_key and profile_store.exists(existing_key):
            candidate_key = profile_store.clone_to_new_key(existing_key)
            candidate_dir = profile_store.resolve(candidate_key)
        else:
            candidate_key = profile_store.create_key()
            candidate_dir = profile_store.resolve(candidate_key, create=True)
    except Exception:
        cleanup_candidate()
        await token_manager._mark_auth_failure(
            token_id,
            "profile_corrupt",
            interactive=True,
        )
        return {"success": False, "auth_status": "需要重新登录"}

    from ..services.browser_captcha_personal import BrowserCaptchaService

    browser = BrowserCaptchaService(
        db,
        force_headed=True,
        persistent_profile_dir=candidate_dir,
    )
    private_result = None
    browser_error_class = ""
    browser_error_interactive = False
    browser_error_status = ""

    def record_browser_error(stage: str, error_class: str, exc: Exception) -> None:
        from ..core.logger import debug_logger

        debug_logger.log_warning(
            "[AccountReauth] browser operation failed: "
            f"stage={stage} error_class={error_class} "
            f"exception_type={type(exc).__name__}"
        )

    try:
        try:
            await browser.initialize()
        except Exception as exc:
            browser_error_class = "browser_start_failed"
            browser_error_status = "稍后重试"
            record_browser_error("initialize", browser_error_class, exc)

        if not browser_error_class:
            try:
                private_result = await browser.capture_account_onboarding_result()
            except TimeoutError as exc:
                browser_error_class = "interactive_verification"
                browser_error_interactive = True
                browser_error_status = "需要重新登录"
                record_browser_error("capture", browser_error_class, exc)
            except Exception as exc:
                browser_error_class = "network"
                browser_error_status = "稍后重试"
                record_browser_error("capture", browser_error_class, exc)
    finally:
        try:
            await browser.close()
        except Exception as exc:
            record_browser_error("close", "network", exc)
            if not browser_error_class:
                browser_error_class = "network"
                browser_error_status = "稍后重试"

    if browser_error_class:
        cleanup_candidate()
        await token_manager._mark_auth_failure(
            token_id,
            browser_error_class,
            interactive=browser_error_interactive,
        )
        return {"success": False, "auth_status": browser_error_status}

    try:
        await _persist_native_reauth_result(
            token_id,
            private_result,
            account_profile_key=candidate_key,
        )
    except ValueError as exc:
        cleanup_candidate()
        if str(exc) == "identity_mismatch":
            return {"success": False, "auth_status": "需要重新登录"}
        await token_manager._mark_auth_failure(
            token_id,
            "interactive_verification",
            interactive=True,
        )
        return {"success": False, "auth_status": "需要重新登录"}
    except Exception:
        cleanup_candidate()
        await token_manager._mark_auth_failure(
            token_id,
            "network",
            interactive=False,
        )
        return {"success": False, "auth_status": "稍后重试"}

    if existing_key and existing_key != candidate_key:
        try:
            profile_store.remove(existing_key)
        except Exception:
            pass
    return {"success": True, "auth_status": "正常"}


async def _get_account_onboarding_service():
    global account_onboarding_service
    if account_onboarding_service is not None:
        return account_onboarding_service

    from ..services.account_onboarding import AccountOnboardingService
    from ..services.browser_captcha_personal import BrowserCaptchaService

    async def count_accounts() -> int:
        return len(await token_manager.get_all_tokens())

    async def launch_browser(_session_id: str):
        profile_store = _get_account_profile_store()
        profile_key = profile_store.create_key()
        profile_dir = profile_store.resolve(profile_key, create=True)
        profile_persisted = False
        browser = BrowserCaptchaService(
            db,
            force_headed=True,
            persistent_profile_dir=profile_dir,
        )
        try:
            private_result = await browser.capture_account_onboarding_result()
            outcome = await _persist_native_onboarding_result(
                private_result,
                account_profile_key=profile_key,
            )
            profile_persisted = True
            return outcome
        finally:
            try:
                await browser.close()
            finally:
                if not profile_persisted:
                    try:
                        profile_store.remove(profile_key)
                    except Exception:
                        pass

    account_onboarding_service = AccountOnboardingService(
        account_counter=count_accounts,
        browser_launcher=launch_browser,
    )
    return account_onboarding_service


# ========== Request Models ==========

class LoginRequest(BaseModel):
    username: str
    password: str


class AddTokenRequest(BaseModel):
    st: str
    project_id: Optional[str] = None  # 用户可选输入project_id
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1
    protocol_mode: str = "session"
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    auto_refresh_enabled: bool = True
    refresh_interval_minutes: int = 120


class UpdateTokenRequest(BaseModel):
    st: Optional[str] = None  # Write-only; omitted/blank keeps the current credential
    project_id: Optional[str] = None  # 用户可选输入project_id
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: Optional[bool] = None
    video_enabled: Optional[bool] = None
    image_concurrency: Optional[int] = None
    video_concurrency: Optional[int] = None
    protocol_mode: Optional[str] = None
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    auto_refresh_enabled: Optional[bool] = None
    refresh_interval_minutes: Optional[int] = None


class ProxyConfigRequest(BaseModel):
    proxy_enabled: bool
    proxy_url: Optional[str] = None
    media_proxy_enabled: Optional[bool] = None
    media_proxy_url: Optional[str] = None


class ProxyTestRequest(BaseModel):
    proxy_url: str
    test_url: Optional[str] = "https://labs.google/"
    timeout_seconds: Optional[int] = 15


class CaptchaScoreTestRequest(BaseModel):
    website_url: Optional[str] = "https://antcpt.com/score_detector/"
    website_key: Optional[str] = "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf"
    action: Optional[str] = "homepage"
    verify_url: Optional[str] = "https://antcpt.com/score_detector/verify.php"
    enterprise: Optional[bool] = False


class GenerationConfigRequest(BaseModel):
    image_timeout: Optional[int] = None
    video_timeout: Optional[int] = None
    max_retries: Optional[int] = None


class CallLogicConfigRequest(BaseModel):
    call_mode: str


class ChangePasswordRequest(BaseModel):
    username: Optional[str] = None
    old_password: str
    new_password: str


class UpdateAPIKeyRequest(BaseModel):
    new_api_key: str


class UpdateDebugConfigRequest(BaseModel):
    enabled: bool


class UpdateAdminConfigRequest(BaseModel):
    error_ban_threshold: int


class WindowsAutostartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ST2ATRequest(BaseModel):
    """ST转AT请求"""
    st: str


class ImportTokenItem(BaseModel):
    """导入Token项"""
    email: Optional[str] = None
    access_token: Optional[str] = None
    session_token: Optional[str] = None
    is_active: bool = True
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1
    protocol_mode: str = "session"
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    auto_refresh_enabled: bool = True
    refresh_interval_minutes: int = 120


class ImportTokensRequest(BaseModel):
    """导入Token请求"""
    tokens: List[ImportTokenItem]


class TokenRefreshConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    refresh_interval_minutes: Optional[int] = None


# ========== Auth Middleware ==========

async def verify_admin_token(request: Request, authorization: str = Header(None)):
    """Verify admin session token (NOT API key)"""
    header_token = ""
    if authorization and authorization.startswith("Bearer "):
        header_token = authorization[7:].strip()

    cookie_token = get_admin_token_from_cookie(request) or ""

    if header_token and header_token in active_admin_tokens:
        return header_token

    if cookie_token and cookie_token in active_admin_tokens:
        return cookie_token

    if header_token or cookie_token:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")

    raise HTTPException(status_code=401, detail="Missing authorization")


def get_admin_token_from_cookie(request: Request) -> Optional[str]:
    token = str(request.cookies.get(ADMIN_SESSION_COOKIE_NAME) or "").strip()
    return token or None


def is_admin_session_token_valid(token: Optional[str]) -> bool:
    normalized = str(token or "").strip()
    return bool(normalized) and normalized in active_admin_tokens


# ========== Auth Endpoints ==========

@router.post("/api/admin/login")
async def admin_login(request: LoginRequest, response: Response):
    """Admin login - returns session token (NOT API key)"""
    admin_config = await db.get_admin_config()

    if not AuthManager.verify_admin(request.username, request.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Generate independent session token
    session_token = f"admin-{secrets.token_urlsafe(32)}"

    # Store in active tokens
    active_admin_tokens.add(session_token)

    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )

    return {
        "success": True,
        "token": session_token,  # Session token (NOT API key)
        "username": admin_config.username
    }


@router.post("/api/admin/logout")
async def admin_logout(response: Response, token: str = Depends(verify_admin_token)):
    """Admin logout - invalidate session token"""
    active_admin_tokens.discard(token)
    response.delete_cookie(ADMIN_SESSION_COOKIE_NAME, path="/")
    return {"success": True, "message": "退出登录成功"}


@router.post("/api/admin/account-onboarding")
async def start_account_onboarding(token: str = Depends(verify_admin_token)):
    service = await _get_account_onboarding_service()
    state = await service.start()
    return state.to_public_dict()


@router.post("/api/admin/test-capability")
async def issue_admin_test_capability(token: str = Depends(verify_admin_token)):
    from ..services.admin_test_capability import get_admin_test_capability_service
    capability = await get_admin_test_capability_service().issue(token)
    return {"capability": capability, "expires_in": 300}


@router.get("/api/admin/test-accounts")
async def get_admin_test_accounts(token: str = Depends(verify_admin_token)):
    """Return only the account identity/status fields needed by the test page."""
    _ = token
    page_data = await db.get_tokens_page_with_stats(limit=100, offset=0)
    items = []
    for row in list(page_data.get("items") or []):
        token_id = int(row.get("id"))
        auth_status, _, _ = _project_public_auth_status(row)
        display_name = str(row.get("name") or row.get("remark") or f"account-{token_id}")
        items.append(
            {
                "id": token_id,
                "display_name": display_name,
                "auth_status": auth_status,
            }
        )
    return {"items": items}


@router.get("/api/admin/extension-connection-status")
async def get_extension_connection_status(token: str = Depends(verify_admin_token)):
    """Return the public, aggregate-only status of the extension captcha worker."""
    _ = token
    response = {
        "service_online": True,
        "configured_port": int(config.server_port),
        "captcha_mode": str(config.captcha_method or ""),
        "extension_connected": False,
        "connection_count": 0,
        "active_account_count": 0,
        "connected_account_count": 0,
        "ready_account_count": 0,
        "status": "disconnected",
        "error_class": "extension_not_connected",
    }
    if response["captcha_mode"] == "personal":
        active_tokens = list(await token_manager.get_active_tokens())
        ready_tokens = [
            item
            for item in active_tokens
            if str(getattr(item, "auth_state", "ok") or "").strip() == "ok"
            and (
                not hasattr(item, "account_profile_key")
                or bool(str(getattr(item, "account_profile_key", "") or "").strip())
            )
        ]
        response["active_account_count"] = len(active_tokens)
        response["ready_account_count"] = len(ready_tokens)
        if ready_tokens:
            response.update(status="ready", error_class=None)
        elif active_tokens:
            response.update(
                status="account_reauth_required",
                error_class="account_authentication_required",
            )
        else:
            response.update(
                status="account_required",
                error_class="account_not_configured",
            )
        return response

    if response["captcha_mode"] != "extension":
        response.update(status="not_required", error_class=None)
        return response

    try:
        from ..services.browser_captcha_extension import ExtensionCaptchaService

        service = await ExtensionCaptchaService.get_instance(
            getattr(token_manager, "db", None)
        )
        response["connection_count"] = sum(
            1
            for connection in list(getattr(service, "active_connections", []) or [])
            if getattr(connection, "capability_marker", "") == "yingce-flow2api-worker-v1"
        )
        eligible_tokens = [
            item
            for item in await token_manager.get_active_tokens()
            if bool(getattr(item, "image_enabled", False))
            and max(0, int(getattr(item, "credits", 0) or 0)) > 0
        ]
        response["active_account_count"] = len(eligible_tokens)
        for item in eligible_tokens:
            connected, _ = await service.has_connection_for_token(item.id)
            if connected:
                response["connected_account_count"] += 1
        response["ready_account_count"] = response["connected_account_count"]
        if response["connection_count"] and response["ready_account_count"]:
            response.update(
                extension_connected=True,
                status="ready",
                error_class=None,
            )
        elif response["connection_count"]:
            response.update(
                status="account_binding_required",
                error_class="extension_account_binding_pending",
            )
    except Exception:
        response.update(status="connecting", error_class="extension_status_unavailable")
    return response


@router.get("/api/admin/account-onboarding/{session_id}")
async def get_account_onboarding_status(
    session_id: str,
    token: str = Depends(verify_admin_token),
):
    service = await _get_account_onboarding_service()
    try:
        state = await service.status(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found")
    return state.to_public_dict()


@router.post("/api/admin/change-password")
async def change_password(
    request: ChangePasswordRequest,
    token: str = Depends(verify_admin_token)
):
    """Change admin password"""
    admin_config = await db.get_admin_config()

    # Verify old password
    if not AuthManager.verify_admin(admin_config.username, request.old_password):
        raise HTTPException(status_code=400, detail="旧密码错误")

    # Update password and username in database
    update_params = {"password": request.new_password}
    if request.username:
        update_params["username"] = request.username

    await db.update_admin_config(**update_params)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    # 🔑 Invalidate all admin session tokens (force re-login for security)
    active_admin_tokens.clear()

    return {"success": True, "message": "密码修改成功,请重新登录"}


# ========== Token Management ==========

def _project_public_auth_status(
    row: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
):
    """Project internal recovery metadata to the five stable public states."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    if not bool(row.get("is_active")):
        return "已停用", 0, False

    has_profile = bool(row.get("has_account_profile"))
    state = str(row.get("auth_state") or "").strip()
    if not has_profile:
        return "需要重新登录", 0, True
    if state == "ok":
        return "正常", 0, False
    if state == "refresh_pending":
        return "等待自动恢复", 0, False
    if state == "backoff":
        retry_at = row.get("auth_next_retry_at")
        if isinstance(retry_at, str):
            try:
                retry_at = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
            except ValueError:
                retry_at = None
        if isinstance(retry_at, datetime):
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = max(0, int((retry_at - current).total_seconds() + 0.999))
        else:
            seconds = 0
        return "稍后重试", seconds, False
    if state == "reauth_required":
        return "需要重新登录", 0, True
    return "需要重新登录", 0, True


@router.get("/api/tokens")
async def get_tokens(
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    token: str = Depends(verify_admin_token),
):
    """Return a bounded, credential-free account status page."""
    explicit_pagination = page is not None or page_size is not None
    try:
        normalized_page = max(1, int(page if page is not None else 1))
    except (TypeError, ValueError):
        normalized_page = 1
    try:
        normalized_page_size = max(
            1,
            min(100, int(page_size if page_size is not None else 25)),
        )
    except (TypeError, ValueError):
        normalized_page_size = 25

    page_data = await db.get_tokens_page_with_stats(
        limit=normalized_page_size,
        offset=(normalized_page - 1) * normalized_page_size,
    )
    rows = list(page_data.get("items") or [])
    token_ids = [int(row["id"]) for row in rows if row.get("id") is not None]

    concurrency_defaults = {
        "image_learned_limit": None,
        "image_inflight": 0,
        "image_cooldown_reason": None,
        "image_cooldown_remaining_seconds": 0,
        "video_learned_limit": None,
        "video_inflight": 0,
        "video_cooldown_reason": None,
        "video_cooldown_remaining_seconds": 0,
    }
    browser_account_defaults = {
        "browser_in_use": False,
        "browser_worker_index": None,
        "browser_reserved_slots": 0,
        "browser_resident_slots": 0,
    }
    browser_overview = {
        "status": "unknown",
        "error_class": "status_unavailable",
        "configured_workers": 0,
        "max_workers": 10,
        "created_workers": 0,
        "live_workers": 0,
        "total_reservations": 0,
        "total_inflight": 0,
        "total_capacity": 0,
        "occupied_slots": 0,
    }

    concurrency_snapshot: Dict[int, Dict[str, Any]] = {}
    concurrency_unavailable = False
    try:
        if concurrency_manager is None:
            concurrency_unavailable = True
        else:
            concurrency_snapshot = await concurrency_manager.get_observability_snapshot(token_ids)
    except Exception:
        concurrency_unavailable = True
        concurrency_snapshot = {}

    browser_accounts: Dict[int, Dict[str, Any]] = {}
    browser_unavailable = False
    try:
        from ..services.browser_captcha_personal import BrowserCaptchaService

        browser_service = await BrowserCaptchaService.get_instance(db)
        if not hasattr(browser_service, "get_observability_snapshot"):
            browser_unavailable = True
        else:
            browser_snapshot = await browser_service.get_observability_snapshot(token_ids)
            raw_browser_overview = dict(browser_snapshot.get("overview") or {})
            browser_overview = {
                field: raw_browser_overview.get(field, default)
                for field, default in browser_overview.items()
            }
            browser_accounts = dict(browser_snapshot.get("accounts") or {})
    except Exception:
        browser_unavailable = True
        browser_accounts = {}

    status_unavailable = concurrency_unavailable or browser_unavailable
    items = []
    for row in rows:
        token_id = int(row.get("id"))
        credits = max(0, int(row.get("credits") or 0))
        credits_reserved = max(0, int(row.get("credits_reserved") or 0))
        is_active = bool(row.get("is_active"))
        auth_status, auth_retry_after_seconds, can_reauth = _project_public_auth_status(row)
        display_name = str(row.get("name") or row.get("remark") or f"account-{token_id}")
        raw_concurrency_status = dict(concurrency_snapshot.get(token_id) or {})
        concurrency_status = {
            field: raw_concurrency_status.get(field, default)
            for field, default in concurrency_defaults.items()
        }
        raw_browser_status = dict(browser_accounts.get(token_id) or {})
        browser_status = {
            field: raw_browser_status.get(field, default)
            for field, default in browser_account_defaults.items()
        }
        items.append({
            "id": token_id,
            "display_name": display_name,
            "is_active": is_active,
            "auth_status": auth_status,
            "auth_retry_after_seconds": auth_retry_after_seconds,
            "can_reauth": can_reauth,
            "status": "unknown" if status_unavailable else "ok",
            "error_class": "status_unavailable" if status_unavailable else None,
            "credits": credits,
            "credits_reserved": credits_reserved,
            "credits_available": max(0, credits - credits_reserved),
            **concurrency_status,
            **browser_status,
        })

    if not explicit_pagination:
        return items
    return {
        "items": items,
        "total": max(0, int(page_data.get("total") or 0)),
        "page": normalized_page,
        "page_size": normalized_page_size,
        "has_next": bool(page_data.get("has_next")),
        "browser": browser_overview,
    }


@router.get("/api/tokens/{token_id}/edit-config")
async def get_token_edit_config(
    token_id: int,
    token: str = Depends(verify_admin_token),
):
    """Return only non-credential fields required by the edit form."""
    edit_config = await db.get_token_edit_config(token_id)
    if not edit_config:
        raise HTTPException(status_code=404, detail="Token not found")
    return {
        "id": int(edit_config.get("id")),
        "remark": edit_config.get("remark") or "",
        "project_id": edit_config.get("current_project_id") or "",
        "project_name": edit_config.get("current_project_name") or "",
        "image_enabled": bool(edit_config.get("image_enabled")),
        "video_enabled": bool(edit_config.get("video_enabled")),
        "image_concurrency": int(edit_config.get("image_concurrency") or -1),
        "video_concurrency": int(edit_config.get("video_concurrency") or -1),
        "protocol_mode": edit_config.get("protocol_mode") or "session",
        "auto_refresh_enabled": bool(edit_config.get("auto_refresh_enabled", True)),
        "refresh_interval_minutes": int(edit_config.get("refresh_interval_minutes") or 120),
    }


@router.post("/api/tokens")
async def add_token(
    request: AddTokenRequest,
    token: str = Depends(verify_admin_token)
):
    """Add a new token"""
    try:
        new_token = await token_manager.add_token(
            st=request.st,
            project_id=request.project_id,  # 🆕 支持用户指定project_id
            project_name=request.project_name,
            remark=request.remark,
            captcha_proxy_url=request.captcha_proxy_url.strip() if request.captcha_proxy_url is not None else None,
            extension_route_key=request.extension_route_key.strip() if request.extension_route_key is not None else None,
            image_enabled=request.image_enabled,
            video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency,
            video_concurrency=request.video_concurrency,
            protocol_mode=request.protocol_mode,
            google_cookies=request.google_cookies,
            login_account=request.login_account,
            login_password=request.login_password,
            proxy_url=request.proxy_url,
            auto_refresh_enabled=request.auto_refresh_enabled,
            refresh_interval_minutes=request.refresh_interval_minutes
        )

        # 热更新并发限制，避免必须重启服务
        if concurrency_manager:
            await concurrency_manager.reset_token(
                new_token.id,
                image_concurrency=new_token.image_concurrency,
                video_concurrency=new_token.video_concurrency
            )

        return {
            "success": True,
            "message": "Token添加成功",
            "token": {
                "id": new_token.id,
                "email": new_token.email,
                "credits": new_token.credits,
                "project_id": new_token.current_project_id,
                "project_name": new_token.current_project_name
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"添加Token失败: {str(e)}")


@router.put("/api/tokens/{token_id}")
async def update_token(
    token_id: int,
    request: UpdateTokenRequest,
    token: str = Depends(verify_admin_token)
):
    """Update token; credential fields are optional write-only replacements."""
    try:
        normalized_st = str(request.st or "").strip()
        at = None
        at_expires = None
        if normalized_st:
            result = await token_manager.flow_client.st_to_at(normalized_st)
            at = result["access_token"]
            expires = result.get("expires")
            if expires:
                try:
                    at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                except (TypeError, ValueError):
                    at_expires = None

        await token_manager.update_token(
            token_id=token_id,
            st=normalized_st or None,
            at=at,
            at_expires=at_expires,
            project_id=request.project_id,
            project_name=request.project_name,
            remark=request.remark,
            captcha_proxy_url=request.captcha_proxy_url.strip() if request.captcha_proxy_url is not None else None,
            extension_route_key=request.extension_route_key.strip() if request.extension_route_key is not None else None,
            image_enabled=request.image_enabled,
            video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency,
            video_concurrency=request.video_concurrency,
            protocol_mode=request.protocol_mode,
            google_cookies=request.google_cookies,
            login_account=request.login_account,
            login_password=request.login_password,
            proxy_url=request.proxy_url,
            auto_refresh_enabled=request.auto_refresh_enabled,
            refresh_interval_minutes=request.refresh_interval_minutes
        )

        # 热更新并发限制，确保管理台修改立即生效
        if concurrency_manager:
            updated_token = await token_manager.get_token(token_id)
            if updated_token:
                await concurrency_manager.reset_token(
                    token_id,
                    image_concurrency=updated_token.image_concurrency,
                    video_concurrency=updated_token.video_concurrency
                )

        return {"success": True, "message": "Token更新成功"}
    except Exception as e:
        from ..core.logger import debug_logger

        debug_logger.log_error(
            f"[API] Token update failed: token_id={token_id}, "
            f"error_type={type(e).__name__}"
        )
        raise HTTPException(status_code=500, detail="Token update failed")


@router.delete("/api/tokens/{token_id}")
async def delete_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Delete token"""
    try:
        await token_manager.delete_token(token_id)
        if concurrency_manager:
            await concurrency_manager.remove_token(token_id)
        return {"success": True, "message": "Token删除成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tokens/{token_id}/enable")
async def enable_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Enable token"""
    await token_manager.enable_token(token_id)
    return {"success": True, "message": "Token已启用"}


@router.post("/api/tokens/{token_id}/disable")
async def disable_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Disable token"""
    await token_manager.disable_token(token_id)
    return {"success": True, "message": "Token已禁用"}


@router.post("/api/tokens/{token_id}/reauth")
async def reauth_token(
    token_id: int,
    token: str = Depends(verify_admin_token),
):
    _ = token
    return await _run_account_reauth(token_id)


@router.post("/api/tokens/{token_id}/refresh-credits")
async def refresh_credits(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """刷新Token余额 🆕"""
    try:
        credits = await token_manager.refresh_credits(token_id)
        return {
            "success": True,
            "message": "余额刷新成功",
            "credits": credits
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刷新余额失败: {str(e)}")


@router.post("/api/tokens/{token_id}/refresh-at")
async def refresh_at(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """手动刷新Token的AT (使用ST转换) 🆕
    
    如果 AT 刷新失败且处于 personal 模式，会自动尝试通过浏览器刷新 ST
    """
    from ..core.logger import debug_logger
    from ..core.config import config
    
    debug_logger.log_info(f"[API] 手动刷新 AT 请求: token_id={token_id}, captcha_method={config.captcha_method}")
    
    try:
        # 调用token_manager的内部刷新方法（包含 ST 自动刷新逻辑）
        success = await token_manager._refresh_at(token_id)

        if success:
            # 获取更新后的token信息
            updated_token = await token_manager.get_token(token_id)
            
            message = "AT刷新成功"
            if config.captcha_method == "personal":
                message += "（支持ST自动刷新）"
            
            debug_logger.log_info(f"[API] AT 刷新成功: token_id={token_id}")
            
            return {
                "success": True,
                "message": message,
                "token": {
                    "id": updated_token.id,
                    "email": updated_token.email,
                    "at_expires": updated_token.at_expires.isoformat() if updated_token.at_expires else None
                }
            }
        else:
            debug_logger.log_error(f"[API] AT 刷新失败: token_id={token_id}")
            
            error_detail = "AT刷新失败"
            if config.captcha_method != "personal":
                error_detail += f"（当前打码模式: {config.captcha_method}，ST自动刷新仅在 personal 模式下可用）"
            
            raise HTTPException(status_code=500, detail=error_detail)
    except HTTPException:
        raise
    except Exception:
        debug_logger.log_error("[API] 刷新AT异常")
        raise HTTPException(status_code=500, detail="刷新AT失败")


@router.post("/api/tokens/st2at")
async def st_to_at(
    request: ST2ATRequest,
    token: str = Depends(verify_admin_token)
):
    """Convert Session Token to Access Token (仅转换,不添加到数据库)"""
    try:
        result = await token_manager.flow_client.st_to_at(request.st)
        return {
            "success": True,
            "message": "ST converted to AT successfully",
            "access_token": result["access_token"],
            "email": result.get("user", {}).get("email"),
            "expires": result.get("expires")
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/tokens/import")
async def import_tokens(
    request: ImportTokensRequest,
    token: str = Depends(verify_admin_token)
):
    """批量导入Token"""
    from datetime import datetime, timezone

    added = 0
    updated = 0
    errors = []
    # 保持与历史逻辑一致：按 created_at DESC 的结果中，优先命中同邮箱“最新一条”
    existing_by_email = {}
    for existing_token in await token_manager.get_all_tokens():
        if existing_token.email and existing_token.email not in existing_by_email:
            existing_by_email[existing_token.email] = existing_token

    for idx, item in enumerate(request.tokens):
        try:
            st = item.session_token

            if not st:
                errors.append(f"第{idx+1}项: 缺少 session_token")
                continue

            # 使用 ST 转 AT 获取用户信息
            try:
                result = await token_manager.flow_client.st_to_at(st)
                at = result["access_token"]
                email = result.get("user", {}).get("email")
                expires = result.get("expires")

                if not email:
                    errors.append(f"第{idx+1}项: 无法获取邮箱信息")
                    continue

                # 解析过期时间
                at_expires = None
                is_expired = False
                if expires:
                    try:
                        at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                        # 判断是否过期
                        now = datetime.now(timezone.utc)
                        is_expired = at_expires <= now
                    except:
                        pass

                # 使用邮箱检查是否已存在
                existing = existing_by_email.get(email)

                if existing:
                    # 更新现有Token
                    await token_manager.update_token(
                        token_id=existing.id,
                        st=st,
                        at=at,
                        at_expires=at_expires,
                        captcha_proxy_url=item.captcha_proxy_url.strip() if item.captcha_proxy_url is not None else None,
                        extension_route_key=item.extension_route_key.strip() if item.extension_route_key is not None else None,
                        image_enabled=item.image_enabled,
                        video_enabled=item.video_enabled,
                        image_concurrency=item.image_concurrency,
                        video_concurrency=item.video_concurrency,
                        protocol_mode=item.protocol_mode,
                        google_cookies=item.google_cookies,
                        login_account=item.login_account,
                        login_password=item.login_password,
                        proxy_url=item.proxy_url,
                        auto_refresh_enabled=item.auto_refresh_enabled,
                        refresh_interval_minutes=item.refresh_interval_minutes
                    )
                    # 如果过期则禁用
                    if is_expired:
                        await token_manager.disable_token(existing.id)
                        existing.is_active = False
                    existing.st = st
                    existing.at = at
                    existing.at_expires = at_expires
                    existing.captcha_proxy_url = item.captcha_proxy_url
                    existing.extension_route_key = item.extension_route_key
                    existing.image_enabled = item.image_enabled
                    existing.video_enabled = item.video_enabled
                    existing.image_concurrency = item.image_concurrency
                    existing.video_concurrency = item.video_concurrency
                    existing.protocol_mode = item.protocol_mode
                    existing.google_cookies = item.google_cookies or ""
                    existing.login_account = item.login_account or ""
                    existing.login_password = item.login_password or ""
                    existing.proxy_url = item.proxy_url or ""
                    existing.auto_refresh_enabled = item.auto_refresh_enabled
                    existing.refresh_interval_minutes = item.refresh_interval_minutes
                    updated += 1
                else:
                    # 添加新Token
                    new_token = await token_manager.add_token(
                        st=st,
                        captcha_proxy_url=item.captcha_proxy_url.strip() if item.captcha_proxy_url is not None else None,
                        extension_route_key=item.extension_route_key.strip() if item.extension_route_key is not None else None,
                        image_enabled=item.image_enabled,
                        video_enabled=item.video_enabled,
                        image_concurrency=item.image_concurrency,
                        video_concurrency=item.video_concurrency,
                        protocol_mode=item.protocol_mode,
                        google_cookies=item.google_cookies,
                        login_account=item.login_account,
                        login_password=item.login_password,
                        proxy_url=item.proxy_url,
                        auto_refresh_enabled=item.auto_refresh_enabled,
                        refresh_interval_minutes=item.refresh_interval_minutes
                    )
                    # 如果过期则禁用
                    if is_expired:
                        await token_manager.disable_token(new_token.id)
                        new_token.is_active = False
                    existing_by_email[email] = new_token
                    added += 1

            except Exception as e:
                errors.append(f"第{idx+1}项: {str(e)}")

        except Exception as e:
            errors.append(f"第{idx+1}项: {str(e)}")

    return {
        "success": True,
        "added": added,
        "updated": updated,
        "errors": errors if errors else None,
        "message": f"导入完成: 新增 {added} 个, 更新 {updated} 个" + (f", {len(errors)} 个失败" if errors else "")
    }


# ========== Config Management ==========

@router.get("/api/config/proxy")
async def get_proxy_config(token: str = Depends(verify_admin_token)):
    """Get proxy configuration"""
    config = await proxy_manager.get_proxy_config()
    return {
        "success": True,
        "config": {
            "enabled": config.enabled,
            "proxy_url": config.proxy_url,
            "media_proxy_enabled": config.media_proxy_enabled,
            "media_proxy_url": config.media_proxy_url
        }
    }


@router.get("/api/proxy/config")
async def get_proxy_config_alias(token: str = Depends(verify_admin_token)):
    """Get proxy configuration (alias for frontend compatibility)"""
    config = await proxy_manager.get_proxy_config()
    return {
        "proxy_enabled": config.enabled,  # Frontend expects proxy_enabled
        "proxy_url": config.proxy_url,
        "media_proxy_enabled": config.media_proxy_enabled,
        "media_proxy_url": config.media_proxy_url
    }


@router.post("/api/proxy/config")
async def update_proxy_config_alias(
    request: ProxyConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update proxy configuration (alias for frontend compatibility)"""
    try:
        await proxy_manager.update_proxy_config(
            enabled=request.proxy_enabled,
            proxy_url=request.proxy_url,
            media_proxy_enabled=request.media_proxy_enabled,
            media_proxy_url=request.media_proxy_url
        )
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "message": "代理配置更新成功"}


@router.post("/api/config/proxy")
async def update_proxy_config(
    request: ProxyConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update proxy configuration"""
    try:
        await proxy_manager.update_proxy_config(
            enabled=request.proxy_enabled,
            proxy_url=request.proxy_url,
            media_proxy_enabled=request.media_proxy_enabled,
            media_proxy_url=request.media_proxy_url
        )
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "message": "代理配置更新成功"}


@router.post("/api/proxy/test")
async def test_proxy_connectivity(
    request: ProxyTestRequest,
    token: str = Depends(verify_admin_token)
):
    """测试代理是否可访问目标站点（默认 https://labs.google/）"""
    proxy_input = (request.proxy_url or "").strip()
    test_url = (request.test_url or "https://labs.google/").strip()
    timeout_seconds = int(request.timeout_seconds or 15)
    timeout_seconds = max(5, min(timeout_seconds, 60))

    if not proxy_input:
        return {
            "success": False,
            "message": "代理地址为空",
            "test_url": test_url
        }

    try:
        proxy_url = proxy_manager.normalize_proxy_url(proxy_input)
    except ValueError as e:
        return {
            "success": False,
            "message": str(e),
            "test_url": test_url
        }

    start_time = time.time()
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        async with AsyncSession() as session:
            resp = await session.get(
                test_url,
                proxies=proxies,
                timeout=timeout_seconds,
                impersonate="chrome120",
                allow_redirects=True,
                verify=False
            )

        elapsed_ms = int((time.time() - start_time) * 1000)
        status_code = resp.status_code
        final_url = str(resp.url)
        ok = 200 <= status_code < 400

        return {
            "success": ok,
            "message": "代理可用" if ok else f"代理可连通，但目标返回状态码 {status_code}",
            "test_url": test_url,
            "final_url": final_url,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms
        }
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return {
            "success": False,
            "message": f"代理测试失败: {str(e)}",
            "test_url": test_url,
            "elapsed_ms": elapsed_ms
        }


@router.get("/api/config/generation")
async def get_generation_config(token: str = Depends(verify_admin_token)):
    """Get generation timeout configuration"""
    config = await db.get_generation_config()
    return {
        "success": True,
        "config": {
            "image_timeout": config.image_timeout,
            "video_timeout": config.video_timeout,
            "max_retries": config.max_retries,
        }
    }


@router.post("/api/config/generation")
async def update_generation_config(
    request: GenerationConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update generation timeout configuration"""
    await db.update_generation_config(
        image_timeout=request.image_timeout,
        video_timeout=request.video_timeout,
        max_retries=request.max_retries,
    )

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "生成配置更新成功"}


@router.get("/api/call-logic/config")
async def get_call_logic_config(token: str = Depends(verify_admin_token)):
    """Get token call logic configuration."""
    config_obj = await db.get_call_logic_config()
    call_mode = getattr(config_obj, "call_mode", None)
    if call_mode not in ("default", "polling"):
        call_mode = "polling" if getattr(config_obj, "polling_mode_enabled", False) else "default"
    return {
        "success": True,
        "config": {
            "call_mode": call_mode,
            "polling_mode_enabled": call_mode == "polling",
        }
    }


@router.post("/api/call-logic/config")
async def update_call_logic_config(
    request: CallLogicConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update token call logic configuration."""
    call_mode = request.call_mode if request.call_mode in ("default", "polling") else None
    if call_mode is None:
        raise HTTPException(status_code=400, detail="Invalid call_mode")

    await db.update_call_logic_config(call_mode)
    await db.reload_config_to_memory()

    return {
        "success": True,
        "message": "Token轮询模式保存成功",
        "config": {
            "call_mode": call_mode,
            "polling_mode_enabled": call_mode == "polling",
        }
    }


# ========== System Info ==========

@router.get("/api/system/info")
async def get_system_info(token: str = Depends(verify_admin_token)):
    """Get system information"""
    stats = await db.get_system_info_stats()

    return {
        "success": True,
        "info": {
            "total_tokens": stats["total_tokens"],
            "active_tokens": stats["active_tokens"],
            "total_credits": stats["total_credits"],
            "version": "1.0.0"
        }
    }


# ========== Additional Routes for Frontend Compatibility ==========

@router.post("/api/login")
async def login(request: LoginRequest, response: Response):
    """Login endpoint (alias for /api/admin/login)"""
    return await admin_login(request, response)


@router.post("/api/logout")
async def logout(response: Response, token: str = Depends(verify_admin_token)):
    """Logout endpoint (alias for /api/admin/logout)"""
    return await admin_logout(response, token)


@router.get("/health")
async def health_check():
    """Public health check endpoint - no auth required"""
    try:
        return await build_public_health_snapshot(db)
    except Exception:
        return {"backend_running": True, "has_active_tokens": False}


@router.get("/api/stats")
async def get_stats(token: str = Depends(verify_admin_token)):
    """Get statistics for dashboard"""
    return await db.get_dashboard_stats()


@router.get("/api/logs")
async def get_logs(
    limit: int = 100,
    token: str = Depends(verify_admin_token)
):
    """Get lightweight request logs for list view"""
    limit = max(1, min(limit, 100))
    logs = await db.get_logs(limit=limit, include_payload=False)

    result = []
    for log in logs:
        raw_status_code = log.get("status_code")
        try:
            status_code = int(raw_status_code) if raw_status_code is not None else None
        except (TypeError, ValueError):
            status_code = None
        result.append({
            "id": log.get("id"),
            "token_id": log.get("token_id"),
            "token_email": log.get("token_email"),
            "token_username": log.get("token_username"),
            "operation": log.get("operation"),
            "status_code": status_code if status_code is not None else raw_status_code,
            "duration": log.get("duration"),
            "status_text": log.get("status_text") or "",
            "progress": log.get("progress") or 0,
            "created_at": log.get("created_at"),
            "updated_at": log.get("updated_at"),
            "error_summary": _extract_error_summary(log.get("response_body_excerpt")) if status_code is not None and status_code >= 400 else "",
        })
    return result


@router.get("/api/logs/{log_id}")
async def get_log_detail(
    log_id: int,
    token: str = Depends(verify_admin_token)
):
    """Get single request log detail (payload loaded on demand)"""
    log = await db.get_log_detail(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="日志不存在")

    error_summary = _extract_error_summary(log.get("response_body"))

    return {
        "id": log.get("id"),
        "token_id": log.get("token_id"),
        "token_email": log.get("token_email"),
        "token_username": log.get("token_username"),
        "operation": log.get("operation"),
        "status_code": log.get("status_code"),
        "duration": log.get("duration"),
        "status_text": log.get("status_text") or "",
        "progress": log.get("progress") or 0,
        "created_at": log.get("created_at"),
        "updated_at": log.get("updated_at"),
        "error_summary": error_summary,
        "request_body": log.get("request_body"),
        "response_body": log.get("response_body")
    }


@router.delete("/api/logs")
async def clear_logs(token: str = Depends(verify_admin_token)):
    """Clear all logs"""
    try:
        await db.clear_all_logs()
        return {"success": True, "message": "所有日志已清空"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/admin/windows-autostart")
async def get_windows_autostart_status(token: str = Depends(verify_admin_token)):
    _ = token
    manager = get_windows_autostart_manager()
    return await asyncio.to_thread(manager.get_status)


@router.post("/api/admin/windows-autostart")
async def update_windows_autostart_status(
    request: WindowsAutostartRequest,
    token: str = Depends(verify_admin_token),
):
    _ = token
    manager = get_windows_autostart_manager()
    return await asyncio.to_thread(manager.set_enabled, request.enabled)


@router.get("/api/admin/config")
async def get_admin_config(token: str = Depends(verify_admin_token)):
    """Get admin configuration"""
    admin_config = await db.get_admin_config()

    return {
        "admin_username": admin_config.username,
        "api_key": admin_config.api_key,
        "error_ban_threshold": admin_config.error_ban_threshold,
        "debug_enabled": config.debug_enabled  # Return actual debug status
    }


@router.post("/api/admin/config")
async def update_admin_config(
    request: UpdateAdminConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update admin configuration (error_ban_threshold)"""
    # Update error_ban_threshold in database
    await db.update_admin_config(error_ban_threshold=request.error_ban_threshold)

    return {"success": True, "message": "配置更新成功"}


@router.post("/api/admin/password")
async def update_admin_password(
    request: ChangePasswordRequest,
    token: str = Depends(verify_admin_token)
):
    """Update admin password"""
    return await change_password(request, token)


@router.post("/api/admin/apikey")
async def update_api_key(
    request: UpdateAPIKeyRequest,
    token: str = Depends(verify_admin_token)
):
    """Update API key (for external API calls, NOT for admin login)"""
    # Update API key in database
    await db.update_admin_config(api_key=request.new_api_key)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "API Key更新成功"}


@router.post("/api/admin/debug")
async def update_debug_config(
    request: UpdateDebugConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update debug configuration"""
    try:
        # Update in-memory config only (not database)
        # This ensures debug mode is automatically disabled on restart
        config.set_debug_enabled(request.enabled)

        status = "enabled" if request.enabled else "disabled"
        return {"success": True, "message": f"Debug mode {status}", "enabled": request.enabled}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update debug config: {str(e)}")


@router.get("/api/generation/timeout")
async def get_generation_timeout(token: str = Depends(verify_admin_token)):
    """Get generation timeout configuration"""
    return await get_generation_config(token)


@router.post("/api/generation/timeout")
async def update_generation_timeout(
    request: GenerationConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update generation timeout configuration"""
    await db.update_generation_config(
        image_timeout=request.image_timeout,
        video_timeout=request.video_timeout,
        max_retries=request.max_retries,
    )

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "生成配置更新成功"}


# ========== AT Auto Refresh Config ==========

@router.get("/api/token-refresh/config")
async def get_token_refresh_config(token: str = Depends(verify_admin_token)):
    """Get AT/protocol refresh configuration."""
    refresh_config = await db.get_token_refresh_config()
    return {
        "success": True,
        "config": {
            "at_auto_refresh_enabled": True,
            "protocol_refresh_enabled": refresh_config.enabled,
            "refresh_interval_minutes": refresh_config.refresh_interval_minutes,
        }
    }


@router.post("/api/token-refresh/enabled")
async def update_token_refresh_enabled(
    request: Optional[dict] = None,
    token: str = Depends(verify_admin_token)
):
    """Update protocol ST refresh enabled; AT refresh remains always enabled."""
    enabled = (request or {}).get("enabled")
    if enabled is not None:
        await db.update_token_refresh_config(enabled=bool(enabled))
    return {
        "success": True,
        "message": "刷新配置已更新"
    }


@router.post("/api/token-refresh/config")
async def update_token_refresh_config(
    request: TokenRefreshConfigRequest,
    token: str = Depends(verify_admin_token)
):
    refresh_config = await db.update_token_refresh_config(
        enabled=request.enabled,
        refresh_interval_minutes=request.refresh_interval_minutes,
    )
    return {
        "success": True,
        "config": {
            "at_auto_refresh_enabled": True,
            "protocol_refresh_enabled": refresh_config.enabled,
            "refresh_interval_minutes": refresh_config.refresh_interval_minutes,
        }
    }


async def _sync_runtime_cache_config():
    from . import routes
    if routes.generation_handler and routes.generation_handler.file_cache:
        file_cache = routes.generation_handler.file_cache
        file_cache.set_timeout(config.cache_timeout)
        await file_cache.refresh_cleanup_task()

# ========== Cache Configuration Endpoints ==========

@router.get("/api/cache/config")
async def get_cache_config(token: str = Depends(verify_admin_token)):
    """Get cache configuration"""
    cache_config = await db.get_cache_config()

    # Calculate effective base URL
    effective_base_url = cache_config.cache_base_url if cache_config.cache_base_url else f"http://127.0.0.1:8000"

    return {
        "success": True,
        "config": {
            "enabled": cache_config.cache_enabled,
            "timeout": cache_config.cache_timeout,
            "base_url": cache_config.cache_base_url or "",
            "effective_base_url": effective_base_url
        }
    }


@router.post("/api/cache/enabled")
async def update_cache_enabled(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update cache enabled status"""
    enabled = request.get("enabled", False)
    await db.update_cache_config(enabled=enabled)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": f"缓存已{'启用' if enabled else '禁用'}"}


@router.post("/api/cache/config")
async def update_cache_config_full(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update complete cache configuration"""
    enabled = request.get("enabled")
    timeout = request.get("timeout")
    base_url = request.get("base_url")

    if timeout is not None:
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="缓存超时时间必须为整数")
        if timeout < 0:
            raise HTTPException(status_code=400, detail="缓存超时时间不能小于 0")

    await db.update_cache_config(enabled=enabled, timeout=timeout, base_url=base_url)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": "缓存配置更新成功"}


@router.post("/api/cache/base-url")
async def update_cache_base_url(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update cache base URL"""
    base_url = request.get("base_url", "")
    await db.update_cache_config(base_url=base_url)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": "缓存Base URL更新成功"}


@router.post("/api/captcha/config")
async def update_captcha_config(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update captcha configuration"""
    captcha_method = request.get("captcha_method")
    yescaptcha_api_key = request.get("yescaptcha_api_key")
    yescaptcha_base_url = request.get("yescaptcha_base_url")
    yescaptcha_task_type = normalize_yescaptcha_task_type(request.get("yescaptcha_task_type"))
    capmonster_api_key = request.get("capmonster_api_key")
    capmonster_base_url = request.get("capmonster_base_url")
    ezcaptcha_api_key = request.get("ezcaptcha_api_key")
    ezcaptcha_base_url = request.get("ezcaptcha_base_url")
    capsolver_api_key = request.get("capsolver_api_key")
    capsolver_base_url = request.get("capsolver_base_url")
    remote_browser_base_url = request.get("remote_browser_base_url")
    remote_browser_api_key = request.get("remote_browser_api_key")
    remote_browser_timeout = request.get("remote_browser_timeout", 60)
    browser_proxy_enabled = request.get("browser_proxy_enabled", False)
    browser_proxy_url = request.get("browser_proxy_url", "")
    browser_count = request.get("browser_count", 1)
    personal_project_pool_size = request.get("personal_project_pool_size")
    personal_max_resident_tabs = request.get("personal_max_resident_tabs")
    browser_personal_fresh_restart_every_n_solves = request.get(
        "browser_personal_fresh_restart_every_n_solves",
        10,
    )
    personal_idle_tab_ttl_seconds = request.get("personal_idle_tab_ttl_seconds")

    # 验证浏览器代理URL格式
    if browser_proxy_enabled and browser_proxy_url:
        is_valid, error_msg = _validate_browser_proxy_url_local(browser_proxy_url)
        if not is_valid:
            return {"success": False, "message": error_msg}

    if remote_browser_base_url:
        try:
            remote_browser_base_url = _normalize_http_base_url(remote_browser_base_url)
        except RuntimeError as e:
            return {"success": False, "message": str(e)}

    try:
        remote_browser_timeout = max(5, int(remote_browser_timeout or 60))
    except Exception:
        return {"success": False, "message": "远程打码超时时间必须是整数秒"}
    try:
        browser_count = max(1, min(10, int(browser_count or 1)))
    except Exception:
        browser_count = 1
    try:
        browser_personal_fresh_restart_every_n_solves = max(
            0,
            int(browser_personal_fresh_restart_every_n_solves if browser_personal_fresh_restart_every_n_solves is not None else 10),
        )
    except Exception:
        return {"success": False, "message": "重置码数必须是整数，0 表示禁用"}

    if captcha_method == "remote_browser":
        if not (remote_browser_base_url or "").strip():
            return {"success": False, "message": "remote_browser 模式需要配置远程打码服务地址"}
        if not (remote_browser_api_key or "").strip():
            return {"success": False, "message": "remote_browser 模式需要配置远程打码服务 API Key"}

    await db.update_captcha_config(
        captcha_method=captcha_method,
        yescaptcha_api_key=yescaptcha_api_key,
        yescaptcha_base_url=yescaptcha_base_url,
        yescaptcha_task_type=yescaptcha_task_type,
        capmonster_api_key=capmonster_api_key,
        capmonster_base_url=capmonster_base_url,
        ezcaptcha_api_key=ezcaptcha_api_key,
        ezcaptcha_base_url=ezcaptcha_base_url,
        capsolver_api_key=capsolver_api_key,
        capsolver_base_url=capsolver_base_url,
        remote_browser_base_url=remote_browser_base_url,
        remote_browser_api_key=remote_browser_api_key,
        remote_browser_timeout=remote_browser_timeout,
        browser_proxy_enabled=browser_proxy_enabled,
        browser_proxy_url=browser_proxy_url if browser_proxy_enabled else None,
        browser_count=browser_count,
        personal_project_pool_size=personal_project_pool_size,
        personal_max_resident_tabs=personal_max_resident_tabs,
        browser_personal_fresh_restart_every_n_solves=browser_personal_fresh_restart_every_n_solves,
        personal_idle_tab_ttl_seconds=personal_idle_tab_ttl_seconds
    )

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    runtime_prepare_started = False
    runtime_prepare_message = ""
    runtime_status_method = None

    if captcha_method in {"browser", "personal"}:
        runtime_status_method = captcha_method
        runtime_prepare_started = _schedule_captcha_runtime_prepare(captcha_method)
        if captcha_method == "browser":
            runtime_prepare_message = (
                "已开始准备有头浏览器打码运行环境，安装进度将自动显示。"
            )
        else:
            runtime_prepare_message = (
                "已开始准备内置浏览器打码运行环境，安装进度将自动显示。"
            )

    return {
        "success": True,
        "message": "验证码配置更新成功",
        "runtime_prepare_started": runtime_prepare_started,
        "runtime_prepare_message": runtime_prepare_message,
        "runtime_status_method": runtime_status_method,
    }


@router.get("/api/captcha/runtime-status")
async def get_captcha_runtime_status(
    method: str = "browser",
    token: str = Depends(verify_admin_token)
):
    """Get background browser runtime preparation status."""
    runtime_method = _normalize_runtime_method(method)
    task = captcha_runtime_prepare_tasks.get(runtime_method)
    status = get_runtime_status(runtime_method)
    status["method"] = runtime_method
    status["task_running"] = bool(task and not task.done())
    return status


@router.get("/api/captcha/config")
async def get_captcha_config(token: str = Depends(verify_admin_token)):
    """Get captcha configuration"""
    captcha_config = await db.get_captcha_config()
    return {
        "captcha_method": captcha_config.captcha_method,
        "yescaptcha_api_key": captcha_config.yescaptcha_api_key,
        "yescaptcha_base_url": captcha_config.yescaptcha_base_url,
        "yescaptcha_task_type": captcha_config.yescaptcha_task_type,
        "capmonster_api_key": captcha_config.capmonster_api_key,
        "capmonster_base_url": captcha_config.capmonster_base_url,
        "ezcaptcha_api_key": captcha_config.ezcaptcha_api_key,
        "ezcaptcha_base_url": captcha_config.ezcaptcha_base_url,
        "capsolver_api_key": captcha_config.capsolver_api_key,
        "capsolver_base_url": captcha_config.capsolver_base_url,
        "remote_browser_base_url": captcha_config.remote_browser_base_url,
        "remote_browser_api_key": captcha_config.remote_browser_api_key,
        "remote_browser_timeout": captcha_config.remote_browser_timeout,
        "browser_proxy_enabled": captcha_config.browser_proxy_enabled,
        "browser_proxy_url": captcha_config.browser_proxy_url or "",
        "browser_count": captcha_config.browser_count,
        "personal_project_pool_size": captcha_config.personal_project_pool_size,
        "personal_max_resident_tabs": captcha_config.personal_max_resident_tabs,
        "browser_personal_fresh_restart_every_n_solves": captcha_config.browser_personal_fresh_restart_every_n_solves,
        "personal_idle_tab_ttl_seconds": captcha_config.personal_idle_tab_ttl_seconds
    }


@router.post("/api/captcha/score-test")
async def test_captcha_score(
    _request: Optional[CaptchaScoreTestRequest] = None,
    _token: str = Depends(verify_admin_token)
):
    """使用当前打码方式获取 token，并提交到 antcpt 校验分数。"""
    req = request or CaptchaScoreTestRequest()
    website_url = (req.website_url or "https://antcpt.com/score_detector/").strip()
    website_key = (req.website_key or "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf").strip()
    action = (req.action or "homepage").strip()
    verify_url = (req.verify_url or "https://antcpt.com/score_detector/verify.php").strip()
    enterprise = bool(req.enterprise)

    started_at = time.time()
    captcha_config = await db.get_captcha_config()
    captcha_method = (captcha_config.captcha_method or config.captcha_method or "").strip().lower()
    browser_proxy_enabled = bool(captcha_config.browser_proxy_enabled)
    browser_proxy_url = captcha_config.browser_proxy_url or ""

    token_value: Optional[str] = None
    fingerprint: Optional[Dict[str, Any]] = None
    token_elapsed_ms = 0
    verify_elapsed_ms = 0
    verify_http_status = None
    verify_result: Dict[str, Any] = {}
    verify_headers: Dict[str, str] = {}
    verify_proxy_used = False
    verify_proxy_source = "none"
    verify_proxy_url = ""
    verify_impersonate = "chrome120"
    page_verify_only = captcha_method in {"browser", "personal", "remote_browser"}
    verify_mode = "browser_page" if page_verify_only else "server_post"

    try:
        token_start = time.time()
        if captcha_method == "browser":
            from ..services.browser_captcha import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(db)
            score_payload, browser_id = await service.get_custom_score(
                website_url=website_url,
                website_key=website_key,
                verify_url=verify_url,
                action=action,
                enterprise=enterprise
            )
            if isinstance(score_payload, dict):
                token_value = score_payload.get("token")
                verify_elapsed_ms = int(score_payload.get("verify_elapsed_ms") or 0)
                verify_http_status = score_payload.get("verify_http_status")
                verify_result = score_payload.get("verify_result") if isinstance(score_payload.get("verify_result"), dict) else {}
                verify_mode = score_payload.get("verify_mode") or "browser_page"
                score_token_elapsed = score_payload.get("token_elapsed_ms")
                if isinstance(score_token_elapsed, (int, float)):
                    token_elapsed_ms = int(score_token_elapsed)
            if token_value:
                fingerprint = await service.get_fingerprint(browser_id)
                verify_proxy_used = bool(browser_proxy_enabled and browser_proxy_url)
                verify_proxy_source = "captcha_browser_proxy" if verify_proxy_used else "browser_direct"
                verify_proxy_url = browser_proxy_url if verify_proxy_used else ""
        elif captcha_method == "personal":
            from ..services.browser_captcha_personal import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(db)
            score_payload = await service.get_custom_score(
                website_url=website_url,
                website_key=website_key,
                verify_url=verify_url,
                action=action,
                enterprise=enterprise
            )
            if isinstance(score_payload, dict):
                token_value = score_payload.get("token")
                verify_elapsed_ms = int(score_payload.get("verify_elapsed_ms") or 0)
                verify_http_status = score_payload.get("verify_http_status")
                verify_result = score_payload.get("verify_result") if isinstance(score_payload.get("verify_result"), dict) else {}
                verify_mode = score_payload.get("verify_mode") or "browser_page"
                score_token_elapsed = score_payload.get("token_elapsed_ms")
                if isinstance(score_token_elapsed, (int, float)):
                    token_elapsed_ms = int(score_token_elapsed)
            if token_value:
                fingerprint = service.get_last_fingerprint()
                verify_proxy_used = bool(browser_proxy_enabled and browser_proxy_url)
                verify_proxy_source = "captcha_browser_proxy" if verify_proxy_used else "browser_direct"
                verify_proxy_url = browser_proxy_url if verify_proxy_used else ""
        elif captcha_method == "remote_browser":
            score_payload = await _score_test_with_remote_browser_service(
                website_url=website_url,
                website_key=website_key,
                verify_url=verify_url,
                action=action,
                enterprise=enterprise,
            )
            if isinstance(score_payload, dict):
                if score_payload.get("success") is False:
                    raise RuntimeError(score_payload.get("message") or "远程打码分数测试失败")
                token_value = score_payload.get("token")
                verify_elapsed_ms = int(score_payload.get("verify_elapsed_ms") or 0)
                verify_http_status = score_payload.get("verify_http_status")
                verify_result = score_payload.get("verify_result") if isinstance(score_payload.get("verify_result"), dict) else {}
                verify_mode = score_payload.get("verify_mode") or "remote_browser_page"
                score_token_elapsed = score_payload.get("token_elapsed_ms")
                if isinstance(score_token_elapsed, (int, float)):
                    token_elapsed_ms = int(score_token_elapsed)
                fingerprint = score_payload.get("fingerprint") if isinstance(score_payload.get("fingerprint"), dict) else None
        elif captcha_method in SUPPORTED_API_CAPTCHA_METHODS:
            if captcha_method == "capsolver" and "antcpt.com" in website_url:
                # CapSolver specifically blocks antcpt.com. Test against labs.google to verify API key config.
                token_value = await _solve_recaptcha_with_api_service(
                    method=captcha_method,
                    website_url="https://labs.google/",
                    website_key="6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
                    action="IMAGE_GENERATION",
                    enterprise=True
                )
                if token_value:
                    if token_elapsed_ms <= 0:
                        token_elapsed_ms = int((time.time() - token_start) * 1000)
                    return {
                        "success": True,
                        "message": "CapSolver不支持antcpt。已成功用 Google Labs 测试连通性",
                        "captcha_method": captcha_method,
                        "website_url": "https://labs.google/",
                        "website_key": "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
                        "action": "IMAGE_GENERATION",
                        "verify_url": "",
                        "enterprise": True,
                        "token_acquired": True,
                        "token_preview": _mask_token(token_value),
                        "token_elapsed_ms": token_elapsed_ms,
                        "verify_elapsed_ms": 0,
                        "verify_http_status": 200,
                        "score": 0.9,
                        "verify_result": {"success": True, "message": "跳过分数校验"},
                        "verify_request_meta": {},
                        "browser_proxy_enabled": browser_proxy_enabled,
                        "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
                        "fingerprint": fingerprint,
                        "elapsed_ms": int((time.time() - started_at) * 1000)
                    }
            else:
                token_value = await _solve_recaptcha_with_api_service(
                    method=captcha_method,
                    website_url=website_url,
                    website_key=website_key,
                    action=action,
                    enterprise=enterprise
                )
        else:
            return {
                "success": False,
                "message": f"当前打码方式不支持分数测试: {captcha_method}",
                "captcha_method": captcha_method,
                "website_url": website_url,
                "website_key": website_key,
                "action": action,
                "verify_url": verify_url,
                "enterprise": enterprise,
                "token_acquired": False,
                "elapsed_ms": int((time.time() - started_at) * 1000)
            }
        if token_elapsed_ms <= 0:
            token_elapsed_ms = int((time.time() - token_start) * 1000)

        # 远程有头打码的 custom-score 可能由页面内直接完成校验，
        # 在部分实现里不会显式回传 token，本地按 verify_result 兜底判定。
        if captcha_method == "remote_browser" and not token_value and isinstance(verify_result, dict):
            if verify_result.get("success") is True:
                token_value = verify_result.get("token") or verify_result.get("gRecaptchaResponse") or "__verified_by_remote__"

        if not token_value:
            return {
                "success": False,
                "message": "未获取到 reCAPTCHA token",
                "captcha_method": captcha_method,
                "website_url": website_url,
                "website_key": website_key,
                "action": action,
                "verify_url": verify_url,
                "enterprise": enterprise,
                "token_acquired": False,
                "token_elapsed_ms": token_elapsed_ms,
                "browser_proxy_enabled": browser_proxy_enabled,
                "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
                "fingerprint": fingerprint,
                "elapsed_ms": int((time.time() - started_at) * 1000)
            }

        if verify_mode == "server_post" and not page_verify_only:
            verify_start = time.time()
            verify_headers = {
                "accept": "application/json, text/javascript, */*; q=0.01",
                "content-type": "application/json",
                "origin": "https://antcpt.com",
                "referer": website_url,
                "x-requested-with": "XMLHttpRequest",
            }
            if isinstance(fingerprint, dict):
                ua = (fingerprint.get("user_agent") or "").strip()
                lang = (fingerprint.get("accept_language") or "").strip()
                sec_ch_ua = (fingerprint.get("sec_ch_ua") or "").strip()
                sec_ch_ua_mobile = (fingerprint.get("sec_ch_ua_mobile") or "").strip()
                sec_ch_ua_platform = (fingerprint.get("sec_ch_ua_platform") or "").strip()

                if ua:
                    verify_headers["user-agent"] = ua
                if lang:
                    verify_headers["accept-language"] = lang if "," in lang else f"{lang},zh;q=0.9"
                if sec_ch_ua:
                    verify_headers["sec-ch-ua"] = sec_ch_ua
                if sec_ch_ua_mobile:
                    verify_headers["sec-ch-ua-mobile"] = sec_ch_ua_mobile
                if sec_ch_ua_platform:
                    verify_headers["sec-ch-ua-platform"] = sec_ch_ua_platform

            if verify_headers.get("user-agent"):
                for header_name, header_value in _guess_client_hints_from_user_agent(
                    verify_headers.get("user-agent", "")
                ).items():
                    if header_value and not verify_headers.get(header_name):
                        verify_headers[header_name] = header_value
                verify_impersonate = _guess_impersonate_from_user_agent(verify_headers.get("user-agent", ""))

            verify_proxies, verify_proxy_used, verify_proxy_source, verify_proxy_url = (
                await _resolve_score_test_verify_proxy(
                    captcha_method=captcha_method,
                    browser_proxy_enabled=browser_proxy_enabled,
                    browser_proxy_url=browser_proxy_url
                )
            )

            async with AsyncSession() as session:
                verify_resp = await session.post(
                    verify_url,
                    json={"g-recaptcha-response": token_value},
                    headers=verify_headers,
                    proxies=verify_proxies,
                    impersonate=verify_impersonate,
                    timeout=30
                )
            verify_elapsed_ms = int((time.time() - verify_start) * 1000)
            verify_http_status = verify_resp.status_code

            try:
                verify_result = verify_resp.json()
            except Exception:
                verify_result = {"raw": verify_resp.text}
        else:
            verify_headers = {
                "origin": "https://antcpt.com",
                "referer": website_url,
                "x-requested-with": "XMLHttpRequest",
            }
            if isinstance(fingerprint, dict):
                verify_headers.update({
                    "user-agent": fingerprint.get("user_agent", ""),
                    "accept-language": fingerprint.get("accept_language", ""),
                    "sec-ch-ua": fingerprint.get("sec_ch_ua", ""),
                    "sec-ch-ua-mobile": fingerprint.get("sec_ch_ua_mobile", ""),
                    "sec-ch-ua-platform": fingerprint.get("sec_ch_ua_platform", ""),
                })

        verify_success = bool(verify_result.get("success")) if isinstance(verify_result, dict) else False
        score_value = verify_result.get("score") if isinstance(verify_result, dict) else None

        return {
            "success": verify_success,
            "message": "分数校验成功" if verify_success else "分数校验未通过",
            "captcha_method": captcha_method,
            "website_url": website_url,
            "website_key": website_key,
            "action": action,
            "verify_url": verify_url,
            "enterprise": enterprise,
            "token_acquired": True,
            "token_preview": _mask_token(token_value),
            "token_elapsed_ms": token_elapsed_ms,
            "verify_elapsed_ms": verify_elapsed_ms,
            "verify_http_status": verify_http_status,
            "score": score_value,
            "verify_result": verify_result,
            "verify_request_meta": {
                "mode": verify_mode,
                "proxy_used": verify_proxy_used,
                "user_agent": verify_headers.get("user-agent", ""),
                "accept_language": verify_headers.get("accept-language", ""),
                "sec_ch_ua": verify_headers.get("sec-ch-ua", ""),
                "sec_ch_ua_mobile": verify_headers.get("sec-ch-ua-mobile", ""),
                "sec_ch_ua_platform": verify_headers.get("sec-ch-ua-platform", ""),
                "origin": verify_headers.get("origin", ""),
                "referer": verify_headers.get("referer", ""),
                "x_requested_with": verify_headers.get("x-requested-with", ""),
                "proxy_source": verify_proxy_source,
                "proxy_url": verify_proxy_url,
                "impersonate": verify_impersonate,
            },
            "browser_proxy_enabled": browser_proxy_enabled,
            "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
            "fingerprint": fingerprint,
            "elapsed_ms": int((time.time() - started_at) * 1000)
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"分数测试失败: {str(e)}",
            "captcha_method": captcha_method,
            "website_url": website_url,
            "website_key": website_key,
            "action": action,
            "verify_url": verify_url,
            "enterprise": enterprise,
            "token_acquired": bool(token_value),
            "token_preview": _mask_token(token_value),
            "token_elapsed_ms": token_elapsed_ms,
            "verify_elapsed_ms": verify_elapsed_ms,
            "verify_http_status": verify_http_status,
            "verify_result": verify_result,
            "verify_request_meta": {
                "mode": verify_mode,
                "proxy_used": verify_proxy_used,
                "user_agent": verify_headers.get("user-agent", ""),
                "accept_language": verify_headers.get("accept-language", ""),
                "sec_ch_ua": verify_headers.get("sec-ch-ua", ""),
                "sec_ch_ua_mobile": verify_headers.get("sec-ch-ua-mobile", ""),
                "sec_ch_ua_platform": verify_headers.get("sec-ch-ua-platform", ""),
                "origin": verify_headers.get("origin", ""),
                "referer": verify_headers.get("referer", ""),
                "x_requested_with": verify_headers.get("x-requested-with", ""),
                "proxy_source": verify_proxy_source,
                "proxy_url": verify_proxy_url,
                "impersonate": verify_impersonate,
            },
            "browser_proxy_enabled": browser_proxy_enabled,
            "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
            "fingerprint": fingerprint,
            "elapsed_ms": int((time.time() - started_at) * 1000)
        }


# ========== Plugin Configuration Endpoints ==========

async def _verify_plugin_connection_token(authorization: Optional[str]) -> None:
    plugin_config = await db.get_plugin_config()
    provided_token = None
    if authorization:
        if authorization.startswith("Bearer "):
            provided_token = authorization[7:]
        else:
            provided_token = authorization
    if not plugin_config.connection_token or provided_token != plugin_config.connection_token:
        raise HTTPException(status_code=401, detail="Invalid connection token")


@router.get("/api/plugin/config")
async def get_plugin_config(request: Request, token: str = Depends(verify_admin_token)):
    """Get plugin configuration"""
    plugin_config = await db.get_plugin_config()

    # Get the actual domain and port from the request
    # This allows the connection URL to reflect the user's actual access path
    host_header = request.headers.get("host", "")

    # Generate connection URL based on actual request
    if host_header:
        # Use the actual domain/IP and port from the request
        connection_url = f"http://{host_header}/api/plugin/update-token"
    else:
        # Fallback to config-based URL
        from ..core.config import config
        server_host = config.server_host
        server_port = config.server_port

        if server_host == "0.0.0.0":
            connection_url = f"http://127.0.0.1:{server_port}/api/plugin/update-token"
        else:
            connection_url = f"http://{server_host}:{server_port}/api/plugin/update-token"

    return {
        "success": True,
        "config": {
            "connection_token": plugin_config.connection_token,
            "connection_url": connection_url,
            "auto_enable_on_update": plugin_config.auto_enable_on_update
        }
    }


@router.post("/api/plugin/config")
async def update_plugin_config(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update plugin configuration"""
    connection_token = request.get("connection_token", "")
    auto_enable_on_update = request.get("auto_enable_on_update", True)  # 默认开启

    # Generate random token if empty
    if not connection_token:
        connection_token = secrets.token_urlsafe(32)

    await db.update_plugin_config(
        connection_token=connection_token,
        auto_enable_on_update=auto_enable_on_update
    )

    return {
        "success": True,
        "message": "插件配置更新成功",
        "connection_token": connection_token,
        "auto_enable_on_update": auto_enable_on_update
    }


async def _upsert_plugin_account(request: dict):
    """Add or update one account after the caller has authenticated."""
    plugin_config = await db.get_plugin_config()

    # Extract session token from request
    session_token = request.get("session_token")

    if not session_token:
        raise HTTPException(status_code=400, detail="Missing session_token")

    # Step 1: Convert ST to AT to get user info (including email)
    try:
        result = await token_manager.flow_client.st_to_at(session_token)
        at = result["access_token"]
        expires = result.get("expires")
        user_info = result.get("user", {})
        email = user_info.get("email", "")

        if not email:
            raise HTTPException(status_code=400, detail="Failed to get email from session token")

        # Parse expiration time
        from datetime import datetime
        at_expires = None
        if expires:
            try:
                at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            except:
                pass

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid session token: {str(e)}")

    # Step 2: Check if token with this email exists
    existing_token = await db.get_token_by_email(email)

    if existing_token:
        # Update existing token
        try:
            # Update token
            await token_manager.update_token(
                token_id=existing_token.id,
                st=session_token,
                at=at,
                at_expires=at_expires,
                extension_route_key=request.get("extension_route_key"),
                protocol_mode=request.get("protocol_mode"),
                google_cookies=request.get("google_cookies"),
                login_account=request.get("login_account"),
                login_password=request.get("login_password"),
                proxy_url=request.get("proxy_url"),
                auto_refresh_enabled=request.get("auto_refresh_enabled"),
                refresh_interval_minutes=request.get("refresh_interval_minutes"),
            )

            # Check if auto-enable is enabled and token is disabled
            if plugin_config.auto_enable_on_update and not existing_token.is_active:
                await token_manager.enable_token(existing_token.id)
                return {
                    "success": True,
                    "message": f"Token updated and auto-enabled for {email}",
                    "action": "updated",
                    "added": 0,
                    "updated": 1,
                    "auto_enabled": True,
                    "email": email,
                    "token_id": existing_token.id,
                }

            return {
                "success": True,
                "message": f"Token updated for {email}",
                "action": "updated",
                "added": 0,
                "updated": 1,
                "email": email,
                "token_id": existing_token.id,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update token: {str(e)}")
    else:
        # Add new token
        try:
            new_token = await token_manager.add_token(
                st=session_token,
                remark="Added by Chrome Extension",
                extension_route_key=request.get("extension_route_key"),
                protocol_mode=request.get("protocol_mode", "session"),
                google_cookies=request.get("google_cookies"),
                login_account=request.get("login_account"),
                login_password=request.get("login_password"),
                proxy_url=request.get("proxy_url"),
                auto_refresh_enabled=request.get("auto_refresh_enabled", True),
                refresh_interval_minutes=request.get("refresh_interval_minutes", 120),
            )

            return {
                "success": True,
                "message": f"Token added for {new_token.email}",
                "action": "added",
                "added": 1,
                "updated": 0,
                "token_id": new_token.id,
                "email": new_token.email,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to add token: {str(e)}")


@router.post("/api/plugin/update-token")
async def plugin_update_token(request: dict, authorization: Optional[str] = Header(None)):
    """Receive a token update authenticated by the plugin connection token."""
    await _verify_plugin_connection_token(authorization)
    return await _upsert_plugin_account(request)


@router.post("/api/plugin/import-current-account")
async def plugin_import_current_account(
    request: dict,
    authorization: Optional[str] = Header(None),
):
    """Import the signed-in account using a bound plugin session or legacy API key."""
    provided = str(authorization or "").strip()
    if provided.lower().startswith("bearer "):
        provided = provided[7:].strip()
    from ..services.extension_pairing import get_extension_pairing_service
    binding = await get_extension_pairing_service().verify_session(provided)
    if binding is None and not AuthManager.verify_api_key(provided):
        raise HTTPException(status_code=401, detail="Invalid plugin session")
    normalized_request = dict(request)
    if binding is not None:
        normalized_request["extension_route_key"] = binding.route_key
        normalized_request["client_label"] = binding.client_label
    if normalized_request.get("google_cookies") and not normalized_request.get("protocol_mode"):
        normalized_request["protocol_mode"] = "protocol"
    return await _upsert_plugin_account(normalized_request)


@router.post("/api/admin/extension-pairing")
async def issue_extension_pairing(
    request: Optional[dict] = None,
    token: str = Depends(verify_admin_token),
):
    from ..services.extension_pairing import get_extension_pairing_service
    instance_id = str((request or {}).get("instance_id") or "").strip()
    issue = await get_extension_pairing_service().issue(instance_id=instance_id)
    return {
        "pairing_handle": issue.pairing_handle,
        "instance_id": issue.instance_id,
        "route_key": issue.route_key,
        "client_label": issue.client_label,
        "capability_marker": issue.capability_marker,
        "expires_at": issue.expires_at,
    }


@router.delete("/api/admin/extension-sessions/{plugin_session_id}")
async def revoke_extension_session(
    plugin_session_id: str,
    token: str = Depends(verify_admin_token),
):
    from ..services.extension_pairing import get_extension_pairing_service
    revoked = await get_extension_pairing_service().revoke_public_id(plugin_session_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Plugin session not found")
    return {"success": True, "session_id": plugin_session_id}


@router.post("/api/plugin/pair/exchange")
async def exchange_extension_pairing(request: dict):
    from ..services.extension_pairing import get_extension_pairing_service
    try:
        session = await get_extension_pairing_service().exchange(
            str(request.get("pairing_handle") or "")
        )
    except PermissionError:
        raise HTTPException(status_code=401, detail="Invalid or expired pairing handle")
    return {
        "plugin_session": session.plugin_session,
        "plugin_session_id": session.plugin_session_id,
        "instance_id": session.instance_id,
        "route_key": session.route_key,
        "client_label": session.client_label,
        "capability_marker": session.capability_marker,
        "expires_at": session.expires_at,
    }


@router.post("/api/plugin/check-tokens")
async def plugin_check_tokens(request: Optional[dict] = None, authorization: Optional[str] = Header(None)):
    """Return token status for external syncers using the plugin connection token."""
    await _verify_plugin_connection_token(authorization)

    request = request or {}
    requested_emails = request.get("emails") if isinstance(request, dict) else None
    email_filter = set()
    if isinstance(requested_emails, list):
        email_filter = {
            str(email or "").strip().lower()
            for email in requested_emails
            if str(email or "").strip()
        }

    rows = await db.get_all_tokens_with_stats()
    tokens = []
    for row in rows:
        email = str(row.get("email") or "").strip()
        if email_filter and email.lower() not in email_filter:
            continue
        token_obj = None
        try:
            token_obj = Token(**row)
        except Exception:
            token_obj = None
        needs_refresh = token_manager.needs_at_refresh(token_obj) if token_obj else True
        tokens.append({
            "id": row.get("id"),
            "email": email,
            "is_active": bool(row.get("is_active")),
            "needs_refresh": needs_refresh,
            "at_expires": row.get("at_expires").isoformat() if hasattr(row.get("at_expires"), "isoformat") else row.get("at_expires"),
            "last_used_at": row.get("last_used_at").isoformat() if hasattr(row.get("last_used_at"), "isoformat") else row.get("last_used_at"),
            "protocol_mode": row.get("protocol_mode") or "session",
            "auto_refresh_enabled": bool(row.get("auto_refresh_enabled", True)),
            "refresh_interval_minutes": row.get("refresh_interval_minutes") or 120,
            "last_st_refresh_at": (
                row.get("last_st_refresh_at").isoformat()
                if hasattr(row.get("last_st_refresh_at"), "isoformat")
                else row.get("last_st_refresh_at")
            ),
            "last_st_refresh_result": row.get("last_st_refresh_result") or "",
            "credits": row.get("credits", 0),
        })

    return {"success": True, "tokens": tokens}
