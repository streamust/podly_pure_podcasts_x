"""LLM-based word-boundary refiner.

Note: We intentionally share some call-setup patterns with BoundaryRefiner.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from jinja2 import Template

from podcast_processor.llm_model_call_utils import (
    apply_service_tier,
    call_litellm_with_tier_retry,
    extract_litellm_content,
    extract_litellm_finish_reason,
    extract_litellm_usage,
    record_service_tier_on_model_call,
    render_prompt_and_upsert_model_call,
    try_update_model_call,
)
from podcast_processor.token_rate_limiter import TokenRateLimiter
from shared.config import Config
from shared.llm_utils import normalize_completion_args_for_model

# Keep the same internal bounds as the existing BoundaryRefiner.
MAX_START_EXTENSION_SECONDS = 30.0
MAX_END_EXTENSION_SECONDS = 15.0

# SHADOW-MODE shrink-cap experiment (2026-10-02; J-F approved 7-day trial).
# Log-only while SHADOW_CAP_ENFORCE is False: when the LLM shrinks a detected
# ad block by more than SHADOW_CAP_SECONDS on either side, record what the
# clamp WOULD have done. The returned refinement is never modified in
# shadow mode. Flip SHADOW_CAP_ENFORCE to True only after the trial review.
SHADOW_CAP_SECONDS = 60.0
SHADOW_CAP_LOG_PATH = os.environ.get(
    "SHADOW_CAP_LOG_PATH",
    "/home/hatch/workspace/goals/podly-podcast-hosting-setup/hidden_files/shadow_cap_log.jsonl",
)
SHADOW_CAP_ENFORCE = False


@dataclass
class WordBoundaryRefinement:
    refined_start: float
    refined_end: float
    start_adjustment_reason: str
    end_adjustment_reason: str


class WordBoundaryRefiner:
    """Refine ad boundaries within a segment.

    When saved word timestamps are available, this refiner uses them for exact
    intra-segment timing. Otherwise it falls back to the existing segment-level
    heuristics.
    """

    def __init__(
        self,
        config: Config,
        logger: logging.Logger | None = None,
        token_limiter: TokenRateLimiter | None = None,
    ):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.token_limiter = token_limiter
        self.template = self._load_template()

    def _load_template(self) -> Template:
        path = (
            Path(__file__).resolve().parent.parent  # project src root
            / "word_boundary_refinement_prompt.jinja"
        )
        if path.exists():
            return Template(path.read_text())
        return Template(
            """Find start/end phrases for the ad break.
Ad: {{ad_start}}s-{{ad_end}}s
{% for seg in context_segments %}[seq={{seg.sequence_num}} start={{seg.start_time}} end={{seg.end_time}}] {{seg.text}}
{% endfor %}
Return only one JSON object (no markdown/code fences, no analysis text) with:
{"refined_start_segment_seq": 0, "refined_start_phrase": "", "refined_end_segment_seq": 0, "refined_end_phrase": "", "start_adjustment_reason": "", "end_adjustment_reason": ""}
"""
        )

    def refine(
        self,
        ad_start: float,
        ad_end: float,
        confidence: float,
        all_segments: list[dict[str, Any]],
        *,
        post_id: int | None = None,
        post_guid: str | None = None,
        first_seq_num: int | None = None,
        last_seq_num: int | None = None,
    ) -> WordBoundaryRefinement:
        context = self._get_context(
            ad_start,
            ad_end,
            all_segments,
            first_seq_num=first_seq_num,
            last_seq_num=last_seq_num,
            post_guid=post_guid,
        )

        prompt, model_call_id = render_prompt_and_upsert_model_call(
            template=self.template,
            ad_start=ad_start,
            ad_end=ad_end,
            confidence=confidence,
            context_segments=context,
            post_id=post_id,
            first_seq_num=first_seq_num,
            last_seq_num=last_seq_num,
            model_name=self.config.llm_model,
            logger=self.logger,
            log_prefix="Word boundary refine",
        )

        raw_response: str | None = None

        try:
            completion_args: dict[str, Any] = {
                "model": self.config.llm_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 4096,
                "timeout": self.config.openai_timeout,
                "api_key": self.config.llm_api_key,
                "base_url": self.config.openai_base_url,
            }
            normalize_completion_args_for_model(completion_args)
            apply_service_tier(completion_args, self.config)
            record_service_tier_on_model_call(
                model_call_id,
                completion_args,
                logger=self.logger,
                log_prefix="Word boundary refine",
            )
            # Pace this call through the shared token bucket. The refiner's
            # calls draw on the same provider per-minute token budget as the
            # ad classifier's, but would otherwise fire unpaced right after
            # classification had just filled that window — the provider's TPM
            # limit (input+output tokens combined) then 429s these calls and
            # they fail permanently. Waiting here trades a short delay for a
            # call that actually goes through.
            if self.token_limiter is not None:
                self.token_limiter.wait_if_needed(
                    completion_args["messages"], self.config.llm_model
                )
            response = call_litellm_with_tier_retry(
                completion_args,
                config=self.config,
                logger=self.logger,
                model_call_id=model_call_id,
            )
            effective_service_tier = completion_args.get("service_tier")

            content = extract_litellm_content(response)
            finish_reason = extract_litellm_finish_reason(response)
            usage = extract_litellm_usage(response)
            raw_response = content

            self.logger.debug(
                "Word boundary refine finish_reason=%s",
                finish_reason or "unknown",
                extra={
                    "content_preview": (content or "")[:200],
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "cached_prompt_tokens": usage.get("cached_prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                },
            )

            self._update_model_call(
                model_call_id,
                status="received_response",
                response=raw_response,
                error_message=None,
                usage=usage,
                service_tier=effective_service_tier,
            )

            rust_result = self._try_rust_refine_from_llm(
                content=content,
                ad_start=ad_start,
                ad_end=ad_end,
                post_guid=post_guid,
                first_seq_num=first_seq_num,
                last_seq_num=last_seq_num,
            )

            if rust_result is not None and rust_result.get("parse_status") == "failed":
                parse_failure_reason = self._parse_failure_reason(finish_reason)
                self.logger.warning(
                    "Word boundary refine: no parseable JSON (%s); falling back to original start",
                    parse_failure_reason,
                    extra={
                        "finish_reason": finish_reason,
                        "content_preview": (content or "")[:200],
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                    },
                )
                self._update_model_call(
                    model_call_id,
                    status="success_heuristic",
                    response=raw_response,
                    error_message=f"parse_failed:{parse_failure_reason}",
                )
                return self._fallback(ad_start, ad_end)

            if rust_result is not None:
                if rust_result.get("parse_status") == "salvaged":
                    self.logger.warning(
                        "Word boundary refine: recovered partial fields via Rust salvage",
                        extra={
                            "rust_path": "word_boundary_refine_from_llm",
                            "content_preview": (content or "")[:200],
                        },
                    )
                refined_start = float(rust_result["refined_start"])
                start_changed = bool(rust_result["start_changed"])
                start_err = rust_result.get("start_error") or None
                refined_end = float(rust_result["refined_end"])
                end_changed = bool(rust_result["end_changed"])
                end_err = rust_result.get("end_error") or None
                start_reason = str(rust_result.get("start_reason") or "")
                end_reason = str(rust_result.get("end_reason") or "")
            else:
                parsed = self._parse_json(content)
                if not parsed:
                    parse_failure_reason = self._parse_failure_reason(finish_reason)
                    self.logger.warning(
                        "Word boundary refine: no parseable JSON (%s); falling back to original start",
                        parse_failure_reason,
                        extra={
                            "finish_reason": finish_reason,
                            "content_preview": (content or "")[:200],
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                            "total_tokens": usage.get("total_tokens"),
                        },
                    )
                    self._update_model_call(
                        model_call_id,
                        status="success_heuristic",
                        response=raw_response,
                        error_message=f"parse_failed:{parse_failure_reason}",
                    )
                    return self._fallback(ad_start, ad_end)

                payload = self._extract_payload(parsed)
                refined_start, start_changed, start_reason, start_err = (
                    self._refine_start(
                        ad_start=ad_start,
                        all_segments=all_segments,
                        context_segments=context,
                        start_segment_seq=payload["start_segment_seq"],
                        start_phrase=payload["start_phrase"],
                        start_word=payload["start_word"],
                        start_occurrence=payload["start_occurrence"],
                        start_word_index=payload["start_word_index"],
                        start_reason=payload["start_reason"],
                    )
                )
                refined_end, end_changed, end_reason, end_err = self._refine_end(
                    ad_end=ad_end,
                    all_segments=all_segments,
                    context_segments=context,
                    end_segment_seq=payload["end_segment_seq"],
                    end_phrase=payload["end_phrase"],
                    end_reason=payload["end_reason"],
                )

            partial_errors = [e for e in [start_err, end_err] if e]

            if refined_start >= float(ad_end):
                refined_start = float(ad_start)
                start_changed = False
                start_reason = ""
                partial_errors.append("start_out_of_window")

            if refined_end <= float(ad_start):
                refined_end = float(ad_end)
                end_changed = False
                end_reason = ""
                partial_errors.append("end_out_of_window")

            # If caller didn't provide reasons, default to unchanged for untouched sides.
            start_reason = self._default_reason(start_reason, changed=start_changed)
            end_reason = self._default_reason(end_reason, changed=end_changed)

            # Guardrail: never return an invalid window. If only one side caused
            # the issue, keep the valid side and revert the other to the
            # original detection instead of failing the whole refinement.
            if refined_end <= refined_start:
                if start_changed and not end_changed:
                    refined_start = float(ad_start)
                    start_changed = False
                    start_reason = ""
                    partial_errors.append("start_invalid_window")
                elif end_changed and not start_changed:
                    refined_end = float(ad_end)
                    end_changed = False
                    end_reason = ""
                    partial_errors.append("end_invalid_window")

            if refined_end <= refined_start:
                self._update_model_call(
                    model_call_id,
                    status="success_heuristic",
                    response=raw_response,
                    error_message="invalid_refined_window",
                )
                return self._fallback(ad_start, ad_end)

            self._update_model_call(
                model_call_id,
                status=self._result_status(start_changed, end_changed, partial_errors),
                response=raw_response,
                error_message=(",".join(partial_errors) if partial_errors else None),
                usage=usage,
                service_tier=effective_service_tier,
            )

            result = WordBoundaryRefinement(
                refined_start=refined_start,
                refined_end=refined_end,
                start_adjustment_reason=start_reason,
                end_adjustment_reason=end_reason,
            )

            # SHADOW-MODE shrink cap (trial): observe, log, and — only if
            # SHADOW_CAP_ENFORCE is True — clamp. In shadow mode the result
            # above is returned untouched.
            shadow_record = self._shadow_cap_record(
                ad_start=ad_start,
                ad_end=ad_end,
                refined_start=refined_start,
                refined_end=refined_end,
                post_id=post_id,
                post_guid=post_guid,
                start_reason=start_reason,
                end_reason=end_reason,
            )
            if shadow_record is not None:
                self._write_shadow_cap_log(shadow_record)
                if SHADOW_CAP_ENFORCE:
                    if shadow_record["start_shrink_s"] > SHADOW_CAP_SECONDS:
                        result = WordBoundaryRefinement(
                            refined_start=shadow_record["clamped_start"],
                            refined_end=result.refined_end,
                            start_adjustment_reason=(
                                result.start_adjustment_reason + " [shrink-capped]"
                            ).strip(),
                            end_adjustment_reason=result.end_adjustment_reason,
                        )
                    if shadow_record["end_shrink_s"] > SHADOW_CAP_SECONDS:
                        result = WordBoundaryRefinement(
                            refined_start=result.refined_start,
                            refined_end=shadow_record["clamped_end"],
                            start_adjustment_reason=result.start_adjustment_reason,
                            end_adjustment_reason=(
                                result.end_adjustment_reason + " [shrink-capped]"
                            ).strip(),
                        )

            self._update_model_call(
                model_call_id,
                status="success",
                response=raw_response,
                error_message=None,
                usage=usage,
                service_tier=effective_service_tier,
            )
            return result

        except Exception as exc:  # noqa: BLE001
            self._update_model_call(
                model_call_id,
                status="failed_permanent",
                response=raw_response,
                error_message=str(exc),
            )
            self.logger.warning("Word boundary refine failed: %s", exc)
            return self._fallback(ad_start, ad_end)

    def _fallback(self, ad_start: float, ad_end: float) -> WordBoundaryRefinement:
        return WordBoundaryRefinement(
            refined_start=ad_start,
            refined_end=ad_end,
            start_adjustment_reason="heuristic_fallback",
            end_adjustment_reason="unchanged",
        )

    @staticmethod
    def _shadow_cap_record(
        *,
        ad_start: float,
        ad_end: float,
        refined_start: float,
        refined_end: float,
        post_id: int | None,
        post_guid: str | None,
        start_reason: str,
        end_reason: str,
    ) -> dict[str, Any] | None:
        """Return a shadow-cap log record when the shrink exceeds the cap.

        Pure function (no I/O, never modifies its inputs): decides whether
        the SHADOW_CAP_SECONDS shrink cap WOULD have engaged and what the
        clamped boundaries would have been.
        """
        start_shrink = float(refined_start) - float(ad_start)
        end_shrink = float(ad_end) - float(refined_end)
        if start_shrink <= SHADOW_CAP_SECONDS and end_shrink <= SHADOW_CAP_SECONDS:
            return None
        return {
            "ts": datetime.now(UTC).isoformat(),
            "event": "shadow_cap_would_clamp",
            "cap_seconds": SHADOW_CAP_SECONDS,
            "enforced": SHADOW_CAP_ENFORCE,
            "post_id": post_id,
            "post_guid": post_guid,
            "ad_start": float(ad_start),
            "ad_end": float(ad_end),
            "refined_start": float(refined_start),
            "refined_end": float(refined_end),
            "start_shrink_s": round(start_shrink, 2),
            "end_shrink_s": round(end_shrink, 2),
            "clamped_start": (
                float(ad_start) + SHADOW_CAP_SECONDS
                if start_shrink > SHADOW_CAP_SECONDS
                else float(refined_start)
            ),
            "clamped_end": (
                float(ad_end) - SHADOW_CAP_SECONDS
                if end_shrink > SHADOW_CAP_SECONDS
                else float(refined_end)
            ),
            "start_reason": start_reason,
            "end_reason": end_reason,
        }

    def _write_shadow_cap_log(self, record: dict[str, Any]) -> None:
        """Append a shadow-cap record as JSONL. Never raises; never touches the DB."""
        try:
            path = Path(SHADOW_CAP_LOG_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception:  # noqa: BLE001
            self.logger.warning("Shadow cap: failed to write log record", exc_info=True)

    def _constrain_start(self, estimated_start: float, orig_start: float) -> float:
        return max(estimated_start, orig_start - MAX_START_EXTENSION_SECONDS)

    def _constrain_end(self, estimated_end: float, orig_end: float) -> float:
        # Allow slight forward extension (for late boundary) but cap it.
        return min(estimated_end, orig_end + MAX_END_EXTENSION_SECONDS)

    def _parse_json(self, content: str) -> dict[str, Any] | None:
        for candidate in self._json_parse_candidates(content):
            parsed = self._parse_json_candidate(candidate)
            if parsed is not None:
                return parsed
        partial = self._parse_partial_json_fields(content)
        if partial:
            return partial
        return None

    @staticmethod
    def _parse_partial_json_fields(content: str) -> dict[str, Any]:
        text = str(content or "")
        parsed: dict[str, Any] = {}

        def _extract_int_or_null(key: str) -> int | None | None:
            match = re.search(
                rf'"{re.escape(key)}"\s*:\s*(null|-?\d+)',
                text,
                flags=re.IGNORECASE,
            )
            if not match:
                return None
            value = match.group(1)
            if value is None:
                return None
            if value.lower() == "null":
                return None
            try:
                return int(value)
            except Exception:  # noqa: BLE001
                return None

        def _extract_string(key: str) -> str | None:
            match = re.search(
                rf'"{re.escape(key)}"\s*:\s*"([^"]*)"',
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not match:
                return None
            value = (match.group(1) or "").strip()
            return value or None

        start_seq = _extract_int_or_null("refined_start_segment_seq")
        end_seq = _extract_int_or_null("refined_end_segment_seq")
        start_phrase = _extract_string("refined_start_phrase")
        end_phrase = _extract_string("refined_end_phrase")
        start_reason = _extract_string("start_adjustment_reason")
        end_reason = _extract_string("end_adjustment_reason")

        if start_seq is not None or re.search(
            r'"refined_start_segment_seq"\s*:\s*null', text, flags=re.IGNORECASE
        ):
            parsed["refined_start_segment_seq"] = start_seq
        if end_seq is not None or re.search(
            r'"refined_end_segment_seq"\s*:\s*null', text, flags=re.IGNORECASE
        ):
            parsed["refined_end_segment_seq"] = end_seq
        if start_phrase is not None:
            parsed["refined_start_phrase"] = start_phrase
        if end_phrase is not None:
            parsed["refined_end_phrase"] = end_phrase
        if start_reason is not None:
            parsed["start_adjustment_reason"] = start_reason
        if end_reason is not None:
            parsed["end_adjustment_reason"] = end_reason

        return parsed

    def _json_parse_candidates(self, content: str) -> list[str]:
        text = (content or "").strip()
        if not text:
            return []

        candidates: list[str] = [text]

        for match in re.finditer(
            r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL
        ):
            block = str(match.group(1) or "").strip()
            if block:
                candidates.append(block)

        unfenced = re.sub(r"```(?:json)?|```", "", text, flags=re.IGNORECASE).strip()
        if unfenced:
            candidates.append(unfenced)

        expanded: list[str] = []
        for candidate in candidates:
            expanded.append(candidate)
            expanded.extend(self._extract_json_objects(candidate))

        return self._dedupe_preserve_order(expanded)

    @staticmethod
    def _dedupe_preserve_order(values: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            deduped.append(normalized)
            seen.add(normalized)
        return deduped

    def _parse_json_candidate(self, candidate: str) -> dict[str, Any] | None:
        attempts = [candidate]
        repaired = self._repair_truncated_json(candidate)
        if repaired and repaired != candidate:
            attempts.append(repaired)

        for attempt in attempts:
            try:
                loaded = json.loads(attempt)
                if isinstance(loaded, dict):
                    return cast(dict[str, Any], loaded)
            except Exception:  # noqa: BLE001
                continue

        return None

    @staticmethod
    def _repair_truncated_json(candidate: str) -> str | None:
        text = (candidate or "").strip()
        if not text:
            return None

        start_idx = text.find("{")
        if start_idx < 0:
            return None

        repaired = text[start_idx:]
        repaired = re.sub(
            r"```(?:json)?|```", "", repaired, flags=re.IGNORECASE
        ).strip()
        repaired = repaired.rstrip(",")

        # Drop an obviously incomplete trailing key/value pair if present.
        repaired = re.sub(r',\s*"[^"]*$', "", repaired)
        repaired = re.sub(r',\s*"[^"]*"$', "", repaired)
        repaired = re.sub(r',\s*"[^"]*"\s*:\s*$', "", repaired)
        repaired = re.sub(r',\s*"[^"]*"\s*:\s*"[^"]*$', "", repaired)

        open_brackets = repaired.count("[")
        close_brackets = repaired.count("]")
        if close_brackets < open_brackets:
            repaired += "]" * (open_brackets - close_brackets)

        open_braces = repaired.count("{")
        close_braces = repaired.count("}")
        if close_braces < open_braces:
            repaired += "}" * (open_braces - close_braces)

        return repaired or None

    @staticmethod
    def _extract_json_objects(text: str) -> list[str]:
        objects: list[str] = []
        depth = 0
        start_idx: int | None = None
        in_string = False
        escaped = False

        for idx, char in enumerate(text):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue

            if char == "{":
                if depth == 0:
                    start_idx = idx
                depth += 1
                continue

            if char != "}" or depth == 0:
                continue

            depth -= 1
            if depth == 0 and start_idx is not None:
                objects.append(text[start_idx : idx + 1])
                start_idx = None

        return objects

    @staticmethod
    def _parse_failure_reason(finish_reason: str | None) -> str:
        if str(finish_reason or "").lower() == "length":
            return "length"
        return "format"

    @staticmethod
    def _has_text(value: Any) -> bool:
        if value is None:
            return False
        try:
            return bool(str(value).strip())
        except Exception:  # noqa: BLE001
            return False

    def _extract_payload(self, parsed: dict[str, Any]) -> dict[str, Any]:
        occurrence = parsed.get("occurrence")
        if occurrence is None:
            occurrence = parsed.get("occurance")

        return {
            "start_segment_seq": parsed.get("refined_start_segment_seq"),
            "start_phrase": parsed.get("refined_start_phrase"),
            "end_segment_seq": parsed.get("refined_end_segment_seq"),
            "end_phrase": parsed.get("refined_end_phrase"),
            "start_word": parsed.get("refined_start_word"),
            "start_occurrence": occurrence,
            "start_word_index": parsed.get("refined_start_word_index"),
            "start_reason": str(parsed.get("start_adjustment_reason") or ""),
            "end_reason": str(parsed.get("end_adjustment_reason") or ""),
        }

    @staticmethod
    def _default_reason(reason: str, *, changed: bool) -> str:
        if reason:
            return reason
        return "refined" if changed else "unchanged"

    @staticmethod
    def _result_status(
        start_changed: bool, end_changed: bool, partial_errors: list[str]
    ) -> str:
        if partial_errors and not start_changed and not end_changed:
            return "success_heuristic"
        return "success"

    def _refine_start(
        self,
        *,
        ad_start: float,
        all_segments: list[dict[str, Any]],
        context_segments: list[dict[str, Any]],
        start_segment_seq: Any,
        start_phrase: Any,
        start_word: Any,
        start_occurrence: Any,
        start_word_index: Any,
        start_reason: str,
    ) -> tuple[float, bool, str, str | None]:
        if self._has_text(start_phrase):
            estimated_start = self._estimate_phrase_time(
                all_segments=all_segments,
                context_segments=context_segments,
                preferred_segment_seq=start_segment_seq,
                phrase=start_phrase,
                direction="start",
            )
            if estimated_start is None:
                return float(ad_start), False, start_reason, "start_phrase_not_found"
            return (
                self._constrain_start(float(estimated_start), ad_start),
                True,
                start_reason,
                None,
            )

        segment_start = self._estimate_segment_boundary_time(
            all_segments=all_segments,
            segment_seq=start_segment_seq,
            boundary="start",
        )
        if segment_start is not None:
            constrained = self._constrain_start(float(segment_start), ad_start)
            return (
                constrained,
                constrained != float(ad_start),
                start_reason,
                None,
            )

        if self._has_text(start_word) or start_word_index is not None:
            estimated_start = self._estimate_word_time(
                all_segments=all_segments,
                segment_seq=start_segment_seq,
                word=start_word,
                occurrence=start_occurrence,
                word_index=start_word_index,
            )
            return (
                self._constrain_start(float(estimated_start), ad_start),
                True,
                start_reason,
                None,
            )

        return float(ad_start), False, (start_reason or "unchanged"), None

    def _refine_end(
        self,
        *,
        ad_end: float,
        all_segments: list[dict[str, Any]],
        context_segments: list[dict[str, Any]],
        end_segment_seq: Any,
        end_phrase: Any,
        end_reason: str,
    ) -> tuple[float, bool, str, str | None]:
        if not self._has_text(end_phrase):
            segment_end = self._estimate_segment_boundary_time(
                all_segments=all_segments,
                segment_seq=end_segment_seq,
                boundary="end",
            )
            if segment_end is not None:
                constrained = self._constrain_end(float(segment_end), ad_end)
                return (
                    constrained,
                    constrained != float(ad_end),
                    (end_reason or "refined"),
                    None,
                )
            return float(ad_end), False, (end_reason or "unchanged"), None

        estimated_end = self._estimate_phrase_time(
            all_segments=all_segments,
            context_segments=context_segments,
            preferred_segment_seq=end_segment_seq,
            phrase=end_phrase,
            direction="end",
        )
        if estimated_end is None:
            return float(ad_end), False, end_reason, "end_phrase_not_found"

        return (
            self._constrain_end(float(estimated_end), ad_end),
            True,
            end_reason,
            None,
        )

    def _get_context(
        self,
        ad_start: float,
        ad_end: float,
        all_segments: list[dict[str, Any]],
        *,
        first_seq_num: int | None,
        last_seq_num: int | None,
        post_guid: str | None = None,
    ) -> list[dict[str, Any]]:
        rust_selected = self._try_rust_get_context(
            ad_start,
            ad_end,
            all_segments,
            first_seq_num=first_seq_num,
            last_seq_num=last_seq_num,
            post_guid=post_guid,
        )
        if rust_selected is not None:
            return rust_selected

        selected = self._context_by_seq_window(
            all_segments,
            first_seq_num=first_seq_num,
            last_seq_num=last_seq_num,
        )
        if selected:
            return selected

        return self._context_by_time_overlap(ad_start, ad_end, all_segments)

    def _try_rust_get_context(
        self,
        ad_start: float,
        ad_end: float,
        all_segments: list[dict[str, Any]],
        *,
        first_seq_num: int | None,
        last_seq_num: int | None,
        post_guid: str | None,
    ) -> list[dict[str, Any]] | None:
        """Run the Rust wb-context selector and re-attach Python-side `words`.

        Returns None when the Rust path is disabled, the caller didn't supply a
        post_guid, or the sidecar fails — letting `_get_context` fall back to
        the in-Python selectors.
        """
        if not post_guid:
            return None

        from shared.processing_paths import get_instance_dir
        from shared.rust_sidecar import rust_word_boundary_enabled, try_wb_context

        if not rust_word_boundary_enabled():
            return None

        try:
            db_path = get_instance_dir() / "sqlite3.db"
            rust_segments = try_wb_context(
                db_path=db_path,
                post_guid=post_guid,
                ad_start=float(ad_start),
                ad_end=float(ad_end),
                first_seq=first_seq_num,
                last_seq=last_seq_num,
            )
        except Exception:
            self.logger.exception(
                "Rust wb-context bootstrap failed; falling back to Python"
            )
            return None

        if rust_segments is None:
            return None

        # Re-attach the in-memory segments (with their `words` arrays) by
        # sequence_num so the downstream phrase-time helpers — which still run
        # in Python — see the same shape they would have built locally.
        by_seq: dict[int, dict[str, Any]] = {}
        for seg in all_segments:
            try:
                by_seq[int(seg.get("sequence_num", -1))] = seg
            except Exception:  # noqa: BLE001
                continue

        rehydrated: list[dict[str, Any]] = []
        for item in rust_segments:
            try:
                seq = int(item.get("sequence_num", -1))
            except Exception:  # noqa: BLE001
                continue
            original = by_seq.get(seq)
            rehydrated.append(original if original is not None else item)

        self.logger.info(
            "Word-boundary context selected via Rust sidecar",
            extra={
                "rust_path": "word_boundary_context",
                "selected_count": len(rehydrated),
            },
        )
        return rehydrated

    def _try_rust_refine_from_llm(
        self,
        *,
        content: str,
        ad_start: float,
        ad_end: float,
        post_guid: str | None,
        first_seq_num: int | None,
        last_seq_num: int | None,
    ) -> dict[str, Any] | None:
        """Bundle parse + context + resolve into a single Rust subprocess call.

        Returns the sidecar payload (with `parse_status` set to `"ok"`,
        `"salvaged"`, or `"failed"`) on success, or `None` to signal that the
        caller should fall back to the Python chain (flag off, no post_guid,
        sidecar error, or unexpected payload).
        """
        if not post_guid:
            return None

        from shared.processing_paths import get_instance_dir
        from shared.rust_sidecar import (
            rust_word_boundary_enabled,
            try_wb_refine_from_llm,
        )

        if not rust_word_boundary_enabled():
            return None

        try:
            db_path = get_instance_dir() / "sqlite3.db"
            result = try_wb_refine_from_llm(
                db_path=db_path,
                post_guid=post_guid,
                orig_ad_start=float(ad_start),
                orig_ad_end=float(ad_end),
                first_seq=first_seq_num,
                last_seq=last_seq_num,
                raw_content=content or "",
            )
        except Exception:
            self.logger.exception(
                "Rust wb-refine-from-llm bootstrap failed; falling back to Python"
            )
            return None

        if result is None:
            return None

        self.logger.info(
            "Word-boundary refined via bundled Rust sidecar",
            extra={
                "rust_path": "word_boundary_refine_from_llm",
                "parse_status": result.get("parse_status"),
                "start_changed": bool(result.get("start_changed")),
                "end_changed": bool(result.get("end_changed")),
            },
        )

        return result

    def _context_by_seq_window(
        self,
        all_segments: list[dict[str, Any]],
        *,
        first_seq_num: int | None,
        last_seq_num: int | None,
    ) -> list[dict[str, Any]]:
        if first_seq_num is None or last_seq_num is None or not all_segments:
            return []

        seq_values: list[int] = []
        for segment in all_segments:
            try:
                seq_values.append(int(segment.get("sequence_num", -1)))
            except Exception:  # noqa: BLE001
                continue
        if not seq_values:
            return []

        min_seq = min(seq_values)
        max_seq = max(seq_values)
        start_seq = max(min_seq, int(first_seq_num) - 8)
        end_seq = min(max_seq, int(last_seq_num) + 8)

        selected: list[dict[str, Any]] = []
        for segment in all_segments:
            try:
                seq = int(segment.get("sequence_num", -1))
            except Exception:  # noqa: BLE001
                continue
            if start_seq <= seq <= end_seq:
                selected.append(segment)

        return selected

    def _context_by_time_overlap(
        self,
        ad_start: float,
        ad_end: float,
        all_segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ad_segs = [
            s for s in all_segments if self._segment_overlaps(s, ad_start, ad_end)
        ]
        if not ad_segs:
            return []

        first_idx = all_segments.index(ad_segs[0])
        last_idx = all_segments.index(ad_segs[-1])
        start_idx = max(0, first_idx - 2)
        end_idx = min(len(all_segments), last_idx + 3)
        return all_segments[start_idx:end_idx]

    @staticmethod
    def _segment_overlaps(
        segment: dict[str, Any], ad_start: float, ad_end: float
    ) -> bool:
        try:
            seg_start = float(segment.get("start_time", 0.0))
        except Exception:  # noqa: BLE001
            seg_start = 0.0
        try:
            seg_end = float(segment.get("end_time", seg_start))
        except Exception:  # noqa: BLE001
            seg_end = seg_start
        return seg_start <= float(ad_end) and seg_end >= float(ad_start)

    def _estimate_phrase_times(
        self,
        *,
        all_segments: list[dict[str, Any]],
        context_segments: list[dict[str, Any]],
        start_segment_seq: Any,
        start_phrase: Any,
        end_segment_seq: Any,
        end_phrase: Any,
    ) -> tuple[float | None, float | None]:
        start_time = self._estimate_phrase_time(
            all_segments=all_segments,
            context_segments=context_segments,
            preferred_segment_seq=start_segment_seq,
            phrase=start_phrase,
            direction="start",
        )
        end_time = self._estimate_phrase_time(
            all_segments=all_segments,
            context_segments=context_segments,
            preferred_segment_seq=end_segment_seq,
            phrase=end_phrase,
            direction="end",
        )
        return start_time, end_time

    def _estimate_phrase_time(
        self,
        *,
        all_segments: list[dict[str, Any]],
        context_segments: list[dict[str, Any]],
        preferred_segment_seq: Any,
        phrase: Any,
        direction: str,
    ) -> float | None:
        phrase_tokens = self._split_words(str(phrase or ""))
        phrase_tokens = [t.lower() for t in phrase_tokens if t]
        if not phrase_tokens:
            return None

        # Search order:
        # 1) preferred segment (if provided)
        # 2) other provided context segments (ad-range ±2)
        candidates: list[dict[str, Any]] = []
        preferred_seg = self._find_segment(all_segments, preferred_segment_seq)
        if preferred_seg is not None:
            candidates.append(preferred_seg)

        # De-duplicate and order additional candidates.
        ordered_context = list(context_segments or [])
        try:
            ordered_context.sort(key=lambda s: int(s.get("sequence_num", -1)))
        except Exception:  # noqa: BLE001
            pass
        if direction == "end":
            ordered_context = list(reversed(ordered_context))

        preferred_seq_int: int | None
        try:
            preferred_seq_int = int(preferred_segment_seq)
        except Exception:  # noqa: BLE001
            preferred_seq_int = None

        for seg in ordered_context:
            try:
                seq = int(seg.get("sequence_num", -1))
            except Exception:  # noqa: BLE001
                seq = None
            if preferred_seq_int is not None and seq == preferred_seq_int:
                continue
            candidates.append(seg)

        for seg in candidates:
            estimated_time = self._estimate_phrase_time_for_segment(
                segment=seg,
                phrase_tokens=phrase_tokens,
                direction=direction,
            )
            if estimated_time is not None:
                return estimated_time

        return None

    def _estimate_phrase_time_for_segment(
        self,
        *,
        segment: dict[str, Any],
        phrase_tokens: list[str],
        direction: str,
    ) -> float | None:
        timed_words = self._segment_word_entries(segment)
        if timed_words:
            match = self._find_phrase_match(
                words=[entry["token"] for entry in timed_words],
                phrase_tokens=phrase_tokens,
                direction=direction,
                max_words=4,
            )
            if match is not None:
                match_start_idx, match_end_idx = match
                exact_time = self._exact_phrase_boundary_time(
                    timed_words,
                    match_start_idx=match_start_idx,
                    match_end_idx=match_end_idx,
                    direction=direction,
                )
                if exact_time is not None:
                    return exact_time

        start_time = float(segment.get("start_time", 0.0))
        end_time = float(segment.get("end_time", start_time))
        duration = max(0.0, end_time - start_time)
        words = [w.lower() for w in self._split_words(str(segment.get("text", "")))]
        if not words or duration <= 0.0:
            return None

        match = self._find_phrase_match(
            words=words,
            phrase_tokens=phrase_tokens,
            direction=direction,
            max_words=4,
        )
        if match is None:
            return None

        match_start_idx, match_end_idx = match
        seconds_per_word = duration / float(len(words))
        if direction == "start":
            estimated = start_time + (float(match_start_idx) * seconds_per_word)
            return min(estimated, end_time)

        estimated = start_time + (float(match_end_idx + 1) * seconds_per_word)
        return min(estimated, end_time)

    def _find_phrase_match(
        self,
        *,
        words: list[str],
        phrase_tokens: list[str],
        direction: str,
        max_words: int,
    ) -> tuple[int, int] | None:
        if not words or not phrase_tokens:
            return None

        if direction == "start":
            base = phrase_tokens[:max_words]
            for k in range(len(base), 0, -1):
                target = base[:k]
                match = self._find_subsequence(words, target, choose="first")
                if match is not None:
                    return match
            return None

        # direction == "end"
        base = phrase_tokens[-max_words:]
        for k in range(len(base), 0, -1):
            target = base[-k:]
            match = self._find_subsequence(words, target, choose="last")
            if match is not None:
                return match
        return None

    def _find_subsequence(
        self, words: list[str], target: list[str], *, choose: str
    ) -> tuple[int, int] | None:
        if not target or len(target) > len(words):
            return None

        matches: list[tuple[int, int]] = []
        k = len(target)
        for i in range(0, len(words) - k + 1):
            if words[i : i + k] == target:
                matches.append((i, i + k - 1))

        if not matches:
            return None
        if choose == "last":
            return matches[-1]
        return matches[0]

    def _estimate_word_time(
        self,
        *,
        all_segments: list[dict[str, Any]],
        segment_seq: Any,
        word: Any,
        occurrence: Any,
        word_index: Any,
    ) -> float:
        seg = self._find_segment(all_segments, segment_seq)
        if not seg:
            return float(all_segments[0]["start_time"]) if all_segments else 0.0

        start_time = float(seg.get("start_time", 0.0))
        end_time = float(seg.get("end_time", start_time))
        duration = max(0.0, end_time - start_time)

        timed_words = self._segment_word_entries(seg)
        if timed_words:
            resolved_index = self._resolve_word_index(
                [entry["token"] for entry in timed_words],
                word=word,
                occurrence=occurrence,
                word_index=word_index,
            )
            exact_start = timed_words[resolved_index].get("start")
            if exact_start is not None:
                return min(float(exact_start), float(seg.get("end_time", end_time)))

        words = self._split_words(str(seg.get("text", "")))
        if not words or duration <= 0.0:
            return start_time

        resolved_index = self._resolve_word_index(
            words,
            word=word,
            occurrence=occurrence,
            word_index=word_index,
        )

        # Heuristic timing: constant word duration within the segment.
        # words_per_second = num_words / segment_duration
        # seconds_per_word = 1 / words_per_second = segment_duration / num_words
        seconds_per_word = duration / float(len(words))
        estimated = start_time + (float(resolved_index) * seconds_per_word)
        # Guardrail: never return a start after the block end.
        return min(estimated, float(seg.get("end_time", end_time)))

    def _find_segment(
        self, all_segments: list[dict[str, Any]], segment_seq: Any
    ) -> dict[str, Any] | None:
        if segment_seq is None:
            return None
        try:
            seq_int = int(segment_seq)
        except Exception:  # noqa: BLE001
            return None

        for seg in all_segments:
            if int(seg.get("sequence_num", -1)) == seq_int:
                return seg
        return None

    @staticmethod
    def _word_field(word: Any, field_name: str) -> Any | None:
        if isinstance(word, dict):
            return word.get(field_name)
        return getattr(word, field_name, None)

    def _segment_word_entries(self, segment: dict[str, Any]) -> list[dict[str, Any]]:
        raw_words = segment.get("words")
        if not isinstance(raw_words, list):
            return []

        entries: list[dict[str, Any]] = []
        for raw_word in raw_words:
            token_raw = self._word_field(raw_word, "word")
            if token_raw is None:
                continue

            token = self._normalize_token(str(token_raw)).lower()
            if not token:
                continue

            start = self._word_field(raw_word, "start")
            end = self._word_field(raw_word, "end")
            entries.append(
                {
                    "token": token,
                    "start": (float(start) if start is not None else None),
                    "end": (float(end) if end is not None else None),
                }
            )

        return entries

    @staticmethod
    def _exact_phrase_boundary_time(
        timed_words: list[dict[str, Any]],
        *,
        match_start_idx: int,
        match_end_idx: int,
        direction: str,
    ) -> float | None:
        if direction == "start":
            start = timed_words[match_start_idx].get("start")
            return float(start) if start is not None else None

        end = timed_words[match_end_idx].get("end")
        return float(end) if end is not None else None

    def _split_words(self, text: str) -> list[str]:
        # Word count/indexing heuristic: split on whitespace, then normalize away
        # leading/trailing punctuation to keep indices stable.
        raw_tokens = [t for t in re.split(r"\s+", (text or "").strip()) if t]
        normalized = [self._normalize_token(t) for t in raw_tokens]
        return [t for t in normalized if t]

    def _normalize_token(self, token: str) -> str:
        # Strip leading/trailing punctuation; keep internal apostrophes.
        # Examples:
        #   "(brought" -> "brought"
        #   "you..." -> "you"
        #   "don't" -> "don't"
        return re.sub(r"(^[^A-Za-z0-9']+)|([^A-Za-z0-9']+$)", "", token)

    def _resolve_word_index(
        self, words: list[str], *, word: Any, occurrence: Any, word_index: Any
    ) -> int:
        # Prefer the verbatim word match if provided.
        # `occurance` chooses which matching instance to use.
        # Defaults to "first" if missing/invalid.
        target_raw = str(word).strip() if word is not None else ""
        target = self._normalize_token(target_raw).lower()
        if target:
            match_indexes = [
                idx for idx, w in enumerate(words) if (w or "").lower() == target
            ]
            if match_indexes:
                occ = str(occurrence).strip().lower() if occurrence is not None else ""
                if occ == "last":
                    return match_indexes[-1]
                # Default to first if LLM response is missing/invalid.
                return match_indexes[0]

        try:
            idx_int = int(word_index)
        except Exception:  # noqa: BLE001
            idx_int = 0

        idx_int = max(0, min(idx_int, len(words) - 1))
        return idx_int

    def _estimate_segment_boundary_time(
        self,
        *,
        all_segments: list[dict[str, Any]],
        segment_seq: Any,
        boundary: str,
    ) -> float | None:
        seg = self._find_segment(all_segments, segment_seq)
        if not seg:
            return None

        try:
            start_time = float(seg.get("start_time", 0.0))
        except Exception:  # noqa: BLE001
            start_time = 0.0
        try:
            end_time = float(seg.get("end_time", start_time))
        except Exception:  # noqa: BLE001
            end_time = start_time

        if boundary == "end":
            return end_time
        return start_time

    def _update_model_call(
        self,
        model_call_id: int | None,
        *,
        status: str,
        response: str | None,
        error_message: str | None,
        usage: dict[str, int | None] | None = None,
        service_tier: str | None = None,
    ) -> None:
        # Forward model + tier + prompt so try_update_model_call can compute
        # the LLM cost for success transitions. This refiner stages updates
        # as "received_response" -> "success" against the same row, so the
        # token usage is already on disk by the time we mark success — we
        # just need to pass these fields through every time so the success
        # gate in try_update_model_call has what it needs.
        try_update_model_call(
            model_call_id,
            status=status,
            response=response,
            error_message=error_message,
            logger=self.logger,
            log_prefix="Word boundary refine",
            usage=usage,
            model_name=getattr(self.config, "llm_model", None),
            service_tier=service_tier,
            prompt="word_boundary_refine",
        )
