"""Database storage layer for Flow2API"""
import asyncio
import aiosqlite
import base64
import ctypes
import os
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional, List, Dict, Any
from pathlib import Path
from .config import DEFAULT_YESCAPTCHA_TASK_TYPE, normalize_yescaptcha_task_type
from .models import Token, TokenStats, Task, RequestLog, AdminConfig, ProxyConfig, GenerationConfig, CacheConfig, Project, CaptchaConfig, PluginConfig, CallLogicConfig, TokenRefreshConfig


_GOOGLE_COOKIES_ENVELOPE_PREFIX = "dpapi-user:v1:"
_AUTH_STATES = {"ok", "refresh_pending", "backoff", "reauth_required"}
_AUTH_ERROR_CLASSES = {
    "",
    "network",
    "oauth_callback_missing",
    "cookie_rejected",
    "profile_missing",
    "profile_corrupt",
    "interactive_verification",
    "browser_start_failed",
    "browser_timeout",
    "identity_mismatch",
}


class _WindowsCurrentUserDpapiProtector:
    """Protect small text values with Windows DPAPI for the current OS user."""

    marker = _GOOGLE_COOKIES_ENVELOPE_PREFIX
    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.c_ulong),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    @classmethod
    def _input_blob(cls, payload: bytes):
        buffer = ctypes.create_string_buffer(payload)
        blob = cls._DataBlob(
            len(payload),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, buffer

    def is_protected(self, value: str) -> bool:
        return str(value or "").startswith(self.marker)

    def protect(self, value: str) -> str:
        payload = str(value or "").encode("utf-8")
        input_blob, input_buffer = self._input_blob(payload)
        output_blob = self._DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        succeeded = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "Flow2API google_cookies",
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        _ = input_buffer
        if not succeeded:
            raise RuntimeError("current-user cookie protection failed")
        try:
            protected = ctypes.string_at(output_blob.pbData, output_blob.cbData)
            return self.marker + base64.urlsafe_b64encode(protected).decode("ascii")
        finally:
            if output_blob.pbData:
                kernel32.LocalFree(output_blob.pbData)

    def unprotect(self, value: str) -> str:
        if not self.is_protected(value):
            raise ValueError("unrecognized cookie envelope")
        encoded = str(value)[len(self.marker):]
        try:
            payload = base64.b64decode(encoded, altchars=b"-_", validate=True)
        except Exception as exc:
            raise ValueError("invalid cookie envelope") from exc
        input_blob, input_buffer = self._input_blob(payload)
        output_blob = self._DataBlob()
        description = ctypes.c_wchar_p()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        succeeded = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        _ = input_buffer
        if not succeeded:
            raise RuntimeError("current-user cookie decryption failed")
        try:
            plaintext = ctypes.string_at(output_blob.pbData, output_blob.cbData)
            return plaintext.decode("utf-8")
        finally:
            if description:
                kernel32.LocalFree(description)
            if output_blob.pbData:
                kernel32.LocalFree(output_blob.pbData)


_default_google_cookies_protector = None


def _get_google_cookies_protector():
    """Return the local current-user protector without introducing another store."""
    global _default_google_cookies_protector
    if os.name != "nt":
        return None
    if _default_google_cookies_protector is None:
        _default_google_cookies_protector = _WindowsCurrentUserDpapiProtector()
    return _default_google_cookies_protector


class Database:
    """SQLite database manager"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            # Store database in data directory
            data_dir = Path(__file__).parent.parent.parent / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "flow.db")
        self.db_path = db_path
        self._write_lock = asyncio.Lock()
        self._connect_timeout = 30
        self._busy_timeout_ms = 30000

    def db_exists(self) -> bool:
        """Check if database file exists"""
        return Path(self.db_path).exists()

    async def _configure_connection(self, db):
        """Apply SQLite runtime settings for better concurrent behavior."""
        await db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        await db.execute("PRAGMA foreign_keys = ON")

    def _current_stats_date(self) -> str:
        """Return the logical date used by daily token statistics."""
        return date.today().isoformat()

    @staticmethod
    def _protect_google_cookies(value: Any) -> str:
        plaintext = str(value or "")
        if not plaintext:
            return ""
        protector = _get_google_cookies_protector()
        if protector is None:
            raise RuntimeError("current-user cookie protection is unavailable")
        if protector.is_protected(plaintext):
            return plaintext
        return protector.protect(plaintext)

    @staticmethod
    def _unprotect_google_cookies(value: Any) -> str:
        stored_value = str(value or "")
        if not stored_value:
            return ""
        protector = _get_google_cookies_protector()
        is_envelope = stored_value.startswith(_GOOGLE_COOKIES_ENVELOPE_PREFIX)
        if protector is not None:
            try:
                is_envelope = is_envelope or protector.is_protected(stored_value)
            except Exception:
                is_envelope = True
        if not is_envelope:
            return stored_value
        if protector is None:
            return ""
        try:
            return str(protector.unprotect(stored_value) or "")
        except Exception:
            return ""

    @classmethod
    def _decode_token_row(cls, row: Any) -> Dict[str, Any]:
        data = dict(row)
        if "google_cookies" in data:
            data["google_cookies"] = cls._unprotect_google_cookies(data.get("google_cookies"))
        if "login_password" in data:
            data["login_password"] = ""
        return data

    @asynccontextmanager
    async def _connect(self, *, write: bool = False):
        """Open a configured SQLite connection and optionally serialize writes."""
        if write:
            async with self._write_lock:
                async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
                    await self._configure_connection(db)
                    yield db
            return

        async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
            await self._configure_connection(db)
            yield db

    async def _table_exists(self, db, table_name: str) -> bool:
        """Check if a table exists in the database"""
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        result = await cursor.fetchone()
        return result is not None

    async def _column_exists(self, db, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table"""
        try:
            cursor = await db.execute(f"PRAGMA table_info({table_name})")
            columns = await cursor.fetchall()
            return any(col[1] == column_name for col in columns)
        except:
            return False

    async def _ensure_extension_plugin_sessions_table(self, db) -> None:
        """Create the digest-only plugin-session store for early API initialization."""
        await db.execute("""
            CREATE TABLE IF NOT EXISTS extension_plugin_sessions (
                session_digest TEXT PRIMARY KEY,
                public_id TEXT UNIQUE NOT NULL,
                instance_id TEXT NOT NULL,
                route_key TEXT NOT NULL,
                client_label TEXT NOT NULL,
                capability_marker TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_extension_plugin_sessions_expires_at "
            "ON extension_plugin_sessions(expires_at)"
        )

    async def _ensure_account_model_availability_table(self, db) -> bool:
        """Create the model-verification allowlist and report whether it was new."""
        table_exists = await self._table_exists(db, "account_model_availability")
        if not table_exists:
            await db.execute("""
                CREATE TABLE account_model_availability (
                    token_id INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_class TEXT NOT NULL DEFAULT '',
                    successful_generations INTEGER NOT NULL DEFAULT 0,
                    explicit_denials INTEGER NOT NULL DEFAULT 0,
                    last_verified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (token_id, model),
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)
        return not table_exists

    async def _backfill_account_model_availability(self, db) -> None:
        """Safely derive missing verification facts from task metadata without reading content."""
        if not await self._table_exists(db, "tasks"):
            return

        cursor = await db.execute("""
            WITH missing_decisive_tasks AS (
                SELECT
                    task.token_id,
                    task.model,
                    task.status,
                    task.has_media,
                    task.error_class,
                    ROW_NUMBER() OVER (
                        PARTITION BY task.token_id, task.model
                        ORDER BY task.id DESC
                    ) AS recency_rank,
                    SUM(
                        CASE
                            WHEN task.status = 'succeeded' AND COALESCE(task.has_media, 0) != 0
                            THEN 1 ELSE 0
                        END
                    ) OVER (PARTITION BY task.token_id, task.model) AS successful_generations,
                    SUM(
                        CASE
                            WHEN task.error_class IN ('model_access_denied', 'membership_tier')
                            THEN 1 ELSE 0
                        END
                    ) OVER (PARTITION BY task.token_id, task.model) AS explicit_denials
                FROM tasks AS task
                WHERE (
                    (task.status = 'succeeded' AND COALESCE(task.has_media, 0) != 0)
                    OR task.error_class IN ('model_access_denied', 'membership_tier')
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM account_model_availability AS availability
                    WHERE availability.token_id = task.token_id
                      AND availability.model = task.model
                )
            )
            SELECT
                token_id,
                model,
                CASE
                    WHEN status = 'succeeded' AND COALESCE(has_media, 0) != 0
                    THEN 'available' ELSE 'unavailable'
                END AS derived_status,
                CASE
                    WHEN status = 'succeeded' AND COALESCE(has_media, 0) != 0
                    THEN '' ELSE error_class
                END AS derived_error_class,
                successful_generations,
                explicit_denials
            FROM missing_decisive_tasks
            WHERE recency_rank = 1
            ORDER BY token_id, model
        """)
        for (
            token_id,
            model,
            status,
            error_class,
            successful_generations,
            explicit_denials,
        ) in await cursor.fetchall():
            await db.execute("""
                INSERT OR IGNORE INTO account_model_availability (
                    token_id, model, status, error_class,
                    successful_generations, explicit_denials, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                int(token_id),
                str(model),
                str(status),
                str(error_class or ""),
                int(successful_generations or 0),
                int(explicit_denials or 0),
            ))

    async def _ensure_config_rows(self, db, config_dict: dict = None):
        """Ensure all config tables have their default rows

        Args:
            db: Database connection
            config_dict: Configuration dictionary from setting.toml (optional)
                        If None, use default values instead of reading from TOML.
        """
        # Ensure admin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM admin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            admin_username = "admin"
            admin_password = "admin"
            api_key = "han1234"
            error_ban_threshold = 3

            if config_dict:
                global_config = config_dict.get("global", {})
                admin_username = global_config.get("admin_username", "admin")
                admin_password = global_config.get("admin_password", "admin")
                api_key = global_config.get("api_key", "han1234")

                admin_config = config_dict.get("admin", {})
                error_ban_threshold = admin_config.get("error_ban_threshold", 3)

            await db.execute("""
                INSERT INTO admin_config (id, username, password, api_key, error_ban_threshold)
                VALUES (1, ?, ?, ?, ?)
            """, (admin_username, admin_password, api_key, error_ban_threshold))

        # Ensure proxy_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM proxy_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            proxy_enabled = False
            proxy_url = None
            media_proxy_enabled = False
            media_proxy_url = None

            if config_dict:
                proxy_config = config_dict.get("proxy", {})
                proxy_enabled = proxy_config.get("proxy_enabled", False)
                proxy_url = proxy_config.get("proxy_url", "")
                proxy_url = proxy_url if proxy_url else None
                media_proxy_enabled = proxy_config.get(
                    "media_proxy_enabled",
                    proxy_config.get("image_io_proxy_enabled", False)
                )
                media_proxy_url = proxy_config.get(
                    "media_proxy_url",
                    proxy_config.get("image_io_proxy_url", "")
                )
                media_proxy_url = media_proxy_url if media_proxy_url else None

            await db.execute("""
                INSERT INTO proxy_config (id, enabled, proxy_url, media_proxy_enabled, media_proxy_url)
                VALUES (1, ?, ?, ?, ?)
            """, (proxy_enabled, proxy_url, media_proxy_enabled, media_proxy_url))

        # Ensure generation_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM generation_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            image_timeout = 300
            video_timeout = 1500
            max_retries = 3

            if config_dict:
                generation_config = config_dict.get("generation", {})
                flow_config = config_dict.get("flow", {})
                image_timeout = generation_config.get("image_timeout", 300)
                video_timeout = generation_config.get("video_timeout", 1500)
                max_retries = flow_config.get("max_retries", 3)

            try:
                max_retries = max(1, int(max_retries))
            except Exception:
                max_retries = 3

            await db.execute("""
                INSERT INTO generation_config (id, image_timeout, video_timeout, max_retries)
                VALUES (1, ?, ?, ?)
            """, (image_timeout, video_timeout, max_retries))

        # Ensure call_logic_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM call_logic_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            call_mode = "default"
            polling_mode_enabled = False

            if config_dict:
                call_logic_config = config_dict.get("call_logic", {})
                call_mode = call_logic_config.get("call_mode", "default")
                if call_mode not in ("default", "polling"):
                    polling_mode_enabled = call_logic_config.get("polling_mode_enabled", False)
                    call_mode = "polling" if polling_mode_enabled else "default"
                else:
                    polling_mode_enabled = call_mode == "polling"

            await db.execute("""
                INSERT INTO call_logic_config (id, call_mode, polling_mode_enabled)
                VALUES (1, ?, ?)
            """, (call_mode, polling_mode_enabled))

        # Ensure cache_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM cache_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            cache_enabled = False
            cache_timeout = 7200
            cache_base_url = None

            if config_dict:
                cache_config = config_dict.get("cache", {})
                cache_enabled = cache_config.get("enabled", False)
                cache_timeout = cache_config.get("timeout", 7200)
                cache_base_url = cache_config.get("base_url", "")
                # Convert empty string to None
                cache_base_url = cache_base_url if cache_base_url else None

            await db.execute("""
                INSERT INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                VALUES (1, ?, ?, ?)
            """, (cache_enabled, cache_timeout, cache_base_url))

        # Ensure debug_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM debug_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            debug_enabled = False
            log_requests = True
            log_responses = True
            mask_token = True

            if config_dict:
                debug_config = config_dict.get("debug", {})
                debug_enabled = debug_config.get("enabled", False)
                log_requests = debug_config.get("log_requests", True)
                log_responses = debug_config.get("log_responses", True)
                mask_token = debug_config.get("mask_token", True)

            await db.execute("""
                INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                VALUES (1, ?, ?, ?, ?)
            """, (debug_enabled, log_requests, log_responses, mask_token))

        # Ensure captcha_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM captcha_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            captcha_method = "browser"
            yescaptcha_api_key = ""
            yescaptcha_base_url = "https://api.yescaptcha.com"
            yescaptcha_task_type = DEFAULT_YESCAPTCHA_TASK_TYPE
            remote_browser_base_url = ""
            remote_browser_api_key = ""
            remote_browser_timeout = 60
            browser_count = 1
            personal_project_pool_size = 4
            personal_max_resident_tabs = 5
            browser_personal_fresh_restart_every_n_solves = 10
            personal_idle_tab_ttl_seconds = 600

            if config_dict:
                captcha_config = config_dict.get("captcha", {})
                captcha_method = captcha_config.get("captcha_method", "browser")
                yescaptcha_api_key = captcha_config.get("yescaptcha_api_key", "")
                yescaptcha_base_url = captcha_config.get("yescaptcha_base_url", "https://api.yescaptcha.com")
                yescaptcha_task_type = normalize_yescaptcha_task_type(captcha_config.get("yescaptcha_task_type"))
                remote_browser_base_url = captcha_config.get("remote_browser_base_url", "")
                remote_browser_api_key = captcha_config.get("remote_browser_api_key", "")
                remote_browser_timeout = captcha_config.get("remote_browser_timeout", 60)
                browser_count = captcha_config.get("browser_count", 1)
                personal_project_pool_size = captcha_config.get("personal_project_pool_size", 4)
                personal_max_resident_tabs = captcha_config.get("personal_max_resident_tabs", 5)
                browser_personal_fresh_restart_every_n_solves = captcha_config.get("browser_personal_fresh_restart_every_n_solves", 10)
                personal_idle_tab_ttl_seconds = captcha_config.get("personal_idle_tab_ttl_seconds", 600)
            try:
                remote_browser_timeout = max(5, int(remote_browser_timeout))
            except Exception:
                remote_browser_timeout = 60
            try:
                browser_count = max(1, min(10, int(browser_count)))
            except Exception:
                browser_count = 1
            try:
                personal_project_pool_size = max(1, min(50, int(personal_project_pool_size)))
            except Exception:
                personal_project_pool_size = 4
            try:
                personal_max_resident_tabs = max(1, min(50, int(personal_max_resident_tabs)))
            except Exception:
                personal_max_resident_tabs = 5
            try:
                browser_personal_fresh_restart_every_n_solves = max(0, int(browser_personal_fresh_restart_every_n_solves))
            except Exception:
                browser_personal_fresh_restart_every_n_solves = 10
            try:
                personal_idle_tab_ttl_seconds = max(60, int(personal_idle_tab_ttl_seconds))
            except Exception:
                personal_idle_tab_ttl_seconds = 600

            await db.execute("""
                INSERT INTO captcha_config (
                    id, captcha_method, yescaptcha_api_key, yescaptcha_base_url,
                    yescaptcha_task_type,
                    remote_browser_base_url, remote_browser_api_key, remote_browser_timeout,
                    browser_count, personal_project_pool_size,
                    personal_max_resident_tabs, browser_personal_fresh_restart_every_n_solves,
                    personal_idle_tab_ttl_seconds
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                captcha_method,
                yescaptcha_api_key,
                yescaptcha_base_url,
                yescaptcha_task_type,
                remote_browser_base_url,
                remote_browser_api_key,
                remote_browser_timeout,
                browser_count,
                personal_project_pool_size,
                personal_max_resident_tabs,
                browser_personal_fresh_restart_every_n_solves,
                personal_idle_tab_ttl_seconds,
            ))

        # Ensure plugin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM plugin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            await db.execute("""
                INSERT INTO plugin_config (id, connection_token, auto_enable_on_update)
                VALUES (1, '', 1)
            """)

        # Ensure token_refresh_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM token_refresh_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            await db.execute("""
                INSERT INTO token_refresh_config (id, enabled, refresh_interval_minutes)
                VALUES (1, 1, 120)
            """)

    async def check_and_migrate_db(self, config_dict: dict = None):
        """Check database integrity and perform migrations if needed

        This method is called during upgrade mode to:
        1. Create missing tables (if they don't exist)
        2. Add missing columns to existing tables
        3. Ensure all config tables have default rows

        Args:
            config_dict: Configuration dictionary from setting.toml (optional)
                        Used only to initialize missing config rows with default values.
                        Existing config rows will NOT be overwritten.
        """
        async with self._connect(write=True) as db:
            print("Checking database integrity and performing migrations...")
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")

            # ========== Step 1: Create missing tables ==========
            # Check and create cache_config table if missing
            if not await self._table_exists(db, "cache_config"):
                print("  ✓ Creating missing table: cache_config")
                await db.execute("""
                    CREATE TABLE cache_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        cache_enabled BOOLEAN DEFAULT 0,
                        cache_timeout INTEGER DEFAULT 7200,
                        cache_base_url TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create proxy_config table if missing
            if not await self._table_exists(db, "proxy_config"):
                print("  ✓ Creating missing table: proxy_config")
                await db.execute("""
                    CREATE TABLE proxy_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        enabled BOOLEAN DEFAULT 0,
                        proxy_url TEXT,
                        media_proxy_enabled BOOLEAN DEFAULT 0,
                        media_proxy_url TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create call_logic_config table if missing
            if not await self._table_exists(db, "call_logic_config"):
                print("  Creating missing table: call_logic_config")
                await db.execute("""
                    CREATE TABLE call_logic_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        call_mode TEXT DEFAULT 'default',
                        polling_mode_enabled BOOLEAN DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create captcha_config table if missing
            if not await self._table_exists(db, "captcha_config"):
                print("  ✓ Creating missing table: captcha_config")
                await db.execute("""
                    CREATE TABLE captcha_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        captcha_method TEXT DEFAULT 'browser',
                        yescaptcha_api_key TEXT DEFAULT '',
                        yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                        yescaptcha_task_type TEXT DEFAULT 'RecaptchaV3TaskProxylessM1S9',
                        capmonster_api_key TEXT DEFAULT '',
                        capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                        ezcaptcha_api_key TEXT DEFAULT '',
                        ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                        capsolver_api_key TEXT DEFAULT '',
                        capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                        remote_browser_base_url TEXT DEFAULT '',
                        remote_browser_api_key TEXT DEFAULT '',
                        remote_browser_timeout INTEGER DEFAULT 60,
                        website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                        page_action TEXT DEFAULT 'IMAGE_GENERATION',
                        browser_proxy_enabled BOOLEAN DEFAULT 0,
                        browser_proxy_url TEXT,
                        browser_count INTEGER DEFAULT 1,
                        personal_project_pool_size INTEGER DEFAULT 4,
                        personal_max_resident_tabs INTEGER DEFAULT 5,
                        browser_personal_fresh_restart_every_n_solves INTEGER DEFAULT 10,
                        personal_idle_tab_ttl_seconds INTEGER DEFAULT 600,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create plugin_config table if missing
            if not await self._table_exists(db, "plugin_config"):
                print("  ✓ Creating missing table: plugin_config")
                await db.execute("""
                    CREATE TABLE plugin_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        connection_token TEXT DEFAULT '',
                        auto_enable_on_update BOOLEAN DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            if not await self._table_exists(db, "token_refresh_config"):
                print("  ✓ Creating missing table: token_refresh_config")
                await db.execute("""
                    CREATE TABLE token_refresh_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        enabled BOOLEAN DEFAULT 1,
                        refresh_interval_minutes INTEGER DEFAULT 120,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # ========== Step 2: Add missing columns to existing tables ==========
            # Check and add missing columns to tokens table
            if await self._table_exists(db, "tokens"):
                columns_to_add = [
                    ("at", "TEXT"),  # Access Token
                    ("at_expires", "TIMESTAMP"),  # AT expiration time
                    ("credits", "INTEGER DEFAULT 0"),  # Balance
                    ("user_paygate_tier", "TEXT"),  # User tier
                    ("current_project_id", "TEXT"),  # Current project UUID
                    ("current_project_name", "TEXT"),  # Project name
                    ("image_enabled", "BOOLEAN DEFAULT 1"),
                    ("video_enabled", "BOOLEAN DEFAULT 1"),
                    ("image_concurrency", "INTEGER DEFAULT -1"),
                    ("video_concurrency", "INTEGER DEFAULT -1"),
                    ("captcha_proxy_url", "TEXT"),  # token级打码代理
                    ("extension_route_key", "TEXT"),  # extension 模式路由键
                    ("protocol_mode", "TEXT DEFAULT 'session'"),  # ST 刷新模式
                    ("google_cookies", "TEXT DEFAULT ''"),  # 协议登录 Google Cookies
                    ("login_account", "TEXT DEFAULT ''"),  # 协议登录账号提示
                    ("login_password", "TEXT DEFAULT ''"),  # 预留
                    ("proxy_url", "TEXT DEFAULT ''"),  # 协议刷新代理
                    ("auto_refresh_enabled", "BOOLEAN DEFAULT 1"),
                    ("refresh_interval_minutes", "INTEGER DEFAULT 120"),
                    ("last_st_refresh_at", "TIMESTAMP"),
                    ("last_st_refresh_result", "TEXT DEFAULT ''"),
                    ("account_profile_key", "TEXT DEFAULT ''"),
                    ("auth_state", "TEXT DEFAULT 'ok'"),
                    ("auth_failure_count", "INTEGER DEFAULT 0"),
                    ("auth_next_retry_at", "TIMESTAMP"),
                    ("last_auth_error_class", "TEXT DEFAULT ''"),
                    ("ban_reason", "TEXT"),  # 禁用原因
                    ("banned_at", "TIMESTAMP"),  # 禁用时间
                ]

                for col_name, col_type in columns_to_add:
                    if not await self._column_exists(db, "tokens", col_name):
                        try:
                            await db.execute(f"ALTER TABLE tokens ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to tokens table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to admin_config table
            if await self._table_exists(db, "admin_config"):
                if not await self._column_exists(db, "admin_config", "error_ban_threshold"):
                    try:
                        await db.execute("ALTER TABLE admin_config ADD COLUMN error_ban_threshold INTEGER DEFAULT 3")
                        print("  ✓ Added column 'error_ban_threshold' to admin_config table")
                    except Exception as e:
                        print(f"  ✗ Failed to add column 'error_ban_threshold': {e}")

            # Check and add missing columns to proxy_config table
            if await self._table_exists(db, "proxy_config"):
                proxy_columns_to_add = [
                    ("media_proxy_enabled", "BOOLEAN DEFAULT 0"),
                    ("media_proxy_url", "TEXT"),
                ]

                for col_name, col_type in proxy_columns_to_add:
                    if not await self._column_exists(db, "proxy_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE proxy_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to proxy_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to generation_config table
            if await self._table_exists(db, "generation_config"):
                generation_columns_to_add = [
                    ("max_retries", "INTEGER DEFAULT 3"),
                ]

                for col_name, col_type in generation_columns_to_add:
                    if not await self._column_exists(db, "generation_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE generation_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to generation_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to captcha_config table
            if await self._table_exists(db, "captcha_config"):
                captcha_columns_to_add = [
                    ("browser_proxy_enabled", "BOOLEAN DEFAULT 0"),
                    ("browser_proxy_url", "TEXT"),
                    ("yescaptcha_task_type", "TEXT DEFAULT 'RecaptchaV3TaskProxylessM1S9'"),
                    ("capmonster_api_key", "TEXT DEFAULT ''"),
                    ("capmonster_base_url", "TEXT DEFAULT 'https://api.capmonster.cloud'"),
                    ("ezcaptcha_api_key", "TEXT DEFAULT ''"),
                    ("ezcaptcha_base_url", "TEXT DEFAULT 'https://api.ez-captcha.com'"),
                    ("capsolver_api_key", "TEXT DEFAULT ''"),
                    ("capsolver_base_url", "TEXT DEFAULT 'https://api.capsolver.com'"),
                    ("browser_count", "INTEGER DEFAULT 1"),
                    ("remote_browser_base_url", "TEXT DEFAULT ''"),
                    ("remote_browser_api_key", "TEXT DEFAULT ''"),
                    ("remote_browser_timeout", "INTEGER DEFAULT 60"),
                ]

                for col_name, col_type in captcha_columns_to_add:
                    if not await self._column_exists(db, "captcha_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE captcha_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to captcha_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to token_stats table
            if await self._table_exists(db, "token_stats"):
                stats_columns_to_add = [
                    ("today_image_count", "INTEGER DEFAULT 0"),
                    ("today_video_count", "INTEGER DEFAULT 0"),
                    ("today_error_count", "INTEGER DEFAULT 0"),
                    ("today_date", "DATE"),
                    ("consecutive_error_count", "INTEGER DEFAULT 0"),  # 🆕 连续错误计数
                ]

                for col_name, col_type in stats_columns_to_add:
                    if not await self._column_exists(db, "token_stats", col_name):
                        try:
                            await db.execute(f"ALTER TABLE token_stats ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to token_stats table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to plugin_config table
            if await self._table_exists(db, "plugin_config"):
                plugin_columns_to_add = [
                    ("auto_enable_on_update", "BOOLEAN DEFAULT 1"),  # 默认开启
                ]

                for col_name, col_type in plugin_columns_to_add:
                    if not await self._column_exists(db, "plugin_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE plugin_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to plugin_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            if await self._table_exists(db, "token_refresh_config"):
                token_refresh_columns_to_add = [
                    ("enabled", "BOOLEAN DEFAULT 1"),
                    ("refresh_interval_minutes", "INTEGER DEFAULT 120"),
                    ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                ]

                for col_name, col_type in token_refresh_columns_to_add:
                    if not await self._column_exists(db, "token_refresh_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE token_refresh_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to token_refresh_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to captcha_config table
            if await self._table_exists(db, "captcha_config"):
                captcha_columns_to_add = [
                    ("personal_project_pool_size", "INTEGER DEFAULT 4"),
                    ("personal_max_resident_tabs", "INTEGER DEFAULT 5"),
                    ("browser_personal_fresh_restart_every_n_solves", "INTEGER DEFAULT 10"),
                    ("personal_idle_tab_ttl_seconds", "INTEGER DEFAULT 600"),
                ]

                for col_name, col_type in captcha_columns_to_add:
                    if not await self._column_exists(db, "captcha_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE captcha_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to captcha_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Extend the existing tasks fact source for Batch 2 idempotency/recovery.
            if await self._table_exists(db, "tasks"):
                task_columns_to_add = [
                    ("idempotency_key", "TEXT"),
                    ("has_media", "BOOLEAN DEFAULT 0"),
                    ("error_class", "TEXT"),
                    ("updated_at", "TIMESTAMP"),
                    ("quota_state", "TEXT"),
                    ("quota_reserved", "INTEGER DEFAULT 0"),
                ]
                for col_name, col_type in task_columns_to_add:
                    if not await self._column_exists(db, "tasks", col_name):
                        await db.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}")
                await db.execute("UPDATE tasks SET updated_at = created_at WHERE updated_at IS NULL")
                await db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency_key_unique "
                    "ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''"
                )

            await self._ensure_account_model_availability_table(db)
            await self._backfill_account_model_availability(db)

            # ========== Step 3: Ensure all config tables have default rows ==========
            # Note: This will NOT overwrite existing config rows
            # It only ensures missing rows are created with default values from setting.toml
            await self._ensure_config_rows(db, config_dict=config_dict)

            await db.commit()
            print("Database migration check completed.")

    async def init_db(self):
        """Initialize database tables"""
        async with self._connect(write=True) as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")
            # Tokens table (Flow2API版本)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    st TEXT UNIQUE NOT NULL,
                    at TEXT,
                    at_expires TIMESTAMP,
                    email TEXT NOT NULL,
                    name TEXT,
                    remark TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP,
                    use_count INTEGER DEFAULT 0,
                    credits INTEGER DEFAULT 0,
                    user_paygate_tier TEXT,
                    current_project_id TEXT,
                    current_project_name TEXT,
                    image_enabled BOOLEAN DEFAULT 1,
                    video_enabled BOOLEAN DEFAULT 1,
                    image_concurrency INTEGER DEFAULT -1,
                    video_concurrency INTEGER DEFAULT -1,
                    captcha_proxy_url TEXT,
                    extension_route_key TEXT,
                    protocol_mode TEXT DEFAULT 'session',
                    google_cookies TEXT DEFAULT '',
                    login_account TEXT DEFAULT '',
                    login_password TEXT DEFAULT '',
                    proxy_url TEXT DEFAULT '',
                    auto_refresh_enabled BOOLEAN DEFAULT 1,
                    refresh_interval_minutes INTEGER DEFAULT 120,
                    last_st_refresh_at TIMESTAMP,
                    last_st_refresh_result TEXT DEFAULT '',
                    account_profile_key TEXT DEFAULT '',
                    auth_state TEXT DEFAULT 'ok',
                    auth_failure_count INTEGER DEFAULT 0,
                    auth_next_retry_at TIMESTAMP,
                    last_auth_error_class TEXT DEFAULT '',
                    ban_reason TEXT,
                    banned_at TIMESTAMP
                )
            """)

            # Projects table (新增)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    project_name TEXT NOT NULL,
                    tool_name TEXT DEFAULT 'PINHOLE',
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Token stats table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS token_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER NOT NULL,
                    image_count INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    last_success_at TIMESTAMP,
                    last_error_at TIMESTAMP,
                    today_image_count INTEGER DEFAULT 0,
                    today_video_count INTEGER DEFAULT 0,
                    today_error_count INTEGER DEFAULT 0,
                    today_date DATE,
                    consecutive_error_count INTEGER DEFAULT 0,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Tasks table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'created',
                    progress INTEGER DEFAULT 0,
                    result_urls TEXT,
                    error_message TEXT,
                    error_class TEXT,
                    has_media BOOLEAN DEFAULT 0,
                    idempotency_key TEXT,
                    quota_state TEXT,
                    quota_reserved INTEGER DEFAULT 0,
                    scene_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Request logs table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER,
                    operation TEXT NOT NULL,
                    request_body TEXT,
                    response_body TEXT,
                    status_code INTEGER NOT NULL,
                    duration FLOAT NOT NULL,
                    status_text TEXT DEFAULT '',
                    progress INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Admin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    username TEXT DEFAULT 'admin',
                    password TEXT DEFAULT 'admin',
                    api_key TEXT DEFAULT 'han1234',
                    error_ban_threshold INTEGER DEFAULT 3,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Proxy config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS proxy_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    proxy_url TEXT,
                    media_proxy_enabled BOOLEAN DEFAULT 0,
                    media_proxy_url TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Generation config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS generation_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    image_timeout INTEGER DEFAULT 300,
                    video_timeout INTEGER DEFAULT 1500,
                    max_retries INTEGER DEFAULT 3,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Call logic config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS call_logic_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    call_mode TEXT DEFAULT 'default',
                    polling_mode_enabled BOOLEAN DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Cache config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cache_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    cache_enabled BOOLEAN DEFAULT 0,
                    cache_timeout INTEGER DEFAULT 7200,
                    cache_base_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Debug config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS debug_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    log_requests BOOLEAN DEFAULT 1,
                    log_responses BOOLEAN DEFAULT 1,
                    mask_token BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Captcha config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS captcha_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    captcha_method TEXT DEFAULT 'browser',
                    yescaptcha_api_key TEXT DEFAULT '',
                    yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                    yescaptcha_task_type TEXT DEFAULT 'RecaptchaV3TaskProxylessM1S9',
                    capmonster_api_key TEXT DEFAULT '',
                    capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                    ezcaptcha_api_key TEXT DEFAULT '',
                    ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                    capsolver_api_key TEXT DEFAULT '',
                    capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                    remote_browser_base_url TEXT DEFAULT '',
                    remote_browser_api_key TEXT DEFAULT '',
                    remote_browser_timeout INTEGER DEFAULT 60,
                    website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                    page_action TEXT DEFAULT 'IMAGE_GENERATION',

                    browser_proxy_enabled BOOLEAN DEFAULT 0,
                    browser_proxy_url TEXT,
                    browser_count INTEGER DEFAULT 1,
                    personal_project_pool_size INTEGER DEFAULT 4,
                    personal_max_resident_tabs INTEGER DEFAULT 5,
                    browser_personal_fresh_restart_every_n_solves INTEGER DEFAULT 10,
                    personal_idle_tab_ttl_seconds INTEGER DEFAULT 600,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Plugin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS plugin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    connection_token TEXT DEFAULT '',
                    auto_enable_on_update BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS token_refresh_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 1,
                    refresh_interval_minutes INTEGER DEFAULT 120,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Extension plugin sessions deliberately retain only a digest and
            # non-secret route binding, so a server restart does not force a
            # paired extension to authenticate again.
            await self._ensure_extension_plugin_sessions_table(db)
            await self._ensure_account_model_availability_table(db)

            # Create indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_task_id ON tasks(task_id)")
            if await self._column_exists(db, "tasks", "idempotency_key"):
                await db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency_key_unique "
                    "ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''"
                )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token_st ON tokens(st)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_project_id ON projects(project_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tokens_email ON tokens(email)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tokens_is_active_last_used_at ON tokens(is_active, last_used_at)")

            # Migrate request_logs table if needed
            await self._migrate_request_logs(db)

            # Request logs query indexes (列表按 created_at 排序 / token 过滤)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_created_at ON request_logs(created_at DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_token_id_created_at ON request_logs(token_id, created_at DESC)")

            # Token stats lookup index
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token_stats_token_id ON token_stats(token_id)")

            await db.commit()

    async def _migrate_request_logs(self, db):
        """Migrate request_logs table from old schema to new schema"""
        try:
            has_model = await self._column_exists(db, "request_logs", "model")
            has_operation = await self._column_exists(db, "request_logs", "operation")

            if has_model and not has_operation:
                print("?? ?????request_logs???,????...")
                await db.execute("ALTER TABLE request_logs RENAME TO request_logs_old")
                await db.execute("""
                    CREATE TABLE request_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_id INTEGER,
                        operation TEXT NOT NULL,
                        request_body TEXT,
                        response_body TEXT,
                        status_code INTEGER NOT NULL,
                        duration FLOAT NOT NULL,
                        status_text TEXT DEFAULT '',
                        progress INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (token_id) REFERENCES tokens(id)
                    )
                """)
                await db.execute("""
                    INSERT INTO request_logs (token_id, operation, request_body, status_code, duration, status_text, progress, created_at, updated_at)
                    SELECT
                        token_id,
                        model as operation,
                        json_object('model', model, 'prompt', substr(prompt, 1, 100)) as request_body,
                        CASE
                            WHEN status = 'completed' THEN 200
                            WHEN status = 'failed' THEN 500
                            ELSE 102
                        END as status_code,
                        response_time as duration,
                        CASE
                            WHEN status = 'completed' THEN 'completed'
                            WHEN status = 'failed' THEN 'failed'
                            ELSE 'processing'
                        END as status_text,
                        CASE
                            WHEN status = 'completed' THEN 100
                            WHEN status = 'failed' THEN 0
                            ELSE 0
                        END as progress,
                        created_at,
                        created_at
                    FROM request_logs_old
                """)
                await db.execute("DROP TABLE request_logs_old")
                print("? request_logs?????")

            if not await self._column_exists(db, "request_logs", "status_text"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN status_text TEXT DEFAULT ''")
            if not await self._column_exists(db, "request_logs", "progress"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN progress INTEGER DEFAULT 0")
            if not await self._column_exists(db, "request_logs", "updated_at"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN updated_at TIMESTAMP")
            await db.execute("UPDATE request_logs SET updated_at = created_at WHERE updated_at IS NULL")
        except Exception as e:
            print(f"?? request_logs?????: {e}")
            # Continue even if migration fails

    # Token operations
    async def add_token(self, token: Token) -> int:
        """Add a new token"""
        protected_google_cookies = self._protect_google_cookies(token.google_cookies)
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO tokens (st, at, at_expires, email, name, remark, is_active,
                                   credits, user_paygate_tier, current_project_id, current_project_name,
                                   image_enabled, video_enabled, image_concurrency, video_concurrency,
                                   captcha_proxy_url, extension_route_key,
                                   protocol_mode, google_cookies, login_account, login_password,
                                   proxy_url, auto_refresh_enabled, refresh_interval_minutes,
                                   last_st_refresh_at, last_st_refresh_result,
                                   account_profile_key, auth_state, auth_failure_count,
                                   auth_next_retry_at, last_auth_error_class)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (token.st, token.at, token.at_expires, token.email, token.name, token.remark,
                  token.is_active, token.credits, token.user_paygate_tier,
                  token.current_project_id, token.current_project_name,
                  token.image_enabled, token.video_enabled,
                  token.image_concurrency, token.video_concurrency,
                  token.captcha_proxy_url, token.extension_route_key,
                  token.protocol_mode, protected_google_cookies, token.login_account,
                  "", token.proxy_url, token.auto_refresh_enabled,
                  token.refresh_interval_minutes, token.last_st_refresh_at,
                  token.last_st_refresh_result, token.account_profile_key,
                  token.auth_state, token.auth_failure_count,
                  token.auth_next_retry_at, token.last_auth_error_class))
            await db.commit()
            token_id = cursor.lastrowid

            # Create stats entry
            await db.execute("""
                INSERT INTO token_stats (token_id) VALUES (?)
            """, (token_id,))
            await db.commit()

            return token_id

    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return Token(**self._decode_token_row(row))
            return None

    async def get_token_edit_config(self, token_id: int) -> Optional[Dict[str, Any]]:
        """Return non-credential fields used by the write-only admin edit form."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    id,
                    remark,
                    current_project_id,
                    current_project_name,
                    image_enabled,
                    video_enabled,
                    image_concurrency,
                    video_concurrency,
                    protocol_mode,
                    auto_refresh_enabled,
                    refresh_interval_minutes
                FROM tokens
                WHERE id = ?
                """,
                (token_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_token_by_st(self, st: str) -> Optional[Token]:
        """Get token by ST"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE st = ?", (st,))
            row = await cursor.fetchone()
            if row:
                return Token(**self._decode_token_row(row))
            return None

    async def get_token_by_email(self, email: str) -> Optional[Token]:
        """Get token by email"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE email = ?", (email,))
            row = await cursor.fetchone()
            if row:
                return Token(**self._decode_token_row(row))
            return None

    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens ORDER BY created_at DESC")
            rows = await cursor.fetchall()
            return [Token(**self._decode_token_row(row)) for row in rows]

    async def get_all_tokens_with_stats(self) -> List[Dict[str, Any]]:
        """Get all tokens with merged statistics in one query"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            today = self._current_stats_date()
            cursor = await db.execute("""
                SELECT
                    t.*,
                    COALESCE(ts.image_count, 0) AS image_count,
                    COALESCE(ts.video_count, 0) AS video_count,
                    COALESCE(ts.error_count, 0) AS error_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_image_count ELSE 0 END, 0) AS today_image_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_video_count ELSE 0 END, 0) AS today_video_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_error_count ELSE 0 END, 0) AS today_error_count,
                    COALESCE(ts.consecutive_error_count, 0) AS consecutive_error_count,
                    ts.last_error_at AS last_error_at
                FROM tokens t
                LEFT JOIN token_stats ts ON ts.token_id = t.id
                ORDER BY t.created_at DESC
            """, (today, today, today))
            rows = await cursor.fetchall()
            return [self._decode_token_row(row) for row in rows]

    async def get_tokens_page_with_stats(self, limit: int, offset: int) -> Dict[str, Any]:
        """Return one deterministic, credential-free account summary page."""
        try:
            normalized_limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            normalized_limit = 25
        try:
            normalized_offset = max(0, int(offset))
        except (TypeError, ValueError):
            normalized_offset = 0

        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            count_cursor = await db.execute("SELECT COUNT(*) AS total FROM tokens")
            count_row = await count_cursor.fetchone()
            total = max(0, int(count_row["total"] if count_row else 0))

            page_cursor = await db.execute(
                """
                SELECT
                    t.id,
                    t.name,
                    t.remark,
                    t.is_active,
                    t.credits,
                    t.ban_reason,
                    t.auth_state,
                    t.auth_next_retry_at,
                    CASE
                        WHEN LENGTH(TRIM(COALESCE(t.account_profile_key, ''))) > 0 THEN 1
                        ELSE 0
                    END AS has_account_profile,
                    t.created_at,
                    COALESCE(SUM(
                        CASE
                            WHEN task.quota_state = 'reserved'
                            THEN task.quota_reserved
                            ELSE 0
                        END
                    ), 0) AS credits_reserved
                FROM tokens AS t
                LEFT JOIN tasks AS task ON task.token_id = t.id
                GROUP BY
                    t.id,
                    t.name,
                    t.remark,
                    t.is_active,
                    t.credits,
                    t.ban_reason,
                    t.auth_state,
                    t.auth_next_retry_at,
                    t.account_profile_key,
                    t.created_at
                ORDER BY t.created_at DESC, t.id DESC
                LIMIT ? OFFSET ?
                """,
                (normalized_limit, normalized_offset),
            )
            rows = await page_cursor.fetchall()

        items = [self._decode_token_row(row) for row in rows]
        return {
            "items": items,
            "total": total,
            "limit": normalized_limit,
            "offset": normalized_offset,
            "has_next": normalized_offset + len(items) < total,
        }

    async def get_dashboard_stats(self) -> Dict[str, int]:
        """Get dashboard counters with aggregated SQL queries"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            today = self._current_stats_date()

            token_cursor = await db.execute("""
                SELECT
                    COUNT(*) AS total_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens,
                    COALESCE(SUM(
                        CASE
                            WHEN is_active = 1 AND auth_state = 'ok' THEN 1
                            ELSE 0
                        END
                    ), 0) AS ready_tokens
                FROM tokens
            """)
            token_row = await token_cursor.fetchone()

            stats_cursor = await db.execute("""
                SELECT
                    COALESCE(SUM(image_count), 0) AS total_images,
                    COALESCE(SUM(video_count), 0) AS total_videos,
                    COALESCE(SUM(error_count), 0) AS total_errors,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_image_count ELSE 0 END), 0) AS today_images,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_video_count ELSE 0 END), 0) AS today_videos,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_error_count ELSE 0 END), 0) AS today_errors
                FROM token_stats
            """, (today, today, today))
            stats_row = await stats_cursor.fetchone()

            token_data = dict(token_row) if token_row else {}
            stats_data = dict(stats_row) if stats_row else {}

            return {
                "total_tokens": int(token_data.get("total_tokens") or 0),
                "active_tokens": int(token_data.get("active_tokens") or 0),
                "ready_tokens": int(token_data.get("ready_tokens") or 0),
                "total_images": int(stats_data.get("total_images") or 0),
                "total_videos": int(stats_data.get("total_videos") or 0),
                "total_errors": int(stats_data.get("total_errors") or 0),
                "today_images": int(stats_data.get("today_images") or 0),
                "today_videos": int(stats_data.get("today_videos") or 0),
                "today_errors": int(stats_data.get("today_errors") or 0)
            }

    async def get_system_info_stats(self) -> Dict[str, int]:
        """Get lightweight system counters used by admin dashboard"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT
                    COUNT(*) AS total_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN credits ELSE 0 END), 0) AS total_credits
                FROM tokens
            """)
            row = await cursor.fetchone()
            data = dict(row) if row else {}
            return {
                "total_tokens": int(data.get("total_tokens") or 0),
                "active_tokens": int(data.get("active_tokens") or 0),
                "total_credits": int(data.get("total_credits") or 0)
            }

    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE is_active = 1 ORDER BY last_used_at ASC")
            rows = await cursor.fetchall()
            return [Token(**self._decode_token_row(row)) for row in rows]

    async def get_auth_recovery_candidates(self, now: datetime) -> List[Token]:
        """Return user-enabled accounts whose authentication recovery may run now."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM tokens
                WHERE is_active = 1
                  AND auto_refresh_enabled = 1
                  AND auth_state != 'reauth_required'
                  AND (
                    auth_state != 'backoff'
                    OR auth_next_retry_at IS NULL
                    OR julianday(auth_next_retry_at) <= julianday(?)
                  )
                ORDER BY last_st_refresh_at ASC, id ASC
                """,
                (now,),
            )
            rows = await cursor.fetchall()
            return [Token(**self._decode_token_row(row)) for row in rows]

    async def update_token_auth_state(
        self,
        token_id: int,
        *,
        state: str,
        failure_count: int,
        next_retry_at: Optional[datetime],
        error_class: str,
    ) -> None:
        """Persist only allowlisted authentication recovery metadata."""
        normalized_state = str(state or "").strip()
        normalized_error_class = str(error_class or "").strip()
        if normalized_state not in _AUTH_STATES:
            raise ValueError("unsupported auth state")
        if normalized_error_class not in _AUTH_ERROR_CLASSES:
            raise ValueError("unsupported auth error class")
        try:
            normalized_failure_count = max(0, int(failure_count))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid auth failure count") from exc

        await self.update_token(
            token_id,
            auth_state=normalized_state,
            auth_failure_count=normalized_failure_count,
            auth_next_retry_at=next_retry_at,
            last_auth_error_class=normalized_error_class,
        )

    async def commit_account_reauth(
        self,
        token_id: int,
        *,
        st: str,
        at: str,
        at_expires: Optional[datetime],
        google_cookies: str,
        account_profile_key: str,
    ) -> None:
        """Commit one verified re-login as a single token-state transaction."""
        profile_key = str(account_profile_key or "").strip()
        if not profile_key:
            raise ValueError("account profile key is required")

        async with self._connect(write=True) as db:
            try:
                cursor = await db.execute(
                    """
                    UPDATE tokens
                    SET st = ?,
                        at = ?,
                        at_expires = ?,
                        protocol_mode = 'protocol',
                        google_cookies = ?,
                        account_profile_key = ?,
                        auth_state = 'ok',
                        auth_failure_count = 0,
                        auth_next_retry_at = NULL,
                        last_auth_error_class = '',
                        last_st_refresh_at = CURRENT_TIMESTAMP,
                        last_st_refresh_result = 'success',
                        is_active = 1,
                        ban_reason = NULL,
                        banned_at = NULL
                    WHERE id = ?
                    """,
                    (
                        st,
                        at,
                        at_expires,
                        self._protect_google_cookies(google_cookies),
                        profile_key,
                        token_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("token_not_found")
                await db.execute(
                    "UPDATE token_stats SET consecutive_error_count = 0 WHERE token_id = ?",
                    (token_id,),
                )
                await db.commit()
            except BaseException:
                try:
                    await db.rollback()
                except Exception:
                    pass
                raise

    async def update_token(self, token_id: int, **kwargs):
        """Update token fields"""
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if key == "google_cookies":
                    value = self._protect_google_cookies(value)
                elif key == "login_password":
                    value = ""
                updates.append(f"{key} = ?")
                params.append(value)

            if updates:
                params.append(token_id)
                query = f"UPDATE tokens SET {', '.join(updates)} WHERE id = ?"
                await db.execute(query, params)
                await db.commit()

    async def delete_token(self, token_id: int):
        """Delete token and related data"""
        async with self._connect(write=True) as db:
            await db.execute("UPDATE request_logs SET token_id = NULL WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM tasks WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM token_stats WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM projects WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM account_model_availability WHERE token_id = ?", (token_id,))
            await db.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            await db.commit()

    # Project operations
    async def add_project(self, project: Project) -> int:
        """Add a new project"""
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO projects (project_id, token_id, project_name, tool_name, is_active)
                VALUES (?, ?, ?, ?, ?)
            """, (project.project_id, project.token_id, project.project_name,
                  project.tool_name, project.is_active))
            await db.commit()
            return cursor.lastrowid

    async def get_project_by_id(self, project_id: str) -> Optional[Project]:
        """Get project by UUID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
            row = await cursor.fetchone()
            if row:
                return Project(**dict(row))
            return None

    async def get_projects_by_token(self, token_id: int) -> List[Project]:
        """Get all projects for a token"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM projects WHERE token_id = ? ORDER BY created_at DESC",
                (token_id,)
            )
            rows = await cursor.fetchall()
            return [Project(**dict(row)) for row in rows]

    async def delete_project(self, project_id: str):
        """Delete project"""
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            await db.commit()

    # Task operations
    @staticmethod
    def _task_from_row(row) -> Optional[Task]:
        if not row:
            return None
        task_dict = dict(row)
        if task_dict.get("result_urls"):
            task_dict["result_urls"] = json.loads(task_dict["result_urls"])
        return Task(**task_dict)

    async def create_task(self, task: Task) -> int:
        """Create a task in the existing tasks fact source."""
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO tasks (
                    task_id, token_id, model, prompt, status, progress, result_urls,
                    error_message, error_class, has_media, idempotency_key,
                    quota_state, quota_reserved, scene_id, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            """, (
                task.task_id,
                task.token_id,
                task.model,
                task.prompt or "",
                task.status,
                task.progress,
                json.dumps(task.result_urls) if task.result_urls else None,
                task.error_message,
                task.error_class,
                bool(task.has_media),
                task.idempotency_key,
                task.quota_state,
                max(0, int(task.quota_reserved or 0)),
                task.scene_id,
                task.completed_at,
            ))
            await db.commit()
            return cursor.lastrowid

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            return self._task_from_row(await cursor.fetchone())

    async def get_task_by_idempotency_key(self, idempotency_key: str) -> Optional[Task]:
        """Get the single persisted task claimed by a non-empty idempotency key."""
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ? LIMIT 1",
                (normalized_key,),
            )
            return self._task_from_row(await cursor.fetchone())

    async def get_or_create_idempotent_task(
        self,
        *,
        idempotency_key: str,
        task_id: str,
        token_id: int,
        model: str,
        status: str = "submitting",
    ) -> tuple[Task, bool]:
        """Atomically claim an idempotency key using the database unique index."""
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key:
            raise ValueError("idempotency_key must not be empty")

        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO tasks (
                    task_id, token_id, model, prompt, status, progress,
                    has_media, idempotency_key, updated_at
                ) VALUES (?, ?, ?, '', ?, 0, 0, ?, CURRENT_TIMESTAMP)
                """,
                (task_id, token_id, model, status, normalized_key),
            )
            created = cursor.rowcount == 1
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ? LIMIT 1",
                (normalized_key,),
            )
            task = self._task_from_row(await cursor.fetchone())
            if task is None:
                raise RuntimeError("idempotent task claim was not persisted")
            return task, created

    async def get_available_token_credits(self, token_id: int) -> int:
        """Return refreshed token credits minus currently frozen task reservations."""
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT COALESCE(t.credits, 0) - COALESCE((
                    SELECT SUM(CASE WHEN quota_state = 'reserved' THEN quota_reserved ELSE 0 END)
                    FROM tasks
                    WHERE token_id = t.id
                ), 0)
                FROM tokens t
                WHERE t.id = ?
                """,
                (token_id,),
            )
            row = await cursor.fetchone()
            return max(0, int(row[0] or 0)) if row else 0

    async def reserve_task_quota(self, task_id: str, token_id: int, units: int = 1) -> bool:
        """Atomically reserve task quota against the current token credit snapshot."""
        units = max(1, int(units or 1))
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT token_id, quota_state, quota_reserved FROM tasks WHERE task_id = ?",
                (task_id,),
            )
            task_row = await cursor.fetchone()
            if not task_row:
                await db.rollback()
                return False

            current_reserved = max(0, int(task_row["quota_reserved"] or 0))
            current_state = str(task_row["quota_state"] or "")
            if current_state == "reserved" and current_reserved > 0:
                same_token = int(task_row["token_id"]) == int(token_id)
                await db.commit()
                return same_token

            cursor = await db.execute("SELECT credits FROM tokens WHERE id = ?", (token_id,))
            token_row = await cursor.fetchone()
            if not token_row:
                await db.rollback()
                return False

            cursor = await db.execute(
                """
                SELECT COALESCE(SUM(quota_reserved), 0)
                FROM tasks
                WHERE token_id = ? AND quota_state = 'reserved' AND task_id <> ?
                """,
                (token_id, task_id),
            )
            reserved_elsewhere = max(0, int((await cursor.fetchone())[0] or 0))
            available = max(0, int(token_row["credits"] or 0) - reserved_elsewhere)
            if available < units:
                await db.rollback()
                return False

            await db.execute(
                """
                UPDATE tasks
                SET token_id = ?, quota_state = 'reserved', quota_reserved = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (token_id, units, task_id),
            )
            await db.commit()
            return True

    async def release_task_quota(self, task_id: str) -> bool:
        """Release a frozen task reservation exactly once without changing token credits."""
        async with self._connect(write=True) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE tasks
                SET quota_state = 'released', quota_reserved = 0, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND quota_state = 'reserved' AND quota_reserved > 0
                """,
                (task_id,),
            )
            changed = cursor.rowcount == 1
            await db.commit()
            return changed

    async def settle_task_quota(self, task_id: str) -> bool:
        """Settle a frozen reservation exactly once and decrement the token snapshot by one unit."""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT token_id, quota_state, quota_reserved FROM tasks WHERE task_id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()
            if not row or str(row["quota_state"] or "") != "reserved":
                await db.commit()
                return False

            units = max(0, int(row["quota_reserved"] or 0))
            if units <= 0:
                await db.commit()
                return False

            await db.execute(
                "UPDATE tokens SET credits = MAX(0, credits - ?) WHERE id = ?",
                (units, int(row["token_id"])),
            )
            await db.execute(
                """
                UPDATE tasks
                SET quota_state = 'settled', quota_reserved = 0, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (task_id,),
            )
            await db.commit()
            return True

    async def get_tasks_by_statuses(self, statuses: List[str]) -> List[Task]:
        """Return persisted tasks matching the requested recovery states."""
        normalized = [str(status).strip() for status in statuses if str(status).strip()]
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY id",
                normalized,
            )
            return [self._task_from_row(row) for row in await cursor.fetchall()]

    async def update_task(self, current_task_id: str, **kwargs):
        """Update task fields while preserving the existing tasks fact source."""
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    if key == "result_urls" and isinstance(value, list):
                        value = json.dumps(value)
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.append(current_task_id)
                query = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?"
                await db.execute(query, params)
                await db.commit()

    async def record_account_model_available(self, token_id: int, model: str) -> None:
        """Record a completed generation that returned real media for an account/model pair."""
        async with self._connect(write=True) as db:
            await db.execute("""
                INSERT INTO account_model_availability (
                    token_id, model, status, error_class,
                    successful_generations, explicit_denials, last_verified_at
                ) VALUES (?, ?, 'available', '', 1, 0, CURRENT_TIMESTAMP)
                ON CONFLICT(token_id, model) DO UPDATE SET
                    status = 'available',
                    error_class = '',
                    successful_generations = account_model_availability.successful_generations + 1,
                    last_verified_at = CURRENT_TIMESTAMP
            """, (int(token_id), str(model)))
            await db.commit()

    async def record_account_model_unavailable(
        self,
        token_id: int,
        model: str,
        error_class: str,
    ) -> None:
        """Record only an explicit model-access denial; transient errors leave facts unchanged."""
        normalized_error_class = str(error_class or "").strip()
        if normalized_error_class not in {"model_access_denied", "membership_tier"}:
            return
        async with self._connect(write=True) as db:
            await db.execute("""
                INSERT INTO account_model_availability (
                    token_id, model, status, error_class,
                    successful_generations, explicit_denials, last_verified_at
                ) VALUES (?, ?, 'unavailable', ?, 0, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(token_id, model) DO UPDATE SET
                    status = 'unavailable',
                    error_class = excluded.error_class,
                    explicit_denials = account_model_availability.explicit_denials + 1,
                    last_verified_at = CURRENT_TIMESTAMP
            """, (int(token_id), str(model), normalized_error_class))
            await db.commit()

    async def get_account_model_availability(self, token_id: int) -> Dict[str, Dict[str, Any]]:
        """Return the public allowlist facts for one diagnostic account."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT model, status, error_class, last_verified_at
                FROM account_model_availability
                WHERE token_id = ?
                ORDER BY model
            """, (int(token_id),))
            return {
                row["model"]: {
                    "status": row["status"],
                    "error_class": row["error_class"],
                    "last_verified_at": row["last_verified_at"],
                }
                for row in await cursor.fetchall()
            }

    # Token stats operations (kept for compatibility, now delegates to specific methods)
    async def increment_token_stats(self, token_id: int, stat_type: str):
        """Increment token statistics (delegates to specific methods)"""
        if stat_type == "image":
            await self.increment_image_count(token_id)
        elif stat_type == "video":
            await self.increment_video_count(token_id)
        elif stat_type == "error":
            await self.increment_error_count(token_id)

    async def get_token_stats(self, token_id: int) -> Optional[TokenStats]:
        """Get token statistics"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return TokenStats(**dict(row))
            return None

    async def increment_image_count(self, token_id: int):
        """Increment image generation count with daily reset"""
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's image usage.
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = 1,
                        today_video_count = 0,
                        today_error_count = 0,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment both
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = today_image_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def increment_video_count(self, token_id: int):
        """Increment video generation count with daily reset"""
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's video usage.
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_image_count = 0,
                        today_video_count = 1,
                        today_error_count = 0,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment both
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_video_count = today_video_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def increment_error_count(self, token_id: int):
        """Increment error count with daily reset

        Updates two counters:
        - error_count: Historical total errors (never reset)
        - consecutive_error_count: Consecutive errors (reset on success/enable)
        - today_error_count: Today's errors (reset on date change)
        """
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's error.
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_image_count = 0,
                        today_video_count = 0,
                        today_error_count = 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment all counters
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_error_count = today_error_count + 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def reset_error_count(self, token_id: int):
        """Reset consecutive error count (only reset consecutive_error_count, keep error_count and today_error_count)

        This is called when:
        - Token is manually enabled by admin
        - Request succeeds (resets consecutive error counter)

        Note: error_count (total historical errors) is NEVER reset
        """
        async with self._connect(write=True) as db:
            await db.execute("""
                UPDATE token_stats SET consecutive_error_count = 0 WHERE token_id = ?
            """, (token_id,))
            await db.commit()

    # Config operations
    async def get_admin_config(self) -> Optional[AdminConfig]:
        """Get admin configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM admin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return AdminConfig(**dict(row))
            return None

    async def update_admin_config(self, **kwargs):
        """Update admin configuration"""
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                query = f"UPDATE admin_config SET {', '.join(updates)} WHERE id = 1"
                await db.execute(query, params)
                await db.commit()

    async def get_proxy_config(self) -> Optional[ProxyConfig]:
        """Get proxy configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return ProxyConfig(**dict(row))
            return None

    async def update_proxy_config(
        self,
        enabled: bool,
        proxy_url: Optional[str] = None,
        media_proxy_enabled: Optional[bool] = None,
        media_proxy_url: Optional[str] = None
    ):
        """Update proxy configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                new_media_proxy_enabled = (
                    media_proxy_enabled
                    if media_proxy_enabled is not None
                    else current.get("media_proxy_enabled", False)
                )
                new_media_proxy_url = (
                    media_proxy_url
                    if media_proxy_url is not None
                    else current.get("media_proxy_url")
                )

                await db.execute("""
                    UPDATE proxy_config
                    SET enabled = ?, proxy_url = ?,
                        media_proxy_enabled = ?, media_proxy_url = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (enabled, proxy_url, new_media_proxy_enabled, new_media_proxy_url))
            else:
                new_media_proxy_enabled = media_proxy_enabled if media_proxy_enabled is not None else False
                new_media_proxy_url = media_proxy_url
                await db.execute("""
                    INSERT INTO proxy_config (id, enabled, proxy_url, media_proxy_enabled, media_proxy_url)
                    VALUES (1, ?, ?, ?, ?)
                """, (enabled, proxy_url, new_media_proxy_enabled, new_media_proxy_url))

            await db.commit()

    async def get_generation_config(self) -> Optional[GenerationConfig]:
        """Get generation configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return GenerationConfig(**dict(row))
            return None

    async def update_generation_config(
        self,
        image_timeout: Optional[int] = None,
        video_timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ):
        """Update generation configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            current = dict(row) if row else {}

            normalized_image_timeout = (
                image_timeout
                if image_timeout is not None
                else current.get("image_timeout", 300)
            )
            normalized_video_timeout = (
                video_timeout
                if video_timeout is not None
                else current.get("video_timeout", 1500)
            )
            try:
                normalized_max_retries = (
                    max(1, int(max_retries))
                    if max_retries is not None
                    else max(1, int(current.get("max_retries", 3)))
                )
            except Exception:
                normalized_max_retries = 3

            if row:
                await db.execute("""
                    UPDATE generation_config
                    SET image_timeout = ?, video_timeout = ?, max_retries = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (normalized_image_timeout, normalized_video_timeout, normalized_max_retries))
            else:
                await db.execute("""
                    INSERT INTO generation_config (id, image_timeout, video_timeout, max_retries)
                    VALUES (1, ?, ?, ?)
                """, (normalized_image_timeout, normalized_video_timeout, normalized_max_retries))
            await db.commit()

    async def get_call_logic_config(self) -> CallLogicConfig:
        """Get token call logic configuration."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM call_logic_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                row_dict = dict(row)
                mode = row_dict.get("call_mode")
                if mode not in ("default", "polling"):
                    row_dict["call_mode"] = "polling" if row_dict.get("polling_mode_enabled") else "default"
                return CallLogicConfig(**row_dict)
            return CallLogicConfig(call_mode="default", polling_mode_enabled=False)

    async def update_call_logic_config(self, call_mode: str):
        """Update token call logic configuration."""
        normalized = "polling" if call_mode == "polling" else "default"
        polling_mode_enabled = normalized == "polling"
        async with self._connect(write=True) as db:
            await db.execute("""
                INSERT OR REPLACE INTO call_logic_config (id, call_mode, polling_mode_enabled, updated_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
            """, (normalized, polling_mode_enabled))
            await db.commit()

    # Request log operations
    async def add_request_log(self, log: RequestLog) -> int:
        """Add request log and return log id"""
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO request_logs (token_id, operation, request_body, response_body, status_code, duration, status_text, progress)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                log.token_id,
                log.operation,
                log.request_body,
                log.response_body,
                log.status_code,
                log.duration,
                log.status_text or "",
                log.progress,
            ))
            await db.commit()
            return cursor.lastrowid

    async def update_request_log(self, log_id: int, **kwargs):
        """Update an existing request log row."""
        if not kwargs:
            return

        allowed_fields = {
            "token_id",
            "operation",
            "request_body",
            "response_body",
            "status_code",
            "duration",
            "status_text",
            "progress",
        }
        update_fields = {key: value for key, value in kwargs.items() if key in allowed_fields}
        if not update_fields:
            return

        clauses = []
        values = []
        for key, value in update_fields.items():
            clauses.append(f"{key} = ?")
            values.append(value)
        clauses.append("updated_at = CURRENT_TIMESTAMP")
        values.append(log_id)

        async with self._connect(write=True) as db:
            await db.execute(
                f"UPDATE request_logs SET {', '.join(clauses)} WHERE id = ?",
                values,
            )
            await db.commit()

    async def get_logs(self, limit: int = 100, token_id: Optional[int] = None, include_payload: bool = False):
        """Get request logs with token info, optionally including payload fields"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            payload_columns = "rl.request_body, rl.response_body," if include_payload else ""
            response_excerpt_column = "substr(COALESCE(rl.response_body, ''), 1, 2048) as response_body_excerpt,"
            has_status_text = await self._column_exists(db, "request_logs", "status_text")
            has_progress = await self._column_exists(db, "request_logs", "progress")
            has_updated_at = await self._column_exists(db, "request_logs", "updated_at")
            status_text_column = "rl.status_text," if has_status_text else "'' as status_text,"
            progress_column = "rl.progress," if has_progress else "0 as progress,"
            updated_at_column = "rl.updated_at," if has_updated_at else "rl.created_at as updated_at,"

            if token_id:
                cursor = await db.execute(f"""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.operation,
                        {payload_columns}
                        {response_excerpt_column}
                        rl.status_code,
                        rl.duration,
                        {status_text_column}
                        {progress_column}
                        rl.created_at,
                        {updated_at_column}
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    WHERE rl.token_id = ?
                    ORDER BY rl.created_at DESC
                    LIMIT ?
                """, (token_id, limit))
            else:
                cursor = await db.execute(f"""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.operation,
                        {payload_columns}
                        {response_excerpt_column}
                        rl.status_code,
                        rl.duration,
                        {status_text_column}
                        {progress_column}
                        rl.created_at,
                        {updated_at_column}
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    ORDER BY rl.created_at DESC
                    LIMIT ?
                """, (limit,))

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_log_detail(self, log_id: int) -> Optional[Dict[str, Any]]:
        """Get single request log detail including payload fields"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            has_status_text = await self._column_exists(db, "request_logs", "status_text")
            has_progress = await self._column_exists(db, "request_logs", "progress")
            has_updated_at = await self._column_exists(db, "request_logs", "updated_at")
            status_text_column = "rl.status_text," if has_status_text else "'' as status_text,"
            progress_column = "rl.progress," if has_progress else "0 as progress,"
            updated_at_column = "rl.updated_at," if has_updated_at else "rl.created_at as updated_at,"
            cursor = await db.execute(f"""
                SELECT
                    rl.id,
                    rl.token_id,
                    rl.operation,
                    rl.request_body,
                    rl.response_body,
                    rl.status_code,
                    rl.duration,
                    {status_text_column}
                    {progress_column}
                    rl.created_at,
                    {updated_at_column}
                    t.email as token_email,
                    t.name as token_username
                FROM request_logs rl
                LEFT JOIN tokens t ON rl.token_id = t.id
                WHERE rl.id = ?
                LIMIT 1
            """, (log_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def clear_all_logs(self):
        """Clear all request logs"""
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM request_logs")
            await db.commit()

    async def init_config_from_toml(self, config_dict: dict, is_first_startup: bool = True):
        """
        Initialize database configuration from setting.toml

        Args:
            config_dict: Configuration dictionary from setting.toml
            is_first_startup: If True, initialize all config rows from setting.toml.
                            If False (upgrade mode), only ensure missing config rows exist with default values.
        """
        async with self._connect(write=True) as db:
            if is_first_startup:
                # First startup: Initialize all config tables with values from setting.toml
                await self._ensure_config_rows(db, config_dict)
            else:
                # Upgrade mode: Only ensure missing config rows exist (with default values, not from TOML)
                await self._ensure_config_rows(db, config_dict=None)

            await db.commit()

    async def reload_config_to_memory(self):
        """
        Reload all configuration from database to in-memory Config instance.
        This should be called after any configuration update to ensure hot-reload.

        Includes:
        - Admin config (username, password, api_key)
        - Cache config (enabled, timeout, base_url)
        - Generation config (image_timeout, video_timeout)
        - Proxy config will be handled by ProxyManager
        """
        from .config import config

        # Reload admin config
        admin_config = await self.get_admin_config()
        if admin_config:
            config.set_admin_username_from_db(admin_config.username)
            config.set_admin_password_from_db(admin_config.password)
            config.api_key = admin_config.api_key

        # Reload cache config
        cache_config = await self.get_cache_config()
        if cache_config:
            config.set_cache_enabled(cache_config.cache_enabled)
            config.set_cache_timeout(cache_config.cache_timeout)
            config.set_cache_base_url(cache_config.cache_base_url or "")

        # Reload generation config
        generation_config = await self.get_generation_config()
        if generation_config:
            config.set_image_timeout(generation_config.image_timeout)
            config.set_video_timeout(generation_config.video_timeout)
            config.set_flow_max_retries(generation_config.max_retries)

        # Reload call logic config
        call_logic_config = await self.get_call_logic_config()
        if call_logic_config:
            config.set_call_logic_mode(call_logic_config.call_mode)

        # Reload debug config
        debug_config = await self.get_debug_config()
        if debug_config:
            config.set_debug_enabled(debug_config.enabled)

        # Reload captcha config
        captcha_config = await self.get_captcha_config()
        if captcha_config:
            config.set_captcha_method(captcha_config.captcha_method)
            config.set_yescaptcha_api_key(captcha_config.yescaptcha_api_key)
            config.set_yescaptcha_base_url(captcha_config.yescaptcha_base_url)
            config.set_yescaptcha_task_type(captcha_config.yescaptcha_task_type)
            config.set_capmonster_api_key(captcha_config.capmonster_api_key)
            config.set_capmonster_base_url(captcha_config.capmonster_base_url)
            config.set_ezcaptcha_api_key(captcha_config.ezcaptcha_api_key)
            config.set_ezcaptcha_base_url(captcha_config.ezcaptcha_base_url)
            config.set_capsolver_api_key(captcha_config.capsolver_api_key)
            config.set_capsolver_base_url(captcha_config.capsolver_base_url)
            config.set_remote_browser_base_url(captcha_config.remote_browser_base_url)
            config.set_remote_browser_api_key(captcha_config.remote_browser_api_key)
            config.set_remote_browser_timeout(captcha_config.remote_browser_timeout)
            config.set_browser_count(captcha_config.browser_count)
            config.set_personal_project_pool_size(captcha_config.personal_project_pool_size)
            config.set_personal_max_resident_tabs(captcha_config.personal_max_resident_tabs)
            config.set_browser_personal_fresh_restart_every_n_solves(
                captcha_config.browser_personal_fresh_restart_every_n_solves
            )
            config.set_personal_idle_tab_ttl_seconds(captcha_config.personal_idle_tab_ttl_seconds)

    # Cache config operations
    async def get_cache_config(self) -> CacheConfig:
        """Get cache configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CacheConfig(**dict(row))
            # Return default if not found
            return CacheConfig(cache_enabled=False, cache_timeout=7200)

    async def update_cache_config(self, enabled: bool = None, timeout: int = None, base_url: Optional[str] = None):
        """Update cache configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = enabled if enabled is not None else current.get("cache_enabled", False)
                new_timeout = timeout if timeout is not None else current.get("cache_timeout", 7200)
                new_base_url = base_url if base_url is not None else current.get("cache_base_url")

                # If base_url is explicitly set to empty string, treat as None
                if base_url == "":
                    new_base_url = None

                await db.execute("""
                    UPDATE cache_config
                    SET cache_enabled = ?, cache_timeout = ?, cache_base_url = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_enabled, new_timeout, new_base_url))
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_timeout = timeout if timeout is not None else 7200
                new_base_url = base_url if base_url is not None else None

                await db.execute("""
                    INSERT INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                    VALUES (1, ?, ?, ?)
                """, (new_enabled, new_timeout, new_base_url))

            await db.commit()

    # Debug config operations
    async def get_debug_config(self) -> 'DebugConfig':
        """Get debug configuration"""
        from .models import DebugConfig
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return DebugConfig(**dict(row))
            # Return default if not found
            return DebugConfig(enabled=False, log_requests=True, log_responses=True, mask_token=True)

    async def update_debug_config(
        self,
        enabled: bool = None,
        log_requests: bool = None,
        log_responses: bool = None,
        mask_token: bool = None
    ):
        """Update debug configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = enabled if enabled is not None else current.get("enabled", False)
                new_log_requests = log_requests if log_requests is not None else current.get("log_requests", True)
                new_log_responses = log_responses if log_responses is not None else current.get("log_responses", True)
                new_mask_token = mask_token if mask_token is not None else current.get("mask_token", True)

                await db.execute("""
                    UPDATE debug_config
                    SET enabled = ?, log_requests = ?, log_responses = ?, mask_token = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_enabled, new_log_requests, new_log_responses, new_mask_token))
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_log_requests = log_requests if log_requests is not None else True
                new_log_responses = log_responses if log_responses is not None else True
                new_mask_token = mask_token if mask_token is not None else True

                await db.execute("""
                    INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                    VALUES (1, ?, ?, ?, ?)
                """, (new_enabled, new_log_requests, new_log_responses, new_mask_token))

            await db.commit()

    # Captcha config operations
    async def get_captcha_config(self) -> CaptchaConfig:
        """Get captcha configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CaptchaConfig(**dict(row))
            return CaptchaConfig()

    async def update_captcha_config(
        self,
        captcha_method: str = None,
        yescaptcha_api_key: str = None,
        yescaptcha_base_url: str = None,
        yescaptcha_task_type: str = None,
        capmonster_api_key: str = None,
        capmonster_base_url: str = None,
        ezcaptcha_api_key: str = None,
        ezcaptcha_base_url: str = None,
        capsolver_api_key: str = None,
        capsolver_base_url: str = None,
        remote_browser_base_url: str = None,
        remote_browser_api_key: str = None,
        remote_browser_timeout: int = None,
        browser_proxy_enabled: bool = None,
        browser_proxy_url: str = None,
        browser_count: int = None,
        personal_project_pool_size: int = None,
        personal_max_resident_tabs: int = None,
        browser_personal_fresh_restart_every_n_solves: int = None,
        personal_idle_tab_ttl_seconds: int = None
    ):
        """Update captcha configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                new_method = captcha_method if captcha_method is not None else current.get("captcha_method", "yescaptcha")
                new_yes_key = yescaptcha_api_key if yescaptcha_api_key is not None else current.get("yescaptcha_api_key", "")
                new_yes_url = yescaptcha_base_url if yescaptcha_base_url is not None else current.get("yescaptcha_base_url", "https://api.yescaptcha.com")
                new_yes_task_type = normalize_yescaptcha_task_type(
                    yescaptcha_task_type if yescaptcha_task_type is not None else current.get("yescaptcha_task_type")
                )
                new_cap_key = capmonster_api_key if capmonster_api_key is not None else current.get("capmonster_api_key", "")
                new_cap_url = capmonster_base_url if capmonster_base_url is not None else current.get("capmonster_base_url", "https://api.capmonster.cloud")
                new_ez_key = ezcaptcha_api_key if ezcaptcha_api_key is not None else current.get("ezcaptcha_api_key", "")
                new_ez_url = ezcaptcha_base_url if ezcaptcha_base_url is not None else current.get("ezcaptcha_base_url", "https://api.ez-captcha.com")
                new_cs_key = capsolver_api_key if capsolver_api_key is not None else current.get("capsolver_api_key", "")
                new_cs_url = capsolver_base_url if capsolver_base_url is not None else current.get("capsolver_base_url", "https://api.capsolver.com")
                new_remote_base_url = remote_browser_base_url if remote_browser_base_url is not None else current.get("remote_browser_base_url", "")
                new_remote_api_key = remote_browser_api_key if remote_browser_api_key is not None else current.get("remote_browser_api_key", "")
                new_remote_timeout = remote_browser_timeout if remote_browser_timeout is not None else current.get("remote_browser_timeout", 60)
                new_proxy_enabled = browser_proxy_enabled if browser_proxy_enabled is not None else current.get("browser_proxy_enabled", False)
                new_proxy_url = browser_proxy_url if browser_proxy_url is not None else current.get("browser_proxy_url")
                new_browser_count = browser_count if browser_count is not None else current.get("browser_count", 1)
                new_personal_project_pool_size = personal_project_pool_size if personal_project_pool_size is not None else current.get("personal_project_pool_size", 4)
                new_personal_max_tabs = personal_max_resident_tabs if personal_max_resident_tabs is not None else current.get("personal_max_resident_tabs", 5)
                new_personal_fresh_restart_every = (
                    browser_personal_fresh_restart_every_n_solves
                    if browser_personal_fresh_restart_every_n_solves is not None
                    else current.get("browser_personal_fresh_restart_every_n_solves", 10)
                )
                new_personal_idle_ttl = personal_idle_tab_ttl_seconds if personal_idle_tab_ttl_seconds is not None else current.get("personal_idle_tab_ttl_seconds", 600)
                new_remote_timeout = max(5, int(new_remote_timeout)) if new_remote_timeout is not None else 60
                new_browser_count = max(1, min(10, int(new_browser_count)))
                new_personal_project_pool_size = max(1, min(50, int(new_personal_project_pool_size)))
                new_personal_max_tabs = max(1, min(50, int(new_personal_max_tabs)))  # 限制1-50
                new_personal_fresh_restart_every = max(0, int(new_personal_fresh_restart_every))
                new_personal_idle_ttl = max(60, int(new_personal_idle_ttl))  # 最少60秒

                await db.execute("""
                    UPDATE captcha_config
                    SET captcha_method = ?, yescaptcha_api_key = ?, yescaptcha_base_url = ?,
                        yescaptcha_task_type = ?,
                        capmonster_api_key = ?, capmonster_base_url = ?,
                        ezcaptcha_api_key = ?, ezcaptcha_base_url = ?,
                        capsolver_api_key = ?, capsolver_base_url = ?,
                        remote_browser_base_url = ?, remote_browser_api_key = ?, remote_browser_timeout = ?,
                        browser_proxy_enabled = ?, browser_proxy_url = ?, browser_count = ?,
                        personal_project_pool_size = ?,
                        personal_max_resident_tabs = ?,
                        browser_personal_fresh_restart_every_n_solves = ?,
                        personal_idle_tab_ttl_seconds = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_method, new_yes_key, new_yes_url, new_yes_task_type,
                      new_cap_key, new_cap_url,
                      new_ez_key, new_ez_url, new_cs_key, new_cs_url,
                      (new_remote_base_url or "").strip(), (new_remote_api_key or "").strip(), new_remote_timeout,
                      new_proxy_enabled, new_proxy_url, new_browser_count, new_personal_project_pool_size,
                      new_personal_max_tabs, new_personal_fresh_restart_every, new_personal_idle_ttl))
            else:
                new_method = captcha_method if captcha_method is not None else "yescaptcha"
                new_yes_key = yescaptcha_api_key if yescaptcha_api_key is not None else ""
                new_yes_url = yescaptcha_base_url if yescaptcha_base_url is not None else "https://api.yescaptcha.com"
                new_yes_task_type = normalize_yescaptcha_task_type(yescaptcha_task_type)
                new_cap_key = capmonster_api_key if capmonster_api_key is not None else ""
                new_cap_url = capmonster_base_url if capmonster_base_url is not None else "https://api.capmonster.cloud"
                new_ez_key = ezcaptcha_api_key if ezcaptcha_api_key is not None else ""
                new_ez_url = ezcaptcha_base_url if ezcaptcha_base_url is not None else "https://api.ez-captcha.com"
                new_cs_key = capsolver_api_key if capsolver_api_key is not None else ""
                new_cs_url = capsolver_base_url if capsolver_base_url is not None else "https://api.capsolver.com"
                new_remote_base_url = remote_browser_base_url if remote_browser_base_url is not None else ""
                new_remote_api_key = remote_browser_api_key if remote_browser_api_key is not None else ""
                new_remote_timeout = remote_browser_timeout if remote_browser_timeout is not None else 60
                new_proxy_enabled = browser_proxy_enabled if browser_proxy_enabled is not None else False
                new_proxy_url = browser_proxy_url
                new_browser_count = browser_count if browser_count is not None else 1
                new_personal_project_pool_size = personal_project_pool_size if personal_project_pool_size is not None else 4
                new_personal_max_tabs = personal_max_resident_tabs if personal_max_resident_tabs is not None else 5
                new_personal_fresh_restart_every = (
                    browser_personal_fresh_restart_every_n_solves
                    if browser_personal_fresh_restart_every_n_solves is not None
                    else 10
                )
                new_personal_idle_ttl = personal_idle_tab_ttl_seconds if personal_idle_tab_ttl_seconds is not None else 600
                new_remote_timeout = max(5, int(new_remote_timeout))
                new_browser_count = max(1, min(10, int(new_browser_count)))
                new_personal_project_pool_size = max(1, min(50, int(new_personal_project_pool_size)))
                new_personal_max_tabs = max(1, min(50, int(new_personal_max_tabs)))
                new_personal_fresh_restart_every = max(0, int(new_personal_fresh_restart_every))
                new_personal_idle_ttl = max(60, int(new_personal_idle_ttl))

                await db.execute("""
                    INSERT INTO captcha_config (id, captcha_method, yescaptcha_api_key, yescaptcha_base_url,
                        yescaptcha_task_type,
                        capmonster_api_key, capmonster_base_url, ezcaptcha_api_key, ezcaptcha_base_url,
                        capsolver_api_key, capsolver_base_url,
                        remote_browser_base_url, remote_browser_api_key, remote_browser_timeout,
                        browser_proxy_enabled, browser_proxy_url, browser_count,
                        personal_project_pool_size,
                        personal_max_resident_tabs, browser_personal_fresh_restart_every_n_solves,
                        personal_idle_tab_ttl_seconds)
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (new_method, new_yes_key, new_yes_url, new_yes_task_type,
                      new_cap_key, new_cap_url,
                      new_ez_key, new_ez_url, new_cs_key, new_cs_url,
                      (new_remote_base_url or "").strip(), (new_remote_api_key or "").strip(), new_remote_timeout,
                      new_proxy_enabled, new_proxy_url, new_browser_count, new_personal_project_pool_size,
                      new_personal_max_tabs, new_personal_fresh_restart_every, new_personal_idle_ttl))

            await db.commit()

    # Plugin config operations
    async def create_extension_plugin_session(
        self,
        *,
        session_digest: str,
        public_id: str,
        instance_id: str,
        route_key: str,
        client_label: str,
        capability_marker: str,
        expires_at: float,
        created_at: float,
    ) -> None:
        """Persist a digest-only extension session and its server binding."""
        async with self._connect(write=True) as db:
            await self._ensure_extension_plugin_sessions_table(db)
            await db.execute(
                """
                INSERT INTO extension_plugin_sessions
                    (session_digest, public_id, instance_id, route_key, client_label,
                     capability_marker, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_digest,
                    public_id,
                    instance_id,
                    route_key,
                    client_label,
                    capability_marker,
                    float(expires_at),
                    float(created_at),
                ),
            )
            await db.commit()

    async def get_extension_plugin_session_by_digest(
        self, session_digest: str, *, now: float
    ) -> Optional[Dict[str, Any]]:
        """Return a live digest-only plugin session, atomically purging expiry."""
        async with self._connect(write=True) as db:
            await self._ensure_extension_plugin_sessions_table(db)
            await db.execute(
                "DELETE FROM extension_plugin_sessions WHERE expires_at <= ?", (float(now),)
            )
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT session_digest, public_id, instance_id, route_key, client_label,
                       capability_marker, expires_at
                FROM extension_plugin_sessions
                WHERE session_digest = ?
                """,
                (session_digest,),
            )
            row = await cursor.fetchone()
            await db.commit()
            return dict(row) if row else None

    async def get_extension_plugin_session_by_public_id(
        self, public_id: str, *, now: float
    ) -> Optional[Dict[str, Any]]:
        """Return a live plugin-session record by its non-secret public id."""
        async with self._connect(write=True) as db:
            await self._ensure_extension_plugin_sessions_table(db)
            await db.execute(
                "DELETE FROM extension_plugin_sessions WHERE expires_at <= ?", (float(now),)
            )
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT session_digest, public_id, instance_id, route_key, client_label,
                       capability_marker, expires_at
                FROM extension_plugin_sessions
                WHERE public_id = ?
                """,
                (public_id,),
            )
            row = await cursor.fetchone()
            await db.commit()
            return dict(row) if row else None

    async def delete_extension_plugin_session_by_digest(self, session_digest: str) -> bool:
        """Revoke one digest-only plugin session."""
        async with self._connect(write=True) as db:
            await self._ensure_extension_plugin_sessions_table(db)
            cursor = await db.execute(
                "DELETE FROM extension_plugin_sessions WHERE session_digest = ?",
                (session_digest,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_plugin_config(self) -> PluginConfig:
        """Get plugin configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM plugin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return PluginConfig(**dict(row))
            return PluginConfig()

    async def update_plugin_config(self, connection_token: str, auto_enable_on_update: bool = True):
        """Update plugin configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM plugin_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                await db.execute("""
                    UPDATE plugin_config
                    SET connection_token = ?, auto_enable_on_update = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (connection_token, auto_enable_on_update))
            else:
                await db.execute("""
                    INSERT INTO plugin_config (id, connection_token, auto_enable_on_update)
                    VALUES (1, ?, ?)
                """, (connection_token, auto_enable_on_update))

            await db.commit()

    async def get_token_refresh_config(self) -> TokenRefreshConfig:
        """Get protocol ST refresh configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_refresh_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return TokenRefreshConfig(**dict(row))
            return TokenRefreshConfig()

    async def update_token_refresh_config(
        self,
        enabled: Optional[bool] = None,
        refresh_interval_minutes: Optional[int] = None,
    ) -> TokenRefreshConfig:
        """Update protocol ST refresh configuration."""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_refresh_config WHERE id = 1")
            row = await cursor.fetchone()
            current = dict(row) if row else {}
            new_enabled = enabled if enabled is not None else bool(current.get("enabled", True))
            try:
                new_interval = int(
                    refresh_interval_minutes
                    if refresh_interval_minutes is not None
                    else current.get("refresh_interval_minutes", 120)
                )
            except Exception:
                new_interval = 120
            new_interval = max(1, new_interval)

            if row:
                await db.execute("""
                    UPDATE token_refresh_config
                    SET enabled = ?, refresh_interval_minutes = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_enabled, new_interval))
            else:
                await db.execute("""
                    INSERT INTO token_refresh_config (id, enabled, refresh_interval_minutes)
                    VALUES (1, ?, ?)
                """, (new_enabled, new_interval))
            await db.commit()

        return await self.get_token_refresh_config()
