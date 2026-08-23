"""Debug: factual_correctness 单 case 真实 judge 原始输出。"""
import time

from app.rag_eval.providers import MockEmbeddingProvider, build_ragas_embeddings, build_ragas_llm
from app.rag_eval.providers import _ensure_ragas_importable

_ensure_ragas_importable()
import ragas
from datasets import Dataset


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


rows = [
    {
        "question": "学生明确表示想自杀时，系统应如何响应？",
        "answer": "当学生明确表示想自杀时，系统应按照高风险流程处置：立即触发预警、联系辅导员进行干预。",
        "contexts": ["明确提到自杀计划时触发预警并联系辅导员。"],
        "reference": "按 HIGH 风险处置，触发预警并联系辅导员。",
    }
]
from app.rag_eval.providers import build_chat_provider
from app.core.config import get_settings

settings = get_settings()
log(f"judge={settings.judge_settings[2]}@{settings.judge_settings[0]}")
llm = build_ragas_llm(build_chat_provider(settings, mock=False))
emb = build_ragas_embeddings(MockEmbeddingProvider(dim=8))
from ragas.metrics import FactualCorrectness

metric = FactualCorrectness(name="factual_correctness_f1")
log("evaluating factual_correctness_f1 (raw)...")
ev = ragas.evaluate(Dataset.from_list(rows), metrics=[metric], llm=llm, embeddings=emb, raise_exceptions=False, show_progress=False)
df = ev.to_pandas()
log("columns: " + ", ".join(str(c) for c in df.columns))
for _, row in df.iterrows():
    log(f"factual_correctness_f1 -> {row['factual_correctness_f1']!r}")
    log(f"response -> {row['response']!r}")
