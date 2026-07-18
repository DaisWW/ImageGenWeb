from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

from PIL import Image
from sqlalchemy import func, select

from imagegen.extensions import db
from imagegen.integrations.diagnostics import response_summary
from imagegen.integrations.images import (
    GenerationRequest,
    OpenAIImagesAdapter,
    ReferencePayload,
)
from imagegen.integrations.openai_chat import OpenAIChatClient, OpenAIChatError
from imagegen.models import (
    ConversationMessage,
    RuntimeLog,
    utcnow,
)
from imagegen.services.runtime_logs import sanitize_details
from tests.support.platform import (
    FailingOnceChatClient,
    FakeChatResponse,
    PlatformTestCase,
    RecordingChatSession,
    RecordingImageSession,
    RejectingTransparencySession,
    png_bytes,
)


class TestProviderAndRuntime(PlatformTestCase):
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
            quality="high",
            output_format="png",
            compression=90,
        )
        adapter.generate(channel, request)
        self.assertEqual(session.request["url"], "https://relay.example/v1/images/generations")
        self.assertEqual(session.request["json"]["n"], 1)
        self.assertEqual(session.request["json"]["prompt"], request.prompt)
        self.assertNotIn("background", session.request["json"])
        self.assertNotIn("data", session.request)
        self.assertEqual(session.request["headers"]["Content-Type"], "application/json")

        transparent_result = adapter.generate(
            channel,
            replace(request, transparent_background=True),
        )
        self.assertEqual(session.request["json"]["background"], "transparent")
        self.assertTrue(session.request["json"]["prompt"].startswith(request.prompt))
        self.assertIn("genuinely transparent canvas", session.request["json"]["prompt"])
        self.assertIn("transparency checkerboard", session.request["json"]["prompt"])
        self.assertIn("semi-transparent specks", session.request["json"]["prompt"])
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
        self.assertIn("genuinely transparent canvas", session.request["data"]["prompt"])
        self.assertEqual([part[0] for part in session.request["files"]], ["image[]", "image[]"])
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
                quality="high",
                output_format="png",
                compression=90,
                transparent_background=True,
            ),
        )

        self.assertEqual(len(session.requests), 2)
        self.assertEqual(session.requests[0]["json"]["background"], "transparent")
        self.assertIn("genuinely transparent canvas", session.requests[0]["json"]["prompt"])
        self.assertNotIn("background", session.requests[1]["json"])
        self.assertIn("#FFFFFF", session.requests[1]["json"]["prompt"])
        self.assertIn("transparency checkerboard", session.requests[1]["json"]["prompt"])
        self.assertNotIn("genuinely transparent canvas", session.requests[1]["json"]["prompt"])
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
        self.assertEqual(session.request["url"], "https://chat.example/v1/responses")
        self.assertEqual(session.request["headers"]["Accept"], "text/event-stream")
        self.assertTrue(session.request["stream"])
        self.assertTrue(session.request["json"]["stream"])
        self.assertEqual(session.request["json"]["instructions"], "系统提示")
        self.assertEqual(session.request["json"]["reasoning"], {"effort": "max"})
        self.assertEqual(
            session.request["json"]["input"],
            [{"role": "user", "content": [{"type": "input_text", "text": "你好"}]}],
        )
        self.assertEqual(result.content, "测试回复")
        self.assertEqual(result.input_tokens, 4)
        self.assertEqual(result.output_tokens, 2)
        self.assertTrue(session.responses == [])

    def test_chat_request_converts_image_parts_for_responses_api(self):
        model = self.app.extensions["chat_model_registry"].get("test-chat")
        session = RecordingChatSession()

        OpenAIChatClient(session).complete(
            model,
            system="系统提示",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "分析图片"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                },
                {"role": "assistant", "content": "已查看"},
            ],
        )

        self.assertEqual(
            session.request["json"]["input"][0]["content"],
            [
                {"type": "input_text", "text": "分析图片"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ],
        )
        self.assertEqual(
            session.request["json"]["input"][1],
            {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "已查看"}],
            },
        )

    def test_chat_retries_one_explicit_http_502(self):
        model = self.app.extensions["chat_model_registry"].get("test-chat")
        first = FakeChatResponse(
            status_code=502,
            payload={"error": {"message": "temporarily unavailable", "type": "upstream_error"}},
            headers={"x-request-id": "first-502", "content-type": "application/json"},
        )
        second = FakeChatResponse()
        session = RecordingChatSession([first, second])

        with patch("imagegen.integrations.openai_chat.time.sleep") as sleep:
            result = OpenAIChatClient(session).complete(
                model,
                system="系统提示",
                messages=[{"role": "user", "content": "你好"}],
            )

        self.assertEqual(result.content, "测试回复")
        self.assertEqual(len(session.requests), 2)
        sleep.assert_called_once_with(0.5)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_chat_timeout_is_a_total_budget_across_retries(self):
        model = replace(
            self.app.extensions["chat_model_registry"].get("test-chat"),
            timeout_seconds=10,
        )
        first = FakeChatResponse(status_code=502)
        session = RecordingChatSession([first])
        clock = [0.0]

        def monotonic():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds + 10

        with patch("imagegen.integrations.openai_chat.time.monotonic", side_effect=monotonic):
            with patch("imagegen.integrations.openai_chat.time.sleep", side_effect=sleep):
                with self.assertRaises(OpenAIChatError) as raised:
                    OpenAIChatClient(session).complete(
                        model,
                        system="系统提示",
                        messages=[{"role": "user", "content": "你好"}],
                    )

        self.assertEqual(raised.exception.code, "chat_timeout")
        self.assertEqual(len(session.requests), 1)
        self.assertTrue(first.closed)

    def test_chat_timeout_stops_a_stream_that_keeps_sending_data(self):
        model = replace(
            self.app.extensions["chat_model_registry"].get("test-chat"),
            timeout_seconds=10,
        )
        clock = [0.0]

        class KeepAliveResponse(FakeChatResponse):
            def iter_lines(self, *, chunk_size=512, decode_unicode=False):
                yield b": keep-alive"
                clock[0] = 11.0
                yield b""

        response = KeepAliveResponse()
        session = RecordingChatSession([response])

        with patch(
            "imagegen.integrations.openai_chat.time.monotonic", side_effect=lambda: clock[0]
        ):
            with self.assertRaises(OpenAIChatError) as raised:
                OpenAIChatClient(session).complete(
                    model,
                    system="系统提示",
                    messages=[{"role": "user", "content": "你好"}],
                )

        self.assertEqual(raised.exception.code, "chat_timeout")
        self.assertGreaterEqual(raised.exception.elapsed_seconds, 10)
        self.assertTrue(response.closed)

    def test_chat_does_not_retry_non_502_errors(self):
        model = self.app.extensions["chat_model_registry"].get("test-chat")
        response = FakeChatResponse(
            status_code=429,
            payload={"error": {"message": "busy", "type": "rate_limit_error"}},
            headers={"x-request-id": "rate-limit", "content-type": "application/json"},
        )
        session = RecordingChatSession([response])

        with self.assertRaises(OpenAIChatError):
            OpenAIChatClient(session).complete(
                model,
                system="系统提示",
                messages=[{"role": "user", "content": "你好"}],
            )

        self.assertEqual(len(session.requests), 1)
        self.assertTrue(response.closed)

    def test_chat_calls_create_runtime_logs_without_prompt_or_credentials(self):
        workspace = self.create_workspace()

        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="这段完整提示词不能进入运行日志",
        )

        entry = db.session.scalar(select(RuntimeLog).where(RuntimeLog.event == "chat.reply"))
        self.assertIsNotNone(entry)
        self.assertEqual(entry.status, "success")
        self.assertEqual(entry.user_id, self.user.id)
        self.assertEqual(entry.workspace_id, workspace.id)
        self.assertEqual(entry.model, "gpt-test")
        serialized = json.dumps(entry.details, ensure_ascii=False)
        self.assertNotIn("这段完整提示词", serialized)
        self.assertNotIn("test-chat-key-not-secret", serialized)

        sanitized = sanitize_details(
            {
                "api_key": "secret-value",
                "prompt": "private prompt",
                "nested": {"authorization": "Bearer private-token"},
            }
        )
        self.assertNotIn("secret-value", json.dumps(sanitized))
        self.assertNotIn("private prompt", json.dumps(sanitized))
        self.assertNotIn("private-token", json.dumps(sanitized))

        diagnostics = response_summary(
            FakeChatResponse(),
            {
                "error": {
                    "code": "invalid_prompt",
                    "type": "invalid_request_error",
                    "message": "private prompt echoed by provider",
                }
            },
        )
        self.assertEqual(diagnostics["error_code"], "invalid_prompt")
        self.assertNotIn("private prompt", json.dumps(diagnostics))

    def test_unrecognized_chat_response_returns_searchable_error_id(self):
        workspace = self.create_workspace()
        chat = OpenAIChatClient(
            RecordingChatSession(
                [
                    FakeChatResponse(
                        headers={
                            "x-request-id": "chat-shape-test",
                            "content-type": "text/event-stream",
                        },
                        lines=[
                            'data: {"type":"response.completed","response":{"id":"chat-shape-test",'
                            '"status":"completed","output":[{"type":"message"}]}}',
                            "",
                        ],
                    )
                ]
            )
        )
        self.services.conversations.client = chat

        response = self.user_client().post(
            f"/api/workspaces/{workspace.id}/messages",
            json={"model_id": "test-chat", "content": "分析这张图", "attachment_ids": []},
        )

        self.assertEqual(response.status_code, 201)
        user_message, error_message = response.json["messages"]
        self.assertEqual(user_message["role"], "user")
        self.assertEqual(error_message["role"], "assistant")
        self.assertEqual(error_message["kind"], "error")
        self.assertEqual(error_message["payload"]["code"], "chat_provider_error")
        self.assertEqual(
            error_message["payload"]["retry_user_message_id"],
            user_message["id"],
        )
        error_id = error_message["payload"]["error_id"]
        self.assertIn(error_id, error_message["content"])
        entry = db.session.get(RuntimeLog, error_id)
        self.assertEqual(entry.status, "error")
        self.assertEqual(entry.http_status, 200)
        self.assertEqual(entry.upstream_request_id, "chat-shape-test")
        self.assertIn("output", entry.details["diagnostics"]["top_level_keys"])
        serialized = json.dumps(entry.details, ensure_ascii=False)
        self.assertNotIn("must-not-be-logged", serialized)

        self.context.pop()
        try:
            admin = self.admin_client()
            listed = admin.get(f"/api/admin/runtime-logs?status=error&search={error_id}")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual([item["id"] for item in listed.json["logs"]], [error_id])
            created_at = datetime.fromisoformat(listed.json["logs"][0]["created_at"])
            self.assertEqual(created_at.utcoffset(), timedelta(0))
            detail = admin.get(f"/api/admin/runtime-logs/{error_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertNotIn("must-not-be-logged", json.dumps(detail.json, ensure_ascii=False))
            self.assertEqual(
                self.user_client().get(f"/api/admin/runtime-logs/{error_id}").status_code,
                403,
            )
        finally:
            self.context.push()

    def test_failed_chat_retry_keeps_user_message_and_excludes_error_from_context(self):
        workspace = self.create_workspace()
        chat = FailingOnceChatClient()
        self.services.conversations.client = chat
        client = self.user_client()

        failed = client.post(
            f"/api/workspaces/{workspace.id}/messages",
            json={
                "model_id": "test-chat",
                "content": "请保留这条用户需求",
                "attachment_ids": [],
            },
        )
        self.assertEqual(failed.status_code, 201)
        user_message, error_message = failed.json["messages"]

        retried = client.post(
            f"/api/workspaces/{workspace.id}/messages/{error_message['id']}/retry",
            json={"model_id": "test-chat"},
        )

        self.assertEqual(retried.status_code, 201)
        self.assertRegex(retried.json["message"]["id"], r"^[a-f0-9]{32}$")
        self.assertEqual(retried.json["message"]["kind"], "message")
        self.assertEqual(
            retried.json["message"]["payload"]["reply_to_message_id"],
            user_message["id"],
        )
        calls_after_retry = len(chat.calls)
        repeated_retry = client.post(
            f"/api/workspaces/{workspace.id}/messages/{error_message['id']}/retry",
            json={"model_id": "test-chat"},
        )
        self.assertEqual(repeated_retry.json["message"]["id"], retried.json["message"]["id"])
        self.assertEqual(len(chat.calls), calls_after_retry)
        retry_context = chat.calls[-1]["messages"]
        self.assertEqual(retry_context, [{"role": "user", "content": user_message["content"]}])
        self.assertNotIn(error_message["content"], json.dumps(retry_context, ensure_ascii=False))

        continued = client.post(
            f"/api/workspaces/{workspace.id}/messages",
            json={
                "model_id": "test-chat",
                "content": "继续补充新的要求",
                "attachment_ids": [],
            },
        )
        self.assertEqual(continued.status_code, 201)
        continued_context = chat.calls[-1]["messages"]
        self.assertEqual(
            [message["content"] for message in continued_context],
            [
                user_message["content"],
                retried.json["message"]["content"],
                "继续补充新的要求",
            ],
        )
        self.assertNotIn(
            error_message["content"], json.dumps(continued_context, ensure_ascii=False)
        )
        stored = client.get(f"/api/workspaces/{workspace.id}/messages").json["messages"]
        self.assertEqual(
            [(message["role"], message["kind"]) for message in stored],
            [
                ("user", "message"),
                ("assistant", "error"),
                ("assistant", "message"),
                ("user", "message"),
                ("assistant", "message"),
            ],
        )

    def test_chat_message_ids_make_send_idempotent(self):
        workspace = self.create_workspace()
        client = self.user_client()
        payload = {
            "message_id": "1" * 32,
            "model_id": "test-chat",
            "content": "同一条消息只发送一次",
            "attachment_ids": [],
        }

        first = client.post(f"/api/workspaces/{workspace.id}/messages", json=payload)
        calls_after_first = len(self.chat_client.calls)
        replay = client.post(f"/api/workspaces/{workspace.id}/messages", json=payload)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(
            first.json["messages"][0]["id"],
            payload["message_id"],
        )
        self.assertRegex(first.json["messages"][1]["id"], r"^[a-f0-9]{32}$")
        self.assertEqual(
            first.json["messages"][0]["payload"]["reply_message_id"],
            first.json["messages"][1]["id"],
        )
        self.assertEqual(
            first.json["messages"][1]["payload"]["reply_to_message_id"],
            payload["message_id"],
        )
        self.assertEqual(replay.json["messages"], first.json["messages"])
        self.assertEqual(len(self.chat_client.calls), calls_after_first)
        self.assertEqual(
            db.session.scalar(
                select(func.count(ConversationMessage.id)).where(
                    ConversationMessage.workspace_id == workspace.id
                )
            ),
            2,
        )

        conflict = client.post(
            f"/api/workspaces/{workspace.id}/messages",
            json={**payload, "content": "尝试复用消息 ID"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["code"], "conversation_message_id_conflict")

    def test_unhandled_api_error_is_logged_with_correlation_id(self):
        @self.app.get("/api/test-unhandled-runtime-error")
        def test_unhandled_runtime_error():
            raise RuntimeError("private exception detail")

        response = self.user_client().get("/api/test-unhandled-runtime-error")

        self.assertEqual(response.status_code, 500)
        error_id = response.json["error_id"]
        entry = db.session.get(RuntimeLog, error_id)
        self.assertEqual(entry.category, "web")
        self.assertEqual(entry.error_code, "internal_error")
        self.assertEqual(entry.details["exception_type"], "RuntimeError")
        self.assertNotIn("private exception detail", json.dumps(entry.details))

    def test_runtime_log_retention_removes_only_expired_events(self):
        expired = self.services.runtime_logs.record(
            category="worker",
            event="test.expired",
            status="success",
        )
        expired.created_at = utcnow() - timedelta(days=31)
        current = self.services.runtime_logs.record(
            category="worker",
            event="test.current",
            status="success",
        )
        db.session.commit()

        removed = self.services.runtime_logs.purge(30)

        self.assertEqual(removed, 1)
        self.assertIsNone(db.session.get(RuntimeLog, expired.id))
        self.assertIsNotNone(db.session.get(RuntimeLog, current.id))
