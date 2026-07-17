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
from decimal import Decimal
from pathlib import Path

from PIL import Image

from imagegen import create_app
from imagegen.extensions import db
from imagegen.integrations.images import (
    ProviderResult,
)
from imagegen.integrations.openai_chat import ChatCompletion, OpenAIChatError
from imagegen.models import (
    RuntimeLog,
)
from imagegen.services import SubmitGeneration
from imagegen.worker import GenerationWorker


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
      sizes: [1024x1024]
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


class FakeDownloadResponse:
    def __init__(self, *, body: bytes = b"image", status_code: int = 200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, _chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class RecordingDownloadSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return self.responses.pop(0)


class FakeChatClient:
    def __init__(self):
        self.calls = []
        self.reply_content = ""
        self.prompt_draft_content = ""
        self.image_review_content = ""

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
        if "工作站生成一个简短、具体的标题" in system:
            content = "红发蓝眼中年男性角色"
        elif "生产图片验收员" in system:
            content = self.image_review_content or json.dumps(
                {
                    "verdict": "pass",
                    "hard_checks": [
                        {
                            "id": "instruction_following",
                            "label": "整体指令遵循",
                            "passed": True,
                            "evidence": "结果符合整体要求",
                        },
                        *(
                            {
                                "id": f"criterion_{index}",
                                "label": f"硬门槛 {index}",
                                "passed": True,
                                "evidence": "图片中可见",
                            }
                            for index in range(1, 7)
                        ),
                    ],
                    "scores": {"composition": 4.5, "visual_quality": 4.3, "usability": 4.4},
                    "findings": [],
                    "suggested_edit": "",
                },
                ensure_ascii=False,
            )
        elif "对话行为规则如下" in system:
            content = self.reply_content or json.dumps(
                {
                    "status": "needs_clarification",
                    "questions": ["请确认人物所处的场景。"],
                    "creative_direction": "other",
                },
                ensure_ascii=False,
            )
        elif "只输出一个 JSON 对象" in system:
            content = self.prompt_draft_content or (
                '{"status":"ready","summary_zh":"一位人物肖像",'
                '"prompt":"cinematic portrait","creative_direction":"other",'
                '"template_id":"custom","style_tags":[],"scene_tags":[],'
                '"selection_reason":"使用通用 Craft 整理清晰的人物肖像需求。",'
                '"brief":{"deliverable":"人物肖像","subject":"人物"},'
                '"hard_checks":["只出现一位主体","主体清晰可见"],'
                '"quality_hint":"low"}'
            )
        else:
            content = self.reply_content or "测试回复"
        return ChatCompletion(
            content=content,
            request_id="chat-request-test",
            input_tokens=18,
            output_tokens=12,
            elapsed_seconds=1.234,
        )


class FailingOnceChatClient(FakeChatClient):
    def complete(self, model, *, system, messages, max_output_tokens=None):
        result = super().complete(
            model,
            system=system,
            messages=messages,
            max_output_tokens=max_output_tokens,
        )
        if len(self.calls) == 1:
            raise OpenAIChatError(
                "测试聊天模型暂时不可用",
                code="chat_test_failure",
                status_code=502,
                request_id="chat-failure-test",
                elapsed_seconds=0.25,
            )
        return result


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
            content=json.dumps(
                {
                    "status": "needs_clarification",
                    "questions": [f"并行回复 {call_number}"],
                    "creative_direction": "other",
                },
                ensure_ascii=False,
            ),
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


class UnrecognizedChatResponse:
    ok = True
    status_code = 200
    headers = {"x-request-id": "chat-shape-test", "content-type": "application/json"}
    content = b'{"output": [{"type": "message"}]}'

    @staticmethod
    def json():
        return {
            "id": "chat-shape-test",
            "output": [{"type": "message", "content": "must-not-be-logged"}],
            "authorization": "Bearer must-not-be-logged",
        }


class UnrecognizedChatSession:
    def post(self, _url, **_kwargs):
        return UnrecognizedChatResponse()


class HoldingExecutor:
    def submit(self, _function, *_args):
        return Future()


class PlatformTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.channel_path = root / "channels.yaml"
        self.channel_path.write_text(CHANNEL_CONFIG, encoding="utf-8")
        self.chat_path = root / "chat_models.yaml"
        self.chat_path.write_text(CHAT_CONFIG, encoding="utf-8")
        os.environ["TEST_IMAGE_KEY"] = "test-key-not-secret"
        os.environ["TEST_CHAT_KEY"] = "test-chat-key-not-secret"
        database_url = os.environ.get("TEST_DATABASE_URL", "").strip()
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "SQLALCHEMY_DATABASE_URI": database_url
                or f"sqlite:///{(root / 'test.db').as_posix()}",
                "CHANNEL_CONFIG_PATH": str(self.channel_path),
                "CHAT_MODEL_CONFIG_PATH": str(self.chat_path),
                "IMAGE_STORAGE_PATH": str(root / "files"),
                "WTF_CSRF_ENABLED": False,
                "AUTO_CREATE_DB": True,
                "TRUST_PROXY_HEADERS": True,
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

    def create_worker(self):
        return GenerationWorker(
            self.app,
            self.app.extensions["channel_registry"],
            self.app.extensions["image_storage"],
        )

    def create_ready_prompt_draft(
        self,
        workspace,
        *,
        prompt="电影感人物肖像",
        mode="text2img",
        reference_ids=(),
        creative_direction_id="auto",
        template_id="custom",
        hard_checks=None,
    ):
        direction = creative_direction_id if creative_direction_id != "auto" else "other"
        self.services.conversations.send(
            workspace,
            model_id="test-chat",
            content=f"请生成：{prompt}",
        )
        self.chat_client.prompt_draft_content = json.dumps(
            {
                "status": "ready",
                "summary_zh": prompt,
                "prompt": prompt,
                "creative_direction": direction,
                "template_id": template_id,
                "style_tags": [],
                "scene_tags": [],
                "selection_reason": "按交付物和场景选择最接近的提示词结构。",
                "brief": {"deliverable": "测试图片", "subject": prompt},
                "hard_checks": hard_checks or ["主体符合提示词", "没有无关主体"],
                "quality_hint": "low",
            },
            ensure_ascii=False,
        )
        return self.services.conversations.create_prompt_draft(
            workspace,
            model_id="test-chat",
            translate_to_english=False,
            mode=mode,
            reference_ids=tuple(reference_ids),
            creative_direction_id=creative_direction_id,
        )

    def record_generation_durations(self, samples, *, model="model-b"):
        db.session.add_all(
            RuntimeLog(
                category="generation",
                event="generation.provider",
                status="success",
                provider_id="test",
                model=model,
                elapsed_seconds=Decimal(value),
            )
            for value in samples
        )
        db.session.commit()

    def submit(self, workspace, **overrides):
        values = {
            "channel_id": "test",
            "model": "model-b",
            "mode": "text2img",
            "prompt": "电影感人物肖像",
            "size": "1024x1024",
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
