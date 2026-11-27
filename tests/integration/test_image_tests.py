from datetime import timedelta
from uuid import uuid4

import yaml
from sqlalchemy import func, select

from imagegen.extensions import db
from imagegen.models import GenerationJob, ImageTestRun, WalletLedger, utcnow
from tests.support.platform import CHANNEL_CONFIG, FakeProviderFactory, PlatformTestCase

ROOT = "/api/admin/image-tests"


class TestAdminImageTests(PlatformTestCase):
    def test_batch_preserves_questions_routes_each_image_and_keeps_wallet_clear(self):
        config = yaml.safe_load(CHANNEL_CONFIG)
        config["channels"].append({**config["channels"][0], "id": "other", "label": "另一个渠道"})
        self.channel_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        self.app.extensions["channel_registry"].reload(force=True)
        admin = self.admin_client()
        with self.app.app_context():
            regular = self.user_client()
            self.assertEqual(regular.get(ROOT).status_code, 403)
        catalog = admin.get(ROOT)
        self.assertNotIn("test-key-not-secret", catalog.get_data(as_text=True))

        question = {
            "title": "文字准确性",
            "prompt": "一张只写「春天」的中文海报",
            "criteria": "文字逐字正确",
            "size": "1024×1536",
            "quality": "high",
        }
        suite_response = admin.post(
            f"{ROOT}/suites", json={"name": "中文海报", "questions": [question]}
        )
        self.assertEqual(suite_response.status_code, 201)
        suite = suite_response.json["suite"]
        request = {
            "request_id": uuid4().hex,
            "suite_id": suite["id"],
            "suite_revision": suite["revision"],
            "targets": [
                {"channel_id": "test", "model": "model-a"},
                {"channel_id": "other", "model": "model-b"},
            ],
        }
        started = admin.post(f"{ROOT}/runs", json=request)
        self.assertEqual(started.status_code, 202, started.json)
        run = started.json["run"]
        self.assertEqual(run["counts"], {"queued": 2})
        self.assertEqual(run["estimated_rmb"], "2.5000")
        self.assertEqual(run["questions"][0]["size"], "1024x1536")
        progress = admin.get(f"{ROOT}/runs/{run['id']}?progress=1").json["run"]
        self.assertNotIn("questions", progress)
        self.assertEqual(progress["results"], run["results"])
        self.assertEqual(admin.post(f"{ROOT}/runs", json=request).json["run"]["id"], run["id"])
        changed = {**request, "targets": request["targets"][:1]}
        self.assertEqual(admin.post(f"{ROOT}/runs", json=changed).status_code, 409)
        with self.app.app_context():
            self.assertEqual(regular.get(f"{ROOT}/runs/{run['id']}").status_code, 403)

        updates = {
            "name": "更新后的题组",
            "revision": suite["revision"],
            "questions": [{**question, "prompt": "改成只写「夏天」"}],
        }
        suite_path = f"{ROOT}/suites/{suite['id']}"
        updated = admin.put(suite_path, json=updates)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(admin.put(suite_path, json=updates).status_code, 409)
        deleted = admin.delete(suite_path, json={"revision": updated.json["suite"]["revision"]})
        self.assertEqual(deleted.status_code, 200)
        self.assertNotIn(suite["id"], [entry["id"] for entry in admin.get(ROOT).json["suites"]])

        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        for result in run["results"]:
            job = db.session.get(GenerationJob, result["id"])
            item_id = job.items[0].id
            self.assertTrue(worker._claim(item_id))
            worker.providers.adapter.fail = result["channel_id"] == "other"
            worker._process_item(item_id)
            self.assertEqual(worker.providers.adapter.request.model, result["model"])
            self.assertEqual(worker.providers.adapter.request.prompt, question["prompt"])

        db.session.expire_all()
        detail_path = f"{ROOT}/runs/{run['id']}"
        finished = admin.get(detail_path).json["run"]
        self.assertEqual(finished["suite_name"], suite["name"])
        self.assertEqual(finished["questions"], run["questions"])
        self.assertEqual(finished["counts"], {"succeeded": 1, "failed": 1})
        succeeded = next(
            result for result in finished["results"] if result["status"] == "succeeded"
        )
        failed = next(result for result in finished["results"] if result["status"] == "failed")
        self.assertEqual(failed["channel_id"], "other")
        self.assertEqual(failed["error_code"], "test_failure")
        self.assertEqual(admin.get(succeeded["image_url"]).mimetype, "image/png")
        self.assertEqual(admin.get(succeeded["thumbnail_url"]).mimetype, "image/webp")
        with self.app.app_context():
            self.assertEqual(regular.get(succeeded["image_url"]).status_code, 403)
        job = db.session.get(GenerationJob, succeeded["id"])
        self.assertEqual(admin.get(f"/media/outputs/{job.items[0].id}").status_code, 404)
        self.assertEqual(
            admin.post(
                "/api/library-images", json={"generation_item_id": job.items[0].id}
            ).status_code,
            404,
        )
        self.assertEqual(admin.get(f"/api/generations/{job.id}").status_code, 404)
        self.assertEqual(admin.get("/api/admin/generations").json["jobs"], [])
        self.assertEqual(admin.get("/api/workspaces").json["workspaces"], [])
        workspaces = [
            admin.post("/api/workspaces", json={"name": name}).json["workspace"]["id"]
            for name in ("绘图一", "绘图二")
        ]
        self.assertEqual(
            admin.put("/api/workspaces/order", json={"workspace_ids": workspaces}).status_code,
            200,
        )
        self.assertEqual(
            [workspace["id"] for workspace in admin.get("/api/workspaces").json["workspaces"]],
            workspaces,
        )
        self.assertEqual(
            db.session.scalar(
                select(func.count(WalletLedger.id)).where(
                    WalletLedger.entry_type == "generation_charge"
                )
            ),
            0,
        )
        self.assertEqual(self.admin.balance_rmb, 0)
        self.assertEqual(self.admin.reserved_rmb, 0)

        interrupted_job = db.session.get(GenerationJob, failed["id"])
        interrupted_job.status = "interrupted"
        interrupted_job.completed_at = utcnow()
        interrupted_job.items[0].status = "interrupted"
        db.session.commit()
        verdict_path = f"{detail_path}/results/{succeeded['id']}"
        self.assertEqual(admin.put(verdict_path, json={"verdict": "pass"}).status_code, 200)
        retry = admin.post(f"{detail_path}/retry", json={"request_id": uuid4().hex})
        self.assertEqual(retry.status_code, 202, retry.json)
        retried = retry.json["run"]
        self.assertEqual(retried["total"], 1)
        self.assertEqual(retried["retry_of"], run["id"])
        self.assertEqual(retried["questions"], run["questions"])
        self.assertEqual(retried["results"][0]["channel_id"], "other")
        self.assertEqual(retried["results"][0]["model"], "model-b")
        self.assertIn(
            "pass", {result["verdict"] for result in admin.get(detail_path).json["run"]["results"]}
        )
        canceled = admin.post(f"{ROOT}/runs/{retried['id']}/cancel").json["run"]
        self.assertEqual(canceled["counts"], {"canceled": 1})
        self.assertEqual(admin.get(f"{ROOT}/runs").json["total"], 2)

    def test_invalid_selection_rolls_back_and_retention_removes_empty_run(self):
        admin = self.admin_client()
        for prompt in ("包含空字符\x00", "不完整的字符\ud800"):
            with self.subTest(prompt=repr(prompt)):
                self.assertEqual(
                    admin.post(
                        f"{ROOT}/suites",
                        json={"name": "非法题组", "questions": [{"title": "A", "prompt": prompt}]},
                    ).status_code,
                    400,
                )
        default_suite = admin.get(ROOT).json["suites"][0]
        self.assertEqual(default_suite["name"], "二次元苗刀·建模三视图")
        self.assertTrue(default_suite["builtin"])
        self.assertEqual(
            admin.delete(
                f"{ROOT}/suites/{default_suite['id']}", json={"revision": default_suite["revision"]}
            ).status_code,
            400,
        )
        (question,) = default_suite["questions"]
        self.assertEqual(question["title"], "鹤羽苗刀·正侧俯三视图")
        request = {
            "request_id": uuid4().hex,
            "suite_id": default_suite["id"],
            "suite_revision": default_suite["revision"],
            "targets": [
                {"channel_id": "test", "model": "model-a"},
                {"channel_id": "test", "model": "disabled-model"},
            ],
        }
        self.assertEqual(admin.post(f"{ROOT}/runs", json=request).status_code, 400)
        self.assertEqual(db.session.scalar(select(func.count(GenerationJob.id))), 0)
        self.assertEqual(admin.get(f"{ROOT}/runs").json["total"], 0)

        request["targets"].pop()
        stale_request = {**request, "suite_revision": default_suite["revision"] - 1}
        self.assertEqual(admin.post(f"{ROOT}/runs", json=stale_request).status_code, 409)
        started = admin.post(f"{ROOT}/runs", json=request)
        self.assertEqual(started.status_code, 202, started.json)
        run = started.json["run"]
        self.assertEqual(run["questions"], [question])
        (result,) = run["results"]
        job = db.session.get(GenerationJob, result["id"])
        self.assertEqual(job.items[0].prompt, question["prompt"])
        path = f"{ROOT}/runs/{run['id']}"
        self.assertEqual(admin.post(f"{path}/cancel").status_code, 200)
        old = utcnow() - timedelta(days=31)
        db.session.get(ImageTestRun, run["id"]).created_at = old
        job.completed_at = old
        db.session.commit()
        self.assertEqual(
            self.create_worker().retention.cleanup(), {"jobs": 1, "assets": 0, "errors": 0}
        )
        self.assertEqual(admin.get(path).status_code, 404)
