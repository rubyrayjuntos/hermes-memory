from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_memory.extract_model_client import ModelBatchInput, OpenAIChatModelExtractor


def _batch() -> ModelBatchInput:
    return ModelBatchInput(
        turns=[{"span_id": 7, "text": "Ray uses Hermes."}],
        rule_findings=[],
        verbs={"uses"},
    )


@pytest.mark.asyncio
async def test_extractor_sends_compact_batch_and_records_usage() -> None:
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "claims": [{
                                "span_id": 7,
                                "subject": "Ray",
                                "verb": "uses",
                                "object": "Hermes",
                                "polarity": "positive",
                            }]
                        })
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=51, completion_tokens=12),
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    extractor = OpenAIChatModelExtractor(
        client, "test-model", timeout_s=2.0, ollama_keep_alive="30m"
    )
    result = await extractor(_batch())

    assert result["claims"][0]["subject"] == "Ray"
    assert result["_usage"] == {"prompt_tokens": 51, "completion_tokens": 12}
    assert calls[0]["model"] == "test-model"
    assert calls[0]["temperature"] == 0
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["extra_body"] == {"keep_alive": "30m"}
    assert calls[0]["messages"][0]["content"].startswith(
        "Extract factual relationships stated in the supplied conversation data."
    )
    assert json.loads(calls[0]["messages"][1]["content"]) == {
        "allowed_verbs": ["uses"],
        "rule_findings": [],
        "turns": [{"span_id": 7, "text": "Ray uses Hermes."}],
    }


@pytest.mark.asyncio
async def test_extractor_surfaces_invalid_json() -> None:
    async def create(**_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))]
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    extractor = OpenAIChatModelExtractor(client, "test-model", timeout_s=2.0)
    with pytest.raises(ValueError, match="invalid JSON"):
        await extractor(_batch())


@pytest.mark.asyncio
async def test_extractor_surfaces_empty_response() -> None:
    async def create(**_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=""))]
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    extractor = OpenAIChatModelExtractor(client, "test-model", timeout_s=2.0)
    with pytest.raises(ValueError, match="empty response"):
        await extractor(_batch())


@pytest.mark.asyncio
async def test_extractor_rejects_malformed_claims() -> None:
    async def create(**_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"claims": [{"span_id": 7, "subject": "Ray"}]})
                    )
                )
            ]
        )

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    extractor = OpenAIChatModelExtractor(client, "test-model", timeout_s=2.0)
    with pytest.raises(ValueError, match="claim fields must be strings"):
        await extractor(_batch())


def test_response_format_json_object_vs_json_schema() -> None:
    from hermes_memory.extract_model_client import response_format_for

    assert response_format_for("json_object", {"uses", "lives_in"}) == {"type": "json_object"}
    schema = response_format_for("json_schema", {"uses", "lives_in"})
    assert schema["type"] == "json_schema"
    verb_enum = schema["json_schema"]["schema"]["properties"]["claims"]["items"]["properties"]["verb"]["enum"]
    assert verb_enum == ["lives_in", "uses"]
