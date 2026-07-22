from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import Asset, Workspace

SERIES_CONTRACT_FIELDS = (
    "identity_anchors",
    "visual_language",
    "palette_materials",
    "composition_rules",
    "typography_rules",
    "must_preserve",
    "allowed_changes",
)


def invalid_series_anchor() -> ServiceError:
    return ServiceError(
        "系列基准已失效，请重新选择一张生成结果",
        code="series_anchor_invalid",
        status_code=409,
    )


@dataclass(frozen=True, slots=True)
class SeriesAnchor:
    asset_id: str
    source_item_id: str
    contract: dict[str, list[str]]

    @classmethod
    def parse(cls, value: object) -> SeriesAnchor | None:
        if isinstance(value, cls):
            value = value.as_dict()
        if not isinstance(value, dict):
            return None
        asset_id = str(value.get("asset_id", "")).strip().lower()
        if len(asset_id) != 32 or any(
            character not in "0123456789abcdef" for character in asset_id
        ):
            return None
        contract = _sanitize_contract(value.get("contract"))
        if not contract:
            return None
        return cls(
            asset_id=asset_id,
            source_item_id=str(value.get("source_item_id", "")).strip().lower()[:32],
            contract=contract,
        )

    @classmethod
    def require(
        cls,
        value: object,
        *,
        invalid_message: str | None = None,
        invalid_code: str = "series_anchor_invalid",
    ) -> SeriesAnchor:
        if not isinstance(value, (dict, cls)):
            raise ServiceError("请先选择一张生成结果作为系列基准", status_code=409)
        anchor = cls.parse(value)
        if anchor is None:
            if invalid_message is None:
                raise invalid_series_anchor()
            raise ServiceError(invalid_message, code=invalid_code, status_code=409)
        return anchor

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "source_item_id": self.source_item_id,
            "contract": {key: list(values) for key, values in self.contract.items()},
        }

    def metadata(self) -> dict[str, str]:
        return {
            "asset_id": self.asset_id,
            "source_item_id": self.source_item_id,
        }

    def order_reference_ids(self, asset_ids: Iterable[str]) -> tuple[str, ...]:
        return (
            self.asset_id,
            *(asset_id for asset_id in asset_ids if asset_id != self.asset_id),
        )


@dataclass(frozen=True, slots=True)
class ResolvedSeriesAnchor:
    anchor: SeriesAnchor
    asset: Asset

    @classmethod
    def for_workspace(cls, workspace: Workspace, value: object) -> ResolvedSeriesAnchor:
        return cls._resolve(workspace, SeriesAnchor.require(value))

    @classmethod
    def active(cls, workspace: Workspace) -> ResolvedSeriesAnchor | None:
        settings = workspace.settings or {}
        if str(settings.get("generation_strategy", "sample")).strip().lower() != "series":
            return None
        anchor = SeriesAnchor.parse(settings.get("series_anchor"))
        if anchor is None:
            raise invalid_series_anchor()
        return cls._resolve(workspace, anchor)

    @classmethod
    def _resolve(cls, workspace: Workspace, anchor: SeriesAnchor) -> ResolvedSeriesAnchor:
        asset = db.session.scalar(
            select(Asset).where(
                Asset.id == anchor.asset_id,
                Asset.workspace_id == workspace.id,
                Asset.deleted_at.is_(None),
            )
        )
        if asset is None:
            raise invalid_series_anchor()
        return cls(anchor=anchor, asset=asset)

    def order_assets(self, assets: Iterable[Asset]) -> list[Asset]:
        return [self.asset, *(asset for asset in assets if asset.id != self.asset.id)]


def _sanitize_contract(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, list[str]] = {}
    for key in SERIES_CONTRACT_FIELDS:
        values = value.get(key)
        if not isinstance(values, list):
            continue
        result: list[str] = []
        for item in values[:6]:
            text = str(item).strip()[:300]
            if text and text not in result:
                result.append(text)
        if result:
            sanitized[key] = result
    return sanitized
