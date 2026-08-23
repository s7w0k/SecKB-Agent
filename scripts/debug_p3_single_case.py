"""P3 调试：单 case 逐步执行并打印进度（定位挂起点）。"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.bootstrap import create_schema, seed_data
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.rag_eval.dataset_schema import load_dataset
from app.rag_eval.pipeline import replay_case
from app.rag_eval.providers import build_answer_provider, build_embedding_provider, build_judge_provider, build_ragas_embeddings, build_ragas_llm
from app.rag_eval.ragas_metrics import DEFAULT_METRICS, evaluate_metrics
from app.services.knowledge import KnowledgeService


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    case_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    settings = get_settings()
    _, cases = load_dataset("data/eval/smoke/rag-smoke.json", "rag")
    case = cases[case_index]
    log(f"case={case['id']} domain={case['domain']}")

    answer_provider = build_answer_provider(settings, mock=False)
    emb_provider = build_embedding_provider(settings, mock=False)
    log(f"providers ready; answer={settings.answer_settings[2]}@judge={settings.judge_settings[2]}")

    log("create_schema + seed_data ...")
    create_schema()
    db = SessionLocal()
    seed_data(db)
    service = KnowledgeService(db, settings)
    log("service ready")

    log("replay_case (route/retrieve/generate) ...")
    t0 = time.time()
    replay = replay_case(case, service=service, chat_provider=answer_provider, top_k=4)
    log(f"replay done in {time.time() - t0:.1f}s; answer_len={len(replay['answer'])} contexts={len(replay['contexts'])}")

    log("build_ragas_llm/embeddings ...")
    judge_provider = build_judge_provider(settings, mock=False)
    ragas_llm = build_ragas_llm(judge_provider)
    ragas_embeddings = build_ragas_embeddings(emb_provider)
    log("ragas adapters ready")

    log(f"evaluate_metrics {list(DEFAULT_METRICS)} ...")
    t0 = time.time()
    scores = evaluate_metrics([replay], list(DEFAULT_METRICS), llm=ragas_llm, embeddings=ragas_embeddings)
    log(f"evaluate done in {time.time() - t0:.1f}s -> {scores}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
