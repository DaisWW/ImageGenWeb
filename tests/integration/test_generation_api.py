from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from decimal import Decimal
from http.client import HTTPResponse
from io import BytesIO
from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import Mock, patch
from uuid import uuid4
from zipfile import ZipFile

from sqlalchemy import event, select
from werkzeug.serving import make_server

from imagegen.extensions import db
from imagegen.models import GENERATION_API_WORKSPACE_KIND, GenerationJob, User, Workspace
from tests.support.platform import FakeProviderFactory, PlatformTestCase, png_bytes


class TestGenerationApi(PlatformTestCase):
    def test_self_service_requires_session_and_csrf_and_revokes_old_keys(self):
        external = self.app.test_client()
        key_url = "/account/generation-api/key"
        with self.app.app_context():
            anonymous = external.post(key_url)
            self.assertEqual(anonymous.status_code, 401)
            self.assertEqual(anonymous.json["code"], "login_required")

        client = self.user_client()
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(client.post(key_url).json["code"], "csrf_error")
        page = client.get("/account/generation-api").get_data(as_text=True)
        csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        csrf_headers = {"X-CSRFToken": csrf_token}
        issued = client.post(key_url, headers=csrf_headers)
        self.assertEqual(issued.status_code, 200)
        self.assertEqual(issued.headers["Cache-Control"], "no-store")
        token = issued.json["api_key"]
        self.assertNotIn(token, client.get("/account/generation-api").get_data(as_text=True))
        self.assertNotEqual(
            self.services.generation_api_keys.get_key(self.user.id).token_digest, token
        )

        config_url = "/api/v1/config"
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(client.get(config_url).status_code, 401)
        with self.app.app_context():
            self.assertEqual(external.get("/api/channels", headers=headers).status_code, 302)
        db.session.remove()
        config_queries = []

        def capture_config_queries(_connection, _cursor, statement, _parameters, _context, _many):
            query = " ".join(statement.lower().split())
            if query.startswith("select ") and any(
                f"from {table}" in query for table in ("generation_api_keys", "workspaces", "users")
            ):
                config_queries.append(query)

        event.listen(db.engine, "before_cursor_execute", capture_config_queries)
        try:
            config = external.get(config_url, headers=headers)
        finally:
            event.remove(db.engine, "before_cursor_execute", capture_config_queries)
        self.assertEqual(config.status_code, 200)
        self.assertEqual(len(config_queries), 1, config_queries)
        (authentication_query,) = config_queries
        self.assertIn("from generation_api_keys", authentication_query)
        self.assertNotIn("join workspaces", authentication_query)
        self.assertNotIn("password_hash", authentication_query)
        self.assertNotIn("workspace", config.json)
        self.assertEqual(config.json["available_balance_rmb"], "20.0000")
        self.assertEqual(config.json["channels"][0]["price_rmb"], "1.2500")
        self.assertNotIn("test-key-not-secret", config.get_data(as_text=True))
        self.assertNotIn("relay.example", config.get_data(as_text=True))

        rotated = client.post(key_url, headers=csrf_headers).json["api_key"]
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

    def test_configured_skill_download_checks_transport_csrf_and_key_owner(self):
        url = "/account/generation-api/skill"
        site = "https://studio.example.test"
        client = self.app.test_client()
        token = self.services.generation_api_keys.issue_key(self.user.id)
        other_token = self.services.generation_api_keys.issue_key(self.admin.id)
        self.assertEqual(client.post(url, base_url=site).status_code, 302)
        self.assertEqual(
            client.post(
                "/login",
                base_url=site,
                data={"username": self.user.username, "password": "StrongPass123!"},
            ).status_code,
            302,
        )
        self.assertEqual(
            client.post(
                url, base_url="http://studio.example.test", data={"api_key": token}
            ).status_code,
            400,
        )
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(client.post(url, base_url=site).json["code"], "csrf_error")
        page = client.get("/account/generation-api", base_url=site).get_data(as_text=True)
        csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        headers = {
            "X-CSRFToken": csrf_token,
            "Referer": f"{site}/account/generation-api",
        }
        self.assertEqual(
            client.post(
                url, base_url=site, headers=headers, data={"api_key": other_token}
            ).status_code,
            403,
        )
        bundle = client.post(url, base_url=site, headers=headers, data={"api_key": token})
        self.assertEqual(bundle.status_code, 200)
        self.assertTrue(bundle.cache_control.no_store)
        with ZipFile(BytesIO(bundle.data)) as archive:
            self.assertEqual(
                json.loads(archive.read("snow-image-api/connection.json")),
                {"base_url": site, "api_key": token},
            )
            self.assertNotIn(token.encode(), archive.read("snow-image-api/SKILL.md"))

    def test_downloaded_skill_connects_without_environment_variables(self):
        client = self.user_client()
        token = self.services.generation_api_keys.issue_key(self.user.id)
        with make_server("127.0.0.1", 0, self.app) as server:
            server_thread = Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                bundle = client.post(
                    "/account/generation-api/skill",
                    base_url=f"http://localhost:{server.server_port}",
                    data={"api_key": token},
                )
                self.assertEqual(bundle.status_code, 200)
                with TemporaryDirectory() as directory:
                    with ZipFile(BytesIO(bundle.data)) as archive:
                        archive.extractall(directory)
                    script = Path(directory) / "snow-image-api/scripts/generate.py"
                    env = os.environ.copy()
                    env.pop("SNOW_IMAGE_API_URL", None)
                    env.pop("SNOW_IMAGE_API_KEY", None)
                    env["NO_PROXY"] = "localhost"
                    result = subprocess.run(
                        [sys.executable, str(script), "config"],
                        cwd=directory,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)["available_balance_rmb"], "20.0000")
                    self.assertNotIn(token, result.stdout + result.stderr)
            finally:
                server.shutdown()
                server_thread.join()

    def test_bearer_submission_replays_once_and_downloads_the_worker_result(self):
        account = self.user_client()
        external = self.app.test_client()
        self.assertEqual(account.get("/api/workspaces").json["workspaces"], [])
        token = account.post("/account/generation-api/key").json["api_key"]
        self.app.config["WTF_CSRF_ENABLED"] = True
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid4())}
        url = "/api/v1/generations"
        payload = {"channel_id": "test", "prompt": "雨后的蓝色街道"}
        submitted = external.post(url, headers=headers, json=payload)
        self.assertEqual(submitted.status_code, 202, submitted.get_json())
        queued = submitted.json["job"]
        self.assertEqual(queued["status"], "queued")
        self.assertNotIn("prompt", queued)
        self.assertNotIn("workspace_id", queued)
        self.assertEqual(queued["reserved_rmb"], "1.2500")
        workspace = db.session.get(GenerationJob, queued["id"]).workspace
        self.assertEqual(workspace.kind, GENERATION_API_WORKSPACE_KIND)
        self.assertEqual(account.get("/api/workspaces").json["workspaces"], [])
        self.assertEqual(account.get("/api/generations/active").json["jobs"], [])
        self.assertEqual(account.get("/api/generations").json["jobs"], [])
        self.assertEqual(account.get(f"/api/generations/{queued['id']}").status_code, 404)
        csrf_page = account.get("/account/generation-api").get_data(as_text=True)
        csrf_token = re.search(r'name="csrf-token" content="([^"]+)"', csrf_page).group(1)
        csrf_headers = {"X-CSRFToken": csrf_token}
        self.assertEqual(
            account.post(
                f"/api/generations/{queued['id']}/cancel", headers=csrf_headers
            ).status_code,
            404,
        )
        self.assertEqual(
            account.post(
                f"/api/generations/{queued['id']}/retry", headers=csrf_headers
            ).status_code,
            404,
        )
        self.assertEqual(account.get(f"/api/workspaces/{workspace.id}/messages").status_code, 404)
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
        self.assertEqual(account.get(f"/media/outputs/{item_id}").status_code, 404)
        self.assertEqual(account.get(f"/media/thumbnails/{item_id}").status_code, 404)
        self.assertEqual(
            account.post(
                "/api/library-images",
                json={"generation_item_id": item_id},
                headers=csrf_headers,
            ).status_code,
            404,
        )
        self.assertEqual(external.get(image_url).status_code, 401)
        with external.get(image_url, headers=headers) as downloaded:
            self.assertEqual(downloaded.status_code, 200)
            self.assertEqual(downloaded.mimetype, "image/png")
            self.assertEqual(downloaded.data, png_bytes())
        with patch("imagegen.web.generation_api.send_file", side_effect=FileNotFoundError):
            self.assertEqual(
                external.get(image_url, headers=headers).json["code"], "image_not_found"
            )
        self.assertEqual(account.get(f"/api/generations/{queued['id']}").status_code, 404)

        ordinary_workspace = self.create_workspace()
        ordinary_job = self.submit(ordinary_workspace)
        self.assertEqual(external.get(f"{url}/{ordinary_job.id}", headers=headers).status_code, 404)
        other_token = self.services.generation_api_keys.issue_key(self.admin.id)
        other_headers = {"Authorization": f"Bearer {other_token}"}
        self.assertEqual(external.get(status_url, headers=other_headers).status_code, 404)
        self.assertEqual(external.get(image_url, headers=other_headers).status_code, 404)

    def test_rotated_key_can_replay_legacy_job_from_a_regular_workspace(self):
        legacy_workspace = self.create_workspace()
        key_url = "/account/generation-api/key"
        submit_url = "/api/v1/generations"
        account = self.user_client()
        external = self.app.test_client()
        body = {"channel_id": "test", "prompt": "雨后的蓝色街道"}
        request_id = str(uuid4())

        token = account.post(key_url).json["api_key"]
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": request_id}
        with patch(
            "imagegen.services.generation_api._get_or_create_api_workspace",
            return_value=legacy_workspace,
        ):
            original = external.post(submit_url, headers=headers, json=body)
        self.assertEqual(original.status_code, 202)

        rotated = account.post(key_url).json["api_key"]
        self.assertEqual(
            external.get(original.headers["Location"], headers=headers).status_code, 401
        )
        headers["Authorization"] = f"Bearer {rotated}"
        self.assertEqual(
            external.get(original.headers["Location"], headers=headers).status_code, 200
        )
        replay = external.post(submit_url, headers=headers, json=body)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["job"]["id"], original.json["job"]["id"])
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("1.2500"))

    def test_invalid_submission_does_not_echo_text_and_rejects_oversized_json(self):
        token = self.services.generation_api_keys.issue_key(self.user.id)
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

    def test_admin_can_filter_generation_history_by_api_archive(self):
        token = self.services.generation_api_keys.issue_key(self.user.id)
        submitted = self.app.test_client().post(
            "/api/v1/generations",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid4())},
            json={"channel_id": "test", "prompt": "管理端筛选测试"},
        )
        self.assertEqual(submitted.status_code, 202)
        job = db.session.get(GenerationJob, submitted.json["job"]["id"])

        admin = self.admin_client()
        filters = admin.get("/api/admin/generation-filters").json
        self.assertIn(job.workspace_id, {workspace["id"] for workspace in filters["workspaces"]})
        history = admin.get(f"/api/admin/generations?workspace_id={job.workspace_id}").json
        self.assertEqual([entry["id"] for entry in history["jobs"]], [job.id])

    def test_insufficient_balance_rolls_back_the_first_api_workspace(self):
        token = self.services.generation_api_keys.issue_key(self.user.id)
        response = self.app.test_client().post(
            "/api/v1/generations",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid4())},
            json={"channel_id": "test", "prompt": "测试图片", "batch_count": 20},
        )
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json["code"], "insufficient_balance")
        self.assertIsNone(
            db.session.scalar(select(Workspace.id).where(Workspace.user_id == self.user.id))
        )
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("0"))


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
