"""AstrBot plugin: Image Injection Auditor.

Log how many images enter each LLM request and where they likely came from.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


PLUGIN_ID = "astrbot_plugin_image_injection_auditor"
PLUGIN_VERSION = "0.1.0"
PLUGIN_DESC = "审计每次 LLM 请求中的图片数量，并尽量标记来源插件"
PLUGIN_REPO = "https://github.com/Whereis-Alice/astrbot_plugin_image_injection_auditor"

STATE_EXTRA_KEY = f"{PLUGIN_ID}.state"
EARLY_PRIORITY = 10000
LATE_PRIORITY = -10000


@dataclass(frozen=True)
class ImageEntry:
    channel: str
    location: str
    ref: str
    fingerprint: str


@dataclass(frozen=True)
class MutationRecord:
    channel: str
    action: str
    source: str
    fingerprints: list[str]
    previews: list[str]
    when: float = field(default_factory=time.time)

    @property
    def count(self) -> int:
        return len(self.fingerprints)


@dataclass
class AuditState:
    request_id: str
    started_at: float
    initial_entries: list[ImageEntry]
    mutations: list[MutationRecord] = field(default_factory=list)


class TrackedImageList(list[Any]):
    """A list that records image additions without changing list behavior."""

    def __init__(
        self,
        values: Iterable[Any],
        *,
        channel: str,
        owner: "ImageInjectionAuditor",
        state: AuditState,
    ) -> None:
        super().__init__(values)
        self._channel = channel
        self._owner = owner
        self._state = state

    def append(self, item: Any) -> None:  # type: ignore[override]
        super().append(item)
        self._record("append", [item])

    def extend(self, values: Iterable[Any]) -> None:  # type: ignore[override]
        values_list = list(values)
        super().extend(values_list)
        self._record("extend", values_list)

    def insert(self, index: int, item: Any) -> None:  # type: ignore[override]
        super().insert(index, item)
        self._record("insert", [item])

    def __iadd__(self, values: Iterable[Any]) -> "TrackedImageList":
        self.extend(values)
        return self

    def __setitem__(self, index: Any, value: Any) -> None:
        if isinstance(index, slice):
            values = list(value)
            super().__setitem__(index, values)
        else:
            values = [value]
            super().__setitem__(index, value)
        self._record("setitem", values)

    def _record(self, action: str, values: list[Any]) -> None:
        self._owner.record_mutation(self._state, self._channel, action, values)


@register(PLUGIN_ID, "Whereis-Alice", PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class ImageInjectionAuditor(Star):
    """Audit images entering AstrBot provider requests."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict[str, Any] | None = None,
    ) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self._last_summary_by_umo: dict[str, str] = {}

    async def initialize(self) -> None:
        logger.info("[%s] plugin initialized", PLUGIN_ID)

    async def terminate(self) -> None:
        logger.info("[%s] plugin terminated", PLUGIN_ID)

    @filter.on_llm_request(priority=EARLY_PRIORITY)
    async def begin_request_audit(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return

        initial_entries = self._collect_provider_request_images(req)
        state = AuditState(
            request_id=f"{int(time.time() * 1000)}-{id(req)}",
            started_at=time.time(),
            initial_entries=initial_entries,
        )
        event.set_extra(STATE_EXTRA_KEY, state)
        setattr(req, STATE_EXTRA_KEY.replace(".", "_"), state)

        req.image_urls = TrackedImageList(
            getattr(req, "image_urls", []) or [],
            channel="request.image_urls",
            owner=self,
            state=state,
        )
        req.extra_user_content_parts = TrackedImageList(
            getattr(req, "extra_user_content_parts", []) or [],
            channel="request.extra_user_content_parts",
            owner=self,
            state=state,
        )

    @filter.on_llm_request(priority=LATE_PRIORITY)
    async def finish_request_audit(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return

        state = self._get_state(event, req)
        final_entries = self._collect_provider_request_images(req)
        if not final_entries and not self._cfg_bool("log_zero_image_requests", False):
            return

        attributed_sources = self._attribute_sources(final_entries, state)
        self._log_summary(
            event=event,
            req=req,
            entries=final_entries,
            sources=attributed_sources,
            state=state,
            phase="llm_request",
        )

    @filter.on_agent_begin(priority=LATE_PRIORITY)
    async def audit_agent_context(
        self,
        event: AstrMessageEvent,
        run_context: Any,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return
        if not self._cfg_bool("log_agent_context", True):
            return

        messages = getattr(run_context, "messages", []) or []
        entries = self._collect_message_images(messages, channel="agent.messages")
        if not entries and not self._cfg_bool("log_zero_image_requests", False):
            return

        sources = ["agent_context" for _ in entries]
        self._log_summary(
            event=event,
            req=None,
            entries=entries,
            sources=sources,
            state=self._get_state(event, None),
            phase="agent_begin_pre_compaction",
        )

    @filter.on_llm_tool_respond(priority=LATE_PRIORITY)
    async def audit_tool_response_images(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
        tool_result: Any,
    ) -> None:
        if not self._cfg_bool("enabled", True):
            return
        if not self._cfg_bool("log_tool_result_images", True):
            return

        entries = self._collect_tool_result_images(tool_result)
        if not entries:
            return

        tool_name = getattr(tool, "name", None) or getattr(tool, "__class__", type(tool)).__name__
        sources = [f"tool:{tool_name}" for _ in entries]
        self._log_summary(
            event=event,
            req=None,
            entries=entries,
            sources=sources,
            state=self._get_state(event, None),
            phase="llm_tool_result",
        )

    @filter.command("image_audit_status")
    async def image_audit_status(self, event: AstrMessageEvent):
        """Show the last image audit summary for this conversation."""
        summary = self._last_summary_by_umo.get(event.unified_msg_origin)
        if not summary:
            yield event.plain_result("ImageAudit: 当前会话还没有审计记录。")
            return
        yield event.plain_result(summary)

    def record_mutation(
        self,
        state: AuditState,
        channel: str,
        action: str,
        values: list[Any],
    ) -> None:
        refs = self._image_refs_from_list_values(channel, values)
        if not refs:
            return

        source = self._detect_calling_plugin()
        previews = [self._preview_ref(ref) for ref in refs]
        state.mutations.append(
            MutationRecord(
                channel=channel,
                action=action,
                source=source,
                fingerprints=[self._fingerprint(ref) for ref in refs],
                previews=previews,
            )
        )

    def _log_summary(
        self,
        *,
        event: AstrMessageEvent,
        req: ProviderRequest | None,
        entries: list[ImageEntry],
        sources: list[str],
        state: AuditState | None,
        phase: str,
    ) -> None:
        source_counts = Counter(sources)
        channel_counts = Counter(entry.channel for entry in entries)
        current_count = sum(
            1
            for entry in entries
            if entry.channel
            in {"request.image_urls", "request.extra_user_content_parts"}
        )
        context_count = sum(
            1
            for entry in entries
            if entry.channel in {"request.contexts", "agent.messages"}
        )
        total_count = len(entries)
        warn_limit = self._cfg_int("warn_image_limit", 30)
        is_over_limit = warn_limit > 0 and total_count >= warn_limit
        log_fn = logger.warning if is_over_limit else logger.info
        model = getattr(req, "model", None) if req else None
        session_id = getattr(req, "session_id", None) if req else None

        summary = (
            f"[ImageAudit] phase={phase} umo={event.unified_msg_origin} "
            f"session={session_id or '-'} model={model or '-'} "
            f"images_total={total_count} current={current_count} "
            f"context_or_history={context_count} warn_limit={warn_limit}"
        )
        log_fn(summary)
        log_fn("[ImageAudit] channels: %s", self._format_counter(channel_counts))
        log_fn("[ImageAudit] sources: %s", self._format_counter(source_counts))

        if state and state.mutations:
            mutation_lines = [
                (
                    f"{record.source} {record.channel}.{record.action} "
                    f"+{record.count} [{', '.join(record.previews[:3])}"
                    f"{'...' if len(record.previews) > 3 else ''}]"
                )
                for record in state.mutations
            ]
            log_fn("[ImageAudit] tracked mutations: %s", " | ".join(mutation_lines))

        if self._cfg_bool("include_details", True):
            detail_limit = self._cfg_int("detail_limit", 12)
            details = self._format_details(entries, sources, detail_limit)
            if details:
                log_fn("[ImageAudit] details: %s", details)

        self._last_summary_by_umo[event.unified_msg_origin] = (
            f"{summary}\n"
            f"channels: {self._format_counter(channel_counts)}\n"
            f"sources: {self._format_counter(source_counts)}"
        )
        self._trim_last_summaries()

    def _attribute_sources(
        self,
        entries: list[ImageEntry],
        state: AuditState | None,
    ) -> list[str]:
        if state is None:
            return [self._infer_source(entry) for entry in entries]

        baseline_sources: dict[str, deque[str]] = defaultdict(deque)
        for entry in state.initial_entries:
            source = (
                "conversation_history"
                if entry.channel == "request.contexts"
                else "astrbot_core_initial"
            )
            baseline_sources[entry.fingerprint].append(source)

        mutation_sources: dict[str, deque[str]] = defaultdict(deque)
        for record in state.mutations:
            for fingerprint in record.fingerprints:
                mutation_sources[fingerprint].append(record.source)

        sources: list[str] = []
        for entry in entries:
            if baseline_sources[entry.fingerprint]:
                sources.append(baseline_sources[entry.fingerprint].popleft())
            elif mutation_sources[entry.fingerprint]:
                source = mutation_sources[entry.fingerprint].popleft()
                inferred = self._infer_source(entry)
                if source == "unknown" and not inferred.startswith("unknown"):
                    source = inferred
                sources.append(source)
            else:
                sources.append(self._infer_source(entry))
        return sources

    def _collect_provider_request_images(self, req: ProviderRequest) -> list[ImageEntry]:
        entries: list[ImageEntry] = []
        for index, image_ref in enumerate(getattr(req, "image_urls", []) or []):
            ref = self._string_ref(image_ref)
            entries.append(
                self._entry("request.image_urls", f"image_urls[{index}]", ref)
            )

        for index, part in enumerate(
            getattr(req, "extra_user_content_parts", []) or []
        ):
            ref = self._extract_image_part_ref(part)
            if ref:
                entries.append(
                    self._entry(
                        "request.extra_user_content_parts",
                        f"extra_user_content_parts[{index}]",
                        ref,
                    )
                )

        if self._cfg_bool("include_context_images", True):
            contexts = getattr(req, "contexts", []) or []
            for index, context in enumerate(contexts):
                entries.extend(
                    self._collect_context_images(
                        context,
                        channel="request.contexts",
                        location=f"contexts[{index}]",
                    )
                )
        return entries

    def _collect_message_images(
        self,
        messages: Iterable[Any],
        *,
        channel: str,
    ) -> list[ImageEntry]:
        entries: list[ImageEntry] = []
        for index, message in enumerate(messages):
            entries.extend(
                self._collect_context_images(
                    message,
                    channel=channel,
                    location=f"messages[{index}]",
                )
            )
        return entries

    def _collect_context_images(
        self,
        context: Any,
        *,
        channel: str,
        location: str,
    ) -> list[ImageEntry]:
        content = None
        if isinstance(context, dict):
            content = context.get("content")
        else:
            content = getattr(context, "content", None)

        entries: list[ImageEntry] = []
        if isinstance(content, list):
            for index, part in enumerate(content):
                ref = self._extract_image_part_ref(part)
                if ref:
                    entries.append(
                        self._entry(channel, f"{location}.content[{index}]", ref)
                    )
        else:
            ref = self._extract_image_part_ref(context)
            if ref:
                entries.append(self._entry(channel, location, ref))
        return entries

    def _collect_tool_result_images(self, tool_result: Any) -> list[ImageEntry]:
        if tool_result is None:
            return []

        content = getattr(tool_result, "content", None)
        if content is None and isinstance(tool_result, dict):
            content = tool_result.get("content")
        if not isinstance(content, list):
            return []

        entries: list[ImageEntry] = []
        for index, part in enumerate(content):
            part_type = self._part_type(part)
            if part_type == "image_url":
                ref = self._extract_image_part_ref(part)
            elif part_type == "image":
                ref = self._extract_mcp_image_ref(part)
            else:
                ref = None
            if ref:
                entries.append(
                    self._entry("tool_result", f"tool_result.content[{index}]", ref)
                )
        return entries

    def _image_refs_from_list_values(
        self,
        channel: str,
        values: Iterable[Any],
    ) -> list[str]:
        refs: list[str] = []
        for value in values:
            if channel == "request.image_urls":
                ref = self._string_ref(value)
            else:
                ref = self._extract_image_part_ref(value)
            if ref:
                refs.append(ref)
        return refs

    def _entry(self, channel: str, location: str, ref: str) -> ImageEntry:
        return ImageEntry(
            channel=channel,
            location=location,
            ref=ref,
            fingerprint=self._fingerprint(ref),
        )

    @staticmethod
    def _string_ref(value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    def _extract_image_part_ref(self, part: Any) -> str | None:
        if self._part_type(part) != "image_url":
            return None

        image_url = None
        if isinstance(part, dict):
            image_url = part.get("image_url")
        else:
            image_url = getattr(part, "image_url", None)

        if isinstance(image_url, dict):
            value = image_url.get("url") or image_url.get("data")
        else:
            value = getattr(image_url, "url", None)
        if value is None and isinstance(image_url, str):
            value = image_url
        return self._string_ref(value) if value else None

    @staticmethod
    def _extract_mcp_image_ref(part: Any) -> str | None:
        if isinstance(part, dict):
            data = part.get("data")
            mime_type = part.get("mimeType") or part.get("mime_type") or "image"
        else:
            data = getattr(part, "data", None)
            mime_type = getattr(part, "mimeType", None) or getattr(
                part, "mime_type", "image"
            )
        if not data:
            return None
        return f"mcp-image:{mime_type};base64,{data}"

    @staticmethod
    def _part_type(part: Any) -> str | None:
        if isinstance(part, dict):
            value = part.get("type")
        else:
            value = getattr(part, "type", None)
        return value if isinstance(value, str) else None

    def _detect_calling_plugin(self) -> str:
        try:
            stack = inspect.stack(context=0)
        except Exception:
            return "unknown"

        for frame_info in stack[2:]:
            plugin_name = self._plugin_name_from_frame(frame_info)
            if plugin_name and plugin_name != PLUGIN_ID:
                return plugin_name
        return "unknown"

    @staticmethod
    def _plugin_name_from_frame(frame_info: inspect.FrameInfo) -> str | None:
        module_name = frame_info.frame.f_globals.get("__name__", "")
        if isinstance(module_name, str):
            for part in module_name.split("."):
                if part.startswith("astrbot_plugin_"):
                    return part

        try:
            parts = Path(frame_info.filename).parts
        except Exception:
            return None
        for part in reversed(parts):
            if part.startswith("astrbot_plugin_"):
                return part
        return None

    def _infer_source(self, entry: ImageEntry) -> str:
        ref_lower = entry.ref.lower()
        location_lower = entry.location.lower()
        combined = f"{ref_lower} {location_lower}"

        if entry.channel in {"request.contexts", "agent.messages"}:
            return "conversation_history_or_current_context"
        if "astrbot_plugin_video_vision_helper_" in combined:
            return "astrbot_plugin_video_vision_helper"
        if "astrbot_plugin_gif_frame_vision_" in combined:
            return "astrbot_plugin_gif_frame_vision"
        if ref_lower.startswith(("data:image/", "base64://", "mcp-image:")):
            return "unknown_inline_image"
        if entry.channel == "request.image_urls":
            return "unknown_image_urls"
        if entry.channel == "request.extra_user_content_parts":
            return "unknown_extra_user_content"
        return "unknown"

    def _fingerprint(self, ref: str) -> str:
        if ref.startswith(("data:", "base64://", "mcp-image:")):
            digest = hashlib.sha256(ref.encode("utf-8", "ignore")).hexdigest()[:16]
            return f"inline:{len(ref)}:{digest}"
        normalized = ref.strip()
        if not self._looks_like_url(normalized):
            normalized = os.path.normcase(os.path.normpath(normalized))
        return normalized

    @staticmethod
    def _looks_like_url(value: str) -> bool:
        lowered = value.lower()
        return lowered.startswith(("http://", "https://", "file://"))

    def _preview_ref(self, ref: str) -> str:
        if ref.startswith(("data:", "base64://", "mcp-image:")):
            digest = hashlib.sha256(ref.encode("utf-8", "ignore")).hexdigest()[:12]
            prefix = ref.split(",", 1)[0]
            return f"{prefix},len={len(ref)},sha256={digest}"
        limit = self._cfg_int("ref_preview_chars", 96)
        if len(ref) <= limit:
            return ref
        head = max(16, limit // 2)
        tail = max(16, limit - head - 3)
        return f"{ref[:head]}...{ref[-tail:]}"

    @staticmethod
    def _format_counter(counter: Counter[str]) -> str:
        if not counter:
            return "-"
        return ", ".join(f"{name}={count}" for name, count in counter.most_common())

    def _format_details(
        self,
        entries: list[ImageEntry],
        sources: list[str],
        limit: int,
    ) -> str:
        chunks: list[str] = []
        for index, (entry, source) in enumerate(zip(entries, sources)):
            if index >= limit:
                chunks.append(f"...(+{len(entries) - limit} more)")
                break
            chunks.append(
                f"#{index + 1} {source} {entry.location} {self._preview_ref(entry.ref)}"
            )
        return " | ".join(chunks)

    def _get_state(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest | None,
    ) -> AuditState | None:
        if req is not None:
            value = getattr(req, STATE_EXTRA_KEY.replace(".", "_"), None)
            if isinstance(value, AuditState):
                return value
        try:
            value = event.get_extra(STATE_EXTRA_KEY)
        except Exception:
            value = None
        return value if isinstance(value, AuditState) else None

    def _trim_last_summaries(self) -> None:
        max_items = self._cfg_int("remember_last_sessions", 50)
        if max_items <= 0:
            self._last_summary_by_umo.clear()
            return
        while len(self._last_summary_by_umo) > max_items:
            first_key = next(iter(self._last_summary_by_umo))
            self._last_summary_by_umo.pop(first_key, None)

    def _cfg(self, key: str, default: Any) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return default

    def _cfg_int(self, key: str, default: int) -> int:
        value = self._cfg(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
