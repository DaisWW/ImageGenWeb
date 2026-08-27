from __future__ import annotations

import json
import threading
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import select

from imagegen.extensions import db
from imagegen.integrations.openai_chat import OpenAIChatError
from imagegen.models import (
    RuntimeLog,
    Workspace,
    utcnow,
)
from imagegen.services import ServiceError
from tests.support.platform import (
    BlockingFirstChatClient,
    FailingAfterOutputChatClient,
    FailingBeforeOutputChatClient,
    PlatformTestCase,
    png_bytes,
)


class TestConversations(PlatformTestCase):
    def test_chat_switches_to_configured_fallback_before_first_output(self):
        self.chat_path.write_text(
            """\
version: 1
context:
  max_context_tokens: 32000
models:
  - id: primary
    label: Primary
    enabled: true
    base_url: https://primary.example
    api_key_env: TEST_CHAT_KEY
    model: gpt-primary
    reasoning_effort: max
    review_reasoning_effort: medium
    timeout_seconds: 30
    max_output_tokens: 1000
    fallback_model_ids: [fallback]
  - id: fallback
    label: Fallback
    enabled: true
    base_url: https://fallback.example
    api_key_env: TEST_CHAT_KEY
    model: gpt-fallback
    reasoning_effort: max
    review_reasoning_effort: medium
    timeout_seconds: 30
    max_output_tokens: 1000
""",
            encoding="utf-8",
        )
        self.app.extensions["chat_model_registry"].reload(force=True)
        client = FailingBeforeOutputChatClient()
        self.services.conversations.client = client
        workspace = self.create_workspace("聊天故障切换")

        _user_message, assistant_message = self.services.conversations.send(
            workspace,
            model_id="primary",
            content="请生成一张人物海报",
        )

        self.assertEqual([call["model_id"] for call in client.calls], ["primary", "fallback"])
        self.assertEqual(assistant_message.provider_id, "fallback")
        self.assertEqual(workspace.settings["chat_model_id"], "primary")
        failed = db.session.scalar(
            select(RuntimeLog).where(
                RuntimeLog.event == "chat.reply",
                RuntimeLog.status == "error",
            )
        )
        self.assertTrue(failed.details["will_retry"])
        self.assertEqual(failed.details["next_model_id"], "fallback")

    def test_chat_does_not_switch_after_non_streaming_output_started(self):
        self.chat_path.write_text(
            """\
version: 1
context:
  max_context_tokens: 32000
models:
  - id: primary
    label: Primary
    enabled: true
    base_url: https://primary.example
    api_key_env: TEST_CHAT_KEY
    model: gpt-primary
    reasoning_effort: max
    review_reasoning_effort: medium
    timeout_seconds: 30
    max_output_tokens: 1000
    fallback_model_ids: [fallback]
  - id: fallback
    label: Fallback
    enabled: true
    base_url: https://fallback.example
    api_key_env: TEST_CHAT_KEY
    model: gpt-fallback
    reasoning_effort: max
    review_reasoning_effort: medium
    timeout_seconds: 30
    max_output_tokens: 1000
""",
            encoding="utf-8",
        )
        self.app.extensions["chat_model_registry"].reload(force=True)
        client = FailingAfterOutputChatClient()
        self.services.conversations.client = client
        workspace = self.create_workspace("输出后不切换")

        _user_message, assistant_message = self.services.conversations.send(
            workspace,
            model_id="primary",
            content="请生成一张人物海报",
        )

        self.assertEqual([call["model_id"] for call in client.calls], ["primary"])
        self.assertEqual(assistant_message.kind, "error")
        self.assertEqual(assistant_message.payload["code"], "chat_upstream_error")

    def test_chat_returns_a_normal_answer_for_non_visual_conversation(self):
        workspace = self.create_workspace("普通对话")
        self.chat_client.reply_content = (
            "我是你的 AI 视觉创作搭档。你可以直接描述想要的画面，"
            "我会帮你梳理创意并整理成生图提示词。"
        )

        _user_message, assistant_message = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="你是谁",
        )

        self.assertEqual(assistant_message.kind, "message")
        self.assertEqual(assistant_message.payload["status"], "conversation")
        self.assertIn("AI 视觉创作搭档", assistant_message.content)
        self.assertNotIn("prompt", assistant_message.payload)
        self.assertEqual(assistant_message.payload["reference_ids"], [])
        self.assertEqual(len(self.chat_client.calls), 1)
        self.assertIn("直接输出自然语言", self.chat_client.calls[0]["system"])

    def test_chat_repairs_invalid_output_once_before_showing_an_error(self):
        workspace = self.create_workspace("自动修复格式")
        replies = iter(
            [
                '{"status":"conversation","message":null}',
                "我是你的 AI 视觉创作搭档。",
            ]
        )
        complete = self.chat_client.complete

        def sequenced_complete(*args, **kwargs):
            self.chat_client.reply_content = next(replies)
            return complete(*args, **kwargs)

        with patch.object(self.chat_client, "complete", side_effect=sequenced_complete):
            _user_message, assistant_message = self.services.conversations.send(
                workspace,
                model_id="test-chat",
                content="你是谁",
            )

        self.assertEqual(assistant_message.kind, "message")
        self.assertEqual(assistant_message.payload["status"], "conversation")
        self.assertEqual(len(self.chat_client.calls), 2)
        self.assertEqual(self.chat_client.calls[1]["messages"][-2]["role"], "assistant")
        self.assertIn(
            "没有通过结构化输出校验",
            self.chat_client.calls[1]["messages"][-1]["content"],
        )
        entries, _total = self.services.runtime_logs.list_runtime(
            category="chat",
            status="success",
            since_hours=None,
        )
        self.assertTrue(entries[0].details["format_repaired"])
        self.assertEqual(entries[0].details["output_tokens"], 24)

    def test_chat_keeps_a_real_provider_error_from_the_repair_request(self):
        workspace = self.create_workspace("修复请求超时")
        complete = self.chat_client.complete

        def invalid_then_timeout(*args, **kwargs):
            if self.chat_client.calls:
                raise OpenAIChatError(
                    "聊天模型响应超时",
                    code="chat_timeout",
                    status_code=504,
                    elapsed_seconds=2.5,
                )
            self.chat_client.reply_content = '{"status":"conversation","message":null}'
            return complete(*args, **kwargs)

        with patch.object(self.chat_client, "complete", side_effect=invalid_then_timeout):
            _user_message, assistant_message = self.services.conversations.send(
                workspace,
                model_id="test-chat",
                content="你是谁",
            )

        self.assertEqual(assistant_message.kind, "error")
        self.assertEqual(assistant_message.payload["code"], "chat_timeout")
        self.assertIn("响应超时", assistant_message.content)

    def test_chat_accepts_nested_message_alias_and_hides_reasoning_tags(self):
        workspace = self.create_workspace("宽松聊天格式")
        self.chat_client.reply_content = json.dumps(
            {
                "status": "chat",
                "result": {"response": "<analysis>不要显示这段推理</analysis>这是可见回复。"},
            },
            ensure_ascii=False,
        )

        _user_message, assistant_message = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="简单聊聊你能做什么",
        )

        self.assertEqual(assistant_message.kind, "message")
        self.assertEqual(assistant_message.content, "这是可见回复。")
        self.assertNotIn("推理", assistant_message.content)
        self.assertEqual(len(self.chat_client.calls), 1)

    def test_chat_hides_reasoning_tags_from_plain_text_and_clarifications(self):
        workspace = self.create_workspace("纯文本隐藏推理")
        self.chat_client.reply_content = "<think>内部推理</think>这是可见回复。"

        _user_message, assistant_message = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="你是谁",
        )

        self.assertEqual(assistant_message.kind, "message")
        self.assertEqual(assistant_message.content, "这是可见回复。")
        self.assertNotIn("内部推理", assistant_message.content)

        clarification_workspace = self.create_workspace("澄清隐藏推理")
        self.chat_client.reply_content = json.dumps(
            {
                "status": "needs_clarification",
                "questions": ["<analysis>内部推理</analysis>请确认海报画幅。"],
            },
            ensure_ascii=False,
        )

        _user_message, clarification = self.services.conversations.send(
            clarification_workspace,
            model_id="test-chat",
            content="帮我做一张海报",
        )

        self.assertEqual(clarification.kind, "message")
        self.assertIn("请确认海报画幅", clarification.content)
        self.assertNotIn("内部推理", clarification.content)

    def test_chat_returns_ready_prompt_draft_in_one_model_call(self):
        workspace = self.create_workspace()
        self.chat_client.reply_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": "竖版新品海报，具体视觉细节由 AI 决定。",
                "prompt": "竖版新品海报，主体明确，层级清晰。",
                "creative_direction": "poster",
                "template_id": "poster-layout-system",
                "style_tags": ["Poster"],
                "scene_tags": ["Commerce"],
                "selection_reason": "交付物是新品海报。",
                "brief": {"deliverable": "新品海报"},
                "hard_checks": ["只有一个主视觉", "版式层级清晰"],
                "quality_hint": "low",
            },
            ensure_ascii=False,
        )

        _user_message, assistant_message = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="请生成一张竖版新品海报，其他细节你决定",
        )

        self.assertEqual(assistant_message.kind, "prompt_draft")
        self.assertEqual(assistant_message.payload["status"], "ready")
        self.assertIn("竖版新品海报", assistant_message.payload["prompt"])
        self.assertEqual(len(self.chat_client.calls), 1)
        self.assertEqual(self.chat_client.calls[0]["reasoning_effort"], "medium")
        self.assertIn(
            "本次调用同时完成需求确认和最终提示词整理", self.chat_client.calls[0]["system"]
        )

    def test_chat_turn_falls_back_after_repeated_invalid_structured_output(self):
        workspace = self.create_workspace("对话结构化错误")
        self.chat_client.reply_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": None,
                "prompt": ["invalid"],
            }
        )

        _user_message, assistant_message = self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content="生成一张新品海报",
        )

        self.assertEqual(assistant_message.kind, "message")
        self.assertEqual(assistant_message.payload["status"], "conversation")
        self.assertIn("继续描述想法", assistant_message.content)
        self.assertEqual(len(self.chat_client.calls), 2)
        self.assertIsNone(
            db.session.query(RuntimeLog)
            .filter(RuntimeLog.event == "chat.reply", RuntimeLog.status == "error")
            .first()
        )

    def test_chat_semantically_decides_whether_attachments_are_generation_references(self):
        cases = (
            (
                "generation",
                "基于这两张水墨武侠图，仿照风格生成一套不同的招式图标。",
                "参考图 1 和参考图 2 作为水墨风格与动态构图依据，生成不同的武侠招式图标。",
                "img2img",
            ),
            (
                "analysis_only",
                "只分析这两张图的水墨风格，之后按文字独立创作，不要把原图传给生图模型。",
                "独立创作一套高反差黑白水墨武侠图标，不使用参考图作为生成输入。",
                "text2img",
            ),
            (
                None,
                "参考这两张图生成一套新的武侠图标。",
                "参考图 1 和参考图 2 的水墨语言，生成一套新的武侠图标。",
                "img2img",
            ),
        )
        for index, (usage, content, prompt, expected_mode) in enumerate(cases):
            with self.subTest(reference_usage=usage):
                workspace = self.create_workspace(f"语义垫图 {index}")
                assets = self.services.workspaces.add_assets(
                    workspace,
                    [
                        ("style-a.png", png_bytes()),
                        ("style-b.png", png_bytes((40, 90, 180))),
                    ],
                )
                response = {
                    "status": "ready",
                    "summary_zh": content,
                    "prompt": prompt,
                    "creative_direction": "icon",
                    "template_id": "icon-symbol-system",
                    "style_tags": [],
                    "scene_tags": [],
                    "selection_reason": "按图标交付物整理。",
                    "brief": {"deliverable": "武侠图标集"},
                    "hard_checks": ["图标清晰"],
                    "quality_hint": "low",
                    "reference_reason": "根据用户是否要求生成阶段直接依赖图片决定。",
                }
                if usage is not None:
                    response["reference_usage"] = usage
                self.chat_client.reply_content = json.dumps(response, ensure_ascii=False)

                _user_message, assistant = self.services.conversations.send(
                    workspace,
                    model_id="test-chat",
                    content=content,
                    attachment_ids=tuple(asset.id for asset in assets),
                )

                expected_references = (
                    [asset.id for asset in assets] if expected_mode == "img2img" else []
                )
                self.assertEqual(assistant.kind, "prompt_draft")
                self.assertEqual(assistant.payload["generation_mode"], expected_mode)
                self.assertEqual(assistant.payload["reference_ids"], expected_references)
                self.assertEqual(
                    [attachment.asset_id for attachment in assistant.attachments],
                    expected_references,
                )
                self.assertEqual(
                    assistant.payload["reference_usage"],
                    "generation" if expected_references else "analysis_only",
                )
                system = self.chat_client.calls[-1]["system"]
                self.assertIn("判断这些图片是否必须作为最终生图输入", system)
                self.assertIn('reference_usage="analysis_only"', system)

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

    def test_chat_preview_event_stream_closes_with_operation(self):
        workspace = self.create_workspace("流式预览")
        operation_id = "d" * 32
        client = self.user_client()

        with self.services.conversations.operations.workspace_operation(
            workspace,
            "reply",
            "正在确认需求",
            operation_id=operation_id,
            message_id="e" * 32,
        ) as operation:
            operation.update_preview("需求确认\n流式测试")
            response = client.get(
                f"/api/workspaces/{workspace.id}/operations/{operation_id}/events",
                buffered=False,
            )
            chunks = iter(response.response)
            preview = next(chunks).decode("utf-8")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "text/event-stream")
            self.assertEqual(response.headers["X-Accel-Buffering"], "no")
            self.assertIn("event: preview", preview)
            self.assertIn("流式测试", preview)

        closing = next(chunks).decode("utf-8")
        self.assertIn("event: close", closing)
        response.close()

    def test_chat_preview_rejects_invalid_and_unknown_operation_ids_immediately(self):
        workspace = self.create_workspace("无效预览")
        client = self.user_client()

        invalid = client.get(f"/api/workspaces/{workspace.id}/operations/not-an-id/events")
        unknown = client.get(f"/api/workspaces/{workspace.id}/operations/{'a' * 32}/events")

        self.assertEqual(invalid.status_code, 404)
        self.assertEqual(unknown.status_code, 404)

    def test_chat_preview_reservation_bridges_request_registration_race(self):
        workspace = self.create_workspace("预览预约")
        operation_id = "c" * 32
        client = self.user_client()
        reserved = client.post(
            f"/api/workspaces/{workspace.id}/operations/{operation_id}/preview-reservation",
            json={},
        )
        self.assertEqual(reserved.status_code, 200)

        with self.services.conversations.operations.workspace_operation(
            workspace,
            "reply",
            "正在确认需求",
            operation_id=operation_id,
        ) as operation:
            operation.update_preview("预约成功")
            response = client.get(
                f"/api/workspaces/{workspace.id}/operations/{operation_id}/events",
                buffered=False,
            )
            self.assertIn("预约成功", next(iter(response.response)).decode("utf-8"))
            response.close()

    def test_chat_preview_connection_limit_releases_on_disconnect(self):
        workspace = self.create_workspace("预览限流")
        other = self.create_workspace("预览限流二")
        settings = self.services.settings.editable_config()
        settings["runtime"]["max_preview_streams"] = 1
        settings["runtime"]["max_preview_streams_per_user"] = 1
        self.services.settings.save(settings, self.admin.id)
        first_id = "1" * 32
        second_id = "2" * 32
        client = self.user_client()

        with ExitStack() as stack:
            first = stack.enter_context(
                self.services.conversations.operations.workspace_operation(
                    workspace,
                    "reply",
                    "第一个预览",
                    operation_id=first_id,
                )
            )
            stack.enter_context(
                self.services.conversations.operations.workspace_operation(
                    other,
                    "reply",
                    "第二个预览",
                    operation_id=second_id,
                    enforce_chat_capacity=False,
                )
            )
            first.update_preview("第一条")
            response = client.get(
                f"/api/workspaces/{workspace.id}/operations/{first_id}/events",
                buffered=False,
            )
            next(iter(response.response))
            full = client.get(f"/api/workspaces/{other.id}/operations/{second_id}/events")
            self.assertEqual(full.status_code, 503)
            response.close()

            recovered = client.get(
                f"/api/workspaces/{other.id}/operations/{second_id}/events",
                buffered=False,
            )
            self.assertEqual(recovered.status_code, 200)
            recovered.close()

    def test_chat_is_serial_per_workspace_and_parallel_across_workspaces(self):
        workspace = self.create_workspace("串行工作站")
        other_workspace = self.create_workspace("并行工作站")
        client = BlockingFirstChatClient()
        conversations = self.services.conversations
        conversations.client = client
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
                except Exception as exc:  # pragma: no cover - 下方断言会检查
                    errors.append(exc)

        thread = threading.Thread(target=send_blocking_message)
        thread.start()
        try:
            self.assertTrue(client.started.wait(5))
            operation = conversations.operation_state(workspace.id)
            self.assertTrue(operation["busy"])
            self.assertEqual(operation["kind"], "reply")
            self.assertEqual(operation["stage"], "upstream_connected")
            self.assertEqual(operation["stage_label"], "已连接上游，等待模型首个输出")
            self.assertEqual(operation["request_body_bytes"], 1024)

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
            self.assertIn("并行回复 2", assistant_message.content)
            self.assertEqual(len(client.calls), 2)
        finally:
            client.release.set()
            thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(conversations.operation_state(workspace.id)["busy"])

    def test_chat_cancel_releases_operation_and_discards_late_reply(self):
        workspace = self.create_workspace("可取消对话")
        client = BlockingFirstChatClient()
        conversations = self.services.conversations
        conversations.client = client
        errors = []

        def send_blocking_message():
            with self.app.app_context():
                thread_workspace = db.session.get(Workspace, workspace.id)
                try:
                    conversations.send(
                        thread_workspace,
                        model_id="test-chat",
                        content="取消这条消息",
                        message_id="a" * 32,
                        operation_id="b" * 32,
                    )
                except Exception as exc:  # pragma: no cover - assertions below inspect it
                    errors.append(exc)

        thread = threading.Thread(target=send_blocking_message)
        thread.start()
        try:
            self.assertTrue(client.started.wait(5))
            self.assertTrue(conversations.operation_state(workspace.id)["busy"])
            response = self.user_client().post(
                f"/api/workspaces/{workspace.id}/operations/{'b' * 32}/cancel"
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(conversations.operation_state(workspace.id)["busy"])
            self.assertTrue(conversations.operation_state(workspace.id)["cancel_requested"])
        finally:
            client.release.set()
            thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(getattr(errors[0], "code", ""), "conversation_canceled")
        self.assertFalse(conversations.operation_state(workspace.id)["busy"])
        messages = (
            self.user_client().get(f"/api/workspaces/{workspace.id}/messages").json["messages"]
        )
        self.assertEqual([message["role"] for message in messages], ["user"])
        _user_message, assistant_message = conversations.send(
            workspace,
            model_id="test-chat",
            content="取消这条消息",
            message_id="a" * 32,
            operation_id="c" * 32,
        )
        self.assertEqual(assistant_message.role, "assistant")

    def test_canceled_chat_keeps_counting_toward_capacity_until_request_returns(self):
        workspaces = [self.create_workspace(f"取消容量 {index}") for index in range(3)]
        conversations = self.services.conversations

        with ExitStack() as active_requests:
            for index, workspace in enumerate(workspaces[:2]):
                operation_id = str(index + 1) * 32
                active_requests.enter_context(
                    conversations.operations.workspace_operation(
                        workspace,
                        "reply",
                        "等待回复",
                        operation_id=operation_id,
                    )
                )
                self.assertTrue(conversations.cancel_operation(workspace.id, operation_id))
                self.assertTrue(conversations.operation_state(workspace.id)["busy"])

            with self.assertRaises(ServiceError) as raised:
                with conversations.operations.workspace_operation(
                    workspaces[2],
                    "reply",
                    "等待回复",
                    operation_id="3" * 32,
                ):
                    pass

        self.assertEqual(raised.exception.code, "conversation_user_limit")
        self.assertEqual(raised.exception.status_code, 429)

    def test_chat_cancel_tombstones_are_consumed_at_zero_timestamp(self):
        workspace = self.create_workspace("零时间取消")
        conversations = self.services.conversations

        with patch("imagegen.services.conversations.operations.monotonic", return_value=0.0):
            self.assertFalse(conversations.cancel_operation(workspace.id, "d" * 32))
            self.assertFalse(conversations.cancel_operation(workspace.id, "e" * 32))
            with self.assertRaises(ServiceError) as raised:
                with conversations.operations.workspace_operation(
                    workspace,
                    "reply",
                    "等待回复",
                    operation_id="d" * 32,
                    message_id="e" * 32,
                ):
                    pass
            with conversations.operations.workspace_operation(
                workspace,
                "reply",
                "再次等待回复",
                operation_id="e" * 32,
            ):
                pass

        self.assertEqual(raised.exception.code, "conversation_canceled")

    def test_chat_progress_stage_does_not_regress(self):
        workspace = self.create_workspace("进度阶段单调")
        conversations = self.services.conversations

        with conversations.operations.workspace_operation(
            workspace, "reply", "等待回复"
        ) as operation:
            operation.update_progress("output", "模型正在输出", output_characters=12)
            operation.update_progress("reasoning", "模型正在分析需求", output_characters=4)
            state = conversations.operation_state(workspace.id)

        self.assertEqual(state["stage"], "output")
        self.assertEqual(state["output_characters"], 12)

    def test_generation_api_holds_workspace_operation_while_submitting(self):
        workspace = self.create_workspace("生成互斥工作站")
        draft = self.create_ready_prompt_draft(workspace, prompt="原子互斥测试")
        conversations = self.services.conversations
        generation_service = self.services.generations
        original_submit = generation_service.submit
        observed_operations = []

        def observed_submit(*args, **kwargs):
            observed_operations.append(conversations.operation_state(workspace.id))
            return original_submit(*args, **kwargs)

        generation_service.submit = observed_submit
        try:
            response = self.user_client().post(
                "/api/generations",
                json={
                    "workspace_id": workspace.id,
                    "channel_id": "test",
                    "model": "model-b",
                    "prompt": "原子互斥测试",
                    "prompt_draft_id": draft.id,
                },
            )
        finally:
            generation_service.submit = original_submit

        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        self.assertEqual(observed_operations[0]["kind"], "generation_submission")
        self.assertFalse(conversations.operation_state(workspace.id)["busy"])

    def test_generation_submission_rejects_an_active_conversation(self):
        workspace = self.create_workspace("对话优先工作站")
        conversations = self.services.conversations

        with conversations._workspace_operation(workspace, "reply", "正在等待 AI 回复"):
            with self.assertRaises(ServiceError) as raised:
                with conversations.generation_submission(workspace):
                    pass

        self.assertEqual(raised.exception.code, "conversation_busy")
        self.assertEqual(raised.exception.status_code, 409)

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
