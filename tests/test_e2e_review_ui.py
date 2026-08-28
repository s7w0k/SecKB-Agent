from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.annotation.e2e_review_ui import ReviewPayload, ReviewStore


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _fixture(tmp_path: Path) -> ReviewStore:
    dataset = tmp_path / "candidate.jsonl"
    corpus = tmp_path / "corpus.jsonl"
    _write_jsonl(dataset, [{
        "query_id": "q-1",
        "question": "《制度 A》的审批要求是什么？",
        "domain": "COMPLIANCE",
        "category": "Single-hop",
        "required_passage_groups": [["COMPLIANCE:a.md:1:0"]],
        "required_source_ids": ["COMPLIANCE:a.md"],
        "required_evidence_ids": ["COMPLIANCE:a.md:1:0"],
        "forbidden_evidence_ids": [],
        "answer_points": ["所有审批必须由两人完成。"],
        "expected_retrieval_behavior": "single_retrieve",
        "should_abstain": False,
        "preferred_evidence_ids": ["COMPLIANCE:a.md:1:0"],
        "expected_citation_ids": ["COMPLIANCE:a.md:1:0"],
        "forbidden_citation_ids": [],
        "injection_evidence_ids": [],
        "conflicting_evidence_ids": [],
        "reviewed": False,
        "annotation_version": "candidate-v1",
    }])
    _write_jsonl(corpus, [
        {
            "stable_key": "COMPLIANCE:a.md:1:0",
            "content": "# 制度 A 所有审批必须由两人完成。",
            "organization_id": 1,
            "workspace_id": 1,
            "classification_level": 1,
            "generation_id": "G002",
        },
        {
            "stable_key": "COMPLIANCE:b.md:1:0",
            "content": "# 无关制度 B 记录保存五年。",
            "organization_id": 1,
            "workspace_id": 1,
            "classification_level": 1,
            "generation_id": "G002",
        },
    ])
    return ReviewStore(dataset, corpus, tmp_path / "session.jsonl", tmp_path / "out")


def _pass_payload() -> ReviewPayload:
    return ReviewPayload(
        reviewer_id="reviewer-01",
        decision="pass",
        question_ok=True,
        category_ok=True,
        evidence_ok=True,
        answer_points_ok=True,
        behavior_ok=True,
    )


def test_review_store_saves_and_resumes_by_hash(tmp_path: Path):
    store = _fixture(tmp_path)
    assert store.progress()["remaining"] == 1
    result = store.save_review("q-1", _pass_payload())
    assert result["progress"]["pass"] == 1

    resumed = ReviewStore(
        store.dataset_path,
        store.corpus_path,
        store.session_path,
        store.export_dir,
    )
    assert resumed.progress()["completed"] == 1
    assert resumed.get_case("q-1")["review"]["reviewer_id"] == "reviewer-01"


def test_pass_requires_all_checks(tmp_path: Path):
    store = _fixture(tmp_path)
    payload = _pass_payload()
    payload.evidence_ok = False
    with pytest.raises(ValueError, match="五个检查项"):
        store.save_review("q-1", payload)


def test_export_is_primary_only_and_keeps_double_review_gate_open(tmp_path: Path):
    store = _fixture(tmp_path)
    with pytest.raises(ValueError, match="尚不能导出"):
        store.export_primary_gold(True)
    store.save_review("q-1", _pass_payload())
    result = store.export_primary_gold(True)
    gold = _load(Path(result["gold"]))
    evidence = json.loads(Path(result["annotation_evidence"]).read_text(encoding="utf-8"))
    assert gold[0]["reviewed"] is True
    assert gold[0]["annotation_version"] == "human-semantic-v1"
    assert evidence["method"] == "human_semantic"
    assert evidence["passage_jaccard"] is None
    assert result["release_gate_expected"] is False


def test_case_packet_is_blind_until_revealed(tmp_path: Path):
    store = _fixture(tmp_path)
    blind = store.get_case("q-1", reveal=False)
    revealed = store.get_case("q-1", reveal=True)
    assert "proposed_roles" not in blind
    assert revealed["proposed_roles"]["required_evidence_ids"] == ["COMPLIANCE:a.md:1:0"]
    assert len(blind["candidate_passages"]) == 2


def test_frontend_has_explicit_five_check_helper_without_auto_decision():
    asset_dir = Path("tools/annotation/e2e_review_ui")
    html = (asset_dir / "index.html").read_text(encoding="utf-8")
    javascript = (asset_dir / "app.js").read_text(encoding="utf-8")
    assert 'id="passAllChecksBtn"' in html
    assert "function passAllChecks()" in javascript
    for field in ("questionOk", "categoryOk", "evidenceOk", "answerPointsOk", "behaviorOk"):
        assert f"els.{field}" in javascript
    helper_body = javascript.split("function passAllChecks()", 1)[1].split("\n}", 1)[0]
    assert "selectDecision" not in helper_body
    assert "saveCurrent" not in helper_body


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
