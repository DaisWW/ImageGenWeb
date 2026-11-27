from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from http.client import HTTPResponse
from io import BytesIO
from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import Mock, patch
from uuid import UUID, uuid4
from zipfile import ZipFile

import yaml
from sqlalchemy import event, func, select
from werkzeug.http import parse_options_header
from werkzeug.serving import make_server

from imagegen.extensions import db
from imagegen.models import (
    GENERATION_API_WORKSPACE_KIND,
    Asset,
    GenerationJob,
    User,
    Workspace,
    utcnow,
)
from imagegen.services import SubmitGeneration
from tests.support.platform import (
    FakeProviderFactory,
    PlatformTestCase,
    mask_png_bytes,
    png_bytes,
)


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
        key_page = client.get("/account/generation-api")
        self.assertEqual(key_page.headers["Cache-Control"], "private, no-store")
        self.assertIn(f'value="{token}"'.encode(), key_page.data)
        self.assertEqual(
            self.services.generation_api_keys.key_for_display(self.user.id), (True, token)
        )
        encrypted_token = self.services.generation_api_keys.get_key(self.user.id).token_encrypted
        self.assertTrue(encrypted_token.startswith("gAAAA"))
        self.assertNotIn(token, encrypted_token)
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
        self.assertNotIn("token_encrypted", authentication_query)
        self.assertNotIn("join workspaces", authentication_query)
        self.assertNotIn("password_hash", authentication_query)
        self.assertNotIn("workspace", config.json)
        self.assertEqual(config.json["available_balance_rmb"], "20.0000")
        self.assertEqual(config.json["channels"][0]["price_rmb"], "1.2500")
        self.assertIn("img2img", config.json["channels"][0]["capabilities"]["modes"])
        self.assertEqual(config.json["limits"]["max_mask_bytes"], 4 * 1024 * 1024)
        self.assertNotIn("test-key-not-secret", config.get_data(as_text=True))
        self.assertNotIn("relay.example", config.get_data(as_text=True))

        channels = yaml.safe_load(self.channel_path.read_text(encoding="utf-8"))
        edit_only = dict(channels["channels"][0])
        edit_only["id"] = "edit-only"
        edit_only["label"] = "垫图专用渠道"
        edit_only["capabilities"] = {
            **edit_only["capabilities"],
            "modes": ["img2img"],
        }
        channels["channels"].append(edit_only)
        self.channel_path.write_text(yaml.safe_dump(channels, allow_unicode=True), encoding="utf-8")
        self.app.extensions["channel_registry"].reload(force=True)
        channel_ids = {
            channel["id"] for channel in external.get(config_url, headers=headers).json["channels"]
        }
        self.assertIn("edit-only", channel_ids)

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
        self.assertTrue(bundle.cache_control.no_store)
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

    def test_api_key_is_only_rendered_for_its_account(self):
        token = self.services.generation_api_keys.issue_key(self.user.id)
        client = self.user_client()
        owner_page = client.get("/account/generation-api")
        client.post("/logout")
        self.assertEqual(
            client.post(
                "/login",
                data={"username": "admin", "password": "StrongPass123!"},
            ).status_code,
            302,
        )
        other_page = client.get("/account/generation-api")
        self.assertIn(f'value="{token}"'.encode(), owner_page.data)
        self.assertNotIn(token.encode(), other_page.data)

    def test_skill_download_embeds_the_current_account_key_on_http(self):
        url = "/account/generation-api/skill"
        site = "http://studio.example.test"
        client = self.app.test_client()
        token = self.services.generation_api_keys.issue_key(self.user.id)
        other_token = self.services.generation_api_keys.issue_key(self.admin.id)
        self.assertEqual(
            client.post(
                "/login",
                base_url=site,
                data={"username": self.user.username, "password": "StrongPass123!"},
            ).status_code,
            302,
        )
        bundle = client.get(url, base_url=site)
        self.assertEqual(bundle.status_code, 200)
        self.assertTrue(bundle.cache_control.no_store)
        _, disposition = parse_options_header(bundle.headers["Content-Disposition"])
        self.assertEqual(disposition["filename"], "snow-image-api-configured.zip")
        with ZipFile(BytesIO(bundle.data)) as archive:
            self.assertEqual(
                json.loads(archive.read("snow-image-api/connection.json")),
                {"base_url": site, "api_key": token},
            )
            self.assertNotIn(token.encode(), archive.read("snow-image-api/SKILL.md"))
            self.assertNotIn(other_token.encode(), archive.read("snow-image-api/connection.json"))

        with patch.object(self.services.generation_api_keys._cipher, "decrypt", return_value=""):
            legacy_bundle = client.get(url, base_url=site)
        self.assertEqual(legacy_bundle.status_code, 200)
        _, disposition = parse_options_header(legacy_bundle.headers["Content-Disposition"])
        self.assertEqual(disposition["filename"], "snow-image-api.zip")
        with ZipFile(BytesIO(legacy_bundle.data)) as archive:
            self.assertNotIn("snow-image-api/connection.json", archive.namelist())

    def test_downloaded_skill_connects_without_environment_variables(self):
        client = self.user_client()
        token = self.services.generation_api_keys.issue_key(self.user.id)
        with make_server("127.0.0.1", 0, self.app) as server:
            server_thread = Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                bundle = client.get(
                    "/account/generation-api/skill",
                    base_url=f"http://localhost:{server.server_port}",
                )
                self.assertEqual(bundle.status_code, 200)
                self.assertTrue(bundle.cache_control.no_store)
                _, disposition = parse_options_header(bundle.headers["Content-Disposition"])
                self.assertEqual(disposition["filename"], "snow-image-api-configured.zip")
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

    def test_multipart_submission_supports_references_masks_and_retention(self):
        token = self.services.generation_api_keys.issue_key(self.user.id)
        client = self.app.test_client()
        request_id = str(uuid4())
        reference = png_bytes()
        mask = mask_png_bytes()
        payload = {"channel_id": "test", "prompt": "修改参考图中的帽子"}
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": request_id,
        }

        def submit(mask_content):
            return client.post(
                "/api/v1/generations",
                headers=headers,
                data={
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "references": (BytesIO(reference), "source.png", "image/png"),
                    "mask": (BytesIO(mask_content), "mask.png", "image/png"),
                },
            )

        submitted = submit(mask)
        self.assertEqual(submitted.status_code, 202, submitted.json)
        job_id = submitted.json["job"]["id"]
        job = db.session.get(GenerationJob, job_id)
        workspace_id = job.workspace_id
        asset_id = job.references[0].asset_id
        self.assertEqual(job.mode, "img2img")
        self.assertEqual(len(job.references), 1)
        self.assertEqual(job.mask_target_asset_id, asset_id)
        self.assertEqual(job.mask_width, 64)
        self.assertEqual(job.mask_height, 48)

        replay = submit(mask)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["job"]["id"], job_id)
        changed = submit(mask_png_bytes(box=(8, 6, 24, 18)))
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json["code"], "idempotency_conflict")
        self.assertEqual(db.session.scalar(select(func.count(Asset.id))), 1)

        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(job.items[0].id, channel))
        worker._process_item(job.items[0].id)
        provider_request = worker.providers.adapter.request
        self.assertEqual(provider_request.references[0].content, reference)
        self.assertIsNotNone(provider_request.mask)
        self.assertEqual(provider_request.mask.mime_type, "image/png")

        job.completed_at = utcnow() - timedelta(days=31)
        db.session.commit()
        self.assertEqual(worker.retention.cleanup(), {"jobs": 1, "assets": 1, "errors": 0})
        self.assertIsNone(db.session.get(GenerationJob, job_id))
        self.assertIsNone(db.session.get(Workspace, workspace_id))
        self.assertIsNone(db.session.get(Asset, asset_id))

    def test_multipart_submission_accepts_multiple_references_and_generation_options(self):
        token = self.services.generation_api_keys.issue_key(self.user.id)
        response = self.app.test_client().post(
            "/api/v1/generations",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": str(uuid4()),
            },
            data={
                "payload": json.dumps(
                    {
                        "channel_id": "test",
                        "prompt": "角色设计",
                        "size": "1024x1024",
                        "quality": "high",
                        "output_format": "png",
                        "compression": 85,
                        "batch_count": 2,
                        "transparent_background": True,
                        "moderation": "auto",
                        "generation_stage": "refine",
                        "generation_strategy": "explore",
                        "item_prompts": ["暖色光线", "冷色光线"],
                        "creative_direction_id": "character",
                    }
                ),
                "references": [
                    (BytesIO(png_bytes()), "subject.png", "image/png"),
                    (BytesIO(png_bytes((35, 120, 180))), "style.png", "image/png"),
                ],
            },
        )
        self.assertEqual(response.status_code, 202, response.json)
        job = db.session.get(GenerationJob, response.json["job"]["id"])
        self.assertEqual(job.mode, "img2img")
        self.assertEqual(len(job.references), 2)
        self.assertEqual(job.quality, "high")
        self.assertEqual(job.output_format, "png")
        self.assertEqual(job.compression, 85)
        self.assertTrue(job.transparent_background)
        self.assertEqual(job.workflow["generation_stage"], "refine")
        self.assertEqual(job.workflow["generation_strategy"], "explore")
        self.assertEqual([item.prompt for item in job.items], ["暖色光线", "冷色光线"])

    def test_api_rejects_masks_without_references_and_explore_masks(self):
        token = self.services.generation_api_keys.issue_key(self.user.id)
        client = self.app.test_client()
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid4()),
        }
        missing_reference = client.post(
            "/api/v1/generations",
            headers=headers,
            data={
                "payload": json.dumps({"channel_id": "test", "prompt": "局部重绘"}),
                "mask": (BytesIO(mask_png_bytes()), "mask.png", "image/png"),
            },
        )
        self.assertEqual(missing_reference.status_code, 422)
        self.assertEqual(missing_reference.json["code"], "mask_reference_count_invalid")

        explore_mask = client.post(
            "/api/v1/generations",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid4())},
            data={
                "payload": json.dumps(
                    {
                        "channel_id": "test",
                        "prompt": "局部重绘",
                        "batch_count": 2,
                        "generation_strategy": "explore",
                        "item_prompts": ["方案甲", "方案乙"],
                    }
                ),
                "references": (BytesIO(png_bytes()), "source.png", "image/png"),
                "mask": (BytesIO(mask_png_bytes()), "mask.png", "image/png"),
            },
        )
        self.assertEqual(explore_mask.status_code, 422)
        self.assertEqual(explore_mask.json["code"], "mask_strategy_invalid")
        self.assertIsNone(
            db.session.scalar(select(Workspace.id).where(Workspace.user_id == self.user.id))
        )

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
        legacy_request = SubmitGeneration(
            **body,
            model="",
            mode="text2img",
            size="auto",
            output_format="png",
            compression=90,
            batch_count=1,
            reference_ids=(),
        )
        original = self.services.generations.submit(
            self.user.id,
            legacy_workspace,
            legacy_request,
            api_request_id=UUID(request_id).hex,
            api_request_digest=hashlib.sha256(
                json.dumps(asdict(legacy_request), sort_keys=True).encode("ascii")
            ).hexdigest(),
        )
        status_url = f"{submit_url}/{original.id}"

        rotated = account.post(key_url).json["api_key"]
        self.assertEqual(external.get(status_url, headers=headers).status_code, 401)
        headers["Authorization"] = f"Bearer {rotated}"
        self.assertEqual(external.get(status_url, headers=headers).status_code, 200)
        replay = external.post(submit_url, headers=headers, json=body)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["job"]["id"], original.id)
        changed = external.post(
            submit_url, headers=headers, json={**body, "generation_stage": "draft"}
        )
        self.assertEqual(changed.status_code, 409)
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
            data={
                "payload": json.dumps(
                    {"channel_id": "test", "prompt": "测试图片", "batch_count": 20}
                ),
                "references": (BytesIO(png_bytes()), "source.png", "image/png"),
            },
        )
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json["code"], "insufficient_balance")
        self.assertIsNone(
            db.session.scalar(select(Workspace.id).where(Workspace.user_id == self.user.id))
        )
        self.assertEqual(db.session.scalar(select(func.count(Asset.id))), 0)
        storage_root = self.app.extensions["image_storage"].root
        self.assertEqual(
            list((storage_root / "users" / str(self.user.id) / "workspaces").glob("*")), []
        )
        self.assertEqual(db.session.get(User, self.user.id).reserved_rmb, Decimal("0"))


class TestGenerationApiSkill(unittest.TestCase):
    def test_skill_sends_reference_and_mask_in_one_request(self):
        script = (
            Path(__file__).resolve().parents[2]
            / "imagegen/skills/snow-image-api/scripts/generate.py"
        )
        module = run_path(str(script))
        with TemporaryDirectory() as directory:
            reference = Path(directory) / "source.png"
            mask = Path(directory) / "mask.png"
            reference.write_bytes(png_bytes())
            mask.write_bytes(mask_png_bytes())
            body, content_type = module["_multipart_generation"](
                json.dumps({"channel_id": "test", "prompt": "修改局部"}, ensure_ascii=False).encode(
                    "utf-8"
                ),
                [reference],
                mask,
            )

            class RecordingOpener:
                def __init__(self):
                    self.requests = []

                def open(self, request, timeout):
                    self.requests.append((request, timeout))
                    return BytesIO(b'{"accepted": true}')

            client = module["GenerationApiClient"]("https://studio.example.test", "test-key")
            opener = RecordingOpener()
            client._opener = opener
            self.assertEqual(
                client.request_json(
                    "generations",
                    body=body,
                    request_id=uuid4(),
                    content_type=content_type,
                ),
                {"accepted": True},
            )

        self.assertEqual(len(opener.requests), 1)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://studio.example.test/api/v1/generations")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertTrue(
            request.get_header("Content-type").startswith("multipart/form-data; boundary=")
        )
        self.assertIn(b'name="payload"', request.data)
        self.assertIn(b'name="references"; filename="reference_0.png"', request.data)
        self.assertIn(b'name="mask"; filename="mask.png"', request.data)
        self.assertIn(png_bytes(), request.data)
        self.assertIn(mask_png_bytes(), request.data)
        self.assertEqual(timeout, 30)

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
