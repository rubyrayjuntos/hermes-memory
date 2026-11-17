from __future__ import annotations

import pytest

from hermes_memory.retrieve_hybrid import retrieve_hybrid


class FakeStore:
    async def claim_vector_search(self, owner, _vec_literal, _limit):
        assert owner == "owner-a"
        return [{
            "claim_id": 7,
            "subject_id": 11,
            "object_id": 12,
            "fact_text": "Acme uses Postgres.",
            "fact_vec": "[1,0]",
        }]

    async def link_entities(self, owner, _phrases, _vec_literal):
        assert owner == "owner-a"
        return [{"entity_id": 11}]

    async def turn_entities(self, owner, turn_ids):
        assert owner == "owner-a"
        assert turn_ids == [914]
        return [{"span_id": 914, "entity_id": 11}]

    async def claims_for_turns(self, owner, turn_ids):
        assert owner == "owner-a"
        assert turn_ids == [914]
        return ["7"]

    async def claim_edges(self, owner, entity_ids, _vec_literal, per_node):
        assert owner == "owner-a"
        assert entity_ids == [11, 12]
        assert per_node == 8
        return []

    async def claims_by_ids(self, owner, claim_ids):
        assert owner == "owner-a"
        assert claim_ids == ["7"]
        return [{
            "claim_id": 7,
            "status": "grounded",
            "fact_text": "Acme uses Postgres.",
            "fact_vec": "[1,0]",
            "span_id": 914,
            "event_time": None,
            "role": "user",
            "content": "Acme uses Postgres.",
            "session_id": "session-a",
        }]


@pytest.mark.asyncio
async def test_hybrid_retrieval_returns_grounded_cited_facts():
    result = await retrieve_hybrid(
        FakeStore(),
        "owner-a",
        "What database does Acme use?",
        "[1,0]",
        [{"id": "conv_914", "src": "conversation"}],
    )

    assert result["ids"] == ["7"]
    assert result["facts"] == [{
        "claim_id": "7",
        "status": "grounded",
        "fact_text": "Acme uses Postgres.",
        "event_time": None,
        "supporting_turns": [{
            "span_id": 914,
            "event_time": None,
            "role": "user",
            "content": "Acme uses Postgres.",
            "ts": None,
            "session_id": "session-a",
        }],
    }]
