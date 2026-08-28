from __future__ import annotations

import copy

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from ..config.matting_models import MattingModelRegistry
from ..errors import ServiceError
from ..extensions import db
from ..integrations.background_removal import (
    BackgroundRemovalAdapter,
    MattingAdapterFactory,
)
from ..models import (
    BackgroundRemovalResult,
    BackgroundRemovalRun,
    GenerationItem,
    utcnow,
)
from ..storage import ImageStorage

MAX_MODELS_PER_RUN = 8
ACTIVE_STATUSES = {"queued", "running"}


class BackgroundRemovalService:
    def __init__(
        self,
        models: MattingModelRegistry,
        storage: ImageStorage,
        adapter_factory: MattingAdapterFactory | None = None,
    ):
        self.models = models
        self.storage = storage
        self.adapter_factory = adapter_factory or MattingAdapterFactory()

    def create_adapter(
        self,
        snapshot,
        *,
        lucida_client_cls=None,
    ) -> BackgroundRemovalAdapter:
        """Resolve an adapter from a persisted model/result snapshot."""
        return self.adapter_factory.create(snapshot, lucida_client_cls=lucida_client_cls)

    def public_models(self) -> list[dict]:
        return [model.public_dict() for model in self.models.list() if model.configured]

    def get_for_item(self, item_id: str, *, user_id: int | None) -> BackgroundRemovalRun | None:
        query = (
            select(BackgroundRemovalRun)
            .options(selectinload(BackgroundRemovalRun.results))
            .where(BackgroundRemovalRun.source_item_id == item_id)
        )
        if user_id is not None:
            query = query.where(BackgroundRemovalRun.user_id == user_id)
        return db.session.scalar(query)

    def get_run(self, run_id: str, *, user_id: int | None) -> BackgroundRemovalRun:
        query = (
            select(BackgroundRemovalRun)
            .options(selectinload(BackgroundRemovalRun.results))
            .where(BackgroundRemovalRun.id == run_id)
        )
        if user_id is not None:
            query = query.where(BackgroundRemovalRun.user_id == user_id)
        run = db.session.scalar(query)
        if run is None:
            raise ServiceError("透明化对比任务不存在", status_code=404)
        return run

    def get_result(self, result_id: str, *, user_id: int | None) -> BackgroundRemovalResult:
        query = (
            select(BackgroundRemovalResult)
            .join(BackgroundRemovalRun)
            .where(BackgroundRemovalResult.id == result_id)
        )
        if user_id is not None:
            query = query.where(BackgroundRemovalRun.user_id == user_id)
        result = db.session.scalar(query)
        if result is None:
            raise ServiceError("透明化结果不存在", status_code=404)
        return result

    def submit(
        self,
        item_id: str,
        *,
        user_id: int,
        model_ids: tuple[str, ...],
    ) -> BackgroundRemovalRun:
        if not 1 <= len(model_ids) <= MAX_MODELS_PER_RUN:
            raise ServiceError(f"请选择 1 到 {MAX_MODELS_PER_RUN} 个透明化模型")
        if len(model_ids) != len(set(model_ids)):
            raise ServiceError("透明化模型不能重复")
        configured = []
        for model_id in model_ids:
            try:
                configured.append(self.models.get(model_id))
            except ValueError as exc:
                raise ServiceError(str(exc)) from exc

        item = db.session.scalar(
            select(GenerationItem)
            .options(
                selectinload(GenerationItem.job),
                selectinload(GenerationItem.background_removal_run).selectinload(
                    BackgroundRemovalRun.results
                ),
            )
            .where(GenerationItem.id == item_id, GenerationItem.user_id == user_id)
            .with_for_update()
        )
        if item is None:
            raise ServiceError("生成结果不存在", status_code=404)
        if item.status != "succeeded" or not item.output_path:
            raise ServiceError("只有已完成的生成结果可以进行透明化", status_code=409)

        run = db.session.scalar(
            select(BackgroundRemovalRun)
            .options(selectinload(BackgroundRemovalRun.results))
            .where(BackgroundRemovalRun.source_item_id == item.id)
            .execution_options(populate_existing=True)
        )
        if run is None:
            run = BackgroundRemovalRun(
                source_item_id=item.id,
                user_id=user_id,
                status="queued",
            )
            db.session.add(run)
            db.session.flush()

        existing = {result.model_id: result for result in run.results}
        config_version = self.models.version
        for model in configured:
            result = existing.get(model.identifier)
            if result is None:
                result = BackgroundRemovalResult(
                    run=run,
                    model_id=model.identifier,
                    model_label=model.label,
                    model_config_version=config_version,
                    model_base_url=model.base_url,
                    upstream_model=model.model,
                    model_timeout_seconds=model.timeout_seconds,
                    model_max_concurrency=model.max_concurrency,
                    adapter_id=model.adapter_id,
                    adapter_options=copy.deepcopy(model.options),
                    status="queued",
                )
                db.session.add(result)
                continue
            if result.status != "failed":
                continue
            result.model_label = model.label
            result.model_config_version = config_version
            result.model_base_url = model.base_url
            result.upstream_model = model.model
            result.model_timeout_seconds = model.timeout_seconds
            result.model_max_concurrency = model.max_concurrency
            result.adapter_id = model.adapter_id
            result.adapter_options = copy.deepcopy(model.options)
            result.status = "queued"
            result.selected = False
            result.claimed_by = None
            result.heartbeat_at = None
            result.started_at = None
            result.completed_at = None
            result.elapsed_seconds = None
            result.error_code = None
            result.error_message = None

        self.refresh_run_status(run)
        db.session.commit()
        return self.get_run(run.id, user_id=user_id)

    def select(self, result_id: str, *, user_id: int | None) -> BackgroundRemovalRun:
        query = (
            select(BackgroundRemovalResult)
            .join(BackgroundRemovalRun)
            .options(
                selectinload(BackgroundRemovalResult.run).selectinload(BackgroundRemovalRun.results)
            )
            .where(BackgroundRemovalResult.id == result_id)
        )
        if user_id is not None:
            query = query.where(BackgroundRemovalRun.user_id == user_id)
        result = db.session.scalar(query.with_for_update())
        if result is None:
            raise ServiceError("透明化结果不存在", status_code=404)
        if result.status != "succeeded" or not result.output_path:
            raise ServiceError("只能选择已经完成的透明化结果", status_code=409)
        if result.selected:
            db.session.commit()
            return self.get_run(result.run_id, user_id=user_id)
        db.session.execute(
            update(BackgroundRemovalResult)
            .where(
                BackgroundRemovalResult.run_id == result.run_id,
                BackgroundRemovalResult.selected.is_(True),
            )
            .values(selected=False)
            .execution_options(synchronize_session="fetch")
        )
        db.session.flush()
        result.selected = True
        result.run.updated_at = utcnow()
        db.session.commit()
        return self.get_run(result.run_id, user_id=user_id)

    @staticmethod
    def refresh_run_status(run: BackgroundRemovalRun) -> None:
        statuses = [result.status for result in run.results]
        if not statuses:
            run.status = "queued"
            run.completed_at = None
        elif any(status in ACTIVE_STATUSES for status in statuses):
            run.status = "running" if "running" in statuses else "queued"
            run.completed_at = None
        elif all(status == "succeeded" for status in statuses):
            run.status = "succeeded"
            run.completed_at = utcnow()
        elif any(status == "succeeded" for status in statuses):
            run.status = "partial"
            run.completed_at = utcnow()
        else:
            run.status = "failed"
            run.completed_at = utcnow()
