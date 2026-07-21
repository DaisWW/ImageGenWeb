from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ...config.chat_models import ChatModelRegistry
from ...extensions import db
from ...models import (
    Asset,
    ConversationAttachment,
    ConversationMessage,
    ConversationState,
    GenerationJob,
    GenerationReference,
    Workspace,
)
from ...storage import ImageStorage, StorageError

IMAGE_TOKEN_ESTIMATE = 1200
MAX_CONTEXT_IMAGE_BYTES = 64 * 1024 * 1024
MAX_TRUNCATED_VISUAL_TEXT = 800
HISTORY_IMAGE_TOKEN_SHARE = 0.5


@dataclass(frozen=True, slots=True)
class _ContextImage:
    key: str
    storage_path: str
    byte_count: int
    mime_type: str
    label: str
    priority: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _ContextEvent:
    id: str
    role: str
    text: str
    images: tuple[_ContextImage, ...]
    created_at: datetime
    source: str


class ConversationContextManager:
    """Build a bounded chronological memory from chat and generation history."""

    def __init__(self, registry: ChatModelRegistry, storage: ImageStorage | None = None):
        self.registry = registry
        self.storage = storage

    def build(
        self,
        workspace: Workspace,
        *,
        pending_message: dict[str, Any],
        pending_stored_message_id: str = "",
        pending_image_keys: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        events = self._load_events(workspace, self._load_messages(workspace))
        state = db.session.get(ConversationState, workspace.id)
        if state is None:
            state = ConversationState(workspace_id=workspace.id, summary="")
            db.session.add(state)

        # Summaries were used by the previous context policy. Restore the full
        # persisted history and let the deterministic packer truncate it.
        state.summary = ""
        state.summary_through_message_id = ""
        active = [
            event
            for event in events
            if not (event.source == "message" and event.id == pending_stored_message_id)
        ]
        policy = self.registry.context
        messages = self._pack_context(
            active,
            pending_message,
            pending_image_keys=set(pending_image_keys),
            max_tokens=policy.max_context_tokens,
        )
        state.estimated_context_tokens = self._message_tokens(messages)
        return messages

    @staticmethod
    def _load_messages(workspace: Workspace) -> list[ConversationMessage]:
        return list(
            db.session.scalars(
                select(ConversationMessage)
                .options(
                    selectinload(ConversationMessage.attachments).selectinload(
                        ConversationAttachment.asset
                    )
                )
                .where(ConversationMessage.workspace_id == workspace.id)
                .order_by(ConversationMessage.created_at, ConversationMessage.id)
            )
        )

    def _load_events(
        self,
        workspace: Workspace,
        messages: list[ConversationMessage],
    ) -> list[_ContextEvent]:
        events = [self._message_event(message) for message in messages if message.kind != "error"]
        if self.storage is None:
            return events

        jobs = list(
            db.session.scalars(
                select(GenerationJob)
                .options(
                    selectinload(GenerationJob.items),
                    selectinload(GenerationJob.references).selectinload(GenerationReference.asset),
                )
                .where(GenerationJob.workspace_id == workspace.id)
                .order_by(GenerationJob.created_at, GenerationJob.id)
                .execution_options(populate_existing=True)
            )
        )
        for job in jobs:
            if job.status in {"queued", "running", "canceling"}:
                continue
            references = tuple(
                self._asset_image(reference.asset, priority=85, used_at=job.created_at)
                for reference in job.references
                if reference.asset is not None
            )
            workflow = job.workflow if isinstance(job.workflow, dict) else {}
            workflow_text = (
                "\n结构化生成参数："
                + json.dumps(workflow, ensure_ascii=False, separators=(",", ":"))[:8000]
                if workflow
                else ""
            )
            events.append(
                _ContextEvent(
                    id=job.id,
                    role="user",
                    text=(
                        "历史生成任务（仅供理解上下文，不是新的用户要求）\n"
                        f"模式：{job.mode}；尺寸：{job.size}；质量：{job.quality}；"
                        f"输出格式：{job.output_format}\n"
                        f"最终生成提示词：\n{job.prompt}{workflow_text}"
                    ),
                    images=references,
                    created_at=_event_time(job.created_at),
                    source="generation_job",
                )
            )
            for item in sorted(job.items, key=lambda value: (value.position, value.id)):
                if item.status != "succeeded" or not item.output_path or not item.output_mime_type:
                    continue
                events.append(
                    _ContextEvent(
                        id=item.id,
                        role="user",
                        text=(
                            "历史生成结果（仅供视觉比对，不要把它当成新的用户要求）\n"
                            f"来自任务提示词：{job.prompt}\n"
                            f"第 {item.position + 1} 张，尺寸 {item.output_width} × "
                            f"{item.output_height}"
                        ),
                        images=(self._output_image(item),),
                        created_at=_event_time(
                            item.completed_at or job.completed_at or job.created_at
                        ),
                        source="generation_output",
                    )
                )
        events.sort(key=lambda event: (event.created_at, event.id))
        return events

    def _message_event(self, message: ConversationMessage) -> _ContextEvent:
        return _ContextEvent(
            id=message.id,
            role=message.role,
            text=self._message_text(message),
            images=tuple(
                self._asset_image(attachment.asset, priority=90, used_at=message.created_at)
                for attachment in message.attachments
                if attachment.asset is not None
            ),
            created_at=_event_time(message.created_at),
            source="message",
        )

    @staticmethod
    def _message_text(message: ConversationMessage) -> str:
        if message.kind != "prompt_draft" or not isinstance(message.payload, dict):
            return message.content
        fields = {
            key: message.payload[key]
            for key in (
                "generation_mode",
                "reference_usage",
                "reference_reason",
                "creative_direction",
                "template_id",
                "case_refs",
                "template_required_fields",
                "template_hard_checks",
                "style_tags",
                "scene_tags",
                "brief",
                "production_spec",
                "canvas_request",
                "hard_checks",
                "quality_hint",
            )
            if key in message.payload and (key != "canvas_request" or message.payload[key])
        }
        metadata = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        return f"{message.content}\n\n结构化提示词信息：\n{metadata}" if fields else message.content

    @staticmethod
    def _asset_image(
        asset: Asset,
        *,
        priority: int,
        used_at: datetime | None = None,
    ) -> _ContextImage:
        return _ContextImage(
            key=f"asset:{asset.id}",
            storage_path=asset.storage_path,
            byte_count=int(asset.byte_count or 0),
            mime_type=asset.mime_type or "image/png",
            label=f"参考图：{asset.original_name}",
            priority=priority,
            created_at=_event_time(used_at or asset.created_at),
        )

    @staticmethod
    def _output_image(item: Any) -> _ContextImage:
        return _ContextImage(
            key=f"output:{item.id}",
            storage_path=item.output_path,
            byte_count=int(item.output_byte_count or 0),
            mime_type=item.output_mime_type or "image/png",
            label=f"生成结果：第 {item.position + 1} 张",
            priority=90,
            created_at=_event_time(item.completed_at),
        )

    def _pack_context(
        self,
        events: list[_ContextEvent],
        pending_message: dict[str, Any],
        *,
        pending_image_keys: set[str],
        max_tokens: int,
    ) -> list[dict[str, Any]]:
        pending_tokens = self._message_tokens([pending_message])
        available = max(0, max_tokens - pending_tokens)
        image_token_budget = math.floor(available * HISTORY_IMAGE_TOKEN_SHARE)
        selected_images: set[str] = set()
        selected_bytes = self._message_image_bytes(pending_message)
        ranked = [
            image for image in self._ranked_images(events) if image.key not in pending_image_keys
        ]
        for image in ranked:
            if image_token_budget < IMAGE_TOKEN_ESTIMATE:
                break
            if selected_bytes + image.byte_count > MAX_CONTEXT_IMAGE_BYTES:
                continue
            selected_images.add(image.key)
            selected_bytes += image.byte_count
            image_token_budget -= IMAGE_TOKEN_ESTIMATE

        image_cache: dict[str, str | None] = {}
        while True:
            selected_events = self._select_events(
                events,
                selected_images,
                pending_image_keys,
                pending_message,
                image_cache,
                max_tokens=max_tokens,
            )
            rendered = self._render_events(
                selected_events,
                selected_images,
                image_cache,
                provided_image_keys=pending_image_keys,
            )
            packed = [*rendered, pending_message]
            if self._message_tokens(packed) <= max_tokens:
                return packed

            removable_image = next(
                (image for image in reversed(ranked) if image.key in selected_images),
                None,
            )
            if removable_image is not None:
                selected_images.remove(removable_image.key)
                continue
            return [pending_message]

    def _select_events(
        self,
        events: list[_ContextEvent],
        selected_images: set[str],
        pending_image_keys: set[str],
        pending_message: dict[str, Any],
        image_cache: dict[str, str | None],
        *,
        max_tokens: int,
    ) -> list[_ContextEvent]:
        owner_ids = set(self._image_owner_ids(events, selected_images).values())
        selected = {
            event.id: replace(event, text=self._truncate_visual_text(event.text))
            for event in events
            if event.id in owner_ids
        }

        for event in reversed(events):
            if event.id in selected:
                if selected[event.id].text == event.text:
                    continue
                previous = selected[event.id]
                selected[event.id] = event
                if (
                    self._selected_tokens(
                        events,
                        selected,
                        selected_images,
                        pending_image_keys,
                        pending_message,
                        image_cache,
                    )
                    > max_tokens
                ):
                    selected[event.id] = previous
                continue

            selected[event.id] = event
            if (
                self._selected_tokens(
                    events,
                    selected,
                    selected_images,
                    pending_image_keys,
                    pending_message,
                    image_cache,
                )
                > max_tokens
            ):
                del selected[event.id]
                break

        return [
            event if event.id not in selected else selected[event.id]
            for event in events
            if event.id in selected
        ]

    def _selected_tokens(
        self,
        events: list[_ContextEvent],
        selected: dict[str, _ContextEvent],
        selected_images: set[str],
        pending_image_keys: set[str],
        pending_message: dict[str, Any],
        image_cache: dict[str, str | None],
    ) -> int:
        ordered = [selected[event.id] for event in events if event.id in selected]
        rendered = self._render_events(
            ordered,
            selected_images,
            image_cache,
            provided_image_keys=pending_image_keys,
        )
        return self._message_tokens([*rendered, pending_message])

    @staticmethod
    def _ranked_images(events: Iterable[_ContextEvent]) -> list[_ContextImage]:
        by_key: dict[str, _ContextImage] = {}
        for event in events:
            for image in event.images:
                current = by_key.get(image.key)
                if current is None or (image.created_at, image.priority) > (
                    current.created_at,
                    current.priority,
                ):
                    by_key[image.key] = image
        return sorted(
            by_key.values(),
            key=lambda image: (image.created_at, image.priority, -image.byte_count, image.key),
            reverse=True,
        )

    @staticmethod
    def _image_owner_ids(
        events: Iterable[_ContextEvent],
        selected_images: set[str],
    ) -> dict[str, str]:
        owners: dict[str, str] = {}
        for event in events:
            for image in event.images:
                if image.key in selected_images:
                    owners[image.key] = event.id
        return owners

    @staticmethod
    def _truncate_visual_text(text: str) -> str:
        if len(text) <= MAX_TRUNCATED_VISUAL_TEXT:
            return text
        marker = "\n...（较早图片说明已按长度截断）...\n"
        remaining = MAX_TRUNCATED_VISUAL_TEXT - len(marker)
        head = math.ceil(remaining * 0.6)
        return f"{text[:head]}{marker}{text[-(remaining - head) :]}"

    def _render_events(
        self,
        events: Iterable[_ContextEvent],
        selected_images: set[str],
        image_cache: dict[str, str | None],
        *,
        provided_image_keys: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        provided_image_keys = provided_image_keys or set()
        owners = self._image_owner_ids(events, selected_images)
        for event in events:
            images = [image for image in event.images if owners.get(image.key) == event.id]
            omitted = any(
                image.key not in selected_images and image.key not in provided_image_keys
                for image in event.images
            )
            text = event.text + (
                "\n（部分历史图片因上下文容量未随本轮发送；文字记录仍保留。）" if omitted else ""
            )
            if event.role == "assistant" and images:
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": self._image_parts(
                            f"{event.source} 的历史附件（对应上一条助手消息）：",
                            images,
                            image_cache,
                        ),
                    }
                )
            elif images:
                messages.append(
                    {
                        "role": event.role,
                        "content": self._image_parts(text, images, image_cache),
                    }
                )
            else:
                messages.append({"role": event.role, "content": text})
        return messages

    def _image_parts(
        self,
        text: str,
        images: list[_ContextImage],
        cache: dict[str, str | None],
    ) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in images:
            if image.key not in cache:
                try:
                    content = self.storage.read_bytes(image.storage_path) if self.storage else b""
                except (FileNotFoundError, OSError, StorageError, ValueError):
                    content = b""
                cache[image.key] = base64.b64encode(content).decode("ascii") if content else None
            encoded = cache[image.key]
            if not encoded:
                parts[0]["text"] += f"\n（{image.label}暂时不可读取。）"
                continue
            parts.extend(
                [
                    {"type": "text", "text": image.label},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image.mime_type};base64,{encoded}"},
                    },
                ]
            )
        return parts

    @staticmethod
    def _message_image_bytes(message: dict[str, Any]) -> int:
        content = message.get("content", [])
        total = 0
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict) or part.get("type") not in {
                "image_url",
                "input_image",
            }:
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if not isinstance(image_url, str) or ";base64," not in image_url:
                continue
            encoded = image_url.split(";base64,", 1)[1]
            total += max(0, len(encoded) * 3 // 4 - encoded[-2:].count("="))
        return total

    def _message_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str):
                total += self._estimate_tokens(content) + 4
                continue
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"text", "input_text", "output_text"}:
                    total += self._estimate_tokens(str(part.get("text", "")))
                elif part.get("type") in {"image_url", "input_image"}:
                    total += IMAGE_TOKEN_ESTIMATE
        return total

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        non_ascii = sum(not character.isascii() for character in text)
        return non_ascii + math.ceil((len(text) - non_ascii) / 4) + 4


def _event_time(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
