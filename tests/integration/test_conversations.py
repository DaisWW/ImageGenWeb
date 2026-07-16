from __future__ import annotations

import base64
import io
import threading
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import patch

from PIL import Image
from sqlalchemy import func, select

from imagegen.extensions import db
from imagegen.models import (
    LibraryImage,
    Workspace,
    utcnow,
)
from imagegen.services import ServiceError
from imagegen.storage import InvalidImageError
from tests.support.platform import (
    BlockingFirstChatClient,
    PlatformTestCase,
    png_bytes,
    png_bytes_with_dimensions,
)


class TestConversations(PlatformTestCase):
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

    def test_generation_api_holds_workspace_operation_while_submitting(self):
        workspace = self.create_workspace("生成互斥工作站")
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
        self.assertEqual(draft.payload["generation_mode"], "img2img")
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

    def test_image_library_deduplicates_and_copies_across_workspaces(self):
        source = self.create_workspace("图库来源")
        target = self.create_workspace("图库目标")
        content = png_bytes((220, 35, 45))
        source_asset = self.services.workspaces.add_assets(
            source,
            [("source.png", content)],
        )[0]
        client = self.user_client()

        saved = client.post("/api/library-images", json={"asset_id": source_asset.id})
        self.assertEqual(saved.status_code, 201, saved.get_data(as_text=True))
        image = saved.json["images"][0]
        self.assertEqual(saved.json["added_count"], 1)
        self.assertEqual(client.get(image["url"]).data, content)

        duplicate = client.post(
            "/api/library-images",
            data={"images": (io.BytesIO(content), "duplicate.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json["added_count"], 0)
        self.assertEqual(duplicate.json["images"][0]["id"], image["id"])

        imported = client.post(f"/api/workspaces/{target.id}/assets/from-library/{image['id']}")
        self.assertEqual(imported.status_code, 201, imported.get_data(as_text=True))
        imported_asset = imported.json["asset"]
        self.assertNotEqual(imported_asset["id"], source_asset.id)
        self.assertEqual(client.get(imported_asset["url"]).data, content)
        imported_again = client.post(
            f"/api/workspaces/{target.id}/assets/from-library/{image['id']}"
        )
        self.assertEqual(imported_again.status_code, 200)
        self.assertEqual(imported_again.json["asset"]["id"], imported_asset["id"])

        self.assertEqual(client.delete(f"/api/workspaces/{source.id}").status_code, 200)
        self.assertEqual(client.get(image["url"]).data, content)
        self.assertEqual(client.delete(f"/api/library-images/{image['id']}").status_code, 200)
        self.assertEqual(client.get(imported_asset["url"]).data, content)
        self.assertEqual(client.get("/api/library-images").json["images"], [])
        self.assertEqual(db.session.scalar(select(func.count(LibraryImage.id))), 0)

    def test_image_library_concurrent_duplicate_uploads_create_one_record(self):
        content = png_bytes((170, 55, 90))
        barrier = threading.Barrier(2)
        storage = self.app.extensions["image_storage"]
        inspect_static = storage.inspect_static
        responses = []
        failures = []

        def synchronized_inspect(payload):
            inspected = inspect_static(payload)
            barrier.wait(timeout=10)
            return inspected

        def upload(name):
            try:
                client = self.app.test_client()
                login = client.post(
                    "/login",
                    data={"username": "artist", "password": "StrongPass123!"},
                )
                if login.status_code != 302:
                    raise AssertionError(f"concurrent login failed: {login.status_code}")
                responses.append(
                    client.post(
                        "/api/library-images",
                        data={"images": (io.BytesIO(content), name)},
                        content_type="multipart/form-data",
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        with patch.object(storage, "inspect_static", side_effect=synchronized_inspect) as inspect:
            threads = [
                threading.Thread(target=upload, args=(f"duplicate-{index}.png",))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertCountEqual([response.status_code for response in responses], [200, 201])
        self.assertEqual(len({response.json["images"][0]["id"] for response in responses}), 1)
        self.assertCountEqual([response.json["added_count"] for response in responses], [0, 1])
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(db.session.scalar(select(func.count(LibraryImage.id))), 1)

    def test_image_library_inspects_new_upload_once(self):
        storage = self.app.extensions["image_storage"]
        with patch.object(storage, "inspect_static", wraps=storage.inspect_static) as inspect:
            response = self.user_client().post(
                "/api/library-images",
                data={"images": (io.BytesIO(png_bytes()), "once.png")},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(inspect.call_count, 1)

    def test_image_library_enforces_count_and_byte_quotas(self):
        client = self.user_client()
        first_content = png_bytes((15, 75, 135))
        second_content = png_bytes((135, 75, 15))

        with patch("imagegen.services.image_library.MAX_LIBRARY_IMAGES", 1):
            first = client.post(
                "/api/library-images",
                data={"images": (io.BytesIO(first_content), "first.png")},
                content_type="multipart/form-data",
            )
            over_count = client.post(
                "/api/library-images",
                data={"images": (io.BytesIO(second_content), "second.png")},
                content_type="multipart/form-data",
            )
            duplicate = client.post(
                "/api/library-images",
                data={"images": (io.BytesIO(first_content), "duplicate.png")},
                content_type="multipart/form-data",
            )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(over_count.status_code, 409)
        self.assertEqual(over_count.json["code"], "library_quota")
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json["added_count"], 0)

        with patch(
            "imagegen.services.image_library.MAX_LIBRARY_BYTES",
            len(first_content) + len(second_content) - 1,
        ):
            over_bytes = client.post(
                "/api/library-images",
                data={"images": (io.BytesIO(second_content), "second.png")},
                content_type="multipart/form-data",
            )

        self.assertEqual(over_bytes.status_code, 409)
        self.assertEqual(over_bytes.json["code"], "library_quota")
        self.assertEqual(db.session.scalar(select(func.count(LibraryImage.id))), 1)

    def test_image_library_list_is_paginated(self):
        contents = [png_bytes((index * 45, 80, 160)) for index in range(1, 4)]
        images, added_count = self.services.image_library.add(
            self.user.id,
            [(f"page-{index}.png", content) for index, content in enumerate(contents)],
        )
        client = self.user_client()

        first = client.get("/api/library-images?offset=0&limit=2")
        second = client.get("/api/library-images?offset=2&limit=2")

        self.assertEqual(added_count, 3)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["total"], 3)
        self.assertTrue(first.json["has_more"])
        self.assertEqual(len(first.json["images"]), 2)
        self.assertFalse(second.json["has_more"])
        self.assertEqual(len(second.json["images"]), 1)
        listed = first.json["images"] + second.json["images"]
        self.assertEqual({entry["id"] for entry in listed}, {image.id for image in images})
        self.assertTrue(all(entry["thumbnail_url"].endswith("/thumbnail") for entry in listed))
        self.assertTrue({"mime_type", "bytes", "width", "height", "created_at"}.issubset(listed[0]))
        self.assertEqual(client.get("/api/library-images?offset=-1").status_code, 400)
        self.assertEqual(client.get("/api/library-images?limit=0").status_code, 400)

    def test_image_library_rejects_animations_and_is_account_private(self):
        output = io.BytesIO()
        frames = [Image.new("RGB", (2, 2), color) for color in ("red", "blue")]
        frames[0].save(
            output,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=100,
        )
        client = self.user_client()
        rejected = client.post(
            "/api/library-images",
            data={"images": (io.BytesIO(output.getvalue()), "animation.webp")},
            content_type="multipart/form-data",
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("仅支持静态图片", rejected.json["error"])

        stored = client.post(
            "/api/library-images",
            data={"images": (io.BytesIO(png_bytes()), "private.png")},
            content_type="multipart/form-data",
        ).json["images"][0]
        stored_url = stored["url"]
        thumbnail_url = stored["thumbnail_url"]
        thumbnail = client.get(thumbnail_url)
        self.assertEqual(thumbnail.status_code, 200)
        self.assertEqual(thumbnail.mimetype, "image/webp")
        with Image.open(io.BytesIO(thumbnail.data)) as preview:
            self.assertEqual(preview.format, "WEBP")
        db.session.get(LibraryImage, stored["id"]).thumbnail_path = None
        db.session.commit()
        legacy_thumbnail = client.get(thumbnail_url)
        self.assertEqual(legacy_thumbnail.status_code, 200)
        self.assertEqual(legacy_thumbnail.mimetype, "image/png")
        self.assertEqual(legacy_thumbnail.data, png_bytes())
        outsider = self.services.users.create(
            username="library-outsider",
            password="StrongPass123!",
            actor_user_id=self.admin.id,
        )
        self.context.pop()
        try:
            outsider_client = self.app.test_client()
            login = outsider_client.post(
                "/login",
                data={"username": outsider.username, "password": "StrongPass123!"},
            )
            self.assertEqual(login.status_code, 302)
            self.assertEqual(outsider_client.get(stored_url).status_code, 404)
            self.assertEqual(outsider_client.get(thumbnail_url).status_code, 404)
            self.assertEqual(outsider_client.get("/api/library-images").json["images"], [])
        finally:
            self.context.push()
