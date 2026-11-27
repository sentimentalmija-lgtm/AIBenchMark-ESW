"""Provider generation with explicit attempts and replayable public messages."""
import copy
import math
import re
import time
from collections.abc import Mapping
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from aibenchmark_esw.llm.costs import (
    BudgetExceededError,
    PricingUnknownError,
    model_info,
    pricing,
    token_upper_bound,
    usage_cost,
    valid_cost,
)
from aibenchmark_esw.sandbox.c_source import mask_noncode

_MAX_RETRY_DELAY_SECONDS = 60.0


def is_fatal_provider_error(error):
    if isinstance(error, PricingUnknownError) or type(error).__name__ in {"ContextWindowExceededError", "ContentPolicyViolationError", "BadRequestError"}:
        return False
    status = getattr(error, "status_code", None)
    return (isinstance(error, (ImportError, BudgetExceededError))
            or type(error).__name__ in {"AuthenticationError", "PermissionDeniedError",
                                       "NotFoundError", "UnsupportedParamsError"}
            or (isinstance(status, int) and status in (401, 403, 404)))


def is_transient_provider_error(error):
    status = getattr(error, "status_code", None)
    return (isinstance(error, (TimeoutError, ConnectionError))
            or type(error).__name__ in {"Timeout", "APITimeoutError", "RateLimitError",
                                       "APIConnectionError", "ServiceUnavailableError"}
            or (isinstance(status, int) and (status in (408, 409, 429) or 500 <= status < 600)))


def _pre_response_error(error):
    status = getattr(error, 'status_code', None)
    return (isinstance(error, ConnectionError)
            or type(error).__name__ in {'RateLimitError', 'APIConnectionError', 'ServiceUnavailableError'}
            or (isinstance(status, int) and (status in (408, 429) or 500 <= status < 600)))


def _rate_limit_retry_after(error: Exception) -> Optional[float]:
    if getattr(error, 'status_code', None) != 429 and type(error).__name__ != 'RateLimitError':
        return None
    for carrier in (getattr(error, 'response', None), error):
        headers = getattr(carrier, 'headers', None)
        if not isinstance(headers, Mapping):
            continue
        for name, value in headers.items():
            if not isinstance(name, str) or name.lower() != 'retry-after' or not isinstance(value, str):
                continue
            value = value.strip()
            try:
                if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
                    delay = float(value)
                else:
                    date = parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    delay = date.timestamp() - time.time()
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            if math.isfinite(delay) and delay >= 0:
                return min(delay, _MAX_RETRY_DELAY_SECONDS)
    return None


class ContextWindowError(ValueError):
    """A measured prompt exceeds the model's published context limit."""


class LLMClient:
    def __init__(self, model_name: str, api_key: Optional[str] = None,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                 request_timeout: float = 60.0, max_retries: int = 0,
                 retry_backoff_seconds: float = 1.0, prompt_strategy: str = "single",
                 review_turn: bool = False, input_cost_per_million=None,
                 output_cost_per_million=None, budget=None, repair_turns=0):
        if temperature is not None and (isinstance(temperature, bool)
                or not isinstance(temperature, (int, float))
                or not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise ValueError("temperature must be a finite number between 0 and 2")
        if max_tokens is not None and (isinstance(max_tokens, bool)
                or not isinstance(max_tokens, int) or max_tokens < 1):
            raise ValueError("max_tokens must be a positive integer")
        if (isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float))
                or not math.isfinite(request_timeout) or request_timeout <= 0):
            raise ValueError("request_timeout must be a finite positive number")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 10:
            raise ValueError("max_retries must be an integer between 0 and 10")
        if (isinstance(retry_backoff_seconds, bool)
                or not isinstance(retry_backoff_seconds, (int, float))
                or not math.isfinite(retry_backoff_seconds) or not 0 <= retry_backoff_seconds <= 60):
            raise ValueError("retry_backoff_seconds must be between 0 and 60")
        if prompt_strategy not in ("single", "plan"):
            raise ValueError("prompt_strategy must be single or plan")
        if not isinstance(review_turn, bool):
            raise ValueError("review_turn must be a boolean")
        if isinstance(repair_turns, bool) or not isinstance(repair_turns, int) or not 0 <= repair_turns <= 10:
            raise ValueError('repair_turns must be an integer between 0 and 10')
        self.model_name, self.api_key = model_name, api_key
        self.temperature, self.max_tokens = temperature, max_tokens
        self.request_timeout = request_timeout
        self.max_retries, self.retry_backoff_seconds = max_retries, retry_backoff_seconds
        self.prompt_strategy, self.review_turn = prompt_strategy, review_turn
        self.repair_turns = repair_turns
        pricing(None, model_name, input_cost_per_million, output_cost_per_million)
        self.input_cost_per_million, self.output_cost_per_million = input_cost_per_million, output_cost_per_million
        self.budget = budget
        self.last_generation: Any = None
        self._initial_message_count = 0
        self.expected_functions: set[str] = set()
        self.cancel_event = None

    def settings(self):
        return {"temperature": self.temperature, "max_tokens": self.max_tokens,
                "request_timeout_seconds": self.request_timeout,
                "max_retries": self.max_retries, "retry_backoff_seconds": self.retry_backoff_seconds,
                "prompt_strategy": self.prompt_strategy, "review_turn": self.review_turn,
                'repair_turns': self.repair_turns, 'context_window_policy': 'trim-optional-once-v2',
                "input_cost_per_million": self.input_cost_per_million,
                "output_cost_per_million": self.output_cost_per_million}

    def build_prompt(self, task_prompt: str, header_code: str, starter_code: str,
                     target_standard: str = "c99", prompt_overrides=None,
                     system_prompt: Optional[str] = None) -> list[dict[str, str]]:
        if target_standard not in ("c99", "c11", "c17"):
            raise ValueError("target_standard must be c99, c11, or c17")
        overrides = prompt_overrides or {}
        if not isinstance(overrides, dict) or set(overrides) - {"allow_dynamic_memory", "extra_rules"}:
            raise ValueError("Unknown prompt override")
        if not isinstance(overrides.get("allow_dynamic_memory", False), bool):
            raise ValueError("allow_dynamic_memory must be a boolean")
        extra = overrides.get("extra_rules", [])
        if not isinstance(extra, list) or any(not isinstance(rule, str) or not rule.strip() for rule in extra):
            raise ValueError("extra_rules must be a list of nonempty strings")
        rules = [f"Write clean, portable {target_standard.upper()} code."]
        if not overrides.get("allow_dynamic_memory", False):
            rules.append("Do NOT use dynamic memory allocation (no malloc/calloc/realloc/free).")
        rules += ["Always check pointers for NULL.",
                  "Do not define main(), setUp(), tearDown(), or a test runner; the evaluator supplies them.",
                  "Follow MISRA-C principles: use fixed-width integers (<stdint.h>).",
                  "Return ONLY the complete implementation code for the requested C file inside a ```c ... ``` code block."]
        rules.extend(extra)
        instruction = ("You are an expert embedded software engineer specializing in safety-critical firmware.\nRules:\n"
                       + "\n".join(f"{index}. {rule}" for index, rule in enumerate(rules, 1)))
        if system_prompt is not None:
            if not isinstance(system_prompt, str) or not system_prompt.strip():
                raise ValueError("System prompt override must be nonempty text")
            instruction = system_prompt
        self.expected_functions = set(re.findall(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*;", mask_noncode(header_code)))
        user = (f"# Embedded Coding Task\n\n{task_prompt}\n\n## API Header Specification:\n"
                f"```c\n{header_code}\n```\n\n## Starter Code / File Template:\n"
                f"```c\n{starter_code}\n```\n\nProvide the complete implementation for the source file.")
        return [{"role": "system", "content": instruction}, {"role": "user", "content": user}]

    def _check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise InterruptedError("Generation cancelled")

    def _trim_optional(self, messages, turn_kind):
        tail_count = 2 if turn_kind in ('review', 'repair') else 1
        if len(messages) <= self._initial_message_count + tail_count:
            return False
        messages[:] = messages[:self._initial_message_count] + messages[-tail_count:]
        self.last_generation['context_trimmed'] = True
        return True

    def _context_check(self, provider, messages, turn_kind):
        info = model_info(provider, self.model_name)
        limit = info.get('max_input_tokens') or info.get('max_tokens')
        count = getattr(provider, 'token_counter', None)
        if not callable(count) or isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return
        try:
            tokens = count(model=self.model_name, messages=messages)
        except Exception:
            return
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            return
        available = limit if info.get('max_input_tokens') else limit - (self.max_tokens or 0)
        if tokens > available:
            if self._trim_optional(messages, turn_kind):
                self._context_check(provider, messages, turn_kind)
                return
            raise ContextWindowError(f'Prompt requires {tokens} tokens, context allows {available}; reduce task/header content or --max-tokens')

    def _complete(self, provider, messages, turn_kind='implement', context_retry=False):
        self._context_check(provider, messages, turn_kind)
        kwargs = {"model": self.model_name, "messages": copy.deepcopy(messages), "timeout": self.request_timeout}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        if self.api_key:
            kwargs["api_key"] = self.api_key
        prices = pricing(provider, self.model_name, self.input_cost_per_million, self.output_cost_per_million)
        self.last_generation["request_messages"].append(copy.deepcopy(messages))
        for attempt in range(self.max_retries + 1):
            self._check_cancelled()
            reservation = 0.0
            if self.budget is not None:
                if self.max_tokens is None:
                    raise BudgetExceededError("--max-cost-usd requires --max-tokens to reserve output costs")
                estimate = (token_upper_bound(messages) * prices["input_per_token"]
                            + self.max_tokens * prices["output_per_token"]) if prices else None
                reservation = self.budget.reserve(estimate)
            record: dict[str, Any] = {"turn": len(self.last_generation["request_messages"]),
                      "attempt": attempt + 1, "latency_seconds": None, "error": None,
                      "cost_usd": None, "budget_charge_usd": reservation, "usage": None}
            self.last_generation["attempts"].append(record)
            start = time.perf_counter()
            delay: Optional[float] = None
            settlement = None
            try:
                response = provider.completion(**kwargs)
                record['latency_seconds'] = round(time.perf_counter() - start, 6)
                usage = getattr(response, "usage", None)
                record["usage"] = {name: getattr(usage, name, None) for name in
                    ("prompt_tokens", "completion_tokens", "total_tokens")} if usage is not None else None
                if record["usage"] is not None:
                    for key in ("prompt_tokens_details", "completion_tokens_details"):
                        detail = getattr(usage, key, None)
                        if not isinstance(detail, dict):
                            detail = {name: getattr(detail, name, None) for name in
                                ("cached_tokens", "reasoning_tokens", "audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens")}
                        detail = {name: value for name, value in detail.items()
                                  if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
                        if detail:
                            record["usage"][key] = detail
                hidden = getattr(response, "_hidden_params", {})
                measured = hidden.get("response_cost") if isinstance(hidden, dict) else None
                record["cost_usd"] = measured if valid_cost(measured) else usage_cost(record["usage"], prices)
                if prices is not None:
                    record["pricing"] = prices
                break
            except Exception as error:
                record['latency_seconds'] = round(time.perf_counter() - start, 6)
                record["error"] = type(error).__name__
                if _pre_response_error(error):
                    settlement = 0.0
                    record['budget_charge_policy'] = 'pre-response-error-zero'
                elif type(error).__name__ in {'ContextWindowExceededError', 'ContextWindowError'}:
                    settlement = 0.0
                else:
                    record['budget_charge_policy'] = 'unknown-attempt-full-reservation'
                if type(error).__name__ == 'ContextWindowExceededError' and not context_retry and self._trim_optional(messages, turn_kind):
                    record['context_retry'] = True
                    delay = 0
                elif not is_transient_provider_error(error) or is_fatal_provider_error(error) or attempt >= self.max_retries:
                    raise
                else:
                    backoff: float = min(_MAX_RETRY_DELAY_SECONDS, self.retry_backoff_seconds * 2 ** attempt)
                    advice = _rate_limit_retry_after(error)
                    if advice is not None and advice > backoff:
                        delay, source = advice, 'retry-after'
                    else:
                        delay, source = backoff, 'exponential-backoff'
                    record["retry_delay_seconds"] = delay
                    record["retry_delay_source"] = source
            finally:
                if self.budget is not None:
                    charge = record['cost_usd'] if record['cost_usd'] is not None else settlement
                    self.budget.settle(reservation, charge)
                    record["budget_charge_usd"] = charge if charge is not None else reservation
            if record.get('context_retry'):
                return self._complete(provider, messages, turn_kind, context_retry=True)
            if delay is not None:
                if self.cancel_event is None:
                    time.sleep(delay)
                elif self.cancel_event.wait(delay):
                    raise InterruptedError("Generation cancelled")
        if not response.choices:
            raise ValueError("Model returned no completion choices")
        choice = response.choices[0]
        raw_text = choice.message.content or ""
        if not isinstance(raw_text, str):
            raise ValueError("Model completion content must be text")
        usage = getattr(response, "usage", None)
        tokens = copy.deepcopy(record["usage"])
        turn = {"resolved_model": getattr(response, "model", None),
                '_turn_kind': turn_kind, 'number': len(self.last_generation['turns']) + 1,
                "finish_reason": getattr(choice, "finish_reason", None),
                "usage": tokens, "response_text": raw_text, "cost_usd": record["cost_usd"]}
        self.last_generation["turns"].append(turn)
        self.last_generation.update(resolved_model=turn["resolved_model"], finish_reason=turn["finish_reason"])
        self._summarize_measurements()
        messages.append({"role": "assistant", "content": raw_text})
        self.last_generation["messages"] = copy.deepcopy(messages)
        if turn["finish_reason"] == "length":
            self.last_generation["partial_response"] = raw_text
            raise ValueError("Model output was truncated at the token limit; increase --max-tokens")
        return raw_text

    def _summarize_measurements(self):
        attempts = self.last_generation["attempts"]
        known, usage, coverage = {}, {}, {}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            values = [(item.get("usage") or {}).get(name) for item in attempts]
            measured = [value for value in values if isinstance(value, int) and not isinstance(value, bool) and value >= 0]
            known[name] = sum(measured) if measured else None
            usage[name] = sum(measured) if attempts and len(measured) == len(attempts) else None
            coverage[name] = {"known_attempts": len(measured), "total_attempts": len(attempts)}
        costs = [item["cost_usd"] for item in attempts if valid_cost(item.get("cost_usd"))]
        complete = bool(attempts) and len(costs) == len(attempts)
        self.last_generation.update(usage=usage if any(value is not None for value in known.values()) else None,
            known_usage=known, usage_complete=bool(attempts) and all(value is not None for value in usage.values()),
            usage_coverage=coverage, cost_usd=sum(costs) if complete else None,
            known_cost_usd=sum(costs) if costs else None, cost_complete=complete,
            budget_charge_usd=sum(item.get("budget_charge_usd", 0) for item in attempts))
        durations = [item['latency_seconds'] for item in attempts if valid_cost(item.get('latency_seconds'))]
        self.last_generation['latency_seconds'] = sum(durations) if attempts and len(durations) == len(attempts) else None

    def generate_solution(self, messages: list[dict[str, str]]) -> str:
        self._initial_message_count = len(messages)
        self.last_generation = {"requested_model": self.model_name, "resolved_model": None,
                                **self.settings(), "latency_seconds": None, "finish_reason": None,
                                "usage": None, "attempts": [], "turns": [],
                                "messages": copy.deepcopy(messages), "request_messages": []}
        start = time.perf_counter()
        try:
            try:
                import litellm
            except ImportError as error:
                raise ImportError("litellm is required for live LLM API evaluation. Install it using: pip install litellm") from error
            conversation = copy.deepcopy(messages)
            if self.prompt_strategy == "plan":
                conversation.append({"role": "user", "content":
                    "Provide a short implementation plan listing the public API, edge cases, and validation checks. Do not produce code yet."})
                self._complete(litellm, conversation, 'plan')
                conversation.append({"role": "user", "content":
                    "Now provide only the complete C implementation that meets the task contract, inside a c code block."})
            raw_text = self._complete(litellm, conversation)
            if self.review_turn:
                conversation.append({"role": "user", "content":
                    "Review the implementation against the API and boundary cases. Return only the corrected complete C implementation in a c code block."})
                raw_text = self._complete(litellm, conversation, 'review')
            return self.extract_c_code(raw_text, self.expected_functions)
        finally:
            self._summarize_measurements()
            self.last_generation['elapsed_wall_seconds'] = round(time.perf_counter() - start, 6)

    def repair_solution(self, diagnostics):
        """Continue the public conversation after a failed compilation."""
        import litellm
        conversation = copy.deepcopy(self.last_generation['messages'])
        conversation.append({'role': 'user', 'content':
            'The implementation failed to compile. Fix these compiler diagnostics and return only the complete C implementation in a c code block.\n'
            + diagnostics[:2048]})
        try:
            return self.extract_c_code(self._complete(litellm, conversation, 'repair'), self.expected_functions)
        finally:
            self._summarize_measurements()

    @staticmethod
    def extract_c_code(response_text: Optional[str], expected_functions=None) -> str:
        """Prefer API implementations, choosing the last with equal API coverage.

        Declarations and usage examples rank lower. Without an API match,
        prefer more substantial implementations as a fallback.
        """
        if not response_text:
            return ""
        blocks = re.findall(r"```([^\r\n`]*)\r?\n(.*?)```", response_text, flags=re.DOTALL)
        c_blocks = [code.strip() for language, code in blocks
                    if language.strip().split() and language.strip().split()[0].lower() in {"c", "cpp", "c++"}]
        candidates = c_blocks or [code.strip() for _, code in blocks]
        if not candidates:
            candidates = [code.strip() for code in re.findall(r"```(.*?)```", response_text, flags=re.DOTALL)]
        expected = set(expected_functions or ())
        def rank(code):
            definitions = set(re.findall(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{", mask_noncode(code)))
            definitions -= {"if", "for", "while", "switch"}
            implementation = definitions - {"main", "setUp", "tearDown"}
            api_coverage = len(expected & definitions)
            return (api_coverage, bool(implementation), "main" not in definitions,
                    0 if api_coverage else len(implementation), 0 if api_coverage else len(code))
        return max(reversed(candidates), key=rank) if candidates else response_text.strip()
