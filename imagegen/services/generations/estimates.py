from __future__ import annotations

import math
from collections.abc import Iterable
from decimal import Decimal
from statistics import fmean

from sqlalchemy import select

from ...config.channels import Channel
from ...extensions import db
from ...models import GenerationItem, GenerationJob, RuntimeLog

_DURATION_SAMPLE_LIMIT = 50
_DURATION_SAMPLE_TARGET = 8
_DURATION_TRIM_RATIO = 0.1


def _duration_values(values: Iterable[Decimal | float | int | None]) -> list[float]:
    durations = []
    for value in values:
        if value is None:
            continue
        duration = float(value)
        if math.isfinite(duration) and duration > 0:
            durations.append(duration)
    return durations


def _robust_duration_estimate(
    samples: Iterable[Decimal | float | int | None], baseline: float
) -> float:
    ordered = sorted(_duration_values(samples))
    if not ordered:
        return baseline

    sample_count = len(ordered)
    trim_count = int(sample_count * _DURATION_TRIM_RATIO)
    trimmed = ordered[trim_count:-trim_count] if trim_count else ordered
    observed = fmean(trimmed)
    confidence = min(1.0, sample_count / _DURATION_SAMPLE_TARGET)
    return baseline + (observed - baseline) * confidence


class GenerationDurationEstimator:
    def estimate_seconds(self, job: GenerationJob, channel: Channel) -> Decimal:
        samples = self._duration_samples(job, channel, exact=True)
        if len(samples) < _DURATION_SAMPLE_TARGET:
            related = self._duration_samples(job, channel, exact=False)
            samples = (
                related
                if len(related) >= _DURATION_SAMPLE_TARGET
                else max(related, self._runtime_duration_samples(job, channel), key=len)
            )

        estimate = _robust_duration_estimate(
            samples,
            baseline=float(channel.limits.estimated_seconds),
        )
        estimate = min(max(estimate, 10.0), float(channel.limits.timeout_seconds))
        return Decimal(str(round(estimate, 3)))

    def _duration_samples(
        self, job: GenerationJob, channel: Channel, *, exact: bool
    ) -> list[float]:
        query = (
            select(GenerationItem.elapsed_seconds)
            .join(GenerationJob)
            .where(
                GenerationItem.status == "succeeded",
                GenerationItem.elapsed_seconds.is_not(None),
                GenerationItem.channel_id == channel.identifier,
                GenerationJob.model == job.model,
                GenerationJob.kind == job.kind,
                GenerationJob.mode == job.mode,
            )
            .order_by(GenerationItem.completed_at.desc())
            .limit(_DURATION_SAMPLE_LIMIT)
        )
        if exact:
            query = query.where(
                GenerationJob.size == job.size,
                GenerationJob.quality == job.quality,
            )
        return _duration_values(db.session.scalars(query))

    @staticmethod
    def _runtime_duration_samples(job: GenerationJob, channel: Channel) -> list[float]:
        query = (
            select(RuntimeLog.elapsed_seconds)
            .where(
                RuntimeLog.category == "generation",
                RuntimeLog.event == "generation.provider",
                RuntimeLog.status == "success",
                RuntimeLog.elapsed_seconds.is_not(None),
                RuntimeLog.provider_id == channel.identifier,
                RuntimeLog.model == job.model,
            )
            .order_by(RuntimeLog.created_at.desc())
            .limit(_DURATION_SAMPLE_LIMIT)
        )
        return _duration_values(db.session.scalars(query))
