from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ...config.chat_models import ChatModelRegistry
from ...extensions import db
from ...image_payloads import prepare_image_bytes
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
CHAT_IMAGE_MAX_SIDE = 1280
CHAT_IMAGE_WEBP_QUALITY = 85
CHAT_IMAGE_COMPRESSION_THRESHOLD_BYTES = 256 * 1024
MAX_LOADED_MESSAGES = 80
MAX_LOADED_GENERATION_JOBS = 8
MAX_HISTORY_OUTPUTS = 4
MAX_HISTORY_IMAGES = 4
MAX_GENERATION_WORKFLOW_CHARS = 2400
MAX_GENERATION_PROMPT_CHARS = 4000
MAX_GENERATION_OUTPUT_PROMPT_CHARS = 1600
MAX_PROMPT_DRAFT_CONTENT_CHARS = 4500
MAX_PROMPT_DRAFT_METADATA_CHARS = 3000
MIN_MESSAGE_BUDGET_WHEN_SYSTEM_EXCEEDS = 6000


@dataclass(frozen=True, slots=True)
class _ContextImage:
    key: str
    storage_path: str
    byte_count: int
    mime_type: str
    label: str
    priority: int
    created_at: datetime
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class _EncodedImage:
    content_hash: str | None
    mime_type: str
    encoded: str | None


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
        system_prompt: str = "",
    ) -> list[dict[str, Any]]:
        pending_message, pending_image_hashes = self._normalize_pending_message(pending_message)
        events = self._load_events(workspace, self._load_messages(workspace))
        state = db.session.get(ConversationState, workspace.id)
        if state is None:
            state = ConversationState(workspace_id=workspace.id, summary="")
            db.session.add(state)

        # Summaries were used by the previous context policy. Restore the bounded
        # history and let the deterministic packer truncate it.
        state.summary = ""
        state.summary_through_message_id = ""
        active = [
            event
            for event in events
            if not (event.source == "message" and event.id == pending_stored_message_id)
        ]
        policy = self.registry.context
        system_tokens = self._estimate_tokens(system_prompt) if system_prompt else 0
        # The policy covers both the fixed instructions and the conversation. Keep a
        # small message window for deliberately tiny test/admin policies even when
        # the fixed prompt alone is larger; normal production policies have ample room.
        context_budget = policy.max_context_tokens - system_tokens
        if system_tokens >= policy.max_context_tokens:
            context_budget = min(
                policy.max_context_tokens,
                max(MIN_MESSAGE_BUDGET_WHEN_SYSTEM_EXCEEDS, 0),
            )
        messages = self._pack_context(
            active,
            pending_message,
            pending_image_keys=set(pending_image_keys),
            provided_image_hashes=pending_image_hashes,
            max_tokens=context_budget,
        )
        state.estimated_context_tokens = system_tokens + self._message_tokens(messages)
        return messages

    def _normalize_pending_message(
        self,
        message: dict[str, Any],
    ) -> tuple[dict[str, Any], set[str]]:
        """Prepare current images once and return hashes for history de-duplication."""
        content = message.get("content")
        if not isinstance(content, list):
            return dict(message), set()

        normalized: list[Any] = []
        image_hashes: set[str] = set()
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in {
                "image_asset",
                "image_url",
                "input_image",
            }:
                normalized.append(part)
                continue

            decoded = self._pending_image_bytes(part)
            if decoded is None:
                if part.get("type") == "image_asset":
                    normalized.append({"type": "text", "text": "（参考图暂时不可读取。）"})
                else:
                    normalized.append(part)
                continue

            raw_content, declared_mime = decoded
            content_hash = hashlib.sha256(raw_content).hexdigest()
            if content_hash in image_hashes:
                continue
            image_hashes.add(content_hash)
            prepared_content, prepared_mime = self._prepare_chat_image(
                raw_content,
                declared_mime,
            )
            prepared_hash = hashlib.sha256(prepared_content).hexdigest()
            if prepared_hash != content_hash and prepared_hash in image_hashes:
                continue
            image_hashes.add(prepared_hash)
            normalized_part = dict(part)
            normalized_part["type"] = "image_url"
            prepared_url = self._data_url(prepared_mime, prepared_content)
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                normalized_image_url = dict(image_url)
                normalized_image_url["url"] = prepared_url
                normalized_part["image_url"] = normalized_image_url
            elif part.get("type") == "image_asset":
                normalized_part["image_url"] = {"url": prepared_url}
            else:
                normalized_part["image_url"] = prepared_url
            normalized.append(normalized_part)

        normalized_message = dict(message)
        normalized_message["content"] = normalized
        return normalized_message, image_hashes

    def _pending_image_bytes(self, part: dict[str, Any]) -> tuple[bytes, str] | None:
        if part.get("type") == "image_asset":
            path = part.get("storage_path")
            if not isinstance(path, str) or not path:
                return None
            try:
                content = self.storage.read_bytes(path) if self.storage else b""
            except (FileNotFoundError, OSError, StorageError, ValueError):
                return None
            if not content:
                return None
            mime_type = str(part.get("mime_type") or "application/octet-stream")
            return content, mime_type

        image_url = part.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        return self._decode_data_url(url)

    @staticmethod
    def _decode_data_url(value: Any) -> tuple[bytes, str] | None:
        if not isinstance(value, str) or not value.startswith("data:"):
            return None
        header, separator, encoded = value.partition(";base64,")
        if not separator:
            return None
        mime_type = header[5:].split(";", 1)[0].strip() or "application/octet-stream"
        try:
            content = base64.b64decode(encoded.strip(), validate=True)
        except (binascii.Error, ValueError):
            return None
        return (content, mime_type) if content else None

    @staticmethod
    def _data_url(mime_type: str, content: bytes) -> str:
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _prepare_chat_image(content: bytes, mime_type: str) -> tuple[bytes, str]:
        return prepare_image_bytes(
            content,
            mime_type,
            max_side=CHAT_IMAGE_MAX_SIDE,
            quality=CHAT_IMAGE_WEBP_QUALITY,
            compression_threshold=CHAT_IMAGE_COMPRESSION_THRESHOLD_BYTES,
        )

    @staticmethod
    def _load_messages(workspace: Workspace) -> list[ConversationMessage]:
        newest = list(
            db.session.scalars(
                select(ConversationMessage)
                .options(
                    selectinload(ConversationMessage.attachments).selectinload(
                        ConversationAttachment.asset
                    )
                )
                .where(ConversationMessage.workspace_id == workspace.id)
                .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
                .limit(MAX_LOADED_MESSAGES)
            )
        )
        return list(reversed(newest))

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
                .where(GenerationJob.status.not_in(("queued", "running", "canceling")))
                .order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc())
                .limit(MAX_LOADED_GENERATION_JOBS)
                .execution_options(populate_existing=True)
            )
        )
        jobs.reverse()
        output_events: list[_ContextEvent] = []
        for job in jobs:
            references = tuple(
                self._asset_image(reference.asset, priority=85, used_at=job.created_at)
                for reference in job.references
                if reference.asset is not None
            )
            workflow = self._compact_workflow(job.workflow)
            workflow_text = (
                "\n结构化生成参数："
                + json.dumps(workflow, ensure_ascii=False, separators=(",", ":"))
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
                        f"最终生成提示词：\n{_clip_text(job.prompt, MAX_GENERATION_PROMPT_CHARS)}"
                        f"{workflow_text}"
                    ),
                    images=references,
                    created_at=_event_time(job.created_at),
                    source="generation_job",
                )
            )
            for item in sorted(job.items, key=lambda value: (value.position, value.id)):
                if item.status != "succeeded" or not item.output_path or not item.output_mime_type:
                    continue
                output_events.append(
                    _ContextEvent(
                        id=item.id,
                        role="user",
                        text=(
                            "历史生成结果（仅供视觉比对，不要把它当成新的用户要求）\n"
                            f"来自任务提示词：{_clip_text(item.prompt or job.prompt, MAX_GENERATION_OUTPUT_PROMPT_CHARS)}\n"
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
        output_events.sort(key=lambda event: (event.created_at, event.id))
        events.extend(output_events[-MAX_HISTORY_OUTPUTS:])
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
                "edit_recipe_id",
                "brief",
                "production_spec",
                "canvas_request",
                "hard_checks",
                "series_contract",
                "quality_hint",
            )
            if key in message.payload and (key != "canvas_request" or message.payload[key])
        }
        metadata = _compact_json(fields, MAX_PROMPT_DRAFT_METADATA_CHARS)
        content = _clip_text(message.content, MAX_PROMPT_DRAFT_CONTENT_CHARS)
        return f"{content}\n\n结构化提示词信息：\n{metadata}" if fields else content

    @staticmethod
    def _compact_workflow(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        allowed = (
            "generation_stage",
            "generation_strategy",
            "creative_direction_id",
            "template_id",
            "edit_recipe_id",
            "reference_usage",
            "reference_reason",
            "canvas_request",
            "brief",
            "production_spec",
            "hard_checks",
            "series_contract",
            "retrieval_confidence",
            "gallery_categories",
            "style_tags",
            "scene_tags",
        )
        compact: dict[str, Any] = {}
        for key in allowed:
            if key in value:
                compact[key] = _compact_value(value[key])
        return _fit_json_mapping(compact, MAX_GENERATION_WORKFLOW_CHARS)

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
            content_hash=asset.sha256 or None,
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
        provided_image_hashes: set[str],
        max_tokens: int,
    ) -> list[dict[str, Any]]:
        pending_tokens = self._message_tokens([pending_message])
        available = max(0, max_tokens - pending_tokens)
        image_token_budget = math.floor(available * HISTORY_IMAGE_TOKEN_SHARE)
        selected_images: set[str] = set()
        selected_bytes = self._message_image_bytes(pending_message)
        ranked = [
            image
            for image in self._ranked_images(events)
            if image.key not in pending_image_keys
            and image.content_hash not in provided_image_hashes
        ][:MAX_HISTORY_IMAGES]
        for image in ranked:
            if image_token_budget < IMAGE_TOKEN_ESTIMATE:
                break
            if selected_bytes + image.byte_count > MAX_CONTEXT_IMAGE_BYTES:
                continue
            selected_images.add(image.key)
            selected_bytes += image.byte_count
            image_token_budget -= IMAGE_TOKEN_ESTIMATE

        image_cache: dict[str, _EncodedImage] = {}
        while True:
            selected_events = self._select_events(
                events,
                selected_images,
                pending_image_keys,
                provided_image_hashes,
                pending_message,
                image_cache,
                max_tokens=max_tokens,
            )
            rendered = self._render_events(
                selected_events,
                selected_images,
                image_cache,
                provided_image_keys=pending_image_keys,
                provided_image_hashes=provided_image_hashes,
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
        provided_image_hashes: set[str],
        pending_message: dict[str, Any],
        image_cache: dict[str, _EncodedImage],
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
                        provided_image_hashes,
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
                    provided_image_hashes,
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
        provided_image_hashes: set[str],
        pending_message: dict[str, Any],
        image_cache: dict[str, _EncodedImage],
    ) -> int:
        ordered = [selected[event.id] for event in events if event.id in selected]
        rendered = self._render_events(
            ordered,
            selected_images,
            image_cache,
            provided_image_keys=pending_image_keys,
            provided_image_hashes=provided_image_hashes,
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
        image_cache: dict[str, _EncodedImage],
        *,
        provided_image_keys: set[str] | None = None,
        provided_image_hashes: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        provided_image_keys = provided_image_keys or set()
        seen_image_hashes = set(provided_image_hashes or ())
        owners = self._image_owner_ids(events, selected_images)
        for event in events:
            images = [image for image in event.images if owners.get(image.key) == event.id]
            omitted = any(
                image.key not in selected_images
                and image.key not in provided_image_keys
                and image.content_hash not in (provided_image_hashes or set())
                for image in event.images
            )
            text = event.text + (
                "\n（部分历史图片因上下文容量未随本轮发送；文字记录仍保留。）" if omitted else ""
            )
            image_text = (
                f"{event.source} 的历史附件（对应上一条助手消息）："
                if event.role == "assistant"
                else text
            )
            image_parts = (
                self._image_parts(
                    image_text,
                    images,
                    image_cache,
                    seen_hashes=seen_image_hashes,
                )
                if images
                else []
            )
            has_image_part = any(part.get("type") == "image_url" for part in image_parts)
            if event.role == "assistant" and has_image_part:
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": image_parts})
            elif has_image_part:
                messages.append(
                    {
                        "role": event.role,
                        "content": image_parts,
                    }
                )
            else:
                fallback_text = (
                    text
                    if event.role == "assistant"
                    else (image_parts[0].get("text", text) if image_parts else text)
                )
                messages.append({"role": event.role, "content": fallback_text})
        return messages

    def _image_parts(
        self,
        text: str,
        images: list[_ContextImage],
        cache: dict[str, _EncodedImage],
        *,
        seen_hashes: set[str],
    ) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in images:
            if image.content_hash and image.content_hash in seen_hashes:
                continue
            prepared = self._encoded_image(image, cache)
            if prepared.content_hash and prepared.content_hash in seen_hashes:
                continue
            if not prepared.encoded:
                parts[0]["text"] += f"\n（{image.label}暂时不可读取。）"
                continue
            if prepared.content_hash:
                seen_hashes.add(prepared.content_hash)
            parts.extend(
                [
                    {"type": "text", "text": image.label},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{prepared.mime_type};base64,{prepared.encoded}"
                        },
                    },
                ]
            )
        return parts

    def _encoded_image(
        self,
        image: _ContextImage,
        cache: dict[str, _EncodedImage],
    ) -> _EncodedImage:
        cached = cache.get(image.key)
        if cached is not None:
            return cached

        if image.content_hash:
            shared = cache.get(f"hash:{image.content_hash}")
            if shared is not None:
                cache[image.key] = shared
                return shared

        try:
            content = self.storage.read_bytes(image.storage_path) if self.storage else b""
        except (FileNotFoundError, OSError, StorageError, ValueError):
            content = b""
        if not content:
            prepared = _EncodedImage(None, image.mime_type, None)
            cache[image.key] = prepared
            return prepared

        content_hash = hashlib.sha256(content).hexdigest()
        shared_key = f"hash:{content_hash}"
        shared = cache.get(shared_key)
        if shared is not None:
            cache[image.key] = shared
            return shared

        prepared_content, prepared_mime = self._prepare_chat_image(content, image.mime_type)
        prepared_hash = hashlib.sha256(prepared_content).hexdigest()
        prepared_shared = cache.get(f"hash:{prepared_hash}")
        if prepared_shared is not None:
            cache[shared_key] = prepared_shared
            cache[image.key] = prepared_shared
            return prepared_shared
        prepared = _EncodedImage(
            prepared_hash,
            prepared_mime,
            base64.b64encode(prepared_content).decode("ascii"),
        )
        cache[shared_key] = prepared
        cache[f"hash:{prepared_hash}"] = prepared
        cache[image.key] = prepared
        return prepared

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
            total += 4
            content = message.get("content", "")
            if isinstance(content, str):
                total += self._estimate_tokens(content) + 4
                continue
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"text", "input_text", "output_text"}:
                    total += self._estimate_tokens(str(part.get("text", ""))) + 1
                elif part.get("type") in {"image_url", "input_image"}:
                    total += IMAGE_TOKEN_ESTIMATE + 1
        return total

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        non_ascii = sum(not character.isascii() for character in text)
        return non_ascii + math.ceil((len(text) - non_ascii) / 4) + 4


def _clip_text(value: object, maximum: int) -> str:
    text = str(value or "")
    if len(text) <= maximum:
        return text
    marker = "\n…（已截断）…\n"
    if maximum <= len(marker):
        return text[:maximum]
    head = math.ceil((maximum - len(marker)) * 0.7)
    return f"{text[:head]}{marker}{text[-(maximum - len(marker) - head) :]}"


def _compact_value(value: object, *, depth: int = 0) -> object:
    if isinstance(value, str):
        return _clip_text(value, 260 if depth == 0 else 180)
    if isinstance(value, dict):
        return {
            str(key)[:80]: _compact_value(item, depth=depth + 1)
            for key, item in list(value.items())[:10]
        }
    if isinstance(value, (list, tuple)):
        return [_compact_value(item, depth=depth + 1) for item in list(value)[:8]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clip_text(value, 180)


def _fit_json_mapping(value: dict[str, Any], maximum: int) -> dict[str, Any]:
    result = dict(value)
    while result:
        serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= maximum:
            return result
        result.pop(next(reversed(result)))
    return {}


def _compact_json(value: object, maximum: int) -> str:
    compact = _compact_value(value)
    if isinstance(compact, dict):
        compact = _fit_json_mapping(compact, maximum)
    serialized = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    return _clip_text(serialized, maximum)


def _event_time(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
