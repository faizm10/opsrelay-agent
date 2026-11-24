"""Live model check. Runs only when explicitly enabled."""

import os

import pytest

from agents import OpenAIStructuredLLM, repair_ci_failure

from .support import CART_PATH

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSRELAY_RUN_LIVE_TESTS") != "1",
    reason="Set OPSRELAY_RUN_LIVE_TESTS=1 to run the live OpenAI test",
)


def test_live_model_repairs_shopping_discount(shopping):
    result = repair_ci_failure(shopping.request(), llm=OpenAIStructuredLLM())

    assert result.diagnosis is not None
    print(result.model_dump_json(indent=2))
    assert result.outcome == "verified", result.reason
    assert result.changed_files == [CART_PATH]
