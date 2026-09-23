from __future__ import annotations

import io

from PIL import Image

from tests.support.platform import (
    FakeProviderFactory,
    PlatformTestCase,
    png_bytes,
)


class TestMedia(PlatformTestCase):
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
        worker = self.create_worker()
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

            saved_to_library = client.post(
                "/api/library-images",
                json={"generation_item_id": item["id"]},
            )
            self.assertEqual(saved_to_library.status_code, 201)
            library_image = saved_to_library.json["images"][0]
            self.assertEqual(client.get(library_image["url"]).data, png_bytes())

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

            reused_again = client.post(f"/api/generation-items/{item['id']}/reference")
            self.assertEqual(reused_again.status_code, 200)
            self.assertEqual(reused_again.json["asset"]["id"], reused_asset["id"])
        finally:
            self.context.push()
