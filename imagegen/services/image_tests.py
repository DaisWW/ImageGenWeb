from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import defer, joinedload, load_only

from ..errors import ServiceError
from ..extensions import db
from ..models import (
    IMAGE_TEST_KIND,
    AuditLog,
    GenerationItem,
    GenerationJob,
    ImageTestRun,
    ImageTestSuite,
    Workspace,
    new_public_id,
)
from .common import normalize_image_size
from .generations import ACTIVE_GENERATION_STATUSES, GenerationService, SubmitGeneration

MAX_TEST_IMAGES = 100
MAX_TEST_QUESTIONS = 20
TEST_QUALITIES = ("auto", "low", "medium", "high")

DEFAULT_SUITE = ImageTestSuite(
    id="default",
    name="基础绘图测评",
    revision=1,
    questions=[
        {
            "title": "构图与指令遵循",
            "prompt": (
                "拍摄一张极简静物广告图。米白色桌面上摆放三个互不遮挡的物体："
                "左边一个红色立方体，中间一个蓝色球体，右边一个黄色圆柱。"
                "相机略高于桌面，纯浅灰背景，柔和自然光。画面不含文字和其他物体。"
            ),
            "criteria": "核对物体数量、形状、颜色和从左至右的排列。观察光影是否统一。",
            "size": "1024x1024",
            "quality": "auto",
        },
        {
            "title": "中文文字与版式",
            "prompt": (
                "设计一张现代中文书店活动海报。主标题准确写「春日书展」，"
                "副标题准确写「周六十点 · 城市书房」。深青底色，米白文字，"
                "中央是一册打开的书，阅读顺序明确。保留留白，不添加其他文字。"
            ),
            "criteria": "逐字核对标题和副标题。观察字形、文字层级及留白。",
            "size": "1024x1536",
            "quality": "auto",
        },
        {
            "title": "光影与材质",
            "prompt": (
                "一张写实产品摄影，一只透明玻璃杯装有半杯清水，"
                "放在细纹胡桃木桌上。清晨的侧逆光穿过杯身，在桌面形成可见的焦散。"
                "远处背景轻微虚化，镜头表现真实的折射、玻璃厚度和木纹。画面无文字。"
            ),
            "criteria": "检查玻璃和清水的折射、水面与杯口形状、焦散方向及木纹细节。",
            "size": "1536x1024",
            "quality": "auto",
        },
    ],
)


class ImageTestService:
    def __init__(self, generations: GenerationService):
        self.generations = generations

    @staticmethod
    def suites() -> list[ImageTestSuite]:
        return [
            DEFAULT_SUITE,
            *db.session.scalars(select(ImageTestSuite).order_by(ImageTestSuite.created_at.desc())),
        ]

    @staticmethod
    def _suite(suite_id: str, *, lock: bool = False) -> ImageTestSuite:
        if suite_id == DEFAULT_SUITE.id:
            if lock:
                raise ServiceError("请先复制内置题组再编辑", status_code=400)
            return DEFAULT_SUITE
        query = select(ImageTestSuite).where(ImageTestSuite.id == suite_id)
        suite = db.session.scalar(query.with_for_update() if lock else query)
        if suite is None:
            raise ServiceError("题组不存在", status_code=404)
        return suite

    def save_suite(self, user_id: int, data: dict, suite_id: str = "") -> ImageTestSuite:
        name = self._text(data.get("name"), 80, "题组名称")
        questions = data.get("questions")
        if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_TEST_QUESTIONS:
            raise ServiceError(f"每个题组需要 1 到 {MAX_TEST_QUESTIONS} 道题")
        parsed = [self._question(question, index) for index, question in enumerate(questions, 1)]
        if suite_id:
            suite = self._suite(suite_id, lock=True)
            self._check_revision(suite, data.get("revision"))
            suite.revision += 1
        else:
            suite = ImageTestSuite(id=new_public_id())
            db.session.add(suite)
        suite.name = name
        suite.questions = parsed
        self._audit(user_id, "suite.update" if suite_id else "suite.create", suite.id, "suite")
        db.session.commit()
        return suite

    def delete_suite(self, user_id: int, suite_id: str, data: dict) -> None:
        suite = self._suite(suite_id, lock=True)
        self._check_revision(suite, data.get("revision"))
        db.session.delete(suite)
        self._audit(user_id, "suite.delete", suite_id, "suite")
        db.session.commit()

    def start(self, user_id: int, data: dict, *, retry_of: str | None = None) -> ImageTestRun:
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ServiceError("测试请求 ID 无效")
        run_id = hashlib.sha256(f"{user_id}:{request_id}".encode()).hexdigest()[:32]
        digest = hashlib.sha256(
            json.dumps(
                [retry_of, data.get("suite_id"), data.get("suite_revision"), data.get("targets")],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        try:
            user = self.generations.billing.lock_user(user_id)
            if not user.is_admin or not user.is_active:
                raise ServiceError("没有生图测评权限", status_code=403)
            existing = db.session.get(ImageTestRun, run_id)
            if existing:
                if existing.request_digest != digest:
                    raise ServiceError("测试请求 ID 已用于其他批次", status_code=409)
                return existing
            if retry_of:
                original = self.get_run(retry_of)
                jobs = self.jobs(retry_of)
                if any(job.status in ACTIVE_GENERATION_STATUSES for job in jobs):
                    raise ServiceError("请等当前批次结束后重试", status_code=409)
                cells = [
                    (job.image_test_question_index, job.channel_id, job.model)
                    for job in jobs
                    if job.status in {"failed", "interrupted"}
                ]
                if not cells:
                    raise ServiceError("当前批次没有可重试的失败项", status_code=409)
                name, questions = original.suite_name, original.questions
            else:
                suite_id = self._text(data.get("suite_id"), 32, "题组")
                suite = self._suite(suite_id)
                self._check_revision(suite, data.get("suite_revision"))
                targets = self._targets(data.get("targets"))
                if len(suite.questions) * len(targets) > MAX_TEST_IMAGES:
                    raise ServiceError(f"单批测评最多生成 {MAX_TEST_IMAGES} 张图")
                name, questions = suite.name, suite.questions
                cells = [
                    (index, channel_id, model)
                    for index in range(len(questions))
                    for channel_id, model in targets
                ]
            run = ImageTestRun(
                id=run_id,
                user_id=user_id,
                suite_name=name,
                questions=questions,
                total_images=len(cells),
                estimated_rmb=Decimal("0"),
                request_digest=digest,
                retry_of=retry_of,
            )
            workspace = db.session.scalar(
                select(Workspace).where(
                    Workspace.user_id == user_id, Workspace.kind == IMAGE_TEST_KIND
                )
            )
            if workspace is None:
                workspace_id = new_public_id()
                workspace = Workspace(
                    id=workspace_id,
                    user_id=user_id,
                    name=f"生图测评 {workspace_id}",
                    kind=IMAGE_TEST_KIND,
                )
                db.session.add(workspace)
            db.session.add(run)
            db.session.flush()
            channels = {
                channel.identifier: channel
                for channel in self.generations.channels.list(include_disabled=False)
                if channel.configured
            }
            requests = []
            for index, channel_id, model in cells:
                question = questions[index]
                channel = channels.get(channel_id)
                if channel is None:
                    raise ServiceError("生图渠道不存在或未启用")
                formats = channel.capabilities.formats
                request = SubmitGeneration(
                    channel_id=channel_id,
                    model=model,
                    mode="text2img",
                    prompt=question["prompt"],
                    size=question["size"],
                    quality=question["quality"],
                    output_format="png" if "png" in formats else formats[0],
                    compression=90,
                    batch_count=1,
                    reference_ids=(),
                )
                requests.append((index, channel, request))
            run.estimated_rmb = self.generations.stage_image_test_jobs(workspace, run_id, requests)
            self._audit(user_id, "run.retry" if retry_of else "run.start", run_id, "run")
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return run

    @staticmethod
    def get_run(run_id: str, *, include_questions: bool = True) -> ImageTestRun:
        options = () if include_questions else (defer(ImageTestRun.questions),)
        run = db.session.get(ImageTestRun, run_id, options=options)
        if run is None:
            raise ServiceError("测评批次不存在或已过期", status_code=404)
        return run

    @staticmethod
    def jobs(run_id: str) -> list[GenerationJob]:
        return list(
            db.session.scalars(
                select(GenerationJob)
                .options(
                    defer(GenerationJob.prompt),
                    defer(GenerationJob.workflow),
                    joinedload(GenerationJob.items).defer(GenerationItem.prompt),
                )
                .where(GenerationJob.image_test_run_id == run_id)
                .order_by(
                    GenerationJob.image_test_question_index,
                    GenerationJob.created_at,
                    GenerationJob.id,
                )
            ).unique()
        )

    @staticmethod
    def list_runs(limit: int, offset: int) -> tuple[list[tuple[ImageTestRun, dict[str, int]]], int]:
        runs = list(
            db.session.scalars(
                select(ImageTestRun)
                .options(
                    load_only(
                        ImageTestRun.suite_name,
                        ImageTestRun.total_images,
                        ImageTestRun.estimated_rmb,
                        ImageTestRun.retry_of,
                        ImageTestRun.created_at,
                    )
                )
                .order_by(ImageTestRun.created_at.desc(), ImageTestRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        counts: dict[str, dict[str, int]] = defaultdict(dict)
        for run_id, status, count in db.session.execute(
            select(GenerationJob.image_test_run_id, GenerationJob.status, func.count())
            .where(GenerationJob.image_test_run_id.in_([run.id for run in runs]))
            .group_by(GenerationJob.image_test_run_id, GenerationJob.status)
        ):
            counts[run_id][status] = count
        total = db.session.scalar(select(func.count()).select_from(ImageTestRun))
        return [(run, counts[run.id]) for run in runs], total

    def cancel(self, user_id: int, run_id: str) -> ImageTestRun:
        run = self.get_run(run_id)
        try:
            if self.generations.stage_image_test_cancellation(run):
                self._audit(user_id, "run.cancel", run_id, "run")
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return run

    @staticmethod
    def review(run_id: str, job_id: str, verdict: object) -> None:
        if verdict not in ("", "pass", "fail", "review"):
            raise ServiceError("复核标记无效")
        job = db.session.scalar(
            select(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.image_test_run_id == run_id)
            .with_for_update()
        )
        if job is None:
            raise ServiceError("测评结果不存在", status_code=404)
        if job.status != "succeeded":
            raise ServiceError("图片生成完成后才能复核", status_code=409)
        job.image_test_verdict = verdict or None
        db.session.commit()

    def _question(self, value: object, number: int) -> dict:
        if not isinstance(value, dict):
            raise ServiceError(f"第 {number} 题无效")
        quality = value.get("quality", "auto")
        if quality not in TEST_QUALITIES:
            raise ServiceError(f"第 {number} 题的质量无效")
        size = normalize_image_size(value.get("size") or "auto")
        return {
            "title": self._text(value.get("title"), 80, f"第 {number} 题的名称"),
            "prompt": self._text(
                value.get("prompt"),
                self.generations.settings.runtime().max_prompt_characters,
                f"第 {number} 题的提示词",
            ),
            "criteria": self._text(
                value.get("criteria", ""), 500, f"第 {number} 题的评审标准", optional=True
            ),
            "size": size,
            "quality": quality,
        }

    def _targets(self, value: object) -> list[tuple[str, str]]:
        if not isinstance(value, list) or not 1 <= len(value) <= MAX_TEST_IMAGES:
            raise ServiceError("请选择要测试的渠道模型")
        targets = []
        for target in value:
            if not isinstance(target, dict):
                raise ServiceError("渠道模型选项无效")
            targets.append(
                (
                    self._text(target.get("channel_id"), 64, "渠道 ID"),
                    self._text(target.get("model"), 100, "模型 ID"),
                )
            )
        return list(dict.fromkeys(targets))

    @staticmethod
    def _text(value: object, limit: int, label: str, *, optional: bool = False) -> str:
        if not isinstance(value, str) or "\x00" in value:
            raise ServiceError(f"{label}必须是有效文本")
        text = value.strip()
        if len(text) > limit or (not text and not optional):
            raise ServiceError(f"{label}长度无效，最多 {limit} 个字符")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ServiceError(f"{label}必须是有效文本") from exc
        return text

    @staticmethod
    def _check_revision(suite: ImageTestSuite, revision: object) -> None:
        if type(revision) is not int or revision != suite.revision:
            raise ServiceError("题组已更新，请刷新后再操作", status_code=409)

    @staticmethod
    def _audit(user_id: int, action: str, target_id: str, kind: str) -> None:
        db.session.add(
            AuditLog(
                actor_user_id=user_id,
                action=f"image_test.{action}",
                target_type=f"image_test_{kind}",
                target_id=target_id,
            )
        )
