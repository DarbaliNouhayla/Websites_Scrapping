"""
base_agent.py — Common base class for all MAS agents.

Handles:
- Groq API call with JSON response format (single item)
- Groq API batch call (multiple items per request) — 10x faster
- Retry with exponential backoff on 429 rate limit errors
- Daily token budget tracking across all agents
- JSON validation and parsing
- Error handling with fallback
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


# ── Token budget tracker ──────────────────────────────────────────────────────

class TokenBudget:
    """
    Estimates daily token usage to avoid hitting the TPD limit.

    llama-3.1-8b-instant free tier: 500,000 tokens/day
    Estimated cost per item processed: ~300 tokens
      (system prompt ~150 + user content ~100 + response ~50)

    This is an estimate — actual usage varies by content length.
    We leave a 10K buffer to avoid hitting the exact limit.
    """

    DAILY_LIMIT = 490_000           # 500K limit - 10K buffer
    TOKENS_PER_ITEM = 300           # estimated average
    TOKENS_PER_BATCH_OVERHEAD = 50  # extra tokens for batch formatting

    def __init__(self):
        self._used: int = 0
        self._day: date | None = None

    def _reset_if_new_day(self) -> None:
        today = date.today()
        if self._day != today:
            self._used = 0
            self._day = today
            log.debug("[TokenBudget] New day — token counter reset")

    def can_process(self, n_items: int) -> bool:
        """Return True if we have enough budget to process n_items."""
        self._reset_if_new_day()
        estimated = n_items * self.TOKENS_PER_ITEM
        return (self._used + estimated) <= self.DAILY_LIMIT

    def max_processable(self) -> int:
        """Return max number of items we can still process today."""
        self._reset_if_new_day()
        remaining = self.DAILY_LIMIT - self._used
        return max(0, remaining // self.TOKENS_PER_ITEM)

    def record(self, n_items: int) -> None:
        """Record estimated token usage after processing n_items."""
        self._reset_if_new_day()
        self._used += n_items * self.TOKENS_PER_ITEM

    @property
    def used(self) -> int:
        self._reset_if_new_day()
        return self._used

    @property
    def remaining(self) -> int:
        self._reset_if_new_day()
        return max(0, self.DAILY_LIMIT - self._used)


# Single shared instance across all agents in one process
_token_budget = TokenBudget()


# ── Base agent ────────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """
    Every MAS agent inherits from this class.

    Agents receive data as dicts and return structured dicts.
    Agents do NOT call asyncpg directly.
    The orchestrator handles all DB reads and writes.
    """

    name: str = "BaseAgent"
    model: str = "llama-3.1-8b-instant"
    max_tokens: int = 300
    temperature: float = 0.0

    # Groq free tier: 30 req/min → 1 req per 2s minimum
    RATE_LIMIT_SLEEP: float = 2.1

    # Retry config for 429 errors
    MAX_RETRIES: int = 3

    # Batch size — items per Groq call
    DEFAULT_BATCH_SIZE: int = 10

    def __init__(self):
        self._client = None

    def _get_groq_client(self):
        """Lazy init — reuses existing Groq client from severity.py."""
        if self._client is None:
            from financial_scraper.utils.severity import _get_client
            self._client = _get_client()
        return self._client

    # ── Single item LLM call ──────────────────────────────────────────────────

    async def _call_llm(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int | None = None,
    ) -> dict | list | None:
        """
        Call Groq for a single item with retry on rate limit.
        Returns parsed JSON or None on failure.
        Never raises.
        """
        for attempt in range(self.MAX_RETRIES):
            try:
                response = await self._get_groq_client().chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_content[:2000]},
                    ],
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=self.temperature,
                    response_format={"type": "json_object"},
                )
                raw = response.choices[0].message.content.strip()
                return json.loads(raw)

            except json.JSONDecodeError as exc:
                log.warning("[%s] JSON parse failed (attempt %d/3): %s",
                            self.name, attempt + 1, exc)
                return None

            except Exception as exc:
                err = str(exc)

                # 429 — rate limit per minute
                if "429" in err or "rate_limit_exceeded" in err.lower():
                    wait = self._extract_wait_time(err)
                    log.warning(
                        "[%s] Rate limit 429 — waiting %.1fs (attempt %d/3)",
                        self.name, wait, attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    continue

                # Daily token limit
                if "tokens per day" in err.lower() or "TPD" in err:
                    log.error("[%s] Daily token limit exhausted", self.name)
                    return None

                # json_validate_failed — max tokens reached
                if "json_validate_failed" in err or "max completion tokens" in err:
                    log.warning(
                        "[%s] Token overflow on attempt %d — retrying with shorter input",
                        self.name, attempt + 1,
                    )
                    # Shorten input and retry
                    user_content = user_content[:500]
                    continue

                # Any other error — don't retry
                log.warning("[%s] LLM call failed: %s", self.name, exc)
                return None

        log.warning("[%s] All %d retries exhausted", self.name, self.MAX_RETRIES)
        return None

    # ── Batch LLM call ────────────────────────────────────────────────────────

    async def _call_llm_batch(
        self,
        system_prompt: str,
        user_contents: list[str],
        batch_size: int | None = None,
    ) -> list[dict | None]:
        """
        Call Groq once per batch_size items instead of once per item.
        10x faster than calling per item.

        Args:
            system_prompt: the agent's system prompt
            user_contents: list of user content strings (one per item)
            batch_size: items per Groq call (default: DEFAULT_BATCH_SIZE)

        Returns:
            list of parsed dicts in same order as user_contents.
            None entries indicate failed items.
        """
        if not user_contents:
            return []

        batch_size = batch_size or self.DEFAULT_BATCH_SIZE
        results: list[dict | None] = []

        # Build batch system prompt — instructs model to return array
        batch_system = (
            system_prompt
            + "\n\n"
            "IMPORTANT: You will receive a JSON array of items, each with an 'id' field.\n"
            "Return a JSON object with a 'results' key containing an array.\n"
            "The array must have EXACTLY the same number of objects as the input.\n"
            "Each output object must include the 'id' from the corresponding input.\n"
            "Example format: {\"results\": [{\"id\": 0, ...}, {\"id\": 1, ...}]}"
        )

        for i in range(0, len(user_contents), batch_size):
            batch = user_contents[i : i + batch_size]

            # Build numbered batch payload
            batch_payload = [
                {"id": j, "content": content[:300]}
                for j, content in enumerate(batch)
            ]

            batch_result = await self._call_llm(
                system_prompt=batch_system,
                user_content=json.dumps(batch_payload, ensure_ascii=False),
                max_tokens=min(self.max_tokens * len(batch), 2000),
            )

            # Parse batch result
            batch_parsed = self._parse_batch_result(batch_result, len(batch))
            results.extend(batch_parsed)

            log.debug(
                "[%s] Batch %d-%d: %d/%d items parsed successfully",
                self.name,
                i, i + len(batch),
                sum(1 for r in batch_parsed if r is not None),
                len(batch),
            )

            # Rate limiting — sleep between batches, not between items
            is_last_batch = (i + batch_size) >= len(user_contents)
            if not is_last_batch:
                await asyncio.sleep(self.RATE_LIMIT_SLEEP)

        return results

    def _parse_batch_result(
        self,
        result: dict | list | None,
        expected_count: int,
    ) -> list[dict | None]:
        """
        Parse the batch response from Groq.
        Handles various response formats the model might return.
        Returns list of length expected_count with None for failures.
        """
        if result is None:
            return [None] * expected_count

        # Format 1: {"results": [...]}
        if isinstance(result, dict) and "results" in result:
            arr = result["results"]
            if isinstance(arr, list) and len(arr) == expected_count:
                return arr
            # Wrong count — try to use what we have
            if isinstance(arr, list):
                padded = arr + [None] * (expected_count - len(arr))
                return padded[:expected_count]

        # Format 2: direct list
        if isinstance(result, list):
            if len(result) == expected_count:
                return result
            padded = result + [None] * (expected_count - len(result))
            return padded[:expected_count]

        # Format 3: dict with any list value
        if isinstance(result, dict):
            for val in result.values():
                if isinstance(val, list):
                    if len(val) == expected_count:
                        return val
                    padded = val + [None] * (expected_count - len(val))
                    return padded[:expected_count]

        # Couldn't parse — return all None
        log.warning("[%s] Could not parse batch result: %s",
                    self.name, str(result)[:100])
        return [None] * expected_count

    # ── Rate limit helpers ────────────────────────────────────────────────────

    @staticmethod
    def _extract_wait_time(error_message: str) -> float:
        """
        Extract wait time from Groq 429 error message.
        Example: "try again in 5m20.544s" → 320.544
        Example: "try again in 30s" → 31.0 (+ 1s buffer)
        """
        # Try "Xm Ys" format
        match = re.search(r"try again in (\d+)m(\d+\.?\d*)s", error_message)
        if match:
            minutes = int(match.group(1))
            seconds = float(match.group(2))
            return minutes * 60 + seconds + 1.0

        # Try "Xs" format
        match = re.search(r"try again in (\d+\.?\d*)s", error_message)
        if match:
            return float(match.group(1)) + 1.0

        # Default backoff
        return 30.0

    # ── Token budget helpers ──────────────────────────────────────────────────

    def _check_and_trim_budget(self, items: list) -> list:
        """
        Check token budget and trim items list if needed.
        Logs a warning if items are trimmed.
        """
        if _token_budget.can_process(len(items)):
            return items

        max_items = _token_budget.max_processable()
        if max_items == 0:
            log.error(
                "[%s] Daily token budget exhausted (%d used) — skipping all items",
                self.name, _token_budget.used,
            )
            return []

        log.warning(
            "[%s] Token budget low — trimming from %d to %d items "
            "(%d tokens remaining)",
            self.name, len(items), max_items, _token_budget.remaining,
        )
        return items[:max_items]

    def _record_usage(self, n_items: int) -> None:
        """Record token usage after processing."""
        _token_budget.record(n_items)
        log.debug(
            "[%s] Token budget: used=%d remaining=%d",
            self.name, _token_budget.used, _token_budget.remaining,
        )

    # ── Result builders ───────────────────────────────────────────────────────

    @abstractmethod
    async def process(self, items: list[dict]) -> list[dict]:
        """
        Process a batch of items.

        Input:  list of dicts from DB
        Output: list of result dicts, each containing:
                  source_id, source_table, agent_result,
                  should_alert, alert_priority
        """
        ...

    def _safe_result(
        self,
        source_id: Any,
        source_table: str,
        agent_result: dict,
        should_alert: bool = False,
        alert_priority: str = "low",
    ) -> dict:
        return {
            "source_id": str(source_id),
            "source_table": source_table,
            "agent": self.name,
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "agent_result": agent_result,
            "should_alert": should_alert,
            "alert_priority": alert_priority,
        }

    def _error_result(
        self,
        source_id: Any,
        source_table: str,
        error: str,
    ) -> dict:
        return {
            "source_id": str(source_id),
            "source_table": source_table,
            "agent": self.name,
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "agent_result": {"error": error},
            "should_alert": False,
            "alert_priority": "low",
            "error": error,
        }
