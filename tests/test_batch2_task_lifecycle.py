import asyncio
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path

from src.core.account_tiers import PAYGATE_TIER_NOT_PAID
from src.core.database import Database
from src.core.models import Task, Token
from src.services.generation_handler import GenerationHandler


IMAGE_MODEL = "gemini-3.1-flash-image-landscape"
VIDEO_MODEL = "veo_3_1_t2v_fast_landscape"


class _FakeTokenManager:
    def __init__(self, token):
        self.token = token
        self.error_calls = 0
        self.success_calls = 0
        self.usage_calls = 0

    async def ensure_valid_token(self, token):
        return token

    async def ensure_project_exists(self, token_id):
        return "project-ref"

    async def record_usage(self, token_id, is_video=False):
        self.usage_calls += 1

    async def record_success(self, token_id):
        self.success_calls += 1

    async def record_error(self, token_id):
        self.error_calls += 1


class _FakeLoadBalancer:
    def __init__(self, token):
        self.token = token
        self.pending = 0

    async def select_token(self, **kwargs):
        if kwargs.get("track_pending"):
            self.pending += 1
        return self.token

    async def release_pending(self, token_id, **kwargs):
        self.pending = max(0, self.pending - 1)

    async def get_unavailable_reason(self, **kwargs):
        return None


class _FakeFlowClient:
    def __init__(self):
        self.submit_calls = 0
        self.check_calls = 0
        self.image_mode = "success"
        self.video_mode = "failed"
        self.submit_started = asyncio.Event()
        self.submit_release = asyncio.Event()
        self.block_image_submit = False
        self.poll_started = asyncio.Event()
        self.poll_release = asyncio.Event()
        self.block_poll = False

    def clear_request_fingerprint(self):
        return None

    async def prefill_remote_browser_pool(self, **kwargs):
        return None

    async def generate_image(self, **kwargs):
        self.submit_calls += 1
        self.submit_started.set()
        if self.block_image_submit:
            await self.submit_release.wait()
        if self.image_mode == "timeout":
            raise asyncio.TimeoutError()
        return (
            {
                "media": [
                    {
                        "name": "media-ref",
                        "image": {"generatedImage": {"fifeUrl": "media-ref"}},
                    }
                ]
            },
            "session-ref",
            {},
        )

    async def generate_video_text(self, **kwargs):
        self.submit_calls += 1
        return {
            "operations": [
                {
                    "operation": {"name": "op-accepted"},
                    "sceneId": "scene-ref",
                }
            ]
        }

    async def check_video_status(self, at, operations):
        self.check_calls += 1
        self.poll_started.set()
        if self.block_poll:
            await self.poll_release.wait()
        operation_name = (operations[0].get("operation") or {}).get("name", "op-ref")
        if self.video_mode == "failed":
            return {
                "operations": [
                    {
                        "status": "MEDIA_GENERATION_STATUS_FAILED",
                        "operation": {
                            "name": operation_name,
                            "error": {"code": "POLICY", "message": ""},
                        },
                    }
                ]
            }
        return {"operations": []}


class _FakeProxyManager:
    pass


async def _collect_generation(handler, *, model, idempotency_key, stream=False):
    chunks = []
    async for chunk in handler.handle_generation(
        model=model,
        prompt="",
        images=None,
        stream=stream,
        idempotency_key=idempotency_key,
    ):
        chunks.append(chunk)
    return chunks


class Batch2TaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._temp_dir.name) / "batch2.db"))
        await self.db.init_db()
        token_id = await self.db.add_token(
            Token(
                st="",
                at=None,
                email="",
                user_paygate_tier=PAYGATE_TIER_NOT_PAID,
                image_concurrency=-1,
                video_concurrency=-1,
            )
        )
        self.token = await self.db.get_token(token_id)
        self.flow = _FakeFlowClient()
        self.token_manager = _FakeTokenManager(self.token)
        self.load_balancer = _FakeLoadBalancer(self.token)
        self.handler = GenerationHandler(
            self.flow,
            self.token_manager,
            self.load_balancer,
            self.db,
            object(),
            _FakeProxyManager(),
        )

    async def asyncTearDown(self):
        self._temp_dir.cleanup()

    async def _task_columns(self):
        async with self.db._connect() as conn:
            cursor = await conn.execute("PRAGMA table_info(tasks)")
            return {row[1] for row in await cursor.fetchall()}

    async def _task_rows_for_key(self, idempotency_key):
        async with self.db._connect() as conn:
            cursor = await conn.execute(
                "SELECT task_id, status, has_media, error_class, prompt "
                "FROM tasks WHERE idempotency_key = ? ORDER BY id",
                (idempotency_key,),
            )
            return await cursor.fetchall()

    async def _wait_for_task_status(self, idempotency_key, terminal_statuses, timeout=1.0):
        real_sleep = asyncio.sleep
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            rows = await self._task_rows_for_key(idempotency_key)
            if rows and rows[0][1] in terminal_statuses:
                return rows[0]
            await real_sleep(0.01)
        self.fail(f"task {idempotency_key!r} did not reach {terminal_statuses}")

    async def test_existing_tasks_fact_source_is_extended_for_idempotency_and_recovery(self):
        columns = await self._task_columns()
        self.assertTrue(
            {"idempotency_key", "has_media", "error_class", "updated_at"}.issubset(columns),
            f"existing tasks table is missing Batch 2 columns: {columns}",
        )

    async def test_concurrent_same_idempotency_key_submits_once_and_reuses_one_persisted_task(self):
        key = "idem-concurrent"
        self.flow.block_image_submit = True

        first = asyncio.create_task(
            _collect_generation(self.handler, model=IMAGE_MODEL, idempotency_key=key)
        )
        second = asyncio.create_task(
            _collect_generation(self.handler, model=IMAGE_MODEL, idempotency_key=key)
        )

        release_task = asyncio.create_task(self.flow.submit_started.wait())
        try:
            done, _ = await asyncio.wait(
                {first, second, release_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=0.5,
            )
            for task in (first, second):
                if task in done and task.done() and task.exception() is not None:
                    await task
            self.assertIn(release_task, done, "first upstream submit never started")
            self.flow.submit_release.set()
            left, right = await asyncio.gather(first, second)
        finally:
            if not release_task.done():
                release_task.cancel()
                with suppress(asyncio.CancelledError):
                    await release_task

        self.assertEqual(self.flow.submit_calls, 1)
        self.assertEqual(left, right)
        rows = await self._task_rows_for_key(key)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "succeeded")
        self.assertEqual(rows[0][2], 1)
        self.assertEqual(rows[0][4], "")

    async def test_submit_timeout_enters_unknown_and_same_key_never_resubmits(self):
        key = "idem-timeout"
        self.flow.image_mode = "timeout"

        await _collect_generation(self.handler, model=IMAGE_MODEL, idempotency_key=key)
        rows = await self._task_rows_for_key(key)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "unknown")
        self.assertEqual(self.flow.submit_calls, 1)

        self.flow.image_mode = "success"
        await _collect_generation(self.handler, model=IMAGE_MODEL, idempotency_key=key)
        rows = await self._task_rows_for_key(key)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "unknown")
        self.assertEqual(self.flow.submit_calls, 1)

    async def test_client_disconnect_after_acceptance_does_not_cancel_background_polling(self):
        key = "idem-disconnect"
        self.flow.block_poll = True

        async def consume_stream():
            async for _ in self.handler.handle_generation(
                model=VIDEO_MODEL,
                prompt="",
                images=None,
                stream=True,
                idempotency_key=key,
            ):
                pass

        consumer = asyncio.create_task(consume_stream())
        poll_wait = asyncio.create_task(self.flow.poll_started.wait())
        done, _ = await asyncio.wait(
            {consumer, poll_wait},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=0.5,
        )
        if consumer in done and consumer.exception() is not None:
            await consumer
        self.assertIn(poll_wait, done, "accepted task never reached polling")

        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer
        self.flow.poll_release.set()

        row = await self._wait_for_task_status(key, {"failed", "succeeded"})
        self.assertEqual(row[1], "failed")
        self.assertEqual(row[2], 0)
        self.assertGreaterEqual(self.flow.check_calls, 1)

    async def test_restart_recovery_resumes_accepted_and_polling_without_resubmitting_unknown(self):
        accepted = Task(
            task_id="op-recover-accepted",
            token_id=self.token.id,
            model="veo_3_1_t2v_fast",
            prompt="",
            status="accepted",
        )
        polling = Task(
            task_id="op-recover-polling",
            token_id=self.token.id,
            model="veo_3_1_t2v_fast",
            prompt="",
            status="polling",
        )
        unknown = Task(
            task_id="local-recover-unknown",
            token_id=self.token.id,
            model="veo_3_1_t2v_fast",
            prompt="",
            status="unknown",
        )
        await self.db.create_task(accepted)
        await self.db.create_task(polling)
        await self.db.create_task(unknown)

        fresh_flow = _FakeFlowClient()
        fresh_flow.video_mode = "failed"
        fresh_handler = GenerationHandler(
            fresh_flow,
            _FakeTokenManager(self.token),
            _FakeLoadBalancer(self.token),
            self.db,
            object(),
            _FakeProxyManager(),
        )

        await fresh_handler.recover_incomplete_tasks()

        self.assertEqual(fresh_flow.submit_calls, 0)
        self.assertEqual(fresh_flow.check_calls, 2)
        self.assertEqual((await self.db.get_task("op-recover-accepted")).status, "failed")
        self.assertEqual((await self.db.get_task("op-recover-polling")).status, "failed")
        self.assertEqual((await self.db.get_task("local-recover-unknown")).status, "unknown")
        self.assertFalse(getattr(await self.db.get_task("op-recover-accepted"), "has_media", True))
        self.assertFalse(getattr(await self.db.get_task("op-recover-polling"), "has_media", True))


if __name__ == "__main__":
    unittest.main()
