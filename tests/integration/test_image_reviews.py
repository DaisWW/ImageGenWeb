from __future__ import annotations

import json

from imagegen.extensions import db
from imagegen.models import (
    GenerationItem,
)
from tests.support.platform import (
    FakeProviderFactory,
    PlatformTestCase,
)


class TestImageReviews(PlatformTestCase):
    def test_ai_image_review_enforces_all_hard_checks_and_persists_result(self):
        workspace = self.create_workspace("图片验收")
        prompt = "一只银色运动鞋居中，不含文字"
        hard_checks = ["只出现一只银色运动鞋", "画面中不得出现文字"]
        draft = self.create_ready_prompt_draft(
            workspace,
            prompt=prompt,
            creative_direction_id="product",
            template_id="product-commerce-visual",
            hard_checks=hard_checks,
        )
        client = self.user_client()
        submitted = client.post(
            "/api/generations",
            json={
                "workspace_id": workspace.id,
                "channel_id": "test",
                "model": "model-b",
                "mode": "text2img",
                "prompt": prompt,
                "prompt_draft_id": draft.id,
                "generation_stage": "final",
            },
        )
        self.assertEqual(submitted.status_code, 202, submitted.get_data(as_text=True))
        item_id = submitted.json["job"]["items"][0]["id"]
        worker = self.create_worker()
        worker.providers = FakeProviderFactory()
        channel = self.app.extensions["channel_registry"].get("test")
        self.assertTrue(worker._claim(item_id, channel))
        worker._process_item(item_id)

        self.chat_client.image_review_content = json.dumps(
            {
                "verdict": "pass",
                "hard_checks": [
                    {
                        "id": "instruction_following",
                        "label": "整体指令遵循",
                        "passed": True,
                        "evidence": "主体与构图可见",
                    },
                    {
                        "id": "criterion_1",
                        "label": hard_checks[0],
                        "passed": True,
                        "evidence": "只出现一只运动鞋",
                    },
                ],
                "scores": {"composition": "NaN", "visual_quality": 4, "usability": 3.5},
                "findings": ["第二项未完成验收"],
                "suggested_edit": "只改变文字区域，移除全部文字；必须保持运动鞋和构图不变。",
            },
            ensure_ascii=False,
        )
        reviewed = client.post(
            f"/api/generation-items/{item_id}/review",
            json={"model_id": "test-chat"},
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.get_data(as_text=True))
        review = reviewed.json["review"]
        self.assertEqual(review["verdict"], "revise")
        missing = next(item for item in review["hard_checks"] if item["id"] == "criterion_2")
        self.assertFalse(missing["passed"])
        self.assertIn("未返回", missing["evidence"])
        self.assertEqual(review["method"], "openai-image-evals")
        self.assertEqual(review["scores"]["composition"], 0.0)
        self.assertIn("只改变文字区域", review["suggested_edit"])
        review_system = self.chat_client.calls[-1]["system"]
        self.assertIn(f"criterion_1: {hard_checks[0]}", review_system)
        self.assertIn(f"criterion_2: {hard_checks[1]}", review_system)

        db.session.expire_all()
        self.assertEqual(db.session.get(GenerationItem, item_id).review["verdict"], "revise")
        serialized = client.get(f"/api/generations/{submitted.json['job']['id']}").json["job"]
        self.assertEqual(serialized["items"][0]["review"]["verdict"], "revise")

        self.chat_client.image_review_content = ""
        passed = client.post(
            f"/api/generation-items/{item_id}/review",
            json={"model_id": "test-chat"},
        )
        self.assertEqual(passed.status_code, 200)
        self.assertEqual(passed.json["review"]["verdict"], "pass")
        self.assertEqual(passed.json["review"]["suggested_edit"], "")
