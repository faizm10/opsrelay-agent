"""Structured LLM calls via LangChain + OpenAI, behind a small injectable protocol."""

from __future__ import annotations

import os
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "gpt-4.1"


class StructuredLLM(Protocol):
    def generate(self, schema: type[T], system: str, user: str) -> T: ...


class OpenAIStructuredLLM:
    """Default model: OpenAI structured outputs through langchain-openai."""

    def __init__(self, model: str | None = None, timeout: float = 60.0, api_key: str | None = None):
        from langchain_openai import ChatOpenAI

        if not api_key and not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set; pass a StructuredLLM explicitly.")
        self._chat = ChatOpenAI(
            model=model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
            temperature=0,
            timeout=timeout,
            max_retries=2,
            **({"api_key": api_key} if api_key else {}),
        )

    def generate(self, schema: type[T], system: str, user: str) -> T:
        from langchain_core.messages import HumanMessage, SystemMessage

        runnable = self._chat.with_structured_output(schema, method="json_schema", strict=True)
        return runnable.invoke([SystemMessage(content=system), HumanMessage(content=user)])


def numbered(content: str) -> str:
    """Prefix each line with its 1-based number ("  12| ") so tracebacks can be matched up."""
    return "".join(f"{i:4d}| {line}" for i, line in enumerate(content.splitlines(keepends=True), 1))


def untrusted_block(label: str, content: str, limit: int = 12_000) -> str:
    """Wrap untrusted input (CI logs, diffs, incidents) so the model reads it as data."""
    if len(content) > limit:
        content = f"[...truncated {len(content) - limit} chars...]\n" + content[-limit:]
    # Neutralise attempts to close the block early.
    content = content.replace(f"</{label}>", f"<\\/{label}>")
    return f"<{label}>\n{content}\n</{label}>"
