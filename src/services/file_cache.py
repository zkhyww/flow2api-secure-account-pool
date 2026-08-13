"""File caching service"""
import os
import asyncio
import base64
import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
import mimetypes
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from urllib.parse import unquote, urljoin, urlparse
from curl_cffi import CurlOpt
from curl_cffi.const import CurlECode
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import (
    DNSError as CurlDNSError,
    HTTPError as CurlHTTPError,
    SSLError as CurlSSLError,
)
from ..core.config import config
from ..core.logger import debug_logger


_REMOTE_MEDIA_EXACT_HOSTS = {"flow-content.google"}
_REMOTE_MEDIA_HOST_SUFFIXES = (
    "googleusercontent.com",
    "googleapis.com",
)
_REMOTE_MEDIA_MAX_REDIRECTS = 5


class FileCache:
    """File caching service for videos"""

    def __init__(
        self,
        cache_dir: str = "tmp",
        default_timeout: int = 7200,
        proxy_manager=None,
        flow_client=None,
    ):
        """
        Initialize file cache

        Args:
            cache_dir: Cache directory path
            default_timeout: Default cache timeout in seconds (default: 2 hours)
            proxy_manager: ProxyManager instance for downloading files
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.default_timeout = max(0, int(default_timeout))
        self.proxy_manager = proxy_manager
        self.flow_client = flow_client
        self._cleanup_task = None
        self._download_locks: Dict[str, asyncio.Lock] = {}
        self.max_remote_media_bytes = max(
            1,
            int(os.getenv("FLOW2API_REMOTE_MEDIA_MAX_BYTES", str(64 * 1024 * 1024))),
        )

    def _is_cleanup_disabled(self) -> bool:
        return self.default_timeout <= 0

    def _get_request_fingerprint(self) -> Optional[Dict[str, Any]]:
        """读取当前请求链路里绑定的浏览器指纹。"""
        if not self.flow_client or not hasattr(self.flow_client, "get_request_fingerprint"):
            return None

        try:
            fingerprint = self.flow_client.get_request_fingerprint()
            if isinstance(fingerprint, dict) and fingerprint:
                return fingerprint
        except Exception:
            debug_logger.log_warning("Get request fingerprint failed")

        return None

    async def _resolve_download_proxy(
        self,
        media_type: str,
        fingerprint: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """根据媒体类型解析下载代理地址。"""
        if isinstance(fingerprint, dict):
            fingerprint_proxy = str(fingerprint.get("proxy_url") or "").strip()
            if fingerprint_proxy:
                return fingerprint_proxy

        if not self.proxy_manager:
            return None

        try:
            # 媒体下载（图片/视频）优先使用独立的上传/下载代理
            if media_type in ("image", "video") and hasattr(self.proxy_manager, "get_media_proxy_url"):
                return await self.proxy_manager.get_media_proxy_url()

            # 其他下载走请求代理
            if hasattr(self.proxy_manager, "get_request_proxy_url"):
                return await self.proxy_manager.get_request_proxy_url()

            # 向后兼容旧实现
            if hasattr(self.proxy_manager, "get_proxy_url"):
                return await self.proxy_manager.get_proxy_url()
        except Exception as exc:
            debug_logger.log_warning("Resolve download proxy failed")
            raise ValueError("remote_media_proxy_unavailable") from exc

        return None

    def _guess_extension(self, url: str, media_type: str) -> str:
        """尽量保留原始扩展名，未知时回退到默认值。"""
        path = urlparse(url).path or ""
        guessed, _ = mimetypes.guess_type(path)
        suffix = Path(path).suffix.lower()

        if media_type == "video":
            if suffix in {".mp4", ".mov", ".webm", ".mkv", ".m4v"}:
                return suffix
            if guessed == "video/webm":
                return ".webm"
            if guessed == "video/quicktime":
                return ".mov"
            return ".mp4"

        if media_type == "image":
            if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp"}:
                return suffix
            if guessed == "image/png":
                return ".png"
            if guessed == "image/webp":
                return ".webp"
            if guessed == "image/gif":
                return ".gif"
            if guessed == "image/avif":
                return ".avif"
            if guessed == "image/bmp":
                return ".bmp"
            return ".jpg"

        return suffix

    def _build_download_headers(
        self,
        media_type: str,
        fingerprint: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """构建媒体下载请求头，优先复用当前打码浏览器指纹。"""
        headers = {
            "Accept": (
                "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                if media_type == "image"
                else "*/*"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Referer": "https://labs.google/",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
        }

        if media_type == "image":
            headers["Sec-Fetch-Dest"] = "image"
        else:
            headers["Sec-Fetch-Dest"] = "video"

        if isinstance(fingerprint, dict):
            if fingerprint.get("user_agent"):
                headers["User-Agent"] = str(fingerprint["user_agent"])
            if fingerprint.get("accept_language"):
                headers["Accept-Language"] = str(fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = str(fingerprint["sec_ch_ua"])
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = str(fingerprint["sec_ch_ua_mobile"])
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = str(fingerprint["sec_ch_ua_platform"])

        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        return headers

    def _write_cached_content(self, file_path: Path, content: bytes):
        """先写临时文件，再原子替换，避免并发读到半截文件。"""
        temp_path = file_path.with_suffix(f"{file_path.suffix}.part")
        try:
            with open(temp_path, "wb") as f:
                f.write(content)
            temp_path.replace(file_path)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    async def start_cleanup_task(self):
        """Start background cleanup task"""
        if self._is_cleanup_disabled():
            debug_logger.log_info("Cache cleanup disabled (timeout <= 0), skip starting cleanup task")
            return False
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            return True
        return True

    async def stop_cleanup_task(self):
        """Stop background cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def refresh_cleanup_task(self) -> bool:
        """Apply the latest timeout setting to the cleanup background task."""
        if self._is_cleanup_disabled():
            await self.stop_cleanup_task()
            return False
        return await self.start_cleanup_task()

    async def _cleanup_loop(self):
        """Background task to clean up expired files"""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                await self._cleanup_expired_files()
            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_logger.log_error(
                    error_message=f"Cleanup task error: {str(e)}",
                    status_code=0,
                    response_text=""
                )

    async def _cleanup_expired_files(self):
        """Remove expired cache files"""
        try:
            timeout = self.get_timeout()
            if timeout <= 0:
                return
            current_time = time.time()
            removed_count = 0

            for file_path in self.cache_dir.iterdir():
                timeout = self.get_timeout()
                if timeout <= 0:
                    debug_logger.log_info("Cache cleanup disabled during cleanup pass, stop deleting files")
                    break
                if file_path.is_file():
                    # Check file age
                    file_age = current_time - file_path.stat().st_mtime
                    if file_age > timeout:
                        try:
                            file_path.unlink()
                            removed_count += 1
                        except Exception:
                            pass

            if removed_count > 0:
                debug_logger.log_info(f"Cleanup: removed {removed_count} expired cache files")

        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to cleanup expired files: {str(e)}",
                status_code=0,
                response_text=""
            )

    def _generate_cache_filename(self, url: str, media_type: str) -> str:
        """Generate unique filename for cached file"""
        # Use URL hash as filename
        url_hash = hashlib.md5(url.encode()).hexdigest()
        ext = self._guess_extension(url, media_type)

        return f"{url_hash}{ext}"

    def _normalize_cache_error(self, error: Exception) -> str:
        """整理缓存错误，避免将底层命令异常直接暴露给用户。"""
        if isinstance(error, FileNotFoundError):
            missing_name = Path(getattr(error, "filename", "") or "curl").name or "curl"
            return f"本机未安装 {missing_name}"

        message = str(error or "").strip()
        if not message:
            return "未知错误"

        if message.startswith("Failed to cache file:"):
            message = message.split(":", 1)[1].strip() or "未知错误"

        return message

    @staticmethod
    def _remote_media_host_allowed(hostname: str) -> bool:
        host = str(hostname or "").strip().rstrip(".").lower()
        return host in _REMOTE_MEDIA_EXACT_HOSTS or any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in _REMOTE_MEDIA_HOST_SUFFIXES
        )

    @classmethod
    def is_safe_remote_media_passthrough_url(cls, url: str) -> bool:
        try:
            parsed = urlparse(str(url or "").strip())
            if parsed.scheme != "https" or not parsed.hostname:
                return False
            if parsed.username is not None or parsed.password is not None:
                return False
            host = parsed.hostname.rstrip(".").lower()
            if not cls._remote_media_host_allowed(host):
                return False
            return int(parsed.port or 443) == 443
        except (TypeError, ValueError):
            return False

    @classmethod
    def _validate_remote_media_target(cls, url: str) -> tuple[str, int]:
        try:
            parsed = urlparse(str(url or ""))
            hostname = parsed.hostname
        except (TypeError, ValueError) as exc:
            raise ValueError("remote_media_target_rejected") from exc
        if parsed.scheme != "https" or not hostname:
            raise ValueError("remote_media_target_rejected")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("remote_media_target_rejected")
        host = hostname.rstrip(".").lower()
        if not cls._remote_media_host_allowed(host):
            raise ValueError("remote_media_target_rejected")
        try:
            port = int(parsed.port or 443)
        except (TypeError, ValueError) as exc:
            raise ValueError("remote_media_target_rejected") from exc
        if port != 443:
            raise ValueError("remote_media_target_rejected")
        return host, port

    @classmethod
    def _resolve_safe_remote_target(cls, url: str) -> tuple[str, int, str]:
        try:
            parsed = urlparse(str(url or ""))
            hostname = parsed.hostname
        except (TypeError, ValueError) as exc:
            raise ValueError("remote_media_target_rejected") from exc
        if parsed.scheme != "https" or not hostname:
            raise ValueError("remote_media_target_rejected")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("remote_media_target_rejected")
        host = hostname.rstrip(".").lower()
        if not cls._remote_media_host_allowed(host):
            raise ValueError("remote_media_target_rejected")
        try:
            port = int(parsed.port or 443)
        except (TypeError, ValueError) as exc:
            raise ValueError("remote_media_target_rejected") from exc
        if port != 443:
            raise ValueError("remote_media_target_rejected")
        try:
            resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("remote_media_dns_failed") from exc
        addresses = []
        for family, _socktype, _proto, _canonname, sockaddr in resolved:
            if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
                continue
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except ValueError as exc:
                raise ValueError("remote_media_dns_no_public_address") from exc
            if not address.is_global:
                raise ValueError("remote_media_dns_non_public_rejected")
            addresses.append(address.compressed)
        if not addresses:
            raise ValueError("remote_media_dns_no_public_address")
        return host, port, addresses[0]

    @staticmethod
    def _format_curl_resolve_entry(host: str, port: int, address: str) -> str:
        parsed_address = ipaddress.ip_address(address)
        formatted_address = parsed_address.compressed
        if parsed_address.version == 6:
            formatted_address = f"[{formatted_address}]"
        return f"{host}:{port}:{formatted_address}"

    @staticmethod
    def _parse_pinned_http_proxy(
        proxy_url: str,
    ) -> tuple[str, int, Optional[str], Optional[str]]:
        try:
            parsed = urlparse(str(proxy_url or "").strip())
            if parsed.scheme.lower() != "http" or not parsed.hostname:
                raise ValueError("remote_media_proxy_unsupported")
            if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
                raise ValueError("remote_media_proxy_unsupported")
            port = int(parsed.port or 80)
            if not 1 <= port <= 65535:
                raise ValueError("remote_media_proxy_unsupported")
            username = unquote(parsed.username) if parsed.username is not None else None
            password = unquote(parsed.password) if parsed.password is not None else None
            if password is not None and username is None:
                raise ValueError("remote_media_proxy_unsupported")
            return parsed.hostname, port, username, password
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("remote_media_proxy_unsupported") from exc

    @staticmethod
    def _wrap_pinned_proxy_tls(raw_socket, origin_host: str):
        context = ssl.create_default_context()
        return context.wrap_socket(raw_socket, server_hostname=origin_host)

    def _download_remote_via_pinned_http_proxy_sync(
        self,
        url: str,
        *,
        host: str,
        port: int,
        address: Optional[str],
        proxy_url: str,
        headers: Dict[str, str],
        max_bytes: int,
        resolve_origin_via_proxy: bool = False,
    ) -> tuple[int, Dict[str, str], bytes]:
        proxy_host, proxy_port, username, password = self._parse_pinned_http_proxy(proxy_url)
        if resolve_origin_via_proxy:
            connect_host = host
        elif address:
            connect_host = f"[{address}]" if ":" in address else address
        else:
            raise ValueError("remote_media_dns_no_public_address")
        connect_authority = f"{connect_host}:{port}"
        tunnel_headers = {"Host": connect_authority}
        if username is not None:
            token = base64.b64encode(
                f"{username}:{password or ''}".encode("utf-8")
            ).decode("ascii")
            tunnel_headers["Proxy-Authorization"] = f"Basic {token}"

        connection = http.client.HTTPConnection(proxy_host, proxy_port, timeout=60)
        try:
            connection.set_tunnel(connect_host, port, headers=tunnel_headers)
            try:
                connection.connect()
            except Exception as exc:
                raise ValueError("remote_media_proxy_connect_failed") from exc

            if connection.sock is None:
                raise ValueError("remote_media_proxy_connect_failed")
            try:
                connection.sock = self._wrap_pinned_proxy_tls(connection.sock, host)
            except Exception as exc:
                raise ValueError("remote_media_proxy_tls_failed") from exc

            parsed = urlparse(url)
            target = parsed.path or "/"
            if parsed.query:
                target = f"{target}?{parsed.query}"

            request_headers: Dict[str, str] = {}
            blocked_headers = {
                "host",
                "connection",
                "proxy-authorization",
                "proxy-connection",
                "content-length",
                "transfer-encoding",
            }
            for raw_name, raw_value in headers.items():
                name = str(raw_name).strip()
                value = str(raw_value)
                if (
                    not name
                    or ":" in name
                    or "\r" in name
                    or "\n" in name
                    or "\r" in value
                    or "\n" in value
                ):
                    raise ValueError("remote_media_download_failed")
                if name.casefold() in blocked_headers:
                    continue
                request_headers[name] = value
            request_headers["Host"] = host
            request_headers["Connection"] = "close"
            request_headers["Accept-Encoding"] = "identity"

            try:
                connection.request("GET", target, headers=request_headers)
                response = connection.getresponse()
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError("remote_media_download_failed") from exc

            response_headers = {
                str(name).casefold(): str(value)
                for name, value in response.getheaders()
            }
            status_code = int(response.status)
            if status_code in {301, 302, 303, 307, 308}:
                return status_code, response_headers, b""
            if status_code != 200:
                return status_code, response_headers, b""

            declared_length = response.getheader("Content-Length")
            if declared_length is not None:
                try:
                    declared_bytes = int(declared_length)
                except (TypeError, ValueError) as exc:
                    raise ValueError("remote_media_download_failed") from exc
                if declared_bytes < 0:
                    raise ValueError("remote_media_download_failed")
                if declared_bytes > max_bytes:
                    raise ValueError("remote_media_too_large")

            streamed = bytearray()
            while True:
                read_size = min(64 * 1024, max_bytes - len(streamed) + 1)
                chunk = response.read(max(1, read_size))
                if not chunk:
                    break
                if len(streamed) + len(chunk) > max_bytes:
                    raise ValueError("remote_media_too_large")
                streamed.extend(chunk)
            return status_code, response_headers, bytes(streamed)
        finally:
            connection.close()

    async def _download_remote_via_pinned_http_proxy(
        self,
        url: str,
        *,
        host: str,
        port: int,
        address: Optional[str],
        proxy_url: str,
        headers: Dict[str, str],
        max_bytes: int,
        resolve_origin_via_proxy: bool = False,
    ) -> tuple[int, Dict[str, str], bytes]:
        return await asyncio.to_thread(
            self._download_remote_via_pinned_http_proxy_sync,
            url,
            host=host,
            port=port,
            address=address,
            proxy_url=proxy_url,
            headers=headers,
            max_bytes=max_bytes,
            resolve_origin_via_proxy=resolve_origin_via_proxy,
        )

    @staticmethod
    def _classify_direct_remote_exception(exc: Exception) -> str:
        code = getattr(exc, "code", 0)
        if isinstance(exc, CurlDNSError) or code == CurlECode.COULDNT_RESOLVE_HOST:
            return "remote_media_pinned_dns_failed"
        if isinstance(exc, CurlSSLError):
            return "remote_media_tls_failed"
        if isinstance(exc, CurlHTTPError):
            return "remote_media_http_failed"
        if code in {CurlECode.COULDNT_CONNECT, CurlECode.QUIC_CONNECT_ERROR}:
            return "remote_media_connect_failed"
        return "remote_media_download_failed"

    async def _download_remote_secure(
        self,
        url: str,
        *,
        proxy_url: Optional[str],
        headers: Dict[str, str],
        trust_env: bool = True,
        require_pinned_transport: bool = False,
    ) -> bytes:
        current_url = str(url or "")
        max_bytes = max(1, int(self.max_remote_media_bytes))
        if proxy_url:
            self._parse_pinned_http_proxy(proxy_url)
        proxy_remote_resolution = bool(proxy_url and require_pinned_transport)
        for redirect_count in range(_REMOTE_MEDIA_MAX_REDIRECTS + 1):
            if proxy_remote_resolution:
                host, port = self._validate_remote_media_target(current_url)
                address = None
            else:
                host, port, address = self._resolve_safe_remote_target(current_url)
            if proxy_url:
                status_code, response_headers, content = await self._download_remote_via_pinned_http_proxy(
                    current_url,
                    host=host,
                    port=port,
                    address=address,
                    proxy_url=proxy_url,
                    headers=headers,
                    max_bytes=max_bytes,
                    resolve_origin_via_proxy=proxy_remote_resolution,
                )
                if status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= _REMOTE_MEDIA_MAX_REDIRECTS:
                        raise ValueError("remote_media_redirect_limit")
                    location = str(response_headers.get("location") or "").strip()
                    if not location:
                        raise ValueError("remote_media_redirect_invalid")
                    current_url = urljoin(current_url, location)
                    if proxy_remote_resolution:
                        self._validate_remote_media_target(current_url)
                    else:
                        self._resolve_safe_remote_target(current_url)
                    continue
                if status_code != 200 or not content:
                    raise ValueError("remote_media_download_failed")
                return content

            streamed = bytearray()
            too_large = False

            def _capture_chunk(chunk: bytes) -> int:
                nonlocal too_large
                if not chunk:
                    return 0
                if len(streamed) + len(chunk) > max_bytes:
                    too_large = True
                    return CURL_WRITEFUNC_ERROR
                streamed.extend(chunk)
                return len(chunk)

            async with AsyncSession(
                curl_options={
                    CurlOpt.RESOLVE: [
                        self._format_curl_resolve_entry(host, port, address)
                    ]
                },
                trust_env=False,
            ) as session:
                try:
                    response = await session.get(
                        current_url,
                        timeout=60,
                        proxy=proxy_url,
                        headers=headers,
                        impersonate="chrome120",
                        verify=True,
                        allow_redirects=False,
                        content_callback=_capture_chunk,
                    )
                except Exception as exc:
                    if too_large:
                        raise ValueError("remote_media_too_large") from exc
                    raise ValueError(self._classify_direct_remote_exception(exc)) from exc
            if too_large:
                raise ValueError("remote_media_too_large")
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect_count >= _REMOTE_MEDIA_MAX_REDIRECTS:
                    raise ValueError("remote_media_redirect_limit")
                location = str(response.headers.get("location") or "").strip()
                if not location:
                    raise ValueError("remote_media_redirect_invalid")
                current_url = urljoin(current_url, location)
                self._resolve_safe_remote_target(current_url)
                continue
            if response.status_code != 200:
                raise ValueError("remote_media_http_failed")
            if not streamed:
                raise ValueError("remote_media_empty_download")
            return bytes(streamed)
        raise ValueError("remote_media_redirect_limit")

    async def download_and_cache(
        self,
        url: str,
        media_type: str,
        *,
        log_source_url: bool = True,
        require_direct_connection: bool = False,
    ) -> str:
        """
        Download file from URL and cache it locally

        Args:
            url: File URL to download
            media_type: 'image' or 'video'
            log_source_url: whether to include the source URL in the info log
            require_direct_connection: require pinned transport; an explicit HTTP proxy may resolve an allowlisted origin through CONNECT

        Returns:
            Local cache filename
        """
        filename = self._generate_cache_filename(url, media_type)
        file_path = self.cache_dir / filename
        download_lock = self._download_locks.setdefault(filename, asyncio.Lock())

        async with download_lock:
            # Check if already cached and not expired
            if file_path.exists():
                if self._is_cleanup_disabled():
                    return filename
                file_age = time.time() - file_path.stat().st_mtime
                if file_age < self.default_timeout:
                    debug_logger.log_info(f"Cache hit: {filename}")
                    return filename
                try:
                    file_path.unlink()
                except Exception:
                    pass

            # Download file. Compatibility adapters may disable source URL logging.
            if log_source_url:
                debug_logger.log_info(f"Downloading file from: {url}")

            fingerprint = self._get_request_fingerprint()
            proxy_url = await self._resolve_download_proxy(media_type, fingerprint=fingerprint)
            headers = self._build_download_headers(media_type, fingerprint=fingerprint)
            try:
                content = await self._download_remote_secure(
                    url,
                    proxy_url=proxy_url,
                    headers=headers,
                    trust_env=not require_direct_connection,
                    require_pinned_transport=require_direct_connection,
                )
                self._write_cached_content(file_path, content)
                debug_logger.log_info(
                    f"File cached (secure): {filename} ({len(content)} bytes)"
                )
                return filename
            except ValueError as exc:
                raise Exception(str(exc)) from exc
            except Exception as exc:
                raise Exception("remote_media_download_failed") from exc

    async def cache_base64_video(self, base64_data: str) -> str:
        """Cache base64 encoded video data to local file

        Args:
            base64_data: Base64 encoded video data (without data:video/... prefix)

        Returns:
            Local cache filename
        """
        import base64 as _b64
        import uuid as _uuid

        unique_id = hashlib.md5(f"{_uuid.uuid4()}{time.time()}".encode()).hexdigest()
        filename = f"{unique_id}.mp4"
        file_path = self.cache_dir / filename

        try:
            video_data = _b64.b64decode(base64_data)
            self._write_cached_content(file_path, video_data)
            debug_logger.log_info(f"Base64 video cached: {filename} ({len(video_data)} bytes)")
            return filename
        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to cache base64 video: {str(e)}",
                status_code=0,
                response_text=""
            )
            raise Exception(f"Failed to cache base64 video: {str(e)}")

    async def cache_base64_image(self, base64_data: str, resolution: str = "") -> str:
        """
        Cache base64 encoded image data to local file

        Args:
            base64_data: Base64 encoded image data (without data:image/... prefix)
            resolution: Resolution info for filename (e.g., "4K", "2K")

        Returns:
            Local cache filename
        """
        import base64
        import uuid

        # Generate unique filename
        unique_id = hashlib.md5(f"{uuid.uuid4()}{time.time()}".encode()).hexdigest()
        suffix = f"_{resolution}" if resolution else ""
        filename = f"{unique_id}{suffix}.jpg"
        file_path = self.cache_dir / filename

        try:
            # Decode base64 and save to file
            image_data = base64.b64decode(base64_data)
            with open(file_path, 'wb') as f:
                f.write(image_data)
            debug_logger.log_info(f"Base64 image cached: {filename} ({len(image_data)} bytes)")
            return filename
        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to cache base64 image: {str(e)}",
                status_code=0,
                response_text=""
            )
            raise Exception(f"Failed to cache base64 image: {str(e)}")

    def get_cache_path(self, filename: str) -> Path:
        """Get full path to cached file"""
        return self.cache_dir / filename

    def set_timeout(self, timeout: int):
        """Set cache timeout in seconds"""
        self.default_timeout = max(0, int(timeout))
        debug_logger.log_info(f"Cache timeout updated to {timeout} seconds")

    def get_timeout(self) -> int:
        """Get current cache timeout"""
        return self.default_timeout

    async def clear_all(self):
        """Clear all cached files"""
        try:
            removed_count = 0
            for file_path in self.cache_dir.iterdir():
                if file_path.is_file():
                    try:
                        file_path.unlink()
                        removed_count += 1
                    except Exception:
                        pass

            debug_logger.log_info(f"Cache cleared: removed {removed_count} files")
            return removed_count

        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to clear cache: {str(e)}",
                status_code=0,
                response_text=""
            )
            raise
