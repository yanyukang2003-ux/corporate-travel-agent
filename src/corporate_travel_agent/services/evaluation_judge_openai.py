"""OpenAI/DeepSeek 输出质量 Judge 适配器（评测专用，不参与产品运行时）。

刻意与 ``agent/openai_adapter.py`` 分开：Judge 只服务评测，不是产品链路上的端口，
应用构造拿不到它。它也不接收候选模型名、实验分组等盲字段。
"""

from __future__ import annotations

import json
from time import monotonic
from typing import Any

from corporate_travel_agent.agent.openai_adapter import _classified_openai_error
from corporate_travel_agent.agent.ports import LanguageModelError, LLMCallMetadata
from corporate_travel_agent.services.evaluation_judge import (
    JudgeError,
    JudgeScore,
    OutputQualityRubric,
)
from corporate_travel_agent.services.evaluation_quality import JudgeInput

JUDGE_PROMPT_VERSION = "output-quality-judge-v1"


class OpenAIOutputQualityJudge:
    """用结构化输出实现的 LLM Judge。"""

    judge_prompt_version = JUDGE_PROMPT_VERSION

    def __init__(
        self,
        *,
        model: str = "gpt-5.6",
        judge_id: str | None = None,
        reasoning_effort: str = "medium",
        max_output_tokens: int | None = 2048,
        client: Any | None = None,
        temperature: float | None = 0.0,
        api_mode: str | None = None,
        request_timeout_seconds: float = 60.0,
    ) -> None:
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("unsupported reasoning effort")
        if max_output_tokens is not None and not 1 <= max_output_tokens <= 128_000:
            raise ValueError("max_output_tokens must be between 1 and 128000")
        if not 1 <= request_timeout_seconds <= 600:
            raise ValueError("request_timeout_seconds must be between 1 and 600")
        self.model = model
        # Judge 身份要能进报告并被复核，因此固定绑定 prompt 版本与模型。
        self.judge_id = judge_id or f"{JUDGE_PROMPT_VERSION}:{model}"
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.last_call_metadata: LLMCallMetadata | None = None
        if api_mode is None:
            lowered = model.lower()
            api_mode = (
                "chat" if "deepseek" in lowered and "flash" not in lowered else "responses"
            )
        if api_mode not in {"chat", "responses"}:
            raise ValueError("api_mode must be 'chat' or 'responses'")
        self.api_mode = api_mode
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise LanguageModelError(
                    "Install the optional 'llm' dependency to use the OpenAI judge"
                ) from exc
            client = OpenAI(max_retries=0, timeout=request_timeout_seconds)
        self.client = client

    def score(
        self,
        judge_input: JudgeInput,
        rubric: OutputQualityRubric,
    ) -> tuple[JudgeScore, LLMCallMetadata]:
        """对一条盲评输入打分或弃权。"""
        started = monotonic()
        self.last_call_metadata = None
        system = _judge_system_prompt(rubric)
        payload = _judge_user_payload(judge_input)
        try:
            if self.api_mode == "chat":
                parsed, usage = self._score_via_chat(system, payload)
            else:
                parsed, usage = self._score_via_responses(system, payload)
        except LanguageModelError:
            raise
        except Exception as exc:
            raise _classified_openai_error(exc) from exc

        metadata = LLMCallMetadata(
            prompt_version=self.judge_prompt_version,
            model=usage.get("actual_model") or self.model,
            requested_model=self.model,
            reasoning_effort=self.reasoning_effort,
            duration_ms=int((monotonic() - started) * 1000),
            response_id=usage.get("response_id"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
        self.last_call_metadata = metadata
        return parsed, metadata

    def _score_via_responses(
        self, system: str, payload: str
    ) -> tuple[JudgeScore, dict[str, Any]]:
        request: dict[str, Any] = {
            "model": self.model,
            "reasoning": {"effort": self.reasoning_effort},
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": payload},
            ],
            "text_format": JudgeScore,
        }
        if self.max_output_tokens is not None:
            request["max_output_tokens"] = self.max_output_tokens
        if self.temperature is not None and self.reasoning_effort in {"none", "low"}:
            request["temperature"] = self.temperature
        response = self.client.responses.parse(**request)
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise JudgeError("The judge returned no parsed output")
        if not isinstance(parsed, JudgeScore):
            parsed = JudgeScore.model_validate(parsed)
        return parsed, _responses_usage(response)

    def _score_via_chat(
        self, system: str, payload: str
    ) -> tuple[JudgeScore, dict[str, Any]]:
        prompt = (
            system
            + "\n\nRespond with one JSON object only, matching this schema:\n"
            + json.dumps(JudgeScore.model_json_schema(), ensure_ascii=False)
        )
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": payload},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_output_tokens or 2048,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature
        response = self.client.chat.completions.create(**request)
        choice = response.choices[0] if response.choices else None
        content = getattr(getattr(choice, "message", None), "content", None)
        if not content or not str(content).strip():
            raise JudgeError("The judge returned empty content")
        text = str(content).strip()
        if text.startswith("```"):
            lines = text.splitlines()
            lines = lines[1:] if lines and lines[0].startswith("```") else lines
            lines = lines[:-1] if lines and lines[-1].strip() == "```" else lines
            text = "\n".join(lines).strip()
        try:
            parsed = JudgeScore.model_validate_json(text)
        except Exception as exc:
            raise JudgeError(f"Judge JSON failed validation: {exc}") from exc
        return parsed, _chat_usage(response)


def _judge_system_prompt(rubric: OutputQualityRubric) -> str:
    dimensions = "\n".join(
        f"- {item.id} (weight {item.weight}): {item.question}"
        for item in rubric.dimensions
    )
    anchors = "\n".join(
        f"- {score}: {text}" for score, text in sorted(rubric.score_anchors.items())
    )
    abstain = "\n".join(f"- {item}" for item in rubric.abstain_when)
    return (
        "You are a blinded evaluation judge for a corporate travel planning agent. "
        f"Apply rubric {rubric.rubric_id} exactly.\n\n"
        "Score only the user-visible output, using the supplied trace evidence "
        "references as the only admissible grounding.\n\n"
        f"Dimensions (score each 1-5):\n{dimensions}\n\n"
        f"Score anchors:\n{anchors}\n\n"
        f"Abstain instead of scoring when any of these hold:\n{abstain}\n\n"
        "Hard rules:\n"
        "- Deterministic hard assertions already decided pass or fail. Your score never "
        "overrides them; do not try to compensate for or excuse a hard failure.\n"
        "- Never infer which model or experiment group produced the output.\n"
        "- Never invent inventory, prices, policy conclusions, or evidence references.\n"
        "- When abstaining set abstained=true, give abstain_reason, and return no dimensions.\n"
        "- When scoring, return every dimension exactly once with a short rationale."
    )


def _judge_user_payload(judge_input: JudgeInput) -> str:
    payload = judge_input.model_dump(mode="json")
    # 盲评：case_id/run_id 只用于对齐，不携带候选身份，但仍从提示中移除以免暗示分组。
    payload.pop("run_id", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _responses_usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    return {
        "actual_model": getattr(response, "model", None),
        "response_id": getattr(response, "id", None),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _chat_usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    return {
        "actual_model": getattr(response, "model", None),
        "response_id": getattr(response, "id", None),
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }
