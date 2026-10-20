"""OpenAI-compatible chat client: JSON mode, retries with backoff, fallback model, schema validation + one repair."""
import asyncio
import json
import re
from typing import TypeVar

import httpx
from pydantic import BaseModel

from app.core.config import Settings
from app.llm.scrub import scrub

T = TypeVar("T", bound=BaseModel)
RETRYABLE = {429, 500, 502, 503, 504}


class LLMError(Exception):
    pass


class LLMBudgetExceeded(LLMError):
    pass


class ModelRejected(LLMError):
    """The provider refused this model or request (4xx other than 429): try the fallback model."""


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start : end + 1])


class LLMClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None, sleep=asyncio.sleep) -> None:
        self._s = settings
        self._http = http or httpx.AsyncClient(base_url=settings.llm_base_url.rstrip("/"), timeout=25)
        self._sleep = sleep
        self.calls = 0
        self.tokens = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _chat(self, model: str, messages: list[dict], temperature: float, max_tokens: int) -> str:
        body = {
            "model": model,
            "messages": [{"role": m["role"], "content": scrub(m["content"])} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "low"
        last = "no attempt made"
        for attempt in range(3):
            if self.calls >= self._s.llm_max_calls:
                raise LLMBudgetExceeded(f"LLM call budget of {self._s.llm_max_calls} reached")
            self.calls += 1
            try:
                resp = await self._http.post("/chat/completions", json=body,
                                             headers={"Authorization": f"Bearer {self._s.llm_api_key}"})
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            else:
                if resp.status_code in RETRYABLE:
                    last = f"HTTP {resp.status_code}"
                elif resp.status_code >= 400:
                    raise ModelRejected(f"{model}: HTTP {resp.status_code}")
                else:
                    data = resp.json()
                    self.tokens += (data.get("usage") or {}).get("total_tokens", 0)
                    return data["choices"][0]["message"].get("content") or ""
            await self._sleep(min(0.5 * 2**attempt, 4))
        raise LLMError(f"{model}: {last} after 3 attempts")

    async def _validated(self, model: str, messages: list[dict], schema: type[T], temperature: float, max_tokens: int) -> T:
        text = await self._chat(model, messages, temperature, max_tokens)
        try:
            return schema.model_validate(parse_json(text))
        except ValueError as exc:  # json errors and pydantic ValidationError are both ValueErrors
            repair = messages + [
                {"role": "assistant", "content": text[:2000]},
                {"role": "user", "content": f"That reply was invalid ({str(exc)[:300]}). Return only the corrected JSON object."},
            ]
        text = await self._chat(model, repair, temperature, max_tokens)
        try:
            return schema.model_validate(parse_json(text))
        except ValueError as exc:
            raise LLMError(f"{model}: invalid output after repair") from exc

    async def complete_json(self, system: str, user: str, schema: type[T], *, temperature: float = 0.1,
                            max_tokens: int = 900) -> T:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        models = [self._s.llm_model] + ([self._s.llm_fallback_model] if self._s.llm_fallback_model != self._s.llm_model else [])
        last: Exception | None = None
        for model in models:
            try:
                return await self._validated(model, messages, schema, temperature, max_tokens)
            except LLMBudgetExceeded:
                raise
            except LLMError as exc:  # rejected model, exhausted retries or unrepairable output: try the next model
                last = exc
        raise LLMError(str(last))
