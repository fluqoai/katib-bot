"""Unit tests for the RAG module — no external services needed."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rag.prompts import build_system_prompt, build_user_prompt, load_skill_body
from rag.types import (
    Category,
    GeneratedDraft,
    PlaceholderField,
    RetrievedContext,
    SearchResult,
)


SKILL_PATH = Path(
    "C:/Users/khayrat/.minimax/skills/arabic-official-correspondence"
)


pytestmark = pytest.mark.unit


# ---- load_skill_body --------------------------------------------------------

def test_load_skill_body_strips_frontmatter():
    body = load_skill_body(SKILL_PATH)
    # Frontmatter is stripped — body should not start with "---"
    assert not body.startswith("---")
    # Body should still contain a known heading from the skill
    assert "مسودة" in body or "إجراء" in body or "خطاب" in body


def test_load_skill_body_raises_when_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="arabic-official-correspondence"):
        load_skill_body(tmp_path)


# ---- build_system_prompt ----------------------------------------------------

def test_build_system_prompt_includes_skill_body():
    prompt = build_system_prompt(SKILL_PATH)
    assert "كاتب" in prompt
    # Should include Kateb-specific guard rails
    assert "لا تخترع" in prompt
    assert "مسودة" in prompt
    # Should embed the JSON output schema
    assert "body_markdown" in prompt
    assert "placeholders" in prompt


# ---- build_user_prompt ------------------------------------------------------

def _mk_ctx() -> RetrievedContext:
    return RetrievedContext(
        templates=[
            SearchResult(
                id="t1", document_id="d1", chunk_index=0,
                content="نموذج طلب شراكة رسمي.",
                category="templates", title="نموذج طلب شراكة",
                source_uri="drive://templates/partnership.md",
                similarity=0.92,
            ),
        ],
        national_regulations=[
            SearchResult(
                id="r1", document_id="d2", chunk_index=0,
                content="نظام الجمعيات الأهلية.",
                category="national_regulations", title="نظام الجمعيات الأهلية",
                source_uri="https://example.gov/reg.pdf",
                similarity=0.88,
            ),
        ],
        internal_policies=[
            SearchResult(
                id="p1", document_id="d3", chunk_index=0,
                content="اللائحة الداخلية للشراكات.",
                category="internal_policies", title="لائحة الشراكات",
                source_uri="drive://internal-policies/partnerships.md",
                similarity=0.81,
            ),
        ],
    )


def test_build_user_prompt_includes_request_and_context():
    ctx = _mk_ctx()
    prompt = build_user_prompt("اكتب لي خطاب شراكة", ctx, {"المرسل": "خالد"})
    assert "اكتب لي خطاب شراكة" in prompt
    assert "المرسل" in prompt and "خالد" in prompt
    # All three categories should be present
    assert "### templates" in prompt
    assert "### national_regulations" in prompt
    assert "### internal_policies" in prompt


def test_build_user_prompt_marks_empty_categories():
    ctx = RetrievedContext()  # all empty
    prompt = build_user_prompt("test", ctx)
    for cat in ("templates", "national_regulations", "internal_policies"):
        assert f"### {cat}" in prompt
        assert "لا توجد مراجع" in prompt


# ---- DraftGenerator._parse_response ----------------------------------------

def test_parse_response_valid_json():
    from rag.generator import DraftGenerator
    raw = json.dumps({
        "title": "طلب شراكة",
        "body_markdown": "# مسودة — للمراجعة\n\nبسم الله...",
        "placeholders": [
            {"field": "التاريخ", "required": True, "hint": "ميلادي/هجري"},
        ],
        "sources": [
            {"document_id": "d1", "title": "نموذج", "category": "templates",
             "contribution": "الهيكل العام"},
        ],
        "checklist": ["املأ التاريخ"],
        "self_audit": {
            "draft_label": True,
            "sources_cited": True,
            "no_invented_values": True,
            "rtl_layout": True,
            "three_categories_covered": ["templates", "national_regulations", "internal_policies"],
        },
    })
    draft = DraftGenerator._parse_response(raw)
    assert draft.body.startswith("# مسودة")
    assert len(draft.placeholders) == 1
    assert draft.placeholders[0].field == "التاريخ"
    assert draft.placeholders[0].required is True
    assert len(draft.sources) == 1


def test_parse_response_strips_code_fence():
    from rag.generator import DraftGenerator
    raw = "```json\n" + json.dumps({
        "title": "t", "body_markdown": "b", "placeholders": [], "sources": [],
    }) + "\n```"
    draft = DraftGenerator._parse_response(raw)
    assert draft.body == "b"


def test_parse_response_handles_prose_wrapped():
    from rag.generator import DraftGenerator
    inner = json.dumps({"title": "t", "body_markdown": "b", "placeholders": [], "sources": []})
    raw = f"Here is the draft:\n\n{inner}\n\nLet me know."
    draft = DraftGenerator._parse_response(raw)
    assert draft.body == "b"


def test_parse_response_raises_on_invalid_json():
    from rag.generator import DraftGenerator
    with pytest.raises(ValueError, match="invalid JSON"):
        DraftGenerator._parse_response("not json at all")
