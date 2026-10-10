"""
Smoke test: can we reach the OpenAI API at all, with the models we actually plan to use.

This is the first test to run, before building anything else. If this fails, nothing
downstream can work, so there is no point debugging agent logic yet.

Run with: uv run pytest tests/smoke/test_smoke_model_connectivity.py -v
"""

import pytest
from openai import OpenAI

from invest_agent.config import get_settings


@pytest.mark.smoke
def test_luna_retrieval_model_responds():
    settings = get_settings()
    if not settings.openai_api_key:
        pytest.skip("OPENAI_API_KEY not set in .env")
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.chat.completions.create(
        model=settings.model_retrieval,
        max_completion_tokens=256,
        messages=[{"role": "user", "content": "Reply with exactly one word: pong"}],
    )
    text = response.choices[0].message.content.strip().lower()
    assert "pong" in text


@pytest.mark.smoke
def test_terra_synthesis_model_responds():
    settings = get_settings()
    if not settings.openai_api_key:
        pytest.skip("OPENAI_API_KEY not set in .env")
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.chat.completions.create(
        model=settings.model_synthesis,
        max_completion_tokens=256,
        messages=[{"role": "user", "content": "Reply with exactly one word: pong"}],
    )
    text = response.choices[0].message.content.strip().lower()
    assert "pong" in text
