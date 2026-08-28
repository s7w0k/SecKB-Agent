"""端到端 RAG Gold 的本地人工复核界面。

启动后仅监听本机，读取 candidate/corpus，断点保存人工结论，并在全部完成后
导出首轮 human-semantic Gold。它不会把首轮复核伪装成双人盲审；首次导出的
AnnotationEvidence 故意保留 passage_jaccard/source_agreement=null。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.rag_eval.annotation_evidence import GOLD_VERSION


Decision = Literal["pass", "modify", "uncertain"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number} JSON 非法: {exc}") from exc
    return rows


def _atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    temp.replace(path)


def _read_selection(path: Path | None) -> list[str] | None:
    if path is None:
        return None
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "query_id" not in reader.fieldnames:
            raise ValueError("selection CSV 必须包含 query_id 列")
        return [str(row["query_id"]).strip() for row in reader if row.get("query_id")]


class ReviewPayload(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=120)
    decision: Decision
    question_ok: bool
    category_ok: bool
    evidence_ok: bool
    answer_points_ok: bool
    behavior_ok: bool
    edited_question: str | None = None
    edited_category: str | None = None
    edited_answer_points: list[str] = Field(default_factory=list)
    edited_expected_behavior: str | None = None
    edited_should_abstain: bool | None = None
    selected_evidence_ids: list[str] = Field(default_factory=list)
    forbidden_evidence_ids: list[str] = Field(default_factory=list)
    forbidden_citation_ids: list[str] = Field(default_factory=list)
    injection_evidence_ids: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=5000)


class ExportPayload(BaseModel):
    confirm_primary_human_review: bool = False


class ReviewStore:
    def __init__(
        self,
        dataset_path: Path,
        corpus_path: Path,
        session_path: Path,
        export_dir: Path,
        selection_path: Path | None = None,
    ) -> None:
        self.dataset_path = dataset_path.resolve()
        self.corpus_path = corpus_path.resolve()
        self.session_path = session_path.resolve()
        self.export_dir = export_dir.resolve()
        all_cases = _load_jsonl(self.dataset_path)
        ids = _read_selection(selection_path)
        case_map = {str(case.get("query_id") or ""): case for case in all_cases}
        if len(case_map) != len(all_cases) or "" in case_map:
            raise ValueError("dataset query_id 缺失或重复")
        if ids is not None:
            missing = [qid for qid in ids if qid not in case_map]
            if missing:
                raise ValueError(f"selection 中有 {len(missing)} 个未知 query_id")
            self.cases = [case_map[qid] for qid in ids]
        else:
            self.cases = all_cases
        self.case_map = {str(case["query_id"]): case for case in self.cases}
        corpus_rows = _load_jsonl(self.corpus_path)
        self.corpus = {str(row.get("stable_key") or ""): row for row in corpus_rows}
        self.corpus_by_domain: dict[str, list[str]] = {}
        for key in self.corpus:
            domain = key.split(":", 1)[0]
            self.corpus_by_domain.setdefault(domain, []).append(key)
        self.reviews: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.dataset_sha256 = _sha256(self.dataset_path)
        self.corpus_sha256 = _sha256(self.corpus_path)
        self._load_session()

    def _load_session(self) -> None:
        if not self.session_path.exists():
            return
        for row in _load_jsonl(self.session_path):
            qid = str(row.get("query_id") or "")
            if row.get("dataset_sha256") != self.dataset_sha256:
                raise ValueError(
                    f"session 与当前 dataset hash 不一致，请换一个 session 文件: {self.session_path}"
                )
            if row.get("corpus_sha256") != self.corpus_sha256:
                raise ValueError(
                    f"session 与当前 corpus hash 不一致，请换一个 session 文件: {self.session_path}"
                )
            if qid in self.case_map:
                self.reviews[qid] = row

    def _persist(self) -> None:
        rows = [self.reviews[qid] for qid in self.case_map if qid in self.reviews]
        _atomic_write_jsonl(self.session_path, rows)

    def progress(self) -> dict[str, Any]:
        decisions = Counter(review.get("decision") for review in self.reviews.values())
        completed = decisions.get("pass", 0) + decisions.get("modify", 0)
        return {
            "total": len(self.cases),
            "saved": len(self.reviews),
            "completed": completed,
            "pass": decisions.get("pass", 0),
            "modify": decisions.get("modify", 0),
            "uncertain": decisions.get("uncertain", 0),
            "remaining": len(self.cases) - completed,
            "percent": round(100 * completed / len(self.cases), 1) if self.cases else 0.0,
        }

    def list_cases(
        self, category: str = "", decision: str = "", search: str = ""
    ) -> list[dict[str, Any]]:
        search_folded = search.strip().casefold()
        rows: list[dict[str, Any]] = []
        for index, case in enumerate(self.cases):
            qid = str(case["query_id"])
            review = self.reviews.get(qid)
            current_decision = str(review.get("decision")) if review else "pending"
            if category and case.get("category") != category:
                continue
            if decision and current_decision != decision:
                continue
            if search_folded and search_folded not in (
                f"{qid} {case.get('question', '')} {case.get('category', '')}".casefold()
            ):
                continue
            rows.append({
                "index": index,
                "query_id": qid,
                "category": case.get("category"),
                "question": case.get("question"),
                "decision": current_decision,
            })
        return rows

    def _candidate_passages(self, case: dict[str, Any]) -> list[dict[str, Any]]:
        role_keys = list(dict.fromkeys(
            list(case.get("required_evidence_ids") or [])
            + list(case.get("forbidden_evidence_ids") or [])
            + list(case.get("forbidden_citation_ids") or [])
            + list(case.get("injection_evidence_ids") or [])
            + list(case.get("conflicting_evidence_ids") or [])
        ))
        answer_points = [str(point) for point in case.get("answer_points") or [] if len(str(point)) >= 12]

        def supports_answer(key: str) -> bool:
            content = str(self.corpus.get(key, {}).get("content") or "")
            return any(point in content for point in answer_points)

        distractors = [
            key for key in self.corpus_by_domain.get(str(case.get("domain") or ""), [])
            if key not in role_keys and not supports_answer(key)
        ]
        rng = random.Random(hashlib.sha256(str(case["query_id"]).encode()).hexdigest())
        rng.shuffle(distractors)
        keys = role_keys + distractors[:2]
        rng.shuffle(keys)
        return [
            {
                "stable_key": key,
                "content": self.corpus.get(key, {}).get("content", ""),
                "metadata": {
                    "organization_id": self.corpus.get(key, {}).get("organization_id"),
                    "workspace_id": self.corpus.get(key, {}).get("workspace_id"),
                    "classification_level": self.corpus.get(key, {}).get("classification_level"),
                    "generation_id": self.corpus.get(key, {}).get("generation_id"),
                },
            }
            for key in keys if key in self.corpus
        ]

    def get_case(self, qid: str, reveal: bool = False) -> dict[str, Any]:
        case = self.case_map.get(qid)
        if case is None:
            raise KeyError(qid)
        result = {
            "case": case,
            "candidate_passages": self._candidate_passages(case),
            "review": self.reviews.get(qid),
            "position": next(i for i, row in enumerate(self.cases) if row["query_id"] == qid),
            "total": len(self.cases),
        }
        if reveal:
            result["proposed_roles"] = {
                "required_evidence_ids": case.get("required_evidence_ids") or [],
                "forbidden_evidence_ids": case.get("forbidden_evidence_ids") or [],
                "forbidden_citation_ids": case.get("forbidden_citation_ids") or [],
                "injection_evidence_ids": case.get("injection_evidence_ids") or [],
                "conflicting_evidence_ids": case.get("conflicting_evidence_ids") or [],
            }
        return result

    def save_review(self, qid: str, payload: ReviewPayload) -> dict[str, Any]:
        if qid not in self.case_map:
            raise KeyError(qid)
        data = payload.model_dump()
        if payload.decision == "modify":
            if not payload.notes.strip():
                raise ValueError("选择“修改”时必须填写修改说明")
            if payload.edited_question is not None and not payload.edited_question.strip():
                raise ValueError("修改后的问题不能为空")
            if payload.edited_answer_points and not all(p.strip() for p in payload.edited_answer_points):
                raise ValueError("修改后的答案要点不能包含空项")
            if not payload.question_ok and not payload.edited_question:
                raise ValueError("题面不通过时必须填写修改后的问题")
            if not payload.category_ok and not payload.edited_category:
                raise ValueError("场景分类不通过时必须选择新的分类")
            if not payload.answer_points_ok and not payload.edited_answer_points:
                raise ValueError("答案要点不通过时必须填写修改后的答案要点")
            if not payload.behavior_ok and not (
                payload.edited_expected_behavior or payload.edited_should_abstain is not None
            ):
                raise ValueError("预期行为不通过时必须修改行为或拒答设置")
        if payload.decision == "pass" and not all((
            payload.question_ok,
            payload.category_ok,
            payload.evidence_ok,
            payload.answer_points_ok,
            payload.behavior_ok,
        )):
            raise ValueError("标记为通过前必须确认五个检查项")
        data.update({
            "query_id": qid,
            "dataset_sha256": self.dataset_sha256,
            "corpus_sha256": self.corpus_sha256,
            "updated_at": _utc_now(),
        })
        with self.lock:
            self.reviews[qid] = data
            self._persist()
        return {"review": data, "progress": self.progress()}

    def export_primary_gold(self, confirmed: bool) -> dict[str, Any]:
        progress = self.progress()
        if not confirmed:
            raise ValueError("必须确认这些记录来自真实人工逐条复核")
        if progress["completed"] != len(self.cases) or progress["uncertain"]:
            raise ValueError(
                f"尚不能导出：completed={progress['completed']}/{len(self.cases)}, "
                f"uncertain={progress['uncertain']}"
            )
        reviewer_ids = sorted({
            str(review.get("reviewer_id") or "").strip()
            for review in self.reviews.values()
            if str(review.get("reviewer_id") or "").strip()
        })
        if not reviewer_ids:
            raise ValueError("缺少 reviewer_id")

        exported: list[dict[str, Any]] = []
        for source in self.cases:
            case = json.loads(json.dumps(source, ensure_ascii=False))
            qid = str(case["query_id"])
            review = self.reviews[qid]
            if review["decision"] == "modify":
                if review.get("edited_question"):
                    case["question"] = review["edited_question"].strip()
                if review.get("edited_category"):
                    case["category"] = review["edited_category"].strip()
                if review.get("edited_answer_points"):
                    case["answer_points"] = [p.strip() for p in review["edited_answer_points"]]
                if review.get("edited_expected_behavior"):
                    case["expected_retrieval_behavior"] = review["edited_expected_behavior"].strip()
                if review.get("edited_should_abstain") is not None:
                    case["should_abstain"] = bool(review["edited_should_abstain"])
                if not review.get("evidence_ok"):
                    selected = list(dict.fromkeys(review["selected_evidence_ids"]))
                    case["required_evidence_ids"] = selected
                    case["required_passage_groups"] = [[key] for key in selected]
                    case["required_source_ids"] = (
                        sorted({key.rsplit(":", 2)[0] for key in selected})
                        or [f"{case.get('domain', 'UNKNOWN')}:no-evidence"]
                    )
                    case["preferred_evidence_ids"] = selected
                    case["expected_citation_ids"] = selected
                    for field in (
                        "forbidden_evidence_ids",
                        "forbidden_citation_ids",
                        "injection_evidence_ids",
                    ):
                        case[field] = list(dict.fromkeys(review[field]))
            case["reviewed"] = True
            case["annotation_version"] = GOLD_VERSION
            case["annotation_confidence"] = "high"
            note = f"primary human review by {review['reviewer_id']} at {review['updated_at']}"
            case["notes"] = f"{case.get('notes', '')} | {note}".strip(" |")
            exported.append(case)

        self.export_dir.mkdir(parents=True, exist_ok=True)
        gold_path = self.export_dir / "human-reviewed-e2e-release-core-200-v1.jsonl"
        _atomic_write_jsonl(gold_path, exported)
        completed_at = _utc_now()
        evidence = {
            "method": "human_semantic",
            "total_cases": len(exported),
            "human_reviewed_cases": len(exported),
            "review_ratio": 1.0,
            "reviewer_count": len(reviewer_ids),
            "source_agreement": None,
            "passage_jaccard": None,
            "completed_at": completed_at,
            "reviewer_ids": reviewer_ids,
            "dataset_sha256": self.dataset_sha256,
            "corpus_sha256": self.corpus_sha256,
            "review_session": str(self.session_path),
            "review_stage": "primary_only",
        }
        evidence_path = self.export_dir / "e2e-annotation-evidence-core-200-primary-human-v1.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "gold": str(gold_path),
            "annotation_evidence": str(evidence_path),
            "gold_sha256": _sha256(gold_path),
            "cases": len(exported),
            "reviewers": reviewer_ids,
            "stage": "primary_only",
            "release_gate_expected": False,
            "next_step": "第二位复核者对固定 60 条做盲审并计算一致性",
        }
        summary_path = self.export_dir / "primary-review-export-summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary


def create_review_app(store: ReviewStore) -> FastAPI:
    static_dir = Path(__file__).with_name("e2e_review_ui")
    app = FastAPI(title="MindBridge E2E RAG 人工复核", version="1.0.0")
    app.state.review_store = store
    app.mount("/assets", StaticFiles(directory=static_dir), name="review-assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return {
            "dataset": str(store.dataset_path),
            "dataset_sha256": store.dataset_sha256,
            "corpus": str(store.corpus_path),
            "corpus_sha256": store.corpus_sha256,
            "session": str(store.session_path),
            "categories": sorted({str(case.get("category") or "") for case in store.cases}),
            "progress": store.progress(),
        }

    @app.get("/api/cases")
    def list_cases(
        category: str = Query(default=""),
        decision: str = Query(default=""),
        search: str = Query(default=""),
    ) -> dict[str, Any]:
        rows = store.list_cases(category=category, decision=decision, search=search)
        return {"items": rows, "count": len(rows), "progress": store.progress()}

    @app.get("/api/cases/{query_id}")
    def get_case(query_id: str, reveal: bool = Query(default=False)) -> dict[str, Any]:
        try:
            return store.get_case(query_id, reveal=reveal)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="query_id 不存在") from exc

    @app.put("/api/reviews/{query_id}")
    def save_review(query_id: str, payload: ReviewPayload) -> dict[str, Any]:
        try:
            return store.save_review(query_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="query_id 不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/export")
    def export(payload: ExportPayload) -> dict[str, Any]:
        try:
            return store.export_primary_gold(payload.confirm_primary_human_review)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 E2E RAG Gold 本地人工复核界面")
    parser.add_argument(
        "--dataset",
        default="data/eval/rag-data-plane/e2e-release-v1/e2e-release-human-core-200-v1.jsonl",
    )
    parser.add_argument(
        "--corpus",
        default="data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl",
    )
    parser.add_argument(
        "--session",
        default="target/rag-benchmark/e2e-human-review/core-200-primary-review-session-v1.jsonl",
    )
    parser.add_argument(
        "--out",
        default="target/rag-benchmark/e2e-human-review/core-200-primary-export-v1",
    )
    parser.add_argument("--selection-csv", default=None, help="可选：只审核 CSV query_id 子集")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    store = ReviewStore(
        Path(args.dataset),
        Path(args.corpus),
        Path(args.session),
        Path(args.out),
        Path(args.selection_csv) if args.selection_csv else None,
    )
    app = create_review_app(store)
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
