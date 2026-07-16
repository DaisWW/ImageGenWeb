from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import call, patch

from sqlalchemy import event, func, select

from imagegen.config.repository import CHANNEL_CONFIG_KEY, CHAT_CONFIG_KEY
from imagegen.extensions import db
from imagegen.models import (
    AuditLog,
    GenerationJob,
    RuntimeLog,
    SystemState,
    WalletLedger,
    WorkerState,
    utcnow,
)
from imagegen.services import ServiceError, SubmitGeneration, SystemSettingsService
from imagegen.services.settings import SYSTEM_SETTINGS_KEY
from imagegen.storage import StorageError
from scripts.backup import copy_private_file, create_backup
from tests.support.platform import (
    FakeProviderFactory,
    PlatformTestCase,
)


class TestAdminAndMaintenance(PlatformTestCase):
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

    def test_admin_generation_history_filters_by_user_and_workspace(self):
        first_workspace = self.create_workspace("筛选工作站一")
        second_workspace = self.create_workspace("筛选工作站二")
        first_job = self.submit(first_workspace)
        second_job = self.submit(second_workspace)

        other = self.services.users.create(
            username="history-filter-user",
            password="StrongPass123!",
            balance_rmb="20",
            actor_user_id=self.admin.id,
        )
        other_workspace = self.services.workspaces.create(other.id, "其他用户工作站")
        other_job = self.services.generations.submit(
            other.id,
            other_workspace,
            SubmitGeneration(
                channel_id="test",
                model="model-b",
                mode="text2img",
                prompt="其他用户的生成记录",
                size="1024x1024",
                output_format="png",
                compression=90,
                batch_count=1,
                reference_ids=(),
            ),
        )

        client = self.admin_client()
        options = client.get("/api/admin/generation-filters").json
        self.assertIn(self.user.id, [user["id"] for user in options["users"]])
        self.assertIn(
            first_workspace.id,
            [workspace["id"] for workspace in options["workspaces"]],
        )

        all_jobs = client.get("/api/admin/generations?limit=10").json
        self.assertEqual(
            {job["id"] for job in all_jobs["jobs"]},
            {first_job.id, second_job.id, other_job.id},
        )
        self.assertEqual(all_jobs["queued_images"], 3)

        user_jobs = client.get(f"/api/admin/generations?user_id={self.user.id}&limit=10").json
        self.assertEqual(
            {job["id"] for job in user_jobs["jobs"]},
            {first_job.id, second_job.id},
        )
        self.assertEqual(user_jobs["queued_images"], 2)

        workspace_jobs = client.get(
            f"/api/admin/generations?workspace_id={second_workspace.id}&limit=10"
        ).json
        self.assertEqual([job["id"] for job in workspace_jobs["jobs"]], [second_job.id])
        self.assertEqual(workspace_jobs["queued_images"], 1)

        no_jobs = client.get(
            f"/api/admin/generations?user_id={self.user.id}&workspace_id={other_workspace.id}"
        ).json
        self.assertEqual(no_jobs["jobs"], [])
        self.assertEqual(no_jobs["queued_images"], 0)

    def test_title_is_admin_configurable_version_is_not_a_setting(self):
        settings = self.services.settings
        self.assertEqual(settings.site_title(), "西郊比克王 AI Studio")
        config = settings.editable_config()
        config["site_title"] = "设计图像中心"
        saved = settings.save(config, self.admin.id)
        self.assertEqual(saved["site_title"], "设计图像中心")
        audit = db.session.scalar(
            select(AuditLog).where(AuditLog.action == "system.settings.update")
        )
        self.assertIn("site_title", audit.details["changed"])
        worker_state = db.session.get(WorkerState, 1)
        worker_state.worker_id = "health-test-worker"
        worker_state.heartbeat_at = utcnow()
        db.session.commit()
        response = self.app.test_client().get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["title"], "设计图像中心")
        self.assertRegex(response.json["version"], r"^\d+\.\d+\.\d+")

    def test_runtime_and_title_reads_share_one_cached_snapshot(self):
        settings = SystemSettingsService()
        statements = []

        def capture_statement(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", capture_statement)
        try:
            first = settings.runtime()
            title = settings.site_title()
            second = settings.runtime()
        finally:
            event.remove(db.engine, "before_cursor_execute", capture_statement)

        self.assertIs(first, second)
        self.assertEqual(title, SystemSettingsService.DEFAULT_SITE_TITLE)
        self.assertEqual(len(statements), 1)

    def test_admin_can_filter_and_inspect_audit_logs(self):
        config = self.services.settings.editable_config()
        config["site_title"] = "审计日志测试站"
        self.services.settings.save(config, self.admin.id)
        self.context.pop()
        try:
            client = self.admin_client()
            listed = client.get(
                f"/api/admin/audit-logs?action=system.settings.update&actor_user_id={self.admin.id}"
            )

            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json["total"], 1)
            item = listed.json["logs"][0]
            self.assertEqual(item["action"], "system.settings.update")
            created_at = datetime.fromisoformat(item["created_at"])
            self.assertEqual(created_at.utcoffset(), timedelta(0))
            detail = client.get(f"/api/admin/audit-logs/{item['id']}")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("site_title", detail.json["log"]["details"]["changed"])
            self.assertEqual(self.user_client().get("/api/admin/audit-logs").status_code, 403)
        finally:
            self.context.push()

    def test_admin_system_settings_are_versioned_and_enforced_at_runtime(self):
        client = self.admin_client()
        initial = client.get("/api/admin/settings").json
        self.assertFalse(initial["managed"])
        self.assertEqual(initial["runtime"]["max_workspaces_per_user"], 10)
        initial["site_title"] = "运行参数测试站"
        initial["runtime"].update(
            {
                "max_workspaces_per_user": 2,
                "max_message_characters": 100,
                "max_batch_images": 2,
                "max_animation_frames": 3,
                "max_animation_fps": 12,
                "worker_poll_milliseconds": 900,
            }
        )

        response = client.put("/api/admin/settings", json=initial)

        self.assertEqual(response.status_code, 200)
        saved = response.json
        self.assertTrue(saved["managed"])
        self.assertTrue(saved["revision"])
        self.assertEqual(self.services.settings.runtime().max_batch_images, 2)
        self.assertIsNotNone(db.session.get(SystemState, SYSTEM_SETTINGS_KEY))
        audit = db.session.scalar(
            select(AuditLog).where(AuditLog.action == "system.settings.update")
        )
        self.assertIn("max_batch_images", audit.details["changed"])

        first = self.create_workspace("限制一")
        self.create_workspace("限制二")
        with self.assertRaisesRegex(ServiceError, "最多创建 2"):
            self.create_workspace("限制三")
        with self.assertRaisesRegex(ServiceError, "1 到 2"):
            self.submit(first, batch_count=3)
        with self.assertRaisesRegex(ServiceError, "不能超过 100"):
            self.services.conversations.send(
                first,
                model_id="test-chat",
                content="字" * 101,
            )

        public = self.user_client().get("/api/runtime-settings")
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.json["settings"]["max_batch_images"], 2)
        self.assertNotIn("worker_poll_milliseconds", public.json["settings"])

        stale = client.put("/api/admin/settings", json=initial)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["code"], "config_conflict")

    def test_admin_title_only_changes_participate_in_settings_revision(self):
        client = self.admin_client()
        initial = client.get("/api/admin/settings").json
        seeded = client.put("/api/admin/settings", json=initial)
        self.assertEqual(seeded.status_code, 200)
        baseline = seeded.json
        first_change = json.loads(json.dumps(baseline))
        second_change = json.loads(json.dumps(baseline))
        first_change["site_title"] = "第一位管理员的标题"
        second_change["site_title"] = "第二位管理员的标题"

        first = client.put("/api/admin/settings", json=first_change)
        second = client.put("/api/admin/settings", json=second_change)

        self.assertEqual(first.status_code, 200)
        self.assertNotEqual(first.json["revision"], baseline["revision"])
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json["code"], "config_conflict")
        self.assertEqual(self.services.settings.site_title(), "第一位管理员的标题")

    def test_admin_can_edit_existing_user_display_name_and_concurrency(self):
        client = self.admin_client()

        response = client.put(
            f"/api/admin/users/{self.user.id}",
            json={"display_name": "视觉设计", "generation_concurrency": 7},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["user"]["display_name"], "视觉设计")
        self.assertEqual(response.json["user"]["generation_concurrency"], 7)
        db.session.refresh(self.user)
        self.assertEqual(self.user.generation_concurrency, 7)
        audit = db.session.scalar(select(AuditLog).where(AuditLog.action == "user.profile.update"))
        self.assertEqual(audit.details["new"]["generation_concurrency"], 7)

        invalid = client.put(
            f"/api/admin/users/{self.user.id}",
            json={"display_name": "视觉设计", "generation_concurrency": 17},
        )
        self.assertEqual(invalid.status_code, 400)
        self.context.pop()
        try:
            forbidden = self.user_client().put(
                f"/api/admin/users/{self.user.id}",
                json={"display_name": "无权限", "generation_concurrency": 1},
            )
            self.assertEqual(forbidden.status_code, 403)
        finally:
            self.context.push()

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

    def test_invalid_admin_channel_config_returns_bad_request(self):
        client = self.admin_client()
        config = client.get("/api/admin/channels").json["config"]
        config["queue"]["global_concurrency"] = 0

        response = client.put("/api/admin/channels", json=config)

        self.assertEqual(response.status_code, 400)
        self.assertIn("global_concurrency", response.json["error"])

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

    def test_retention_keeps_metadata_when_file_deletion_fails_then_retries(self):
        workspace = self.create_workspace("清理重试")
        job = self.submit(workspace)
        job = self.services.generations.cancel(job.id, user_id=self.user.id)
        job.completed_at = utcnow() - timedelta(days=31)
        db.session.commit()
        storage = self.app.extensions["image_storage"]
        worker = self.create_worker()

        with patch.object(
            storage,
            "delete_job_directory",
            side_effect=StorageError("volume unavailable"),
        ):
            failed = worker.retention.cleanup()

        self.assertEqual(failed, {"jobs": 0, "assets": 0, "errors": 1})
        self.assertIsNotNone(db.session.get(GenerationJob, job.id))
        retried = worker.retention.cleanup()
        self.assertEqual(retried["jobs"], 1)
        self.assertIsNone(db.session.get(GenerationJob, job.id))

    def test_retention_keeps_metadata_when_database_commit_fails(self):
        workspace = self.create_workspace("数据库清理失败")
        job = self.submit(workspace)
        job = self.services.generations.cancel(job.id, user_id=self.user.id)
        job.completed_at = utcnow() - timedelta(days=31)
        db.session.commit()
        worker = self.create_worker()
        session = db.session()

        with patch.object(session, "commit", side_effect=RuntimeError("database unavailable")):
            result = worker.retention.cleanup()

        self.assertEqual(result["errors"], 1)
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(GenerationJob, job.id))

    def test_worker_periodic_cleanup_failure_does_not_escape(self):
        worker = self.create_worker()
        worker._last_cleanup = float("-inf")

        with patch.object(worker.retention, "cleanup", side_effect=RuntimeError("cleanup failed")):
            worker._run_periodic_cleanup()

        entry = db.session.scalar(
            select(RuntimeLog)
            .where(RuntimeLog.event == "worker.retention_cleanup")
            .order_by(RuntimeLog.created_at.desc())
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.status, "error")

    def test_worker_periodic_cleanup_settings_failure_does_not_escape(self):
        worker = self.create_worker()

        with patch.object(worker.settings, "runtime", side_effect=RuntimeError("settings failed")):
            worker._run_periodic_cleanup()

        entry = db.session.scalar(
            select(RuntimeLog)
            .where(RuntimeLog.event == "worker.retention_cleanup")
            .order_by(RuntimeLog.created_at.desc())
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.status, "error")

    def test_storage_wraps_recursive_delete_errors(self):
        storage = self.app.extensions["image_storage"]
        directory = storage.root / "users" / str(self.user.id) / "workspaces" / "locked"
        directory.mkdir(parents=True)

        with (
            patch("imagegen.storage.shutil.rmtree", side_effect=OSError("locked")),
            self.assertRaisesRegex(StorageError, "删除存储目录失败"),
        ):
            storage.delete_workspace(self.user.id, "locked")

    def test_backup_stops_writers_and_restores_only_previously_running_services(self):
        root = Path(self.temp.name) / "backup-test"
        env_file = Path(self.temp.name) / "backup.env"
        env_file.write_text("SECRET_KEY=test", encoding="utf-8")

        with (
            patch("scripts.backup.restrict_private_path"),
            patch("scripts.backup.copy_private_file"),
            patch(
                "scripts.backup.running_services",
                return_value={"db", "web", "worker"},
            ),
            patch("scripts.backup.docker_output", side_effect=[b"database", b"files"]) as output,
            patch("scripts.backup.docker_run") as run,
        ):
            target = create_backup(root, env_file)

        self.assertEqual((target / "database.dump").read_bytes(), b"database")
        self.assertEqual((target / "files.tar.gz").read_bytes(), b"files")
        self.assertEqual(
            run.call_args_list,
            [
                call("stop", "--timeout", "720", "worker", "web"),
                call("start", "web", "worker"),
            ],
        )
        self.assertEqual(output.call_count, 2)

    def test_backup_checks_database_before_creating_output_directory(self):
        root = Path(self.temp.name) / "database-offline-backup-test"
        env_file = Path(self.temp.name) / "database-offline-backup.env"
        env_file.write_text("SECRET_KEY=test", encoding="utf-8")

        with (
            patch("scripts.backup.running_services", return_value={"web"}),
            self.assertRaisesRegex(RuntimeError, "数据库容器未运行"),
        ):
            create_backup(root, env_file)

        self.assertFalse(root.exists())

    def test_backup_restarts_services_after_snapshot_failure(self):
        root = Path(self.temp.name) / "failed-backup-test"
        env_file = Path(self.temp.name) / "failed-backup.env"
        env_file.write_text("SECRET_KEY=test", encoding="utf-8")

        with (
            patch("scripts.backup.restrict_private_path"),
            patch(
                "scripts.backup.running_services",
                return_value={"db", "web"},
            ),
            patch("scripts.backup.docker_output", side_effect=RuntimeError("pg_dump failed")),
            patch("scripts.backup.docker_run") as run,
            self.assertRaisesRegex(RuntimeError, "pg_dump failed"),
        ):
            create_backup(root, env_file)

        self.assertEqual(
            run.call_args_list,
            [
                call("stop", "--timeout", "720", "web"),
                call("start", "web"),
            ],
        )

    def test_retention_removes_old_generation_but_keeps_wallet_ledger(self):
        workspace = self.create_workspace()
        job = self.submit(workspace)
        worker = self.create_worker()
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
