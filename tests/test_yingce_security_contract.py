import asyncio
import inspect
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from curl_cffi import CurlOpt
from curl_cffi.const import CurlECode
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import (
    ConnectionError as CurlConnectionError,
    DNSError as CurlDNSError,
    HTTPError as CurlHTTPError,
    SSLError as CurlSSLError,
)

from src.services.compat_video_tasks import CompatVideoTaskRegistry
from src.services.file_cache import FileCache


class _RedirectResponse:
    def __init__(self, status_code, *, headers=None, content=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content


class _FakeSession:
    responses = []
    calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        type(self).calls.append((url, kwargs))
        if type(self).responses:
            return type(self).responses.pop(0)
        return _RedirectResponse(200, content=b"synthetic-media")


class _PinnedSession:
    responses = []
    init_kwargs = []
    calls = []

    def __init__(self, *args, **kwargs):
        type(self).init_kwargs.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        type(self).calls.append((url, kwargs))
        response = type(self).responses.pop(0)
        callback = kwargs.get("content_callback")
        if callback is not None and response.content:
            callback(response.content)
        return response


class _PinnedErrorSession:
    error = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        raise type(self).error


class _SyntheticMediaProxyManager:
    async def get_media_proxy_url(self):
        return "http://synthetic-proxy.invalid:8080"


class _ProxySessionProbe:
    init_kwargs = []
    get_calls = []

    def __init__(self, *args, **kwargs):
        type(self).init_kwargs.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        type(self).get_calls.append((url, kwargs))
        callback = kwargs.get("content_callback")
        if callback is not None:
            callback(b"synthetic-media")
        return _RedirectResponse(200, content=b"synthetic-media")


class _SyntheticConnectProxy:
    def __init__(self):
        self.server = None
        self.port = None
        self.connect_request_seen = False
        self.connect_method_is_connect = False
        self.connect_target_is_pinned_ip = False
        self.connect_target_is_origin_host = False

    async def __aenter__(self):
        async def handle(reader, writer):
            try:
                request_line = await reader.readline()
                parts = request_line.decode("ascii", "ignore").strip().split()
                self.connect_request_seen = bool(parts)
                self.connect_method_is_connect = bool(parts) and parts[0].upper() == "CONNECT"
                target = parts[1] if len(parts) >= 2 else ""
                host = target.rsplit(":", 1)[0].strip("[]").casefold()
                self.connect_target_is_pinned_ip = host == "142.250.72.97"
                self.connect_target_is_origin_host = host == "flow-content.google"
                writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.server.close()
        await self.server.wait_closed()
        return False


class _FakeTunnelResponse:
    status = 200

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def getheaders(self):
        return []

    def getheader(self, name):
        return None

    def read(self, size=-1):
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class _SyntheticBodyResponse:
    def __init__(self, *, status=200, headers=None, chunks=None):
        self.status = status
        self._headers = [(str(name), str(value)) for name, value in (headers or {}).items()]
        self._header_map = {name.casefold(): value for name, value in self._headers}
        self._chunks = [bytes(chunk) for chunk in (chunks or [])]

    def getheaders(self):
        return list(self._headers)

    def getheader(self, name):
        return self._header_map.get(str(name).casefold())

    def read(self, size=-1):
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if size is not None and size >= 0 and len(chunk) > size:
            self._chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk


class _SyntheticHttpConnection:
    def __init__(self, response, *, connect_error=None):
        self.response = response
        self.connect_error = connect_error
        self.sock = None
        self.tunnel_call = None
        self.request_call = None
        self.closed = False

    def set_tunnel(self, host, port=None, headers=None):
        self.tunnel_call = (host, port, dict(headers or {}))

    def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        self.sock = object()

    def request(self, method, target, headers=None):
        self.request_call = (method, target, dict(headers or {}))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class _FakeCompletedProcess:
    returncode = 1
    stderr = b""


class YingceSsrfSecurityContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _FakeSession.responses = []
        _FakeSession.calls = []

    @staticmethod
    def _public_dns(host, port, *args, **kwargs):
        address = "127.0.0.1" if host == "redirect.test.invalid" else "142.250.72.97"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    async def test_direct_local_or_private_targets_are_rejected_before_network_io(self):
        link_local = "http://" + ".".join(("169", "254", "1", "1")) + "/synthetic.mp4"
        blocked = (
            "http://127.0.0.1/synthetic.mp4",
            "http://localhost/synthetic.mp4",
            "http://10.0.0.1/synthetic.mp4",
            link_local,
            "http://[::1]/synthetic.mp4",
            "http://[fe80::1]/synthetic.mp4",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("src.services.file_cache.AsyncSession", return_value=_FakeSession()):
                with patch.object(subprocess, "run", return_value=_FakeCompletedProcess()):
                    for target in blocked:
                        with self.subTest(target=target):
                            with self.assertRaisesRegex(Exception, "remote_media_target_rejected"):
                                await cache.download_and_cache(target, "video", log_source_url=False)
        self.assertEqual([], _FakeSession.calls)

    def test_pinned_proxy_tls_keeps_origin_hostname_verification(self):
        context = Mock()
        wrapped = object()
        context.wrap_socket.return_value = wrapped
        raw_socket = object()
        with patch("src.services.file_cache.ssl.create_default_context", return_value=context):
            result = FileCache._wrap_pinned_proxy_tls(raw_socket, "flow-content.google")
        self.assertIs(wrapped, result)
        context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="flow-content.google",
        )

    def test_yingce_remote_proxy_tls_failure_is_stable(self):
        response = _SyntheticBodyResponse()
        connection = _SyntheticHttpConnection(response)
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("src.services.file_cache.http.client.HTTPConnection", return_value=connection):
                with patch.object(cache, "_wrap_pinned_proxy_tls", side_effect=RuntimeError("private-detail")):
                    with self.assertRaisesRegex(ValueError, "^remote_media_proxy_tls_failed$"):
                        cache._download_remote_via_pinned_http_proxy_sync(
                            "https://flow-content.google/synthetic.mp4",
                            host="flow-content.google",
                            port=443,
                            address=None,
                            proxy_url="http://synthetic-proxy.invalid:8080",
                            headers={},
                            max_bytes=16,
                            resolve_origin_via_proxy=True,
                        )
        self.assertEqual("flow-content.google", connection.tunnel_call[0])

    async def test_yingce_pinned_mode_http_proxy_connects_by_allowlisted_origin_hostname(self):
        async with _SyntheticConnectProxy() as proxy:
            class LocalProxyManager:
                async def get_media_proxy_url(self):
                    return f"http://127.0.0.1:{proxy.port}"

            real_getaddrinfo = socket.getaddrinfo

            def pinned_origin_dns(host, port, *args, **kwargs):
                if host == "flow-content.google":
                    return [
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.72.97", port))
                    ]
                return real_getaddrinfo(host, port, *args, **kwargs)

            with tempfile.TemporaryDirectory() as temp_dir:
                cache = FileCache(cache_dir=temp_dir, proxy_manager=LocalProxyManager())
                with patch("socket.getaddrinfo", side_effect=pinned_origin_dns):
                    with self.assertRaisesRegex(Exception, "remote_media_proxy_connect_failed"):
                        await cache.download_and_cache(
                            "https://flow-content.google/synthetic.mp4",
                            "video",
                            log_source_url=False,
                            require_direct_connection=True,
                        )
        self.assertFalse(proxy.connect_target_is_pinned_ip)
        self.assertTrue(proxy.connect_target_is_origin_host)

    async def test_yingce_direct_private_fake_ip_stays_rejected_without_proxy(self):
        private_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("socket.getaddrinfo", return_value=private_dns):
                with patch(
                    "src.services.file_cache.AsyncSession",
                    side_effect=AssertionError("private direct target must not open a session"),
                ):
                    with self.assertRaisesRegex(Exception, "^remote_media_dns_non_public_rejected$"):
                        await cache.download_and_cache(
                            "https://flow-content.google/synthetic-private-fake-ip.mp4",
                            "video",
                            log_source_url=False,
                            require_direct_connection=True,
                        )

    async def test_yingce_http_proxy_remote_resolves_origin_without_local_origin_dns(self):
        response = _SyntheticBodyResponse(
            headers={"Content-Length": "3"},
            chunks=[b"abc"],
        )
        connection = _SyntheticHttpConnection(response)
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir, proxy_manager=_SyntheticMediaProxyManager())
            with patch(
                "socket.getaddrinfo",
                side_effect=AssertionError("origin DNS must not run for trusted proxy remote resolution"),
            ):
                with patch(
                    "src.services.file_cache.http.client.HTTPConnection",
                    return_value=connection,
                ):
                    with patch.object(
                        cache,
                        "_wrap_pinned_proxy_tls",
                        side_effect=lambda raw_socket, origin_host: raw_socket,
                    ) as wrap_tls:
                        filename = await cache.download_and_cache(
                            "https://flow-content.google/synthetic-proxy-remote-dns.mp4",
                            "video",
                            log_source_url=False,
                            require_direct_connection=True,
                        )
            self.assertTrue((cache.cache_dir / filename).is_file())
        tunnel_host, tunnel_port, tunnel_headers = connection.tunnel_call
        self.assertEqual("flow-content.google", tunnel_host)
        self.assertEqual(443, tunnel_port)
        self.assertEqual("flow-content.google:443", tunnel_headers["Host"])
        self.assertEqual("flow-content.google", connection.request_call[2]["Host"])
        wrap_tls.assert_called_once_with(connection.sock, "flow-content.google")

    async def test_connect_to_option_cannot_prove_pinned_proxy_connect_target(self):
        async with _SyntheticConnectProxy() as proxy:
            async with AsyncSession(
                curl_options={
                    CurlOpt.RESOLVE: ["flow-content.google:443:142.250.72.97"],
                    CurlOpt.CONNECT_TO: ["flow-content.google:443:142.250.72.97:443"],
                },
                trust_env=False,
            ) as session:
                with self.assertRaises(Exception):
                    await session.get(
                        "https://flow-content.google/synthetic-connect-to.mp4",
                        proxy=f"http://127.0.0.1:{proxy.port}",
                        verify=True,
                        allow_redirects=False,
                    )
        self.assertFalse(proxy.connect_request_seen)
        self.assertFalse(proxy.connect_target_is_pinned_ip)

    def test_proxy_production_path_does_not_use_curl_connect_to(self):
        secure_source = inspect.getsource(FileCache._download_remote_secure)
        pinned_source = inspect.getsource(FileCache._download_remote_via_pinned_http_proxy)
        self.assertNotIn("CONNECT_TO", secure_source)
        self.assertNotIn("CONNECT_TO", pinned_source)

    async def test_default_remote_download_uses_pinned_configured_media_proxy_without_async_session(self):
        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.72.97", 443))]
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir, proxy_manager=_SyntheticMediaProxyManager())
            pinned_download = AsyncMock(return_value=(200, {}, b"synthetic-media"))
            with patch("socket.getaddrinfo", return_value=public_dns):
                with patch.object(cache, "_download_remote_via_pinned_http_proxy", pinned_download):
                    with patch(
                        "src.services.file_cache.AsyncSession",
                        side_effect=AssertionError("explicit proxy must not use AsyncSession"),
                    ):
                        filename = await cache.download_and_cache(
                            "https://flow-content.google/synthetic-default.mp4",
                            "video",
                            log_source_url=False,
                        )
            self.assertTrue((cache.cache_dir / filename).is_file())
        pinned_download.assert_awaited_once()
        self.assertEqual("flow-content.google", pinned_download.await_args.kwargs["host"])
        self.assertEqual("142.250.72.97", pinned_download.await_args.kwargs["address"])

    async def test_direct_pinning_mode_accepts_http_proxy_and_rejects_non_http_before_dns(self):
        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.72.97", 443))]
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir, proxy_manager=_SyntheticMediaProxyManager())
            pinned_download = AsyncMock(return_value=(200, {}, b"synthetic-media"))
            with patch("socket.getaddrinfo", return_value=public_dns):
                with patch.object(cache, "_download_remote_via_pinned_http_proxy", pinned_download):
                    with patch(
                        "src.services.file_cache.AsyncSession",
                        side_effect=AssertionError("pinned HTTP proxy must not use AsyncSession"),
                    ):
                        await cache.download_and_cache(
                            "https://flow-content.google/synthetic-direct.mp4",
                            "video",
                            log_source_url=False,
                            require_direct_connection=True,
                        )
            pinned_download.assert_awaited_once()

        class NonHttpProxyManager:
            async def get_media_proxy_url(self):
                return "socks5://synthetic-proxy.invalid:1080"

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir, proxy_manager=NonHttpProxyManager())
            with patch.object(
                cache,
                "_resolve_safe_remote_target",
                side_effect=AssertionError("DNS must not run for unsupported proxy scheme"),
            ):
                with patch(
                    "src.services.file_cache.AsyncSession",
                    side_effect=AssertionError("unsupported proxy must not create AsyncSession"),
                ):
                    with self.assertRaisesRegex(Exception, "remote_media_proxy_unsupported"):
                        await cache.download_and_cache(
                            "https://flow-content.google/synthetic-direct.mp4",
                            "video",
                            log_source_url=False,
                            require_direct_connection=True,
                        )

    async def test_yingce_proxy_source_failure_is_stable_and_does_not_log_detail(self):
        class UnavailableProxyManager:
            async def get_media_proxy_url(self):
                raise RuntimeError("synthetic-private-proxy-detail")

        warnings = []
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir, proxy_manager=UnavailableProxyManager())
            with patch(
                "src.services.file_cache.debug_logger.log_warning",
                side_effect=lambda message, *args, **kwargs: warnings.append(str(message)),
            ):
                with self.assertRaisesRegex(Exception, "^remote_media_proxy_unavailable$"):
                    await cache.download_and_cache(
                        "https://flow-content.google/synthetic-proxy-unavailable.mp4",
                        "video",
                        log_source_url=False,
                        require_direct_connection=True,
                    )
        self.assertTrue(warnings)
        self.assertNotIn("synthetic-private-proxy-detail", " ".join(warnings))

    async def test_yingce_managed_proxy_download_logs_remain_redacted(self):
        class CredentialedProxyManager:
            async def get_media_proxy_url(self):
                return "http://synthetic-user:synthetic-pass@synthetic-proxy.invalid:8080"

        messages = []
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir, proxy_manager=CredentialedProxyManager())
            tunnel = AsyncMock(return_value=(200, {}, b"synthetic-media"))
            with patch.object(cache, "_download_remote_via_pinned_http_proxy", tunnel):
                with patch(
                    "src.services.file_cache.debug_logger.log_info",
                    side_effect=lambda message, *args, **kwargs: messages.append(str(message)),
                ):
                    with patch(
                        "socket.getaddrinfo",
                        side_effect=AssertionError("trusted proxy path must not use local origin DNS"),
                    ):
                        await cache.download_and_cache(
                            "https://flow-content.google/synthetic-private-media-path.mp4",
                            "video",
                            log_source_url=False,
                            require_direct_connection=True,
                        )
        serialized = " ".join(messages)
        for private_value in (
            "synthetic-private-media-path",
            "synthetic-proxy.invalid",
            "synthetic-user",
            "synthetic-pass",
        ):
            self.assertNotIn(private_value, serialized)

    async def test_yingce_account_fingerprint_proxy_wins_without_manager_fallback(self):
        class BoundFlowClient:
            def get_request_fingerprint(self):
                return {"proxy_url": "http://account-bound-proxy.invalid:8080"}

        class ForbiddenFallbackProxyManager:
            async def get_media_proxy_url(self):
                raise AssertionError("account-bound proxy must not be replaced")

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(
                cache_dir=temp_dir,
                proxy_manager=ForbiddenFallbackProxyManager(),
                flow_client=BoundFlowClient(),
            )
            tunnel = AsyncMock(return_value=(200, {}, b"synthetic-media"))
            with patch.object(
                cache,
                "_download_remote_via_pinned_http_proxy",
                tunnel,
            ):
                with patch(
                    "socket.getaddrinfo",
                    side_effect=AssertionError("origin DNS must not run for account-bound remote proxy"),
                ):
                    filename = await cache.download_and_cache(
                        "https://flow-content.google/synthetic-account-bound.mp4",
                        "video",
                        log_source_url=False,
                        require_direct_connection=True,
                    )
            self.assertTrue((cache.cache_dir / filename).is_file())
        tunnel.assert_awaited_once()
        self.assertEqual(
            "http://account-bound-proxy.invalid:8080",
            tunnel.await_args.kwargs["proxy_url"],
        )
        self.assertEqual("flow-content.google", tunnel.await_args.kwargs["host"])

    async def test_proxy_secure_download_pins_connect_target_to_prechecked_ip(self):
        public_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.72.97", 443))
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            pinned_download = AsyncMock(return_value=(200, {}, b"synthetic-media"))
            with patch("socket.getaddrinfo", return_value=public_dns):
                with patch.object(cache, "_download_remote_via_pinned_http_proxy", pinned_download):
                    with patch(
                        "src.services.file_cache.AsyncSession",
                        side_effect=AssertionError("explicit proxy must use pinned transport"),
                    ):
                        content = await cache._download_remote_secure(
                            "https://flow-content.google/synthetic.mp4",
                            proxy_url="http://synthetic-proxy.invalid:8080",
                            headers={},
                        )
        self.assertEqual(b"synthetic-media", content)
        pinned_download.assert_awaited_once()
        self.assertEqual("142.250.72.97", pinned_download.await_args.kwargs["address"])
        self.assertEqual("flow-content.google", pinned_download.await_args.kwargs["host"])

    def test_pinned_proxy_connect_authority_ipv4_ipv6_and_proxy_auth_is_connect_only(self):
        cases = (
            ("142.250.72.97", "142.250.72.97", "142.250.72.97:443"),
            ("2001:4860:4860::8888", "[2001:4860:4860::8888]", "[2001:4860:4860::8888]:443"),
        )
        for address, expected_tunnel_host, expected_authority in cases:
            with self.subTest(address=address):
                response = _SyntheticBodyResponse(
                    headers={"Content-Length": "3"},
                    chunks=[b"abc"],
                )
                connection = _SyntheticHttpConnection(response)
                with tempfile.TemporaryDirectory() as temp_dir:
                    cache = FileCache(cache_dir=temp_dir)
                    with patch("src.services.file_cache.http.client.HTTPConnection", return_value=connection):
                        with patch.object(
                            cache,
                            "_wrap_pinned_proxy_tls",
                            side_effect=lambda raw_socket, origin_host: raw_socket,
                        ) as wrap_tls:
                            status, _headers, content = cache._download_remote_via_pinned_http_proxy_sync(
                                "https://flow-content.google/synthetic.mp4",
                                host="flow-content.google",
                                port=443,
                                address=address,
                                proxy_url="http://synthetic-user@synthetic-proxy.invalid:8080",
                                headers={"Proxy-Authorization": "must-not-leak"},
                                max_bytes=16,
                            )
                self.assertEqual(200, status)
                self.assertEqual(b"abc", content)
                tunnel_host, tunnel_port, tunnel_headers = connection.tunnel_call
                self.assertEqual(expected_tunnel_host, tunnel_host)
                self.assertEqual(443, tunnel_port)
                self.assertEqual(expected_authority, tunnel_headers["Host"])
                self.assertIn("Proxy-Authorization", tunnel_headers)
                self.assertNotIn("Proxy-Authorization", connection.request_call[2])
                self.assertEqual("flow-content.google", connection.request_call[2]["Host"])
                wrap_tls.assert_called_once_with(connection.sock, "flow-content.google")

    def test_pinned_proxy_connect_1xx_and_non_200_fail_closed(self):
        for status in (100, 407, 502):
            with self.subTest(status=status):
                response = _SyntheticBodyResponse(status=status)
                connection = _SyntheticHttpConnection(
                    response,
                    connect_error=OSError(f"synthetic CONNECT status {status}"),
                )
                with tempfile.TemporaryDirectory() as temp_dir:
                    cache = FileCache(cache_dir=temp_dir)
                    with patch("src.services.file_cache.http.client.HTTPConnection", return_value=connection):
                        with self.assertRaisesRegex(ValueError, "remote_media_proxy_connect_failed"):
                            cache._download_remote_via_pinned_http_proxy_sync(
                                "https://flow-content.google/synthetic.mp4",
                                host="flow-content.google",
                                port=443,
                                address=None,
                                proxy_url="http://synthetic-proxy.invalid:8080",
                                headers={},
                                max_bytes=16,
                                resolve_origin_via_proxy=True,
                            )
                self.assertTrue(connection.closed)
                self.assertEqual("flow-content.google", connection.tunnel_call[0])

    def test_pinned_proxy_body_framing_and_actual_byte_limit(self):
        framing_cases = (
            ({"Content-Length": "4"}, [b"ab", b"cd"]),
            ({"Transfer-Encoding": "chunked"}, [b"a", b"bcd"]),
            ({"Connection": "close"}, [b"abc", b"d"]),
        )
        for response_headers, chunks in framing_cases:
            with self.subTest(headers=response_headers):
                response = _SyntheticBodyResponse(headers=response_headers, chunks=chunks)
                connection = _SyntheticHttpConnection(response)
                with tempfile.TemporaryDirectory() as temp_dir:
                    cache = FileCache(cache_dir=temp_dir)
                    with patch("src.services.file_cache.http.client.HTTPConnection", return_value=connection):
                        with patch.object(
                            cache,
                            "_wrap_pinned_proxy_tls",
                            side_effect=lambda raw_socket, origin_host: raw_socket,
                        ):
                            status, _headers, content = cache._download_remote_via_pinned_http_proxy_sync(
                                "https://flow-content.google/synthetic.mp4",
                                host="flow-content.google",
                                port=443,
                                address="142.250.72.97",
                                proxy_url="http://synthetic-proxy.invalid:8080",
                                headers={},
                                max_bytes=4,
                            )
                self.assertEqual(200, status)
                self.assertEqual(b"abcd", content)

        forged_low = _SyntheticBodyResponse(
            headers={"Content-Length": "1"},
            chunks=[b"abcde"],
        )
        connection = _SyntheticHttpConnection(forged_low)
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("src.services.file_cache.http.client.HTTPConnection", return_value=connection):
                with patch.object(
                    cache,
                    "_wrap_pinned_proxy_tls",
                    side_effect=lambda raw_socket, origin_host: raw_socket,
                ):
                    with self.assertRaisesRegex(ValueError, "remote_media_too_large"):
                        cache._download_remote_via_pinned_http_proxy_sync(
                            "https://flow-content.google/synthetic.mp4",
                            host="flow-content.google",
                            port=443,
                            address="142.250.72.97",
                            proxy_url="http://synthetic-proxy.invalid:8080",
                            headers={},
                            max_bytes=4,
                        )

    async def test_pinned_proxy_socket_and_tls_work_runs_via_to_thread(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch.object(
                cache,
                "_download_remote_via_pinned_http_proxy_sync",
                return_value=(200, {}, b"synthetic-media"),
            ) as sync_download:
                real_to_thread = asyncio.to_thread
                with patch(
                    "src.services.file_cache.asyncio.to_thread",
                    side_effect=real_to_thread,
                ) as to_thread:
                    result = await cache._download_remote_via_pinned_http_proxy(
                        "https://flow-content.google/synthetic.mp4",
                        host="flow-content.google",
                        port=443,
                        address="142.250.72.97",
                        proxy_url="http://synthetic-proxy.invalid:8080",
                        headers={},
                        max_bytes=16,
                    )
        self.assertEqual((200, {}, b"synthetic-media"), result)
        to_thread.assert_awaited_once()
        sync_download.assert_called_once()

    async def test_yingce_remote_proxy_rejects_invalid_targets_and_redirects_before_tunnel(self):
        invalid_targets = (
            "http://flow-content.google/synthetic.mp4",
            "https://flow-content.google:444/synthetic.mp4",
            "https://example.invalid/synthetic.mp4",
            "https://synthetic-user@flow-content.google/synthetic.mp4",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            for target in invalid_targets:
                with self.subTest(target=target):
                    with patch.object(
                        cache,
                        "_download_remote_via_pinned_http_proxy",
                        side_effect=AssertionError("invalid target must not reach proxy tunnel"),
                    ):
                        with self.assertRaisesRegex(ValueError, "^remote_media_target_rejected$"):
                            await cache._download_remote_secure(
                                target,
                                proxy_url="http://synthetic-proxy.invalid:8080",
                                headers={},
                                trust_env=False,
                                require_pinned_transport=True,
                            )

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            tunnel = AsyncMock(
                return_value=(302, {"location": "https://example.invalid/blocked.mp4"}, b"")
            )
            with patch.object(cache, "_download_remote_via_pinned_http_proxy", tunnel):
                with self.assertRaisesRegex(ValueError, "^remote_media_target_rejected$"):
                    await cache._download_remote_secure(
                        "https://flow-content.google/synthetic-start.mp4",
                        proxy_url="http://synthetic-proxy.invalid:8080",
                        headers={},
                        trust_env=False,
                        require_pinned_transport=True,
                    )
            tunnel.assert_awaited_once()

    async def test_yingce_no_managed_proxy_does_not_inherit_system_proxy(self):
        private_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("socket.getaddrinfo", return_value=private_dns):
                with patch(
                    "src.services.file_cache.http.client.HTTPConnection",
                    side_effect=AssertionError("system proxy must not be used"),
                ):
                    with patch(
                        "src.services.file_cache.AsyncSession",
                        side_effect=AssertionError("private direct target must fail before network"),
                    ):
                        with self.assertRaisesRegex(Exception, "^remote_media_dns_non_public_rejected$"):
                            await cache.download_and_cache(
                                "https://flow-content.google/synthetic-no-managed-proxy.mp4",
                                "video",
                                log_source_url=False,
                                require_direct_connection=True,
                            )

    async def test_yingce_remote_proxy_redirect_revalidates_each_allowed_hop_without_local_dns(self):
        calls = []
        responses = [
            (302, {"location": "https://lh3.googleusercontent.com/synthetic-final.mp4"}, b""),
            (200, {}, b"synthetic-media"),
        ]

        async def fake_tunnel(url, **kwargs):
            calls.append((kwargs["host"], kwargs["address"]))
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch(
                "socket.getaddrinfo",
                side_effect=AssertionError("trusted proxy redirect must not use local origin DNS"),
            ):
                with patch.object(cache, "_download_remote_via_pinned_http_proxy", side_effect=fake_tunnel):
                    content = await cache._download_remote_secure(
                        "https://flow-content.google/synthetic-start.mp4",
                        proxy_url="http://synthetic-proxy.invalid:8080",
                        headers={},
                        trust_env=False,
                        require_pinned_transport=True,
                    )
        self.assertEqual(b"synthetic-media", content)
        self.assertEqual(
            [
                ("flow-content.google", None),
                ("lh3.googleusercontent.com", None),
            ],
            calls,
        )

    async def test_default_pinned_proxy_private_redirect_is_rejected_before_second_tunnel(self):
        calls = []

        async def fake_tunnel(url, **kwargs):
            calls.append(kwargs["host"])
            return (
                302,
                {"location": "https://lh3.googleusercontent.com/synthetic-private.mp4"},
                b"",
            )

        def mixed_dns(host, port, *args, **kwargs):
            address = "142.250.72.97" if host == "flow-content.google" else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("socket.getaddrinfo", side_effect=mixed_dns):
                with patch.object(cache, "_download_remote_via_pinned_http_proxy", side_effect=fake_tunnel):
                    with self.assertRaisesRegex(ValueError, "remote_media_dns_non_public_rejected"):
                        await cache._download_remote_secure(
                            "https://flow-content.google/synthetic-start.mp4",
                            proxy_url="http://synthetic-proxy.invalid:8080",
                            headers={},
                            trust_env=False,
                            require_pinned_transport=False,
                        )
        self.assertEqual(["flow-content.google"], calls)

    def test_pinned_proxy_stream_limit_stops_before_unbounded_buffering(self):
        connection = Mock()
        connection.sock = object()
        response = Mock()
        response.status = 200
        response.getheaders.return_value = []
        response.getheader.return_value = None
        response.read.side_effect = [b"1234", b"5678", b"9"]
        connection.getresponse.return_value = response

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("src.services.file_cache.http.client.HTTPConnection", return_value=connection):
                with patch.object(cache, "_wrap_pinned_proxy_tls", return_value=object()):
                    with self.assertRaisesRegex(ValueError, "remote_media_too_large"):
                        cache._download_remote_via_pinned_http_proxy_sync(
                            "https://flow-content.google/synthetic.mp4",
                            host="flow-content.google",
                            port=443,
                            address="142.250.72.97",
                            proxy_url="http://synthetic-proxy.invalid:8080",
                            headers={"Host": "forbidden.invalid", "Connection": "keep-alive"},
                            max_bytes=8,
                        )
        self.assertTrue(connection.close.called)
        request_headers = connection.request.call_args.kwargs["headers"]
        self.assertEqual("flow-content.google", request_headers["Host"])
        self.assertEqual("close", request_headers["Connection"])

    async def test_pinned_proxy_rejects_unsupported_proxy_scheme_before_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with self.assertRaisesRegex(ValueError, "^remote_media_proxy_unsupported$"):
                await cache._download_remote_secure(
                    "https://flow-content.google/synthetic.mp4",
                    proxy_url="https://synthetic-proxy.invalid:8443",
                    headers={},
                    trust_env=False,
                    require_pinned_transport=True,
                )

    async def test_safe_remote_target_failure_layers_are_distinct(self):
        target_rejections = (
            "http://flow-content.google/synthetic.mp4",
            "https://synthetic-user@flow-content.google/synthetic.mp4",
            "https://not-allowed.invalid/synthetic.mp4",
            "https://flow-content.google:444/synthetic.mp4",
            "https://flow-content.google:invalid/synthetic.mp4",
        )
        for target in target_rejections:
            with self.subTest(layer="target"):
                with self.assertRaisesRegex(ValueError, "^remote_media_target_rejected$"):
                    FileCache._resolve_safe_remote_target(target)

        with patch("socket.getaddrinfo", side_effect=socket.gaierror("private-detail")):
            with self.assertRaisesRegex(ValueError, "^remote_media_dns_failed$") as raised:
                FileCache._resolve_safe_remote_target(
                    "https://flow-content.google/synthetic.mp4"
                )
        self.assertNotIn("private-detail", str(raised.exception))

        non_public = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]
        with patch("socket.getaddrinfo", return_value=non_public):
            with self.assertRaisesRegex(
                ValueError, "^remote_media_dns_non_public_rejected$"
            ):
                FileCache._resolve_safe_remote_target(
                    "https://flow-content.google/synthetic.mp4"
                )

        with patch("socket.getaddrinfo", return_value=[]):
            with self.assertRaisesRegex(
                ValueError, "^remote_media_dns_no_public_address$"
            ):
                FileCache._resolve_safe_remote_target(
                    "https://flow-content.google/synthetic.mp4"
                )

    async def test_direct_pinned_resolve_formats_ipv6_for_libcurl(self):
        address = socket.inet_ntop(
            socket.AF_INET6,
            bytes.fromhex("20014860000000000000000000008888"),
        )
        _PinnedSession.responses = [_RedirectResponse(200, content=b"synthetic-media")]
        _PinnedSession.init_kwargs = []
        _PinnedSession.calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch.object(
                cache,
                "_resolve_safe_remote_target",
                return_value=("flow-content.google", 443, address),
            ):
                with patch("src.services.file_cache.AsyncSession", _PinnedSession):
                    await cache.download_and_cache(
                        "https://flow-content.google/synthetic.mp4",
                        "video",
                        log_source_url=False,
                    )
        resolve_entries = next(
            iter(_PinnedSession.init_kwargs[0]["curl_options"].values())
        )
        self.assertEqual([f"flow-content.google:443:[{address}]"], resolve_entries)

    async def test_redirect_target_revalidation_preserves_dns_failure_layer(self):
        _PinnedSession.responses = [
            _RedirectResponse(
                302,
                headers={
                    "location": "https://lh3.googleusercontent.com/synthetic-final.mp4"
                },
            )
        ]
        _PinnedSession.init_kwargs = []
        _PinnedSession.calls = []
        dns_calls = [0]

        def first_ok_then_dns_failure(host, port, *args, **kwargs):
            dns_calls[0] += 1
            if dns_calls[0] == 1:
                return self._public_dns(host, port, *args, **kwargs)
            raise socket.gaierror("private-redirect-detail")

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("socket.getaddrinfo", side_effect=first_ok_then_dns_failure):
                with patch("src.services.file_cache.AsyncSession", _PinnedSession):
                    with self.assertRaisesRegex(
                        Exception, "^remote_media_dns_failed$"
                    ) as raised:
                        await cache.download_and_cache(
                            "https://flow-content.google/synthetic-start.mp4",
                            "video",
                            log_source_url=False,
                        )
        self.assertNotIn("private-redirect-detail", str(raised.exception))
        self.assertEqual(1, len(_PinnedSession.calls))

    async def test_direct_pinned_path_classifies_curl_failures(self):
        cases = (
            ("dns", CurlDNSError("private-detail", CurlECode.COULDNT_RESOLVE_HOST), "remote_media_pinned_dns_failed"),
            ("connect", CurlConnectionError("private-detail", CurlECode.COULDNT_CONNECT), "remote_media_connect_failed"),
            ("tls", CurlSSLError("private-detail", CurlECode.SSL_CONNECT_ERROR), "remote_media_tls_failed"),
            ("http", CurlHTTPError("private-detail", CurlECode.HTTP2), "remote_media_http_failed"),
            ("transfer", CurlConnectionError("private-detail", CurlECode.RECV_ERROR), "remote_media_download_failed"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            for name, error, expected in cases:
                with self.subTest(name=name):
                    _PinnedErrorSession.error = error
                    with patch("socket.getaddrinfo", side_effect=self._public_dns):
                        with patch("src.services.file_cache.AsyncSession", _PinnedErrorSession):
                            with self.assertRaises(Exception) as raised:
                                await cache.download_and_cache(
                                    "https://flow-content.google/synthetic.mp4",
                                    "video",
                                    log_source_url=False,
                                )
                    self.assertEqual(expected, str(raised.exception))
                    self.assertNotIn("private-detail", str(raised.exception))

    async def test_direct_pinned_non_200_is_http_failure(self):
        _PinnedSession.responses = [_RedirectResponse(503, content=b"x")]
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("socket.getaddrinfo", side_effect=self._public_dns):
                with patch("src.services.file_cache.AsyncSession", _PinnedSession):
                    with self.assertRaisesRegex(Exception, "^remote_media_http_failed$"):
                        await cache.download_and_cache(
                            "https://flow-content.google/synthetic.mp4",
                            "video",
                            log_source_url=False,
                        )

    async def test_direct_pinned_empty_200_is_distinct(self):
        _PinnedSession.responses = [_RedirectResponse(200, content=b"")]
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("socket.getaddrinfo", side_effect=self._public_dns):
                with patch("src.services.file_cache.AsyncSession", _PinnedSession):
                    with self.assertRaisesRegex(Exception, "^remote_media_empty_download$"):
                        await cache.download_and_cache(
                            "https://flow-content.google/synthetic.mp4",
                            "video",
                            log_source_url=False,
                        )

    async def test_allowed_redirect_revalidates_dns_and_pins_each_network_hop(self):
        scheme = "https"
        first_host = "flow-content.google"
        second_host = "lh3.googleusercontent.com"
        _PinnedSession.responses = [
            _RedirectResponse(
                302,
                headers={"location": f"{scheme}://{second_host}/synthetic-final.mp4"},
            ),
            _RedirectResponse(200, content=b"synthetic-media"),
        ]
        _PinnedSession.init_kwargs = []
        _PinnedSession.calls = []
        dns_calls = []

        def public_dns(host, port, *args, **kwargs):
            dns_calls.append((host, port))
            address = "142.250.72.97" if host == first_host else "142.250.72.98"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("socket.getaddrinfo", side_effect=public_dns):
                with patch("src.services.file_cache.AsyncSession", _PinnedSession):
                    filename = await cache.download_and_cache(
                        f"{scheme}://{first_host}/synthetic-start.mp4",
                        "video",
                        log_source_url=False,
                    )
            self.assertTrue((cache.cache_dir / filename).is_file())

        self.assertGreaterEqual(dns_calls.count((first_host, 443)), 1)
        self.assertGreaterEqual(dns_calls.count((second_host, 443)), 1)
        self.assertEqual(2, len(_PinnedSession.init_kwargs))
        first_resolve = next(iter(_PinnedSession.init_kwargs[0]["curl_options"].values()))
        second_resolve = next(iter(_PinnedSession.init_kwargs[1]["curl_options"].values()))
        self.assertEqual([f"{first_host}:443:142.250.72.97"], first_resolve)
        self.assertEqual([f"{second_host}:443:142.250.72.98"], second_resolve)
        self.assertEqual(2, len(_PinnedSession.calls))
        for _url, kwargs in _PinnedSession.calls:
            self.assertTrue(kwargs["verify"])
            self.assertFalse(kwargs["allow_redirects"])
            self.assertIsNone(kwargs["proxy"])

    async def test_redirect_is_revalidated_and_private_redirect_target_is_never_fetched(self):
        _FakeSession.responses = [
            _RedirectResponse(
                302,
                headers={"location": "http://redirect.test.invalid/private.mp4"},
            )
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("socket.getaddrinfo", side_effect=self._public_dns):
                with patch("src.services.file_cache.AsyncSession", return_value=_FakeSession()):
                    with patch.object(subprocess, "run", return_value=_FakeCompletedProcess()):
                        with self.assertRaisesRegex(Exception, "remote_media_target_rejected"):
                            await cache.download_and_cache(
                                "https://lh3.googleusercontent.com/synthetic.mp4",
                                "video",
                                log_source_url=False,
                            )
        self.assertEqual(1, len(_FakeSession.calls))
        self.assertEqual(
            "https://lh3.googleusercontent.com/synthetic.mp4",
            _FakeSession.calls[0][0],
        )
        self.assertFalse(_FakeSession.calls[0][1].get("allow_redirects", True))


class CompatVideoTaskActiveTtlSecurityContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_active_task_becomes_terminal_and_releases_capacity(self):
        now = [1000.0]
        registry = CompatVideoTaskRegistry(ttl_seconds=1, capacity=1, clock=lambda: now[0])
        first = await registry.create(model="omni", size=None, seconds=10)
        await registry.update(first.id, status="in_progress", progress=10)

        now[0] = 1002.0
        expired = await registry.get(first.id)
        self.assertIsNotNone(expired)
        self.assertEqual("failed", expired.status)
        self.assertEqual("task_timeout", expired.error_code)
        self.assertEqual(100, expired.progress)

        second = await registry.create(model="omni", size=None, seconds=10)
        self.assertNotEqual(first.id, second.id)
        self.assertIsNone(await registry.get(first.id))

    async def test_expired_active_idempotent_task_does_not_reuse_stale_work(self):
        now = [2000.0]
        registry = CompatVideoTaskRegistry(ttl_seconds=1, capacity=1, clock=lambda: now[0])
        first, reused = await registry.create_idempotent(
            model="omni", size=None, seconds=10,
            idempotency_digest="synthetic-digest",
            request_fingerprint="synthetic-fingerprint",
        )
        self.assertFalse(reused)

        now[0] = 2002.0
        expired = await registry.get(first.id)
        self.assertIsNotNone(expired)
        self.assertEqual("failed", expired.status)
        self.assertEqual("task_timeout", expired.error_code)

        second, reused = await registry.create_idempotent(
            model="omni", size=None, seconds=10,
            idempotency_digest="synthetic-digest",
            request_fingerprint="synthetic-fingerprint",
        )
        self.assertFalse(reused)
        self.assertNotEqual(first.id, second.id)


if __name__ == "__main__":
    unittest.main()
