"""Opt in with RUN_LIVE_MODEL=1; these checks call OpenAI with synthetic portal data."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from test_agent import PortalState, email, metadata

from docket.agent import MODEL, run_agent
from docket.models import DocType

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_MODEL") != "1", reason="Requires explicit live-model opt-in"
)
ROOT = Path(__file__).resolve().parents[1]
RUN_ID = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]


async def check_case(label, text, count=1, *, category=DocType.TRANSCRIPTS, subject=""):
    load_dotenv(ROOT / ".env")
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY is required for live checks"
    directory = ROOT / "data" / "model-checks" / RUN_ID / label
    meta = metadata(0)
    meta.counts[category] = count
    portal = PortalState(meta)
    inbound = email(text).model_copy(update={"subject": subject})
    result = await run_agent(inbound, directory, portal_factory=portal.factory)
    events = [json.loads(line) for line in (directory / "actions.jsonl").read_text().splitlines()]
    evidence = {
        "case": label,
        "model": MODEL,
        "source": "Synthetic portal data; real OpenAI model and SDK tool loop",
        "synthetic_counts": meta.model_dump(mode="json")["counts"],
        "decision": result.decision.model_dump(mode="json"),
        "attached_originals": sum(bundle.file_count for bundle in result.bundles),
        "events": events,
    }
    (directory / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return result, portal, events


@pytest.mark.parametrize("count", [0, 1], ids=["empty", "available"])
async def test_real_model_uses_observed_availability(count):
    result, portal, events = await check_case(
        f"availability-{count}",
        "Check whether M12205 has Transcripts, and retrieve them if available.",
        count,
    )
    tools = [event["tool"] for event in events if event["event"] == "tool_call"]
    assert tools and tools[0] == "inspect_matter"
    assert ("retrieve_documents" in tools) is bool(count)
    assert portal.inspections == ["M12205"]
    assert len(portal.requests) == count
    assert result.decision.action == ("deliver" if count else "report")
    assert result.decision.reason == ("completed" if count else "empty_category")
    assert sum(bundle.file_count for bundle in result.bundles) == count
    assert any(event["event"] == "model_turn" for event in events)


@pytest.mark.parametrize(
    "label,text,action",
    [
        ("missing-matter", "Could you send me Transcripts?", "clarify"),
        ("missing-category", "Please send documents for M12205.", "clarify"),
        ("conflicting-matters", "Send Transcripts from M12205 and M12383.", "clarify"),
        ("conflicting-categories", "Send Exhibits or Transcripts from M12205.", "clarify"),
        ("acknowledgement", "Great, thanks!", "no_reply"),
        ("informal-clear", "yo can you grab transcripts from m12205 please?", "deliver"),
    ],
)
async def test_real_model_interprets_requests(label, text, action):
    result, portal, events = await check_case(label, text)
    assert result.decision.action == action
    assert any(event["event"] == "model_turn" for event in events)
    if action == "deliver":
        assert len(portal.requests) == 1
        assert sum(bundle.file_count for bundle in result.bundles) == 1
    else:
        assert portal.requests == []
        assert result.bundles == []
    assert (result.reply is None) == (action == "no_reply")


@pytest.mark.parametrize(
    "label,text,action",
    [
        ("thanks-with-matter", "Thanks for M12205!", "no_reply"),
        (
            "thanks-with-new-request",
            "Thanks! Please send one Other Document from M12205.",
            "deliver",
        ),
    ],
)
async def test_real_model_distinguishes_thanks_from_new_request(label, text, action):
    result, portal, events = await check_case(label, text, category=DocType.OTHER_DOCUMENTS)
    assert result.decision.action == action
    assert any(event["event"] == "model_turn" for event in events)
    if action == "deliver":
        assert len(portal.requests) == 1
        request = portal.requests[0]
        assert (request.matter, request.doc_type, request.limit) == (
            "M12205",
            DocType.OTHER_DOCUMENTS,
            1,
        )
        assert sum(bundle.file_count for bundle in result.bundles) == 1
        assert result.reply is not None
    else:
        assert portal.inspections == portal.requests == []
        assert result.reply is None
        assert result.bundles == []


async def test_real_model_uses_subject_request_when_body_is_a_signoff():
    result, portal, events = await check_case(
        "subject-request-with-signoff",
        "Thanks, Jane",
        category=DocType.OTHER_DOCUMENTS,
        subject="Please send 1 Other Document from M12205",
    )
    assert result.decision.action == "deliver"
    assert len(portal.requests) == 1
    request = portal.requests[0]
    assert (request.matter, request.doc_type, request.limit) == (
        "M12205",
        DocType.OTHER_DOCUMENTS,
        1,
    )
    assert any(event.get("tool") == "retrieve_documents" for event in events)
    assert sum(bundle.file_count for bundle in result.bundles) == 1
    assert result.reply is not None
