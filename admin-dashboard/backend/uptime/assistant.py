"""Dashboard assistant: answers questions about current incidents and the repair agent in plain words.

The API builds a context snapshot (service status, open incidents, what the agent found)
and this module sends it with the conversation to OpenAI's chat completions API. Probe
evidence is passed without source locations, and the model is told to keep code details
out of its answers unless the user asks for them.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4.1-mini"
TIMEOUT_SECONDS = 60
MAX_TURNS = 12

SYSTEM_PROMPT = """You are the OpsRelay assistant on a service status dashboard. OpsRelay watches the services and
repositories a workspace has connected. When a service breaks, an AI repair agent investigates,
writes a fix, tests it and opens a pull request (PR) on GitHub for a person to review and merge.

Answer questions about what is happening using only the status snapshot below.
- Use simple, everyday words. Assume the reader is not an engineer. Keep answers short: a few sentences or bullets.
- Explain money values in dollars (values ending in _cents are cents).
- Do not mention file names, function names or code unless the user explicitly asks for technical details.
- If the snapshot does not say something, say you don't know. Never claim a problem is fixed unless the snapshot says
  so.
- A failing check whose source starts with "external:" (for example the Stripe payment API) is a third-party
  outage, not a bug: say so plainly, and that there is no code fix to wait for.
- Steps people can take: wait for the agent, review and merge its PR, confirm on the dashboard whether the merged fix
  worked, or restore the app from the demo outage controls.

Status snapshot (JSON):
"""


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    incident_id: str | None = Field(default=None, max_length=200)


class AssistantError(RuntimeError):
    """The assistant cannot answer; ``status`` is the HTTP status to return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def plain_failures(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Failed probe checks from an incident's latest failure, without source locations."""
    probes = (evidence.get("latest_failure") or {}).get("probes") or {}
    failures = []
    for kind, probe in probes.items():
        if not probe or probe.get("ok"):
            continue
        details = probe.get("details") or {}
        for f in details.get("failures") or []:
            failures.append({k: f.get(k) for k in ("check", "component", "reason", "expected", "actual")})
        if not details.get("failures"):
            failures.append({"check": kind, "reason": probe.get("error") or f"HTTP {probe.get('http_status')}"})
    return failures


async def answer(context: dict[str, Any], messages: list[ChatMessage],
                 transport: httpx.AsyncBaseTransport | None = None) -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise AssistantError(503, "The assistant needs OPENAI_API_KEY in .env.")
    model = os.environ.get("OPSRELAY_ASSISTANT_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT + json.dumps(context, default=str, indent=1)}]
                    + [m.model_dump() for m in messages[-MAX_TURNS:]],
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.post(OPENAI_URL, json=payload, headers={"Authorization": f"Bearer {key}"})
    except httpx.TimeoutException as exc:
        raise AssistantError(504, "The assistant took too long to answer. Try again.") from exc
    except httpx.RequestError as exc:
        raise AssistantError(502, "Cannot reach the AI provider.") from exc
    if response.status_code == 401:
        raise AssistantError(502, "The AI provider rejected OPENAI_API_KEY.")
    if response.status_code == 429:
        raise AssistantError(502, "The AI provider's rate limit or quota was reached. Try again shortly.")
    if response.status_code != 200:
        raise AssistantError(502, f"The AI provider returned HTTP {response.status_code}.")
    try:
        return response.json()["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise AssistantError(502, "The AI provider sent an unexpected response.") from exc
