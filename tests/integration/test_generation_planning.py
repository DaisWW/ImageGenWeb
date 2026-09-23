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

    def test_sample_repeats_the_base_prompt(self):
        plan = GenerationPlan.build(
            strategy="sample",
            prompt="一张产品海报",
            count=3,
            draft=None,
            max_prompt_characters=8000,
        )

        self.assertEqual(plan.prompts, ("一张产品海报",) * 3)
        self.assertEqual(plan.metadata, {"generation_strategy": "sample"})

    def test_retired_series_strategy_is_rejected(self):
        with self.assertRaisesRegex(ServiceError, "生成方式无效"):
            GenerationPlan.build(
                strategy="series",
                prompt="一张产品海报",
                count=1,
                draft=None,
                max_prompt_characters=8000,
            )

    def test_explore_creates_one_prompt_per_controlled_variant(self):
        plan = GenerationPlan.build(
            strategy="explore",
            prompt="一张产品海报",
            count=3,
            draft=self.draft,
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

    def test_explore_deduplicates_normalized_variants_and_requires_enough_unique_ones(self):
        draft = {
            "language": "zh",
            "exploration_plan": [
                {"label": " 中心层级 ", "delta": [" 主体采用中心构图 "]},
                {"label": "中心层级", "delta": ["主体采用中心构图"]},
                {"label": "非对称留白", "delta": ["右侧保留呼吸空间"]},
            ],
        }

        plan = GenerationPlan.build(
            strategy="explore",
            prompt="一张产品海报",
            count=2,
            draft=draft,
            max_prompt_characters=8000,
        )

        self.assertEqual(
            [item["label"] for item in plan.metadata["variant_plan"]],
            ["中心层级", "非对称留白"],
        )
        self.assertEqual(len(set(plan.prompts)), 2)

        with self.assertRaisesRegex(ServiceError, "没有足够的探索方案"):
            GenerationPlan.build(
                strategy="explore",
                prompt="一张产品海报",
                count=3,
                draft=draft,
                max_prompt_characters=8000,
            )

    def test_explore_requires_two_to_four_images_and_a_reviewed_draft(self):
        for count in (1, 5):
            with self.subTest(count=count), self.assertRaises(ServiceError):
                GenerationPlan.build(
                    strategy="explore",
                    prompt="产品",
                    count=count,
                    draft=self.draft,
                    max_prompt_characters=8000,
                )
        with self.assertRaisesRegex(ServiceError, "AI 整理"):
            GenerationPlan.build(
                strategy="explore",
                prompt="产品",
                count=2,
                draft=None,
                max_prompt_characters=8000,
            )
