from __future__ import annotations

import socket
import threading
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import requests
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from imagegen.config.channels import ChannelRegistry
from imagegen.extensions import db
from imagegen.integrations.images import (
    OpenAIImagesAdapter,
    PinnedHostSSLAdapter,
    ProviderError,
)
from imagegen.models import (
    GenerationItem,
    GenerationQueueState,
    User,
    WorkerState,
    Workspace,
)
from imagegen.serializers import display_amount
from imagegen.services import ServiceError, SubmitGeneration, money
from tests.support.platform import (
    CHANNEL_CONFIG,
    FakeDownloadResponse,
    PlatformTestCase,
    RecordingDownloadSession,
)


class TestFoundations(PlatformTestCase):
    def test_display_amount_trims_only_redundant_fraction_zeros(self):
        self.assertEqual(display_amount("100.0000"), "100.00")
        self.assertEqual(display_amount("1.2500"), "1.25")
        self.assertEqual(display_amount("1.2340"), "1.234")
        self.assertEqual(display_amount("1.2345"), "1.2345")

    def test_money_and_channel_prices_reject_non_finite_values(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaisesRegex(ServiceError, "金额格式无效"):
                money(value)

            invalid_path = Path(self.temp.name) / f"channels-{value}.yaml"
            invalid_path.write_text(
                CHANNEL_CONFIG.replace("price_rmb: 1.2500", f"price_rmb: {value}"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "price_rmb 无效"):
                ChannelRegistry(invalid_path)

    def test_internal_state_rows_are_bootstrapped(self):
        self.assertIsNotNone(db.session.get(GenerationQueueState, 1))
        self.assertIsNotNone(db.session.get(WorkerState, 1))

    def test_database_rejects_case_insensitive_duplicate_usernames(self):
        duplicate = User(
            username="ARTIST",
            display_name="重复用户",
            password_hash="not-used",
            role="user",
            status="active",
            balance_rmb=Decimal("0"),
            reserved_rmb=Decimal("0"),
            generation_concurrency=2,
            password_version=1,
        )
        db.session.add(duplicate)

        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_postgresql_global_queue_admission_is_atomic(self):
        if db.engine.dialect.name != "postgresql":
            self.skipTest("PostgreSQL row-lock regression")
        self.channel_path.write_text(
            CHANNEL_CONFIG.replace("max_queued_per_user: 20", "max_queued_per_user: 1").replace(
                "max_queued_global: 100", "max_queued_global: 1"
            ),
            encoding="utf-8",
        )
        self.assertTrue(self.app.extensions["channel_registry"].reload(force=True))
        second_user = self.services.users.create(
            username="second-artist",
            password="StrongPass123!",
            balance_rmb="20",
            actor_user_id=self.admin.id,
        )
        first_workspace = self.services.workspaces.create(self.user.id, "并发队列 A")
        second_workspace = self.services.workspaces.create(second_user.id, "并发队列 B")
        submissions = (
            (self.user.id, first_workspace.id),
            (second_user.id, second_workspace.id),
        )
        barrier = threading.Barrier(2)
        outcomes = []

        def submit_in_thread(user_id, workspace_id):
            with self.app.app_context():
                workspace = db.session.get(Workspace, workspace_id)
                request = SubmitGeneration(
                    channel_id="test",
                    model="model-b",
                    mode="text2img",
                    prompt="并发队列测试",
                    size="1024x1024",
                    output_format="png",
                    compression=90,
                    batch_count=1,
                    reference_ids=(),
                )
                barrier.wait(timeout=10)
                try:
                    self.services.generations.submit(user_id, workspace, request)
                    outcomes.append("accepted")
                except ServiceError as exc:
                    outcomes.append(exc.code)
                finally:
                    db.session.remove()

        threads = [
            threading.Thread(target=submit_in_thread, args=submission) for submission in submissions
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["accepted", "queue_full"])
        self.assertEqual(
            db.session.scalar(
                select(func.count(GenerationItem.id)).where(GenerationItem.status == "queued")
            ),
            1,
        )

    def test_postgresql_workspace_delete_serializes_with_submission(self):
        if db.engine.dialect.name != "postgresql":
            self.skipTest("PostgreSQL row-lock regression")
        workspace = self.create_workspace("删除与提交竞态")
        workspace_id = workspace.id
        user_id = self.user.id
        generation_service = self.services.generations
        original_capacity_check = generation_service._ensure_queue_capacity
        submission_locked = threading.Event()
        continue_submission = threading.Event()
        delete_started = threading.Event()
        errors = []

        def blocking_capacity_check(locked_user_id, requested_count):
            submission_locked.set()
            if not continue_submission.wait(10):
                raise RuntimeError("并发测试等待提交超时")
            return original_capacity_check(locked_user_id, requested_count)

        def submit_in_thread():
            with self.app.app_context():
                current_workspace = db.session.get(Workspace, workspace_id)
                request = SubmitGeneration(
                    channel_id="test",
                    model="model-b",
                    mode="text2img",
                    prompt="删除竞态测试",
                    size="1024x1024",
                    output_format="png",
                    compression=90,
                    batch_count=1,
                    reference_ids=(),
                )
                try:
                    generation_service.submit(user_id, current_workspace, request)
                except Exception as exc:
                    errors.append(("submit", exc))
                finally:
                    db.session.remove()

        def delete_in_thread():
            with self.app.app_context():
                current_workspace = db.session.get(Workspace, workspace_id)
                delete_started.set()
                try:
                    self.services.workspaces.delete(current_workspace)
                except Exception as exc:
                    errors.append(("delete", exc))
                finally:
                    db.session.remove()

        generation_service._ensure_queue_capacity = blocking_capacity_check
        try:
            submit_thread = threading.Thread(target=submit_in_thread)
            submit_thread.start()
            self.assertTrue(submission_locked.wait(5))
            delete_thread = threading.Thread(target=delete_in_thread)
            delete_thread.start()
            self.assertTrue(delete_started.wait(5))
            continue_submission.set()
            submit_thread.join(15)
            delete_thread.join(15)
        finally:
            generation_service._ensure_queue_capacity = original_capacity_check
            continue_submission.set()

        self.assertFalse(submit_thread.is_alive())
        self.assertFalse(delete_thread.is_alive())
        self.assertEqual(errors, [])
        db.session.expire_all()
        self.assertIsNone(db.session.get(Workspace, workspace_id))
        self.assertEqual(db.session.get(User, user_id).reserved_rmb, Decimal("0.0000"))

    def test_workspace_mutation_holds_conversation_exclusivity(self):
        workspace = self.create_workspace("工作站变更互斥")
        conversations = self.services.conversations

        with conversations.workspace_mutation(workspace, "正在清空工作站"):
            with self.assertRaises(ServiceError) as raised:
                with conversations.generation_submission(workspace):
                    pass

        self.assertEqual(raised.exception.code, "conversation_busy")

    def test_image_download_pins_validated_dns_and_limits_authorization_on_redirect(self):
        channel = self.app.extensions["channel_registry"].get("test")
        redirect = FakeDownloadResponse(
            status_code=302,
            headers={"Location": "https://cdn.example/final.png"},
        )
        final = FakeDownloadResponse(body=b"downloaded-image")
        session = RecordingDownloadSession([redirect, final])
        adapter = OpenAIImagesAdapter()
        adapter._local.session = session
        addresses = {"relay.example": "8.8.8.8", "cdn.example": "1.1.1.1"}

        def resolve(hostname, port, *, type):
            return [
                (
                    socket.AF_INET,
                    type,
                    socket.IPPROTO_TCP,
                    "",
                    (addresses[hostname], port),
                )
            ]

        with patch("imagegen.integrations.images.socket.getaddrinfo", side_effect=resolve):
            content = adapter._download(
                "https://relay.example/generated.png",
                channel,
                "request-id",
            )

        self.assertEqual(content, b"downloaded-image")
        self.assertEqual(
            [request["url"] for request in session.requests],
            ["https://8.8.8.8/generated.png", "https://1.1.1.1/final.png"],
        )
        self.assertEqual(session.requests[0]["headers"]["Host"], "relay.example")
        self.assertIn("Authorization", session.requests[0]["headers"])
        self.assertEqual(session.requests[1]["headers"], {"Host": "cdn.example"})
        self.assertTrue(redirect.closed)
        self.assertTrue(final.closed)

    def test_image_download_rejects_credentials_and_non_public_dns(self):
        channel = self.app.extensions["channel_registry"].get("test")
        adapter = OpenAIImagesAdapter()
        adapter._local.session = RecordingDownloadSession([])

        with self.assertRaises(ProviderError) as credentials_error:
            adapter._download(
                "https://user:password@relay.example/image.png",
                channel,
                "request-id",
            )
        self.assertEqual(credentials_error.exception.code, "invalid_response")

        private_result = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        ]
        with (
            patch(
                "imagegen.integrations.images.socket.getaddrinfo",
                return_value=private_result,
            ),
            self.assertRaises(ProviderError) as private_error,
        ):
            adapter._download("https://relay.example/image.png", channel, "request-id")
        self.assertEqual(private_error.exception.code, "invalid_response")

    def test_image_download_closes_error_response(self):
        channel = self.app.extensions["channel_registry"].get("test")
        response = FakeDownloadResponse(status_code=502)
        adapter = OpenAIImagesAdapter()
        adapter._local.session = RecordingDownloadSession([response])
        public_result = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", 443),
            )
        ]

        with (
            patch(
                "imagegen.integrations.images.socket.getaddrinfo",
                return_value=public_result,
            ),
            self.assertRaises(ProviderError),
        ):
            adapter._download("https://relay.example/image.png", channel, "request-id")
        self.assertTrue(response.closed)

    def test_pinned_https_adapter_preserves_sni_and_certificate_hostname(self):
        adapter = PinnedHostSSLAdapter()
        prepared = requests.Request(
            "GET",
            "https://8.8.8.8:8443/image.png",
            headers={"Host": "images.example:8443"},
        ).prepare()

        _host, pool_kwargs = adapter.build_connection_pool_key_attributes(
            prepared,
            True,
        )

        self.assertEqual(pool_kwargs["assert_hostname"], "images.example")
        self.assertEqual(pool_kwargs["server_hostname"], "images.example")

    def test_large_static_assets_are_compressed(self):
        response = self.app.test_client().get(
            "/static/js/studio.js",
            headers={"Accept-Encoding": "br"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Encoding"), "br")
        self.assertIn("Accept-Encoding", response.headers.get("Vary", ""))
        self.assertLess(len(response.data), Path("static/js/studio.js").stat().st_size // 2)
