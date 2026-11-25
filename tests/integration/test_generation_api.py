from __future__ import annotations

import re
import unittest
from decimal import Decimal
from http.client import HTTPResponse
from io import BytesIO
from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4
from zipfile import ZipFile

from imagegen.extensions import db
from imagegen.models import User
from tests.support.platform import FakeProviderFactory, PlatformTestCase, png_bytes


class TestGenerationApi(PlatformTestCase):
    def test_self_service_requires_session_and_csrf_and_revokes_old_keys(self):
        workspace = self.create_workspace()
        external = self.app.test_client()
        key_url = "/account/generation-api/key"
        body = {"workspace_id": workspace.id}
        with self.app.app_context():
            anonymous = external.post(key_url, json=body)
            self.assertEqual(anonymous.status_code, 401)
            self.assertEqual(anonymous.json["code"], "login_required")

        client = self.user_client()
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(client.post(key_url, json=body).json["code"], "csrf_error")
        page = client.get("/account/generation-api").get_data(as_text=True)
        csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        csrf_headers = {"X-CSRFToken": csrf_token}
        foreign_workspace = self.services.workspaces.create(self.admin.id, "管理工作站")
        self.assertEqual(
            client.post(
                key_url, headers=csrf_headers, json={"workspace_id": foreign_workspace.id}
            ).status_code,
            404,
        )
        issued = client.post(key_url, json=body, headers=csrf_headers)
        self.assertEqual(issued.status_code, 200)
        self.assertEqual(issued.headers["Cache-Control"], "no-store")
        token = issued.json["api_key"]
        self.assertNotIn(token, client.get("/account/generation-api").get_data(as_text=True))
        self.assertNotEqual(self.services.generation_api.get_key(self.user.id).token_digest, token)

        config_url = "/api/v1/config"
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(client.get(config_url).status_code, 401)
        with self.app.app_context():
            self.assertEqual(external.get("/api/channels", headers=headers).status_code, 302)
        config = external.get(config_url, headers=headers)
        self.assertEqual(config.status_code, 200)
        self.assertEqual(config.json["workspace"]["id"], workspace.id)
        self.assertEqual(config.json["channels"][0]["price_rmb"], "1.2500")
        self.assertNotIn("test-key-not-secret", config.get_data(as_text=True))
        self.assertNotIn("relay.example", config.get_data(as_text=True))

        rotated = client.post(key_url, json=body, headers=csrf_headers).json["api_key"]
        self.assertEqual(external.get(config_url, headers=headers).status_code, 401)
        rotated_headers = {"Authorization": f"Bearer {rotated}"}
        self.assertEqual(external.get(config_url, headers=rotated_headers).status_code, 200)
        self.services.users.update_status(self.user.id, "disabled", self.admin.id)
        self.assertEqual(external.get(config_url, headers=rotated_headers).status_code, 401)
        with self.app.app_context():
            disabled = client.delete(key_url, headers=csrf_headers)
            self.assertEqual(disabled.status_code, 401)
            self.assertEqual(disabled.json["code"], "login_required")
        self.services.users.update_status(self.user.id, "active", self.admin.id)
        self.assertEqual(client.delete(key_url, headers=csrf_headers).status_code, 204)
        denied = external.get(config_url, headers=rotated_headers)
        self.assertEqual(denied.status_code, 401)
        self.assertTrue(denied.headers["WWW-Authenticate"].startswith("Bearer"))

        bundle = client.get("/account/generation-api/skill")
        with ZipFile(BytesIO(bundle.data)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "snow-image-api/SKILL.md",
                    "snow-image-api/references/api.md",
                    "snow-image-api/references/request.json",
                    "snow-image-api/scripts/generate.py",
                },
            )

    def test_bearer_submission_replays_once_and_downloads_the_worker_result(self):
        workspace = self.create_workspace()
        original_settings = dict(workspace.settings)
        account = self.user_client()
        external = self.app.test_client()
        token = account.post(
            "/account/generation-api/key", json={"workspace_id": workspace.id}
        ).json["api_key"]
        self.app.config["WTF_CSRF_ENABLED"] = True
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid4())}
        url = "/api/v1/generations"
        payload = {"channel_id": "test", "prompt": "雨后的蓝色街道"}
        submitted = external.post(url, headers=headers, json=payload)
        self.assertEqual(submitted.status_code, 202, submitted.get_json())
        queued = submitted.json["job"]
        self.assertEqual(queued["status"], "queued")
        self.assertNotIn("prompt", queued)
        self.assertEqual(queued["reserved_rmb"], "1.2500")
        self.assertEqual(workspace.settings, original_settings)
        status_url = submitted.headers["Location"]
        self.assertEqual(status_url, f"{url}/{queued['id']}")

        repeated = external.post(url, headers=headers, json=payload)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json["job"]["id"], queued["id"])
        changed = external.post(url, headers=headers, json={**payload, "batch_count": 2})
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json["code"], "idempotency_conflict")
        invalid_payload = external.post(url, headers=headers, json={**payload, "batch_count": True})
        self.assertEqual(invalid_payload.status_code, 400)
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("1.2500"))

        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        item_id = queued["items"][0]["id"]
        self.assertTrue(worker._claim(item_id, channel))
        worker._process_item(item_id)
        db.session.expire_all()

        succeeded = external.get(status_url, headers=headers).json["job"]
        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(succeeded["charged_rmb"], "1.2500")
        self.assertEqual(succeeded["reserved_rmb"], "0.0000")
        self.assertEqual(db.session.get(User, self.user.id).balance_rmb, Decimal("18.7500"))
        image_url = succeeded["items"][0]["download_url"]
        self.assertEqual(external.get(image_url).status_code, 401)
        with external.get(image_url, headers=headers) as downloaded:
            self.assertEqual(downloaded.status_code, 200)
            self.assertEqual(downloaded.mimetype, "image/png")
            self.assertEqual(downloaded.data, png_bytes())
        with patch("imagegen.web.generation_api.send_file", side_effect=FileNotFoundError):
            self.assertEqual(
                external.get(image_url, headers=headers).json["code"], "image_not_found"
            )
        self.assertEqual(account.get(f"/api/generations/{queued['id']}").status_code, 200)

        ordinary_job = self.submit(workspace)
        self.assertEqual(external.get(f"{url}/{ordinary_job.id}", headers=headers).status_code, 404)
        other_workspace = self.services.workspaces.create(self.admin.id, "另一账号的工作站")
        other_token = self.services.generation_api.issue_key(self.admin.id, other_workspace.id)
        other_headers = {"Authorization": f"Bearer {other_token}"}
        self.assertEqual(external.get(status_url, headers=other_headers).status_code, 404)
        self.assertEqual(external.get(image_url, headers=other_headers).status_code, 404)

    def test_rebinding_key_does_not_submit_the_same_request_twice(self):
        first = self.create_workspace()
        second = self.create_workspace("另一结果工作站")
        key_url = "/account/generation-api/key"
        submit_url = "/api/v1/generations"
        account = self.user_client()
        external = self.app.test_client()
        body = {"channel_id": "test", "prompt": "雨后的蓝色街道"}
        request_id = str(uuid4())

        token = account.post(key_url, json={"workspace_id": first.id}).json["api_key"]
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": request_id}
        original = external.post(submit_url, headers=headers, json=body)
        self.assertEqual(original.status_code, 202)

        rotated = account.post(key_url, json={"workspace_id": second.id}).json["api_key"]
        headers["Authorization"] = f"Bearer {rotated}"
        self.assertEqual(
            external.get(original.headers["Location"], headers=headers).status_code, 404
        )
        replay = external.post(submit_url, headers=headers, json=body)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json["code"], "idempotency_conflict")
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("1.2500"))

    def test_invalid_submission_does_not_echo_text_and_rejects_oversized_json(self):
        workspace = self.create_workspace()
        token = self.services.generation_api.issue_key(self.user.id, workspace.id)
        client = self.app.test_client()
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid4())}
        submit_url = "/api/v1/generations"
        for field in ("channel_id", "model", "output_format"):
            with self.subTest(field=field):
                payload = {"channel_id": "test", "prompt": "测试图片", field: "private-input"}
                response = client.post(submit_url, headers=headers, json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("private-input", response.get_data(as_text=True))
        oversized = client.post(
            submit_url, headers=headers, json={"channel_id": "test", "prompt": "a" * (256 * 1024)}
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized.json["code"], "payload_too_large")

        with (
            patch.object(
                self.services.generation_api, "submit", side_effect=RuntimeError("private-input")
            ),
            self.assertLogs("imagegen.app", level="ERROR") as logs,
        ):
            response = client.post(
                submit_url, headers=headers, json={"channel_id": "test", "prompt": "测试图片"}
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["code"], "internal_error")
        self.assertNotIn("private-input", "\n".join(logs.output))


class TestGenerationApiSkill(unittest.TestCase):
    def test_skill_replaces_the_image_only_after_a_complete_download(self):
        script = (
            Path(__file__).resolve().parents[2]
            / "imagegen/skills/snow-image-api/scripts/generate.py"
        )
        save_image = run_path(str(script))["save_image"]
        full_image = png_bytes()
        headers = (
            f"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
            f"Content-Length: {len(full_image)}\r\n\r\n"
        ).encode("ascii")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"complete older file")
            partial_socket = Mock(makefile=Mock(return_value=BytesIO(headers + full_image[:24])))
            with HTTPResponse(partial_socket) as partial:
                partial.begin()
                with self.assertRaisesRegex(OSError, "Incomplete image download"):
                    save_image(partial, path, "image/png")
            self.assertEqual(path.read_bytes(), b"complete older file")
            self.assertEqual(list(path.parent.iterdir()), [path])

            full_socket = Mock(makefile=Mock(return_value=BytesIO(headers + full_image)))
            with HTTPResponse(full_socket) as complete:
                complete.begin()
                save_image(complete, path, "image/png")
            self.assertEqual(path.read_bytes(), full_image)
            self.assertEqual(list(path.parent.iterdir()), [path])
