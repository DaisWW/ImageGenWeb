from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import threading
import unittest
import zlib
from concurrent.futures import Future
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from PIL import Image
from sqlalchemy import func, select

from imagegen import create_app
from imagegen.config.repository import CHANNEL_CONFIG_KEY, CHAT_CONFIG_KEY
from imagegen.extensions import db
from imagegen.integrations.images import (
    GenerationRequest,
    OpenAIImagesAdapter,
    ProviderResult,
    ReferencePayload,
)
from imagegen.integrations.openai_chat import ChatCompletion, OpenAIChatClient
from imagegen.models import (
    AuditLog,
    ConversationMessage,
    GenerationItem,
    GenerationJob,
    SystemState,
    User,
    WalletLedger,
    Workspace,
    utcnow,
)
from imagegen.serializers import display_amount
from imagegen.services import ServiceError, SubmitGeneration
from imagegen.services.conversation import CHAT_SYSTEM_PROMPT
from imagegen.storage import InvalidImageError
from imagegen.worker import GenerationWorker
from scripts.backup import copy_private_file


def png_bytes(color=(35, 160, 110)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(stream, format="PNG")
    return stream.getvalue()


def opaque_icon_png_bytes() -> bytes:
    stream = io.BytesIO()
    image = Image.new("RGB", (64, 64), (255, 255, 255))
    image.paste((35, 160, 110), (16, 16, 48, 48))
    image.save(stream, format="PNG")
    return stream.getvalue()


def png_bytes_with_dimensions(width: int, height: int) -> bytes:
    content = bytearray(png_bytes())
    content[16:20] = width.to_bytes(4, "big")
    content[20:24] = height.to_bytes(4, "big")
    content[29:33] = zlib.crc32(content[12:29]).to_bytes(4, "big")
    return bytes(content)


CHANNEL_CONFIG = """\
version: 1
queue:
  global_concurrency: 4
  max_queued_per_user: 20
  max_queued_global: 100
  history_retention_days: 30
  stale_running_minutes: 20
channels:
  - id: test
    label: 测试渠道
    enabled: true
    adapter: openai_images
    base_url: https://relay.example
    api_key_env: TEST_IMAGE_KEY
    models:
      - id: model-a
        label: 模型 A
      - id: model-b
        label: 模型 B
    price_rmb: 1.2500
    capabilities:
      modes: [text2img, img2img]
      max_reference_images: 8
      max_reference_image_mb: 10
      max_reference_total_mb: 40
      reference_field: image
      sizes: [1024x1024]
      qualities: [medium]
      formats: [png, jpeg, webp]
    limits:
      max_concurrency: 3
      timeout_seconds: 600
      estimated_seconds: 120
"""


CHAT_CONFIG = """\
version: 1
context:
  compact_at_tokens: 24000
  max_context_tokens: 32000
  keep_recent_messages: 12
models:
  - id: test-chat
    label: 测试 GPT
    enabled: true
    base_url: https://chat.example
    api_key_env: TEST_CHAT_KEY
    model: gpt-test
    reasoning_effort: max
    timeout_seconds: 30
    max_output_tokens: 1000
"""


class FakeAdapter:
    def __init__(self, *, fail: bool = False, vary: bool = False):
        self.fail = fail
        self.vary = vary
        self.request = None
        self.requests = []

    def generate(self, _channel, request):
        self.request = request
        self.requests.append(request)
        if self.fail:
            from imagegen.integrations.images import ProviderError

            raise ProviderError("测试失败", code="test_failure", status_code=502)
        index = len(self.requests)
        return ProviderResult(
            content=png_bytes((35, min(250, 130 + index * 20), 110)) if self.vary else png_bytes(),
            request_id=f"request-test-{index}",
        )


class FakeProviderFactory:
    def __init__(self, *, fail: bool = False, vary: bool = False):
        self.adapter = FakeAdapter(fail=fail, vary=vary)

    def for_channel(self, _channel):
        return self.adapter


class BlockingAdapter:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def generate(self, _channel, _request):
        self.started.set()
        if not self.release.wait(10):
            raise RuntimeError("blocking adapter timed out")
        return ProviderResult(content=png_bytes(), request_id="request-after-cancel")


class BlockingProviderFactory:
    def __init__(self):
        self.adapter = BlockingAdapter()

    def for_channel(self, _channel):
        return self.adapter


class FakeImageHTTPResponse:
    headers = {"x-request-id": "image-http-test"}

    def __init__(self, *, content: bytes | None = None, status_code: int = 200, payload=None):
        self.status_code = status_code
        self.payload = payload or {
            "data": [{"b64_json": base64.b64encode(content or png_bytes()).decode("ascii")}]
        }

    def json(self):
        return self.payload

    def close(self):
        pass


class RecordingImageSession:
    def __init__(self):
        self.request = None
        self.requests = []

    def post(self, url, **kwargs):
        self.request = {"url": url, **kwargs}
        self.requests.append(self.request)
        payload = kwargs.get("json") or kwargs.get("data") or {}
        content = (
            opaque_icon_png_bytes() if payload.get("background") == "transparent" else png_bytes()
        )
        return FakeImageHTTPResponse(content=content)


class RejectingTransparencySession(RecordingImageSession):
    def post(self, url, **kwargs):
        self.request = {"url": url, **kwargs}
        self.requests.append(self.request)
        payload = kwargs.get("json") or kwargs.get("data") or {}
        if payload.get("background") == "transparent":
            return FakeImageHTTPResponse(
                status_code=400,
                payload={
                    "error": {"message": "Transparent background is not supported for this model."}
                },
            )
        return FakeImageHTTPResponse(content=opaque_icon_png_bytes())


class FakeChatClient:
    def __init__(self):
        self.calls = []

    def complete(self, model, *, system, messages, max_output_tokens=None):
        self.calls.append(
            {
                "model_id": model.identifier,
                "model": model.model,
                "system": system,
                "messages": messages,
                "max_output_tokens": max_output_tokens,
            }
        )
        if "只输出一个 JSON 对象" in system:
            content = '{"summary_zh":"一位人物肖像","prompt":"cinematic portrait"}'
        else:
            content = "我已理解需求，请确认人物所处的场景。"
        return ChatCompletion(
            content=content,
            request_id="chat-request-test",
            input_tokens=18,
            output_tokens=12,
            elapsed_seconds=1.234,
        )


class BlockingFirstChatClient:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self._lock = threading.Lock()

    def complete(self, _model, *, system, messages, max_output_tokens=None):
        with self._lock:
            self.calls.append(
                {
                    "system": system,
                    "messages": messages,
                    "max_output_tokens": max_output_tokens,
                }
            )
            call_number = len(self.calls)
        if call_number == 1:
            self.started.set()
            if not self.release.wait(10):
                raise RuntimeError("blocking chat client timed out")
        return ChatCompletion(
            content=f"并行回复 {call_number}",
            request_id=f"parallel-chat-{call_number}",
            input_tokens=10,
            output_tokens=5,
            elapsed_seconds=0.5,
        )


class FakeChatResponse:
    ok = True
    status_code = 200
    headers = {"x-request-id": "chat-http-test"}

    @staticmethod
    def json():
        return {
            "choices": [{"message": {"content": "测试回复"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }


class RecordingChatSession:
    def __init__(self):
        self.request = None

    def post(self, url, **kwargs):
        self.request = {"url": url, **kwargs}
        return FakeChatResponse()


class HoldingExecutor:
    def submit(self, _function, *_args):
        return Future()


class ImageGenPlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.channel_path = root / "channels.yaml"
        self.channel_path.write_text(CHANNEL_CONFIG, encoding="utf-8")
        self.chat_path = root / "chat_models.yaml"
        self.chat_path.write_text(CHAT_CONFIG, encoding="utf-8")
        os.environ["TEST_IMAGE_KEY"] = "test-key-not-secret"
        os.environ["TEST_CHAT_KEY"] = "test-chat-key-not-secret"
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(root / 'test.db').as_posix()}",
                "CHANNEL_CONFIG_PATH": str(self.channel_path),
                "CHAT_MODEL_CONFIG_PATH": str(self.chat_path),
                "IMAGE_STORAGE_PATH": str(root / "files"),
                "WTF_CSRF_ENABLED": False,
                "AUTO_CREATE_DB": True,
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        self.services = self.app.extensions["imagegen_services"]
        self.admin = self.services.users.create(
            username="admin",
            password="StrongPass123!",
            role="admin",
        )
        self.user = self.services.users.create(
            username="artist",
            password="StrongPass123!",
            display_name="设计同事",
            balance_rmb="20",
            actor_user_id=self.admin.id,
        )
        self.chat_client = FakeChatClient()
        conversations = self.services.conversations
        conversations.client = self.chat_client
        conversations.context.client = self.chat_client

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.context.pop()
        self.temp.cleanup()
        os.environ.pop("TEST_IMAGE_KEY", None)
        os.environ.pop("TEST_CHAT_KEY", None)

    def create_workspace(self, name="角色设计", kind="image"):
        return self.services.workspaces.create(self.user.id, name, kind)

    def test_display_amount_trims_only_redundant_fraction_zeros(self):
        self.assertEqual(display_amount("100.0000"), "100.00")
        self.assertEqual(display_amount("1.2500"), "1.25")
        self.assertEqual(display_amount("1.2340"), "1.234")
        self.assertEqual(display_amount("1.2345"), "1.2345")

    def submit(self, workspace, **overrides):
        values = {
            "channel_id": "test",
            "model": "model-b",
            "mode": "text2img",
            "prompt": "电影感人物肖像",
            "size": "1024x1024",
            "quality": "medium",
            "output_format": "png",
            "compression": 90,
            "batch_count": 1,
            "reference_ids": (),
        }
        values.update(overrides)
        return self.services.generations.submit(self.user.id, workspace, SubmitGeneration(**values))

    def user_client(self):
        client = self.app.test_client()
        response = client.post(
            "/login",
            data={"username": "artist", "password": "StrongPass123!"},
        )
        self.assertEqual(response.status_code, 302)
        return client

    def admin_client(self):
        client = self.app.test_client()
        response = client.post(
            "/login",
            data={"username": "admin", "password": "StrongPass123!"},
        )
        self.assertEqual(response.status_code, 302)
        return client

    def test_logout_clears_remember_cookie_and_requires_login(self):
        client = self.app.test_client()
        response = client.post(
            "/login",
            data={
                "username": "artist",
                "password": "StrongPass123!",
                "remember": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(client.get_cookie("remember_token"))

        response = client.post("/logout")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/login"))
        self.assertIsNone(client.get_cookie("remember_token"))
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_password_reset_revokes_old_remember_cookie(self):
        client = self.app.test_client()
        response = client.post(
            "/login",
            data={
                "username": "artist",
                "password": "StrongPass123!",
                "remember": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(client.get_cookie("remember_token"))

        self.services.users.reset_password(
            self.user.id,
            "ReplacementPass123!",
            self.admin.id,
        )
        client.delete_cookie(self.app.config.get("SESSION_COOKIE_NAME", "session"))

        self.context.pop()
        try:
            response = client.get("/")
        finally:
            self.context.push()

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_changing_own_password_refreshes_current_remember_identity(self):
        client = self.app.test_client()
        client.post(
            "/login",
            data={
                "username": "artist",
                "password": "StrongPass123!",
                "remember": "1",
            },
        )
        old_token = client.get_cookie("remember_token").value

        response = client.post(
            "/account/password",
            json={
                "current_password": "StrongPass123!",
                "new_password": "ReplacementPass123!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/").status_code, 200)
        self.assertNotEqual(client.get_cookie("remember_token").value, old_token)

    def test_password_can_be_short_but_not_empty(self):
        self.services.auth.set_password(self.user, "12345678")
        self.assertTrue(self.services.auth.verify_password(self.user, "12345678"))

        with self.assertRaisesRegex(ServiceError, "密码不能为空"):
            self.services.auth.set_password(self.user, "")

    def test_chat_system_prompt_uses_a_natural_visual_partner_identity(self):
        self.assertIn("AI 视觉创作搭档", CHAT_SYSTEM_PROMPT)
        self.assertIn("不要像客服、产品说明书或信息收集表", CHAT_SYSTEM_PROMPT)
        self.assertIn("先直接回应用户当前的问题", CHAT_SYSTEM_PROMPT)
        self.assertNotIn("公司内部 AI 视觉创作工作台的需求顾问", CHAT_SYSTEM_PROMPT)

    def test_image_workspace_chat_uses_static_image_guidance(self):
        workspace = self.create_workspace("单图讨论")

        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="设计一张电影感人物海报",
        )

        system = self.chat_client.calls[-1]["system"]
        self.assertIn("当前是静态图片工作站", system)
        self.assertIn("一张完整画面", system)
        self.assertNotIn("当前工作站用于制作帧动画", system)

    def test_animation_workspace_chat_uses_motion_specific_guidance(self):
        workspace = self.create_workspace("动作讨论", kind="animation")

        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="角色原地挥手并循环",
        )

        system = self.chat_client.calls[-1]["system"]
        self.assertIn("当前工作站用于制作帧动画", system)
        self.assertIn("动作起点", system)
        self.assertIn("首尾衔接", system)

    def test_admin_creates_user_and_balance_ledger_is_immutable_history(self):
        self.services.billing.adjust(
            user_id=self.user.id,
            actor_user_id=self.admin.id,
            amount="5.25",
            operation="add",
            note="季度额度",
        )
        user = db.session.get(User, self.user.id)
        self.assertEqual(user.balance_rmb, Decimal("25.2500"))
        entries = list(
            db.session.scalars(
                select(WalletLedger)
                .where(WalletLedger.user_id == self.user.id)
                .order_by(WalletLedger.id)
            )
        )
        self.assertEqual([entry.entry_type for entry in entries], ["initial_balance", "admin_add"])
        self.assertEqual(entries[-1].amount_rmb, Decimal("5.2500"))

    def test_spending_summary_uses_shanghai_day_and_only_generation_charges(self):
        now = datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)
        db.session.add_all(
            [
                WalletLedger(
                    user_id=self.user.id,
                    entry_type="generation_charge",
                    amount_rmb=Decimal("-1.2500"),
                    balance_after_rmb=Decimal("18.7500"),
                    note="昨日生图",
                    created_at=datetime(2026, 7, 13, 15, 59, tzinfo=timezone.utc),
                ),
                WalletLedger(
                    user_id=self.user.id,
                    entry_type="generation_charge",
                    amount_rmb=Decimal("-2.5000"),
                    balance_after_rmb=Decimal("16.2500"),
                    note="今日生图",
                    created_at=datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc),
                ),
                WalletLedger(
                    user_id=self.user.id,
                    actor_user_id=self.admin.id,
                    entry_type="admin_subtract",
                    amount_rmb=Decimal("-9.0000"),
                    balance_after_rmb=Decimal("7.2500"),
                    note="余额调整不计消费",
                    created_at=datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        db.session.commit()

        summary = self.services.billing.spending_summary(self.user.id, now=now)

        self.assertEqual(summary.total_rmb, Decimal("3.7500"))
        self.assertEqual(summary.today_rmb, Decimal("2.5000"))

    def test_user_and_admin_apis_include_spending_summaries(self):
        db.session.add_all(
            [
                WalletLedger(
                    user_id=self.user.id,
                    entry_type="generation_charge",
                    amount_rmb=Decimal("-1.2500"),
                    balance_after_rmb=Decimal("18.7500"),
                    note="用户生图",
                    created_at=utcnow(),
                ),
                WalletLedger(
                    user_id=self.admin.id,
                    entry_type="generation_charge",
                    amount_rmb=Decimal("-0.7500"),
                    balance_after_rmb=Decimal("0.0000"),
                    note="管理员生图",
                    created_at=utcnow(),
                ),
            ]
        )
        db.session.commit()

        user_client = self.user_client()
        me = user_client.get("/api/me").json
        self.assertEqual(me["spending"], {"today_rmb": "1.2500", "total_rmb": "1.2500"})
        user_client.post("/logout")

        admin_data = self.admin_client().get("/api/admin/users").json
        self.assertEqual(admin_data["spending"], {"today_rmb": "2.0000", "total_rmb": "2.0000"})
        users = {user["id"]: user for user in admin_data["users"]}
        self.assertEqual(
            users[self.user.id]["spending"],
            {"today_rmb": "1.2500", "total_rmb": "1.2500"},
        )

    def test_admin_balance_adjustment_note_is_optional(self):
        self.services.billing.adjust(
            user_id=self.user.id,
            actor_user_id=self.admin.id,
            amount="1.00",
            operation="add",
            note="",
        )
        entry = db.session.scalar(
            select(WalletLedger)
            .where(WalletLedger.user_id == self.user.id)
            .order_by(WalletLedger.id.desc())
        )
        self.assertEqual(entry.entry_type, "admin_add")
        self.assertEqual(entry.note, "")

    def test_at_most_ten_workspaces_per_user(self):
        for index in range(10):
            self.create_workspace(f"工作站 {index + 1}")
        with self.assertRaisesRegex(ServiceError, "最多创建 10 个"):
            self.create_workspace("第十一个")

        response = self.user_client().get("/api/workspaces")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["max_count"], 10)

    def test_first_studio_visit_creates_two_ready_to_use_starter_workspaces(self):
        client = self.user_client()

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        workspaces = self.services.workspaces.list(self.user.id)
        self.assertEqual(
            [workspace.name for workspace in workspaces],
            ["海风与远方", "参考图再创作"],
        )
        by_name = {workspace.name: workspace for workspace in workspaces}

        text_messages = client.get(f"/api/workspaces/{by_name['海风与远方'].id}/messages").json[
            "messages"
        ]
        self.assertEqual(len(text_messages), 1)
        self.assertEqual(text_messages[0]["kind"], "prompt_draft")
        self.assertEqual(text_messages[0]["payload"]["reference_ids"], [])
        self.assertIn("海洋", text_messages[0]["payload"]["prompt"])
        self.assertIn("天空", text_messages[0]["payload"]["prompt"])

        reference_messages = client.get(
            f"/api/workspaces/{by_name['参考图再创作'].id}/messages"
        ).json["messages"]
        self.assertEqual(len(reference_messages), 1)
        reference_draft = reference_messages[0]
        self.assertEqual(reference_draft["kind"], "prompt_draft")
        self.assertEqual(len(reference_draft["attachments"]), 1)
        self.assertEqual(
            reference_draft["payload"]["reference_ids"],
            [reference_draft["attachments"][0]["id"]],
        )
        image = client.get(reference_draft["attachments"][0]["url"])
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.mimetype, "image/png")
        image.close()

        client.get("/")
        self.assertEqual(len(self.services.workspaces.list(self.user.id)), 2)

    def test_existing_workspace_is_not_replaced_with_starter_content(self):
        existing = self.create_workspace("我的项目")

        response = self.user_client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [workspace.id for workspace in self.services.workspaces.list(self.user.id)],
            [existing.id],
        )

    def test_custom_size_workspace_setting_persists(self):
        workspace = self.create_workspace()
        client = self.user_client()

        response = client.patch(
            f"/api/workspaces/{workspace.id}",
            json={"settings": {"size": "1280x720"}},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["workspace"]["settings"]["size"], "1280x720")
        workspaces = client.get("/api/workspaces").json["workspaces"]
        restored = next(item for item in workspaces if item["id"] == workspace.id)
        self.assertEqual(restored["settings"]["size"], "1280x720")

    def test_chat_timestamps_and_response_duration_persist_through_api(self):
        workspace = self.create_workspace()
        sent_after = utcnow()
        user_message, assistant_message = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="设计一位穿晚礼服的中年男性",
        )

        self.assertGreaterEqual(user_message.created_at, sent_after)
        self.assertGreaterEqual(assistant_message.created_at, user_message.created_at)
        self.assertEqual(float(assistant_message.elapsed_seconds), 1.234)

        response = self.user_client().get(f"/api/workspaces/{workspace.id}/messages")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["total"], 2)
        self.assertFalse(response.json["conversation_operation"]["busy"])
        messages = response.json["messages"]
        self.assertIsNotNone(datetime.fromisoformat(messages[0]["created_at"]).tzinfo)
        self.assertIsNotNone(datetime.fromisoformat(messages[1]["created_at"]).tzinfo)
        self.assertIsNone(messages[0]["elapsed_seconds"])
        self.assertEqual(messages[1]["elapsed_seconds"], 1.234)

    def test_chat_is_serial_per_workspace_and_parallel_across_workspaces(self):
        workspace = self.create_workspace("串行工作站")
        other_workspace = self.create_workspace("并行工作站")
        client = BlockingFirstChatClient()
        conversations = self.services.conversations
        conversations.client = client
        conversations.context.client = client
        errors = []

        def send_blocking_message():
            with self.app.app_context():
                thread_workspace = db.session.get(Workspace, workspace.id)
                try:
                    conversations.send(
                        thread_workspace,
                        model_id="test-chat",
                        content="第一条消息",
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

        thread = threading.Thread(target=send_blocking_message)
        thread.start()
        try:
            self.assertTrue(client.started.wait(5))
            operation = conversations.operation_state(workspace.id)
            self.assertTrue(operation["busy"])
            self.assertEqual(operation["kind"], "reply")

            with self.assertRaises(ServiceError) as raised:
                conversations.send(
                    workspace,
                    model_id="test-chat",
                    content="不应并行的第二条消息",
                )
            self.assertEqual(raised.exception.code, "conversation_busy")
            self.assertEqual(raised.exception.status_code, 409)

            _user_message, assistant_message = conversations.send(
                other_workspace,
                model_id="test-chat",
                content="另一个工作站的消息",
            )
            self.assertEqual(assistant_message.content, "并行回复 2")
            self.assertEqual(len(client.calls), 2)
        finally:
            client.release.set()
            thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(conversations.operation_state(workspace.id)["busy"])

    def test_chat_operations_enforce_user_and_global_capacity(self):
        conversations = self.services.conversations
        own_workspaces = [
            self.create_workspace("并发工作站一"),
            self.create_workspace("并发工作站二"),
            self.create_workspace("并发工作站三"),
        ]
        with ExitStack() as operations:
            for workspace in own_workspaces[:2]:
                operations.enter_context(
                    conversations._workspace_operation(workspace, "reply", "等待回复")
                )
            with self.assertRaises(ServiceError) as raised:
                with conversations._workspace_operation(own_workspaces[2], "reply", "等待回复"):
                    pass
            self.assertEqual(raised.exception.code, "conversation_user_limit")
            self.assertEqual(raised.exception.status_code, 429)

        other = self.services.users.create(
            username="parallel-user",
            password="StrongPass123!",
            balance_rmb="5",
            actor_user_id=self.admin.id,
        )
        third = self.services.users.create(
            username="capacity-user",
            password="StrongPass123!",
            balance_rmb="5",
            actor_user_id=self.admin.id,
        )
        other_workspaces = [
            self.services.workspaces.create(other.id, "其他工作站一"),
            self.services.workspaces.create(other.id, "其他工作站二"),
        ]
        capacity_workspace = self.services.workspaces.create(third.id, "容量工作站")
        with ExitStack() as operations:
            for workspace in [*own_workspaces[:2], *other_workspaces]:
                operations.enter_context(
                    conversations._workspace_operation(workspace, "reply", "等待回复")
                )
            with self.assertRaises(ServiceError) as raised:
                with conversations._workspace_operation(capacity_workspace, "reply", "等待回复"):
                    pass
            self.assertEqual(raised.exception.code, "conversation_capacity")
            self.assertEqual(raised.exception.status_code, 503)

    def test_chat_multiple_attachments_are_sent_persisted_and_cannot_cross_workspaces(self):
        workspace = self.create_workspace()
        other_workspace = self.create_workspace("其他工作站")
        first_content = png_bytes((220, 35, 45))
        second_content = png_bytes((25, 80, 220))
        assets = self.services.workspaces.add_assets(
            workspace,
            [("subject.png", first_content), ("layout.png", second_content)],
        )
        user_message, _assistant_message = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="同时参考这两张图",
            attachment_ids=(assets[1].id, assets[0].id),
        )
        self.assertEqual(
            [item.asset_id for item in user_message.attachments],
            [assets[1].id, assets[0].id],
        )
        model_parts = self.chat_client.calls[-1]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in model_parts], ["text", "image_url", "image_url"])
        self.assertTrue(
            model_parts[1]["image_url"]["url"].endswith(
                base64.b64encode(second_content).decode("ascii")
            )
        )
        self.assertTrue(
            model_parts[2]["image_url"]["url"].endswith(
                base64.b64encode(first_content).decode("ascii")
            )
        )

        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
        )
        self.assertEqual(draft.payload["reference_ids"], [assets[1].id, assets[0].id])
        draft_parts = self.chat_client.calls[-1]["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in draft_parts], ["text", "image_url", "image_url"])

        with self.assertRaisesRegex(ServiceError, "参考图不存在"):
            self.services.conversations.send(
                other_workspace,
                model_id="test-chat",
                content="错误引用",
                attachment_ids=(assets[0].id,),
            )

    def test_workspace_reference_limit_allows_delete_then_custom_add(self):
        workspace = self.create_workspace()
        assets = self.services.workspaces.add_assets(
            workspace,
            [(f"reference-{index}.png", png_bytes((index * 20, 80, 160))) for index in range(8)],
        )
        with self.assertRaisesRegex(ServiceError, "最多保留 8 张参考图"):
            self.services.workspaces.add_assets(
                workspace,
                [("too-many.png", png_bytes((250, 250, 20)))],
            )

        self.services.workspaces.remove_asset(workspace, assets[0].id)
        replacement = self.services.workspaces.add_assets(
            workspace,
            [("custom-replacement.png", png_bytes((250, 250, 20)))],
        )
        self.assertEqual(len(replacement), 1)

    def test_reference_upload_rejects_oversized_and_incomplete_images(self):
        workspace = self.create_workspace()
        client = self.user_client()
        for content in (
            png_bytes_with_dimensions(9000, 100),
            png_bytes_with_dimensions(8192, 8192),
            png_bytes()[:40],
        ):
            response = client.post(
                f"/api/workspaces/{workspace.id}/assets",
                data={"references": (io.BytesIO(content), "invalid.png")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json["code"], "invalid_image")

        with self.assertRaises(InvalidImageError):
            self.app.extensions["image_storage"].inspect(png_bytes()[:40])

    def test_chat_api_uploads_multiple_references_and_delete_preserves_history(self):
        workspace = self.create_workspace()
        client = self.user_client()
        response = client.post(
            f"/api/workspaces/{workspace.id}/assets",
            data={
                "references": [
                    (io.BytesIO(png_bytes((220, 35, 45))), "subject.png"),
                    (io.BytesIO(png_bytes((25, 80, 220))), "layout.png"),
                ]
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        assets = response.json["assets"]
        self.assertEqual([asset["name"] for asset in assets], ["subject.png", "layout.png"])

        response = client.post(
            f"/api/workspaces/{workspace.id}/messages",
            json={
                "model_id": "test-chat",
                "content": "融合人物与版式参考",
                "attachment_ids": [asset["id"] for asset in assets],
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            [asset["id"] for asset in response.json["messages"][0]["attachments"]],
            [asset["id"] for asset in assets],
        )

        response = client.delete(f"/api/workspaces/{workspace.id}/assets/{assets[0]['id']}")
        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        workspaces = client.get("/api/workspaces").json["workspaces"]
        current = next(item for item in workspaces if item["id"] == workspace.id)
        self.assertEqual([asset["id"] for asset in current["assets"]], [assets[1]["id"]])
        messages = client.get(f"/api/workspaces/{workspace.id}/messages").json["messages"]
        self.assertEqual(
            [asset["id"] for asset in messages[0]["attachments"]],
            [asset["id"] for asset in assets],
        )

    def test_prompt_translation_defaults_off_and_records_draft_duration(self):
        workspace = self.create_workspace()
        self.assertFalse(workspace.settings["translate_prompt"])
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="电影感人物肖像",
        )
        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
        )
        self.assertEqual(draft.payload["language"], "zh")
        self.assertEqual(float(draft.elapsed_seconds), 1.234)
        self.assertIn("中文生图提示词", self.chat_client.calls[-1]["system"])

        translated = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=True,
        )
        self.assertEqual(translated.payload["language"], "en")
        self.assertIn("英文生图提示词", self.chat_client.calls[-1]["system"])

    def test_admin_can_customize_workspace_prompts_for_chat_and_drafts(self):
        client = self.admin_client()
        config = client.get("/api/admin/chat-models").json["config"]
        self.assertIn("当前是静态图片工作站", config["workspace_prompts"]["image"])
        self.assertIn(
            "当前工作站用于制作帧动画",
            config["workspace_prompts"]["animation"],
        )
        config["workspace_prompts"] = {
            "image": "自定义图片规则：画面只采用一个明确的视觉中心。",
            "animation": "自定义动画规则：角色造型和镜头必须逐帧稳定。",
        }

        response = client.put("/api/admin/chat-models", json=config)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["config"]["workspace_prompts"], config["workspace_prompts"])
        image_workspace = self.create_workspace("自定义单图")
        self.services.conversations.send(
            image_workspace,
            model_id="test-chat",
            content="生成一张产品主视觉",
        )
        self.assertIn("自定义图片规则", self.chat_client.calls[-1]["system"])
        self.services.conversations.create_prompt_draft(
            image_workspace,
            model_id="test-chat",
            translate_to_english=False,
        )
        self.assertIn("自定义图片规则", self.chat_client.calls[-1]["system"])
        self.assertIn("只输出一个 JSON 对象", self.chat_client.calls[-1]["system"])

        animation_workspace = self.create_workspace("自定义动画", kind="animation")
        self.services.conversations.send(
            animation_workspace,
            model_id="test-chat",
            content="角色转身后挥手",
        )
        self.assertIn("自定义动画规则", self.chat_client.calls[-1]["system"])
        self.assertNotIn("自定义图片规则", self.chat_client.calls[-1]["system"])

    def test_admin_can_assign_a_dedicated_prompt_draft_model(self):
        config = self.admin_client().get("/api/admin/chat-models").json["config"]
        config["models"].append(
            {
                "id": "prompt-mini",
                "label": "GPT 5.4 Mini",
                "enabled": True,
                "base_url": "https://chat.example",
                "api_key": "prompt-mini-key",
                "model": "gpt-5.4-mini",
                "reasoning_effort": "low",
                "timeout_seconds": 30,
                "max_output_tokens": 1000,
            }
        )
        config["prompt_draft_model_id"] = "prompt-mini"

        response = self.admin_client().put("/api/admin/chat-models", json=config)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["config"]["prompt_draft_model_id"], "prompt-mini")
        workspace = self.create_workspace()
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="电影感人物肖像",
        )
        draft = self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
        )
        self.assertEqual(self.chat_client.calls[-1]["model_id"], "prompt-mini")
        self.assertEqual(self.chat_client.calls[-1]["model"], "gpt-5.4-mini")
        self.assertEqual(draft.provider_id, "prompt-mini")
        self.assertEqual(workspace.settings["chat_model_id"], "test-chat")

    def test_active_generation_blocks_chat_until_job_is_terminal(self):
        workspace = self.create_workspace()
        self.submit(workspace)
        with self.assertRaisesRegex(ServiceError, "图片尚未生成完成"):
            self.services.conversations.send(
                workspace,
                model_id="test-chat",
                content="继续调整画面",
            )

    def test_first_chat_message_auto_titles_workspace_and_clear_removes_transcript(
        self,
    ):
        workspace = self.create_workspace("新会话")
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="红发蓝眼的中年男性角色设定",
        )
        self.assertEqual(workspace.name, "红发蓝眼的中年男性角色设定")
        self.services.workspaces.clear(workspace)
        count = db.session.scalar(
            select(func.count(ConversationMessage.id)).where(
                ConversationMessage.workspace_id == workspace.id
            )
        )
        self.assertEqual(count, 0)

    def test_channel_exposes_configured_models_and_price_without_key(self):
        channel = self.app.extensions["channel_registry"].get("test")
        public = channel.public_dict()
        self.assertEqual(public["price_rmb"], "1.2500")
        self.assertEqual([model["id"] for model in public["models"]], ["model-a", "model-b"])
        self.assertNotIn("api_key", public)
        self.assertNotIn("test-key", repr(channel))

    def test_text_to_image_uses_json_and_image_edit_uses_multipart(self):
        channel = self.app.extensions["channel_registry"].get("test")
        adapter = OpenAIImagesAdapter()
        session = RecordingImageSession()
        adapter._local.session = session
        request = GenerationRequest(
            prompt="电影感肖像",
            model="model-a",
            size="1024x1024",
            quality="medium",
            output_format="png",
            compression=90,
        )
        adapter.generate(channel, request)
        self.assertEqual(session.request["url"], "https://relay.example/v1/images/generations")
        self.assertEqual(session.request["json"]["n"], 1)
        self.assertNotIn("background", session.request["json"])
        self.assertNotIn("data", session.request)
        self.assertEqual(session.request["headers"]["Content-Type"], "application/json")

        transparent_result = adapter.generate(
            channel,
            replace(request, transparent_background=True),
        )
        self.assertEqual(session.request["json"]["background"], "transparent")
        with Image.open(io.BytesIO(transparent_result.content)) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getchannel("A").getextrema(), (0, 255))

        subject = png_bytes((220, 35, 45))
        layout = png_bytes((25, 80, 220))
        edit_request = replace(
            request,
            transparent_background=True,
            references=(
                ReferencePayload("subject.png", subject, "image/png"),
                ReferencePayload("layout.png", layout, "image/png"),
            ),
        )
        edit_result = adapter.generate(channel, edit_request)
        self.assertEqual(session.request["url"], "https://relay.example/v1/images/edits")
        self.assertEqual(session.request["data"]["n"], "1")
        self.assertEqual(session.request["data"]["background"], "transparent")
        self.assertEqual([part[0] for part in session.request["files"]], ["image", "image"])
        self.assertEqual(
            [part[1][0] for part in session.request["files"]],
            ["subject.png", "layout.png"],
        )
        self.assertEqual(
            [part[1][1] for part in session.request["files"]],
            [subject, layout],
        )
        self.assertNotIn("Content-Type", session.request["headers"])
        with Image.open(io.BytesIO(edit_result.content)) as image:
            self.assertEqual(image.getchannel("A").getextrema(), (0, 255))

    def test_transparent_background_retries_with_a_convertible_canvas(self):
        channel = self.app.extensions["channel_registry"].get("test")
        adapter = OpenAIImagesAdapter()
        session = RejectingTransparencySession()
        adapter._local.session = session

        result = adapter.generate(
            channel,
            GenerationRequest(
                prompt="极简上传图标",
                model="model-a",
                size="1024x1024",
                quality="medium",
                output_format="png",
                compression=90,
                transparent_background=True,
            ),
        )

        self.assertEqual(len(session.requests), 2)
        self.assertEqual(session.requests[0]["json"]["background"], "transparent")
        self.assertNotIn("background", session.requests[1]["json"])
        self.assertIn("#FFFFFF", session.requests[1]["json"]["prompt"])
        with Image.open(io.BytesIO(result.content)) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getchannel("A").getextrema(), (0, 255))

    def test_chat_request_sends_configured_reasoning_effort(self):
        model = self.app.extensions["chat_model_registry"].get("test-chat")
        session = RecordingChatSession()
        result = OpenAIChatClient(session).complete(
            model,
            system="系统提示",
            messages=[{"role": "user", "content": "你好"}],
        )
        self.assertEqual(model.reasoning_effort, "max")
        self.assertEqual(model.public_dict()["reasoning_effort"], "max")
        self.assertEqual(session.request["json"]["reasoning_effort"], "max")
        self.assertEqual(result.content, "测试回复")

    def test_submit_uses_one_channel_selected_model_and_reserves_batch_price(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, batch_count=3)
        user = db.session.get(User, self.user.id)
        self.assertEqual(job.channel_id, "test")
        self.assertEqual(job.model, "model-b")
        self.assertEqual(job.requested_count, 3)
        self.assertEqual(job.reserved_rmb, Decimal("3.7500"))
        self.assertEqual(user.reserved_rmb, Decimal("3.7500"))
        self.assertEqual(len(job.items), 3)

    def test_transparent_background_is_validated_persisted_and_serialized(self):
        workspace = self.create_workspace()
        with self.assertRaisesRegex(ServiceError, "透明背景仅支持 PNG 或 WebP"):
            self.submit(
                workspace,
                output_format="jpeg",
                transparent_background=True,
            )

        client = self.user_client()
        response = client.post(
            "/api/generations",
            json={
                "workspace_id": workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "text2img",
                "prompt": "极简云朵图标",
                "size": "1024x1024",
                "quality": "medium",
                "output_format": "png",
                "compression": 90,
                "batch_count": 1,
                "reference_ids": [],
                "transparent_background": True,
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json["job"]["transparent_background"])
        db.session.refresh(workspace)
        self.assertTrue(workspace.settings["transparent_background"])
        saved_job = db.session.get(GenerationJob, response.json["job"]["id"])
        self.assertTrue(saved_job.transparent_background)

    def test_animation_workspace_creation_and_parameters_are_persisted(self):
        client = self.user_client()
        response = client.post(
            "/api/workspaces",
            json={"name": "眨眼循环", "kind": "animation"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["workspace"]["kind"], "animation")

        workspace = db.session.get(Workspace, response.json["workspace"]["id"])
        job = self.submit(
            workspace,
            frame_count=8,
            animation_fps=12,
            animation_loop=False,
            animation_format="gif",
        )

        self.assertEqual(job.kind, "animation")
        self.assertEqual(job.requested_count, 8)
        self.assertEqual(job.animation_fps, 12)
        self.assertFalse(job.animation_loop)
        self.assertEqual(job.animation_format, "gif")
        self.assertEqual(job.reserved_rmb, Decimal("10.0000"))
        self.assertEqual(workspace.settings["animation_frame_count"], 8)
        payload = client.get(f"/api/generations/{job.id}").json["job"]
        self.assertEqual(payload["kind"], "animation")
        self.assertEqual(payload["animation_duration_seconds"], 0.667)

    def test_animation_frames_run_in_order_and_export_animated_webp(self):
        workspace = self.create_workspace("挥手循环", kind="animation")
        job = self.submit(
            workspace,
            frame_count=3,
            animation_fps=6,
            animation_loop=True,
            animation_format="webp",
        )
        item_ids = [item.id for item in job.items]
        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        worker.providers = FakeProviderFactory(vary=True)
        channel = self.app.extensions["channel_registry"].get("test")

        self.assertFalse(worker._claim(item_ids[1], channel))
        for item_id in item_ids:
            self.assertTrue(worker._claim(item_id, channel))
            worker._process_item(item_id)

        db.session.expire_all()
        saved_job = db.session.get(GenerationJob, job.id)
        self.assertEqual(saved_job.status, "succeeded")
        self.assertEqual(len(worker.providers.adapter.requests), 3)
        self.assertFalse(worker.providers.adapter.requests[0].references)
        self.assertEqual(len(worker.providers.adapter.requests[1].references), 1)
        self.assertIn("frame 2 of 3", worker.providers.adapter.requests[1].prompt)

        response = self.user_client().get(f"/media/animations/{job.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/webp")
        content = bytes(response.data)
        response.close()
        with Image.open(io.BytesIO(content)) as animation:
            self.assertTrue(animation.is_animated)
            self.assertEqual(animation.n_frames, 3)

    def test_animation_failure_stops_tail_and_releases_all_reserved_balance(self):
        workspace = self.create_workspace("失败动画", kind="animation")
        job = self.submit(workspace, frame_count=3)
        first_item_id = job.items[0].id
        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        worker.providers = FakeProviderFactory(fail=True)
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(first_item_id, channel))

        worker._process_item(first_item_id)

        db.session.expire_all()
        saved_job = db.session.get(GenerationJob, job.id)
        self.assertEqual(
            [item.status for item in saved_job.items], ["failed", "canceled", "canceled"]
        )
        self.assertEqual(saved_job.status, "failed")
        self.assertEqual(saved_job.items[1].error_code, "animation_dependency_failed")
        user = db.session.get(User, self.user.id)
        self.assertEqual(user.balance_rmb, Decimal("20.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))

    def test_custom_size_is_accepted_and_normalized(self):
        workspace = self.create_workspace()

        job = self.submit(workspace, size="1280X720")

        self.assertEqual(job.size, "1280x720")
        self.assertEqual(workspace.settings["size"], "1280x720")

    def test_invalid_custom_size_is_rejected(self):
        workspace = self.create_workspace()
        for size in ("1024", "0x1024", "63x1024", "9000x1024"):
            with self.subTest(size=size), self.assertRaisesRegex(ServiceError, "尺寸格式"):
                self.submit(workspace, size=size)

    def test_unknown_model_is_rejected(self):
        workspace = self.create_workspace()
        with self.assertRaisesRegex(ServiceError, "不支持模型"):
            self.submit(workspace, model="unknown")

    def test_multi_reference_assets_are_ordered_and_attached(self):
        workspace = self.create_workspace()
        assets = self.services.workspaces.add_assets(
            workspace,
            [("front.png", png_bytes()), ("style.png", png_bytes((40, 90, 180)))],
        )
        job = self.submit(
            workspace,
            mode="img2img",
            reference_ids=(assets[1].id, assets[0].id),
        )
        self.assertEqual(
            [reference.asset_id for reference in job.references],
            [assets[1].id, assets[0].id],
        )

    def test_canceling_queued_batch_releases_all_reserved_balance(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, batch_count=2)
        canceled = self.services.generations.cancel(job.id, user_id=self.user.id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(canceled.status, "canceled")
        self.assertEqual(canceled.reserved_rmb, Decimal("0.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertTrue(all(item.status == "canceled" for item in canceled.items))

    def test_canceling_running_item_discards_late_provider_result(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        providers = BlockingProviderFactory()
        worker.providers = providers
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))

        processing = threading.Thread(target=worker._process_item, args=(job.items[0].id,))
        processing.start()
        self.assertTrue(providers.adapter.started.wait(5))
        db.session.expire_all()
        canceled = self.services.generations.cancel(job.id, user_id=self.user.id)
        self.assertEqual(canceled.status, "canceling")
        providers.adapter.release.set()
        processing.join(10)
        self.assertFalse(processing.is_alive())

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.status, "canceled")
        self.assertIsNone(item.output_path)
        self.assertEqual(user.balance_rmb, Decimal("20.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        charge_count = db.session.scalar(
            select(func.count(WalletLedger.id)).where(
                WalletLedger.generation_item_id == item.id,
                WalletLedger.entry_type == "generation_charge",
            )
        )
        self.assertEqual(charge_count, 0)

    def test_worker_success_saves_image_and_charges_exactly_once(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, transparent_background=True)
        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))
        worker._process_item(job.items[0].id)
        self.assertTrue(worker.providers.adapter.request.transparent_background)

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.status, "succeeded")
        self.assertEqual(item.charged_rmb, Decimal("1.2500"))
        self.assertEqual(user.balance_rmb, Decimal("18.7500"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        self.assertTrue(self.app.extensions["image_storage"].read(item.output_path).is_file())
        charge_count = db.session.scalar(
            select(func.count(WalletLedger.id)).where(
                WalletLedger.generation_item_id == item.id,
                WalletLedger.entry_type == "generation_charge",
            )
        )
        self.assertEqual(charge_count, 1)

    def test_worker_failure_releases_reservation_without_charge(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        worker.providers = FakeProviderFactory(fail=True)
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))
        worker._process_item(job.items[0].id)

        db.session.expire_all()
        item = db.session.get(GenerationItem, job.items[0].id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.status, "failed")
        self.assertEqual(user.balance_rmb, Decimal("20.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))

    def test_worker_restart_recovers_recent_claim_and_discards_late_result(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        item_id = job.items[0].id
        old_worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        old_worker.worker_id = "worker-before-restart"
        old_worker.providers = BlockingProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(old_worker._claim(item_id, channel))

        processing = threading.Thread(target=old_worker._process_item, args=(item_id,))
        processing.start()
        self.assertTrue(old_worker.providers.adapter.started.wait(5))
        try:
            replacement = GenerationWorker(
                self.app,
                self.app.extensions["channel_registry"],
                self.app.extensions["image_storage"],
            )
            replacement.worker_id = "worker-after-restart"
            replacement._recover_orphaned_items(immediate=True)
        finally:
            old_worker.providers.adapter.release.set()
            processing.join(10)

        self.assertFalse(processing.is_alive())
        db.session.expire_all()
        item = db.session.get(GenerationItem, item_id)
        user = db.session.get(User, self.user.id)
        self.assertEqual(item.status, "interrupted")
        self.assertIsNone(item.output_path)
        self.assertEqual(user.balance_rmb, Decimal("20.0000"))
        self.assertEqual(user.reserved_rmb, Decimal("0.0000"))
        charge_count = db.session.scalar(
            select(func.count(WalletLedger.id)).where(
                WalletLedger.generation_item_id == item_id,
                WalletLedger.entry_type == "generation_charge",
            )
        )
        self.assertEqual(charge_count, 0)

    def test_worker_heartbeats_only_its_active_claims(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        item_id = job.items[0].id
        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        worker.worker_id = "heartbeat-worker"
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(item_id, channel))
        stale_heartbeat = utcnow() - timedelta(minutes=10)
        db.session.get(GenerationItem, item_id).heartbeat_at = stale_heartbeat
        db.session.commit()
        worker._futures[item_id] = Future()

        worker._heartbeat_claims()

        db.session.expire_all()
        heartbeat = db.session.get(GenerationItem, item_id).heartbeat_at
        self.assertNotEqual(heartbeat, stale_heartbeat)

    def test_worker_instances_have_unique_claim_identifiers(self):
        first = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        second = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        self.assertNotEqual(first.worker_id, second.worker_id)

    def test_worker_periodic_recovery_skips_live_future_and_recovers_abandoned_claim(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        item_id = job.items[0].id
        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        worker.worker_id = "periodic-recovery-worker"
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(item_id, channel))
        db.session.get(GenerationItem, item_id).heartbeat_at = utcnow() - timedelta(minutes=30)
        db.session.commit()
        worker._futures[item_id] = Future()

        worker._recover_orphaned_items(immediate=False)
        db.session.expire_all()
        self.assertEqual(db.session.get(GenerationItem, item_id).status, "running")

        worker._futures.clear()
        worker._recover_orphaned_items(immediate=False)
        db.session.expire_all()
        self.assertEqual(db.session.get(GenerationItem, item_id).status, "interrupted")
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("0.0000"))

    def test_worker_keeps_excess_images_queued_at_user_and_channel_limits(self):
        workspace = self.create_workspace()
        job = self.submit(workspace, batch_count=4)
        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        worker._thread_pool = HoldingExecutor()

        worker._schedule_available()
        db.session.expire_all()
        statuses = [item.status for item in db.session.get(GenerationJob, job.id).items]
        self.assertEqual(statuses.count("running"), 2)
        self.assertEqual(statuses.count("queued"), 2)

        db.session.get(User, self.user.id).generation_concurrency = 4
        db.session.commit()
        worker._schedule_available()
        db.session.expire_all()
        statuses = [item.status for item in db.session.get(GenerationJob, job.id).items]
        self.assertEqual(statuses.count("running"), 3)
        self.assertEqual(statuses.count("queued"), 1)

    def test_worker_schedules_with_its_own_application_context(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        job_id = job.id
        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        worker._thread_pool = HoldingExecutor()

        self.context.pop()
        try:
            worker._schedule_available()
        finally:
            self.context.push()

        db.session.expire_all()
        item = db.session.get(GenerationJob, job_id).items[0]
        self.assertEqual(item.status, "running")

    def test_queue_position_progress_and_estimated_end_are_serialized(self):
        first_workspace = self.create_workspace("第一队列")
        second_workspace = self.create_workspace("第二队列")
        first = self.submit(first_workspace)
        second = self.submit(second_workspace)
        client = self.user_client()

        queued = client.get("/api/generations?limit=10").json
        self.assertEqual(queued["queue_total"], 2)
        positions = {job["id"]: job["queue_position"] for job in queued["jobs"]}
        self.assertEqual(positions[first.id], 1)
        self.assertEqual(positions[second.id], 2)
        self.assertTrue(all(job["progress_percent"] == 0 for job in queued["jobs"]))
        self.assertTrue(all(job["estimated_end_at"] is None for job in queued["jobs"]))

        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(first.items[0].id, channel))
        running = client.get(f"/api/generations/{first.id}").json["job"]
        self.assertEqual(running["status"], "running")
        self.assertGreaterEqual(running["progress_percent"], 1)
        self.assertIsNotNone(running["estimated_end_at"])
        self.assertIsNone(running["queue_position"])

    def test_chat_attachment_media_is_visible_only_to_its_owner(self):
        workspace = self.create_workspace()
        asset = self.services.workspaces.add_assets(
            workspace, [("chat-reference.png", png_bytes())]
        )[0]
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="参考这张图片继续设计",
            attachment_ids=(asset.id,),
        )
        outsider = self.services.users.create(
            username="outsider",
            password="StrongPass123!",
            balance_rmb="5",
            actor_user_id=self.admin.id,
        )
        workspace_id = workspace.id
        outsider_username = outsider.username
        self.context.pop()
        try:
            client = self.user_client()
            messages = client.get(f"/api/workspaces/{workspace_id}/messages").json["messages"]
            attachment = messages[0]["attachments"][0]
            response = client.get(attachment["url"])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/png")
            self.assertEqual(response.data, png_bytes())
            response.close()

            outsider_client = self.app.test_client()
            login = outsider_client.post(
                "/login",
                data={"username": outsider_username, "password": "StrongPass123!"},
            )
            self.assertEqual(login.status_code, 302)
            denied = outsider_client.get(attachment["url"])
            self.assertEqual(denied.status_code, 404)
            denied.close()
        finally:
            self.context.push()

    def test_generated_image_history_view_download_and_reuse(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))
        worker._process_item(job.items[0].id)
        job_id = job.id
        workspace_id = workspace.id
        self.context.pop()
        try:
            client = self.user_client()
            history = client.get(f"/api/generations?workspace_id={workspace_id}&limit=10").json[
                "jobs"
            ]
            self.assertEqual([entry["id"] for entry in history], [job_id])
            item = history[0]["items"][0]
            self.assertEqual(item["status"], "succeeded")
            self.assertEqual(item["charged_rmb"], "1.2500")
            self.assertTrue(item["image_url"])
            self.assertTrue(item["thumbnail_url"])
            self.assertTrue(item["download_url"].endswith("?download=1"))

            original = client.get(item["image_url"])
            self.assertEqual(original.status_code, 200)
            self.assertEqual(original.mimetype, "image/png")
            self.assertEqual(original.data, png_bytes())
            self.assertNotIn("attachment", original.headers.get("Content-Disposition", ""))
            original.close()

            thumbnail = client.get(item["thumbnail_url"])
            self.assertEqual(thumbnail.status_code, 200)
            self.assertEqual(thumbnail.mimetype, "image/webp")
            with Image.open(io.BytesIO(thumbnail.data)) as preview:
                self.assertEqual(preview.format, "WEBP")
            thumbnail.close()

            download = client.get(item["download_url"])
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.data, png_bytes())
            self.assertIn("attachment", download.headers["Content-Disposition"].lower())
            self.assertIn(f"image_{item['id']}.png", download.headers["Content-Disposition"])
            download.close()

            reused = client.post(f"/api/generation-items/{item['id']}/reference")
            self.assertEqual(reused.status_code, 201)
            reused_asset = reused.json["asset"]
            self.assertTrue(reused_asset["name"].startswith("result_"))
            reused_response = client.get(reused_asset["url"])
            self.assertEqual(reused_response.data, png_bytes())
            reused_response.close()
        finally:
            self.context.push()

    def test_admin_uses_the_same_studio_with_an_extra_admin_entry(self):
        self.context.pop()
        try:
            user_client = self.user_client()
            user_page = user_client.get("/")
            self.assertEqual(user_page.status_code, 200)
            self.assertNotIn(b"header-admin", user_page.data)
            self.assertEqual(user_client.get("/admin").status_code, 403)

            admin_client = self.admin_client()
            admin_page = admin_client.get("/")
            self.assertEqual(admin_page.status_code, 200)
            self.assertIn(b"header-admin", admin_page.data)
            self.assertEqual(admin_client.get("/admin").status_code, 200)
        finally:
            self.context.push()

    def test_title_is_admin_configurable_version_is_not_a_setting(self):
        settings = self.services.settings
        self.assertEqual(settings.site_title(), "西郊比克王 AI Studio")
        self.assertEqual(settings.set_site_title("设计图像中心", self.admin.id), "设计图像中心")
        audit = db.session.scalar(select(AuditLog).where(AuditLog.action == "system.title.update"))
        self.assertEqual(audit.details["new_title"], "设计图像中心")
        response = self.app.test_client().get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["title"], "设计图像中心")
        self.assertRegex(response.json["version"], r"^\d+\.\d+\.\d+")

    def test_backup_copies_deployment_environment_with_private_permissions(self):
        root = Path(self.temp.name)
        source = root / "source.env"
        destination = root / "deployment.env"
        source.write_text("CONFIG_ENCRYPTION_KEY=test-only\n", encoding="utf-8")

        copy_private_file(source, destination)

        self.assertEqual(destination.read_bytes(), source.read_bytes())
        if os.name != "nt":
            self.assertEqual(destination.stat().st_mode & 0o077, 0)

    def test_admin_channel_config_is_encrypted_versioned_and_hot_reloaded(self):
        client = self.admin_client()
        initial = client.get("/api/admin/channels").json["config"]
        self.assertFalse(initial["managed"])
        self.assertEqual(initial["source"], "file")
        self.assertNotIn("test-key-not-secret", json.dumps(initial))

        initial["channels"][0]["price_rmb"] = "2.5000"
        response = client.put("/api/admin/channels", json=initial)
        self.assertEqual(response.status_code, 200)
        saved = response.json["config"]
        self.assertTrue(saved["managed"])
        self.assertEqual(saved["source"], "database")
        self.assertEqual(saved["channels"][0]["price_rmb"], "2.5000")

        channel = self.app.extensions["channel_registry"].get("test")
        self.assertEqual(channel.price_rmb, Decimal("2.5000"))
        self.assertEqual(channel.api_key, "test-key-not-secret")
        stored = db.session.get(SystemState, CHANNEL_CONFIG_KEY)
        self.assertIn("api_key_encrypted", stored.value)
        self.assertNotIn("test-key-not-secret", stored.value)

        stale = client.put("/api/admin/channels", json=initial)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["code"], "config_conflict")

    def test_admin_chat_config_replaces_key_without_exposing_it(self):
        client = self.admin_client()
        config = client.get("/api/admin/chat-models").json["config"]
        self.assertNotIn("test-chat-key-not-secret", json.dumps(config))
        config["models"][0]["api_key"] = "replacement-chat-key"
        config["models"][0]["reasoning_effort"] = "high"

        response = client.put("/api/admin/chat-models", json=config)
        self.assertEqual(response.status_code, 200)
        saved = response.json["config"]
        self.assertTrue(saved["managed"])
        self.assertNotIn("replacement-chat-key", json.dumps(saved))
        model = self.app.extensions["chat_model_registry"].get("test-chat")
        self.assertEqual(model.api_key, "replacement-chat-key")
        self.assertEqual(model.reasoning_effort, "high")
        stored = db.session.get(SystemState, CHAT_CONFIG_KEY)
        self.assertNotIn("replacement-chat-key", stored.value)

    def test_invalid_hot_reload_keeps_previous_channel_snapshot(self):
        registry = self.app.extensions["channel_registry"]
        old_version = registry.version
        self.channel_path.write_text("version: 1\nchannels: []\n", encoding="utf-8")
        self.assertFalse(registry.reload())
        self.assertEqual(registry.version, old_version)
        self.assertIn("至少需要一个渠道", registry.last_error)
        self.assertEqual(registry.get("test").label, "测试渠道")

    def test_retention_removes_old_generation_but_keeps_wallet_ledger(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        worker = GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        worker._claim(job.items[0].id, channel)
        worker._process_item(job.items[0].id)
        job = db.session.get(GenerationJob, job.id)
        job.completed_at = utcnow() - timedelta(days=31)
        db.session.commit()
        result = worker.retention.cleanup()
        self.assertEqual(result["jobs"], 1)
        self.assertIsNone(db.session.get(GenerationJob, job.id))
        self.assertEqual(
            db.session.scalar(
                select(func.count(WalletLedger.id)).where(WalletLedger.user_id == self.user.id)
            ),
            2,
        )

    def test_no_public_registration_route(self):
        response = self.app.test_client().get("/register")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
