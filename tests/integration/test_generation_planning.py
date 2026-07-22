from __future__ import annotations

import unittest

from imagegen.errors import ServiceError
from imagegen.services.generations.planning import GenerationPlan


class TestGenerationPlanning(unittest.TestCase):
    def setUp(self):
        self.draft = {
            "language": "zh",
            "exploration_plan": [
                {"label": "中心层级", "delta": ["主体采用中心构图"]},
                {"label": "非对称留白", "delta": ["右侧保留呼吸空间"]},
                {"label": "材质近景", "delta": ["镜头更接近主体"]},
                {"label": "环境叙事", "delta": ["增加前中后景层次"]},
            ],
        }
        self.anchor = {
            "asset_id": "a" * 32,
            "source_item_id": "b" * 32,
            "contract": {
                "identity_anchors": ["同一主体"],
                "visual_language": ["电影感"],
                "palette_materials": ["冷蓝金属"],
                "must_preserve": ["主体轮廓"],
                "allowed_changes": ["动作和场景"],
            },
        }

    def test_sample_repeats_the_base_prompt(self):
        plan = GenerationPlan.build(
            strategy="sample",
            prompt="一张产品海报",
            count=3,
            draft=None,
            series_anchor=None,
            max_prompt_characters=8000,
        )

        self.assertEqual(plan.prompts, ("一张产品海报",) * 3)
        self.assertEqual(plan.metadata, {"generation_strategy": "sample"})

    def test_explore_creates_one_prompt_per_controlled_variant(self):
        plan = GenerationPlan.build(
            strategy="explore",
            prompt="一张产品海报",
            count=3,
            draft=self.draft,
            series_anchor=None,
            max_prompt_characters=8000,
        )

        self.assertEqual(plan.strategy, "explore")
        self.assertEqual(len(plan.prompts), 3)
        self.assertEqual(len(set(plan.prompts)), 3)
        self.assertEqual(
            [item["label"] for item in plan.metadata["variant_plan"]],
            [
                "中心层级",
                "非对称留白",
                "材质近景",
            ],
        )
        self.assertTrue(all("受控探索方案" in prompt for prompt in plan.prompts))

    def test_explore_requires_two_to_four_images_and_a_reviewed_draft(self):
        for count in (1, 5):
            with self.subTest(count=count), self.assertRaises(ServiceError):
                GenerationPlan.build(
                    strategy="explore",
                    prompt="产品",
                    count=count,
                    draft=self.draft,
                    series_anchor=None,
                    max_prompt_characters=8000,
                )
        with self.assertRaisesRegex(ServiceError, "AI 整理"):
            GenerationPlan.build(
                strategy="explore",
                prompt="产品",
                count=2,
                draft=None,
                series_anchor=None,
                max_prompt_characters=8000,
            )

    def test_series_repeats_the_series_contract_for_each_image(self):
        plan = GenerationPlan.build(
            strategy="series",
            prompt="系列第二张海报",
            count=2,
            draft=self.draft,
            series_anchor=self.anchor,
            max_prompt_characters=8000,
        )

        self.assertEqual(len(plan.prompts), 2)
        self.assertEqual(plan.prompts[0], plan.prompts[1])
        self.assertIn("系列一致性契约", plan.prompts[0])
        self.assertEqual(plan.metadata["series_anchor"]["asset_id"], "a" * 32)
        self.assertEqual(plan.metadata["series_contract"], self.anchor["contract"])
