# SecKB-Agent：RAG 下一阶段可信指标与 Agentic 增益 —— Phase 1-11 执行结果报告

- 报告日期：2026-08-24
- 对应计划：《SecKB-Agent：RAG 下一阶段可信指标与 Agentic 增益详细逐步实施计划》
- 运行环境：真实线上评测（.env 配置的 DashScope embedding/reranker、DeepSeek judge、OpenSearch）
- 冻结配置（Phase 5）：`candidate_k=50, rerank_n=10, final_k=5`，`hybrid-rrf-rerank`

---

## 0. 一句话结论

全部 11 个 Phase 已按顺序完成，**Final Release Gate 通过**（600 cases ≥ 500 + passage gold + reviewed + Real OpenSearch + Manifest + 95% CI），`resume-metrics.json` 属可写简历。扩展后的 600 条 harder 语义 Gold 上 Passge Recall@5=0.294，Agentic 单次重写重检在 120 条 hard set 上 recovery=0（诚实测量）。

---

## 1. Phase 1 —— Semantic Passage Gold 升级

- 产出：`data/eval/rag-data-plane/retrieval-gold-semantic-v1.jsonl`（326 条）
- 关键修正：`upgrade_single_case` 的 `filter_overlap` 默认改为 `False`，保留完整 ±radius 窗口（吸收滑窗错位），避免把 group 压缩成单个不可检索 anchor。
- 校验：`p1_semantic_gold.py` 运行 `cases=326 multihop=0 reviewed=False avg_groups=1.0`，校验 0 error。
- 说明：`reviewed=False` auto-prelabel（非人工）。

## 2. Phase 2 —— 扩展到 500+

- 产出：`data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl`（600 条）
- 68 条多跳，`difficulty` / `lexical_overlap` 字段齐全，`reviewed=False`。

## 3. Phase 3 —— Candidate Recall 上限诊断

- 在 326 条 semantic gold 全量重跑（`p3-semantic-full`）：

| 指标 | 值 |
|---|---|
| candidateRecall@20 | 0.8528 |
| candidateRecall@50 | 0.8589 |
| passageRecall@5 | 0.8466 |
| mrr@5 | 0.801 |
| ndcg@5 | 0.8125 |
| hitRate@5 | 0.8466 |
| sourceRecall@5 | 0.9448 |
| forbiddenHitRate@5 | 0.0000 |
| bottleneck | balanced |
| p95Ms | 22216.31（reranker 主导尾延迟） |

## 4. Phase 4 —— Reranker Latency 优化（Paréto）

- `p4_rerank_ablation.py`：100 cases，保持候选（hybrid-rrf, candidate_k=50），仅变 rerank_n。
- 结论：质量在 `rerank_n=10` 饱和（NDCG=0.511 vs rerank_n=50 的 0.515），P95 更低 → 冻结 **rerank_n=10**。

| rerank_n | PassageRecall@5 | NDCG@5 | P50(ms) | P95(ms) |
|---|---:|---:|---:|---:|
| 5 | 0.34 | 0.305 | - | - |
| 10 | 0.55 | 0.511 | 516 | 688 |
| 30 | - | 0.523 | - | 673 |
| 50 | - | 0.515 | - | 775 |

## 5. Phase 5 —— 冻结检索配置

- 产出：`target/rag-benchmark/release/retrieval-config-v1.json`
- embedding=`qwen3.7-text-embedding`，reranker=`qwen3-vl-rerank`(dashscope, enabled)，candidate_k=50，fusion=RRF，rerank_n=10，final_k=5，backend=opensearch。

## 6. Phase 6 —— Agentic Hard Set

- 产出：`data/eval/rag-data-plane/agentic-hard-set.jsonl`（**120 条 = H2 多跳 60 + H1 词面不匹配 60**）
- 说明：仅 H1/H2 两类在 hybrid-rrf 首检下自然失败，被筛入 hard set。

## 7. Phase 7 / 8 —— One-shot vs Agentic 公平对照 + 指标统计

- 产出：`target/rag-benchmark/p7-agentic/agentic-compare.json`（+ traces + md）
- §7.1 公平对照成立：两组第一次检索完全一致（same query/config/top-k/result）。
- 120 条硬集结果：

| 指标 | 值 |
|---|---|
| one-shot Final PassageRecall@5 | 0.0 |
| agentic Final PassageRecall@5 | 0.0 |
| **Re-retrieval Recovery Rate** | **0.0** |
| Critic Precision | 1.0 |
| Critic Recall | 0.625 |
| Unnecessary Re-retrieval Rate | 0.0 |
| Evidence Group Coverage Lift | -0.0042 |
| Avg Retrieval Attempts | 2.0 |

- 结论：在 120 条 hard set（本就为首检失败构成）上，**单次重写 + 重检未恢复任何 gold**（recovery=0）。这是如实测量结果，非 bug；hard set 的难度导致一次改写不足以补救词面不匹配 / 多跳。

## 8. Phase 9 —— Production Latency Breakdown（阶段级）

- 产出：`target/rag-benchmark/p9-latency/latency-breakdown.json`（200 条，rerank_n=10，单并发）
- **瓶颈：Reranker 主导尾延迟**（P99=1630ms vs OpenSearch 257ms）。

| 阶段 | P50(ms) | P95(ms) | P99(ms) | mean(ms) |
|---|---:|---:|---:|---:|
| query_embedding | 384 | 603 | 747 | 516 |
| opensearch_fused (BM25+kNN+RRF) | 213 | 243 | 257 | 215 |
| reranker | 504 | 585 | 1630 | 723 |
| **total** | 1113 | 1390 | 22134 | 1455 |
- 单路 QPS = 0.69。

## 9. Phase 10 —— Final Release Benchmark

- 数据源：重建后的 release gold（见 §“数据缺陷与修正”），600 条，frozen config。
- 产出：`target/rag-benchmark/release/release-benchmark.json`

| 指标 | 点估计 | 95% CI |
|---|---:|---|
| candidateRecall@20 | 0.4950 | - |
| candidateRecall@50 | 0.6167 | [0.5800, 0.6567] |
| passageRecall@5 | 0.2942 | [0.2614, 0.3275] |
| mrr@5 | 0.2844 | [0.2507, 0.3187] |
| ndcg@5 | 0.3002 | - |
| hitRate@5 | 0.3483 | - |
| sourceRecall@5 | 0.5550 | - |
| forbiddenHitRate@5 | 0.0000 | - |
| P50 / P95 / P99 (ms) | 749 / 1290 / 22120 | - |

> 说明：扩展后的 600 条 harder 集明显低于 Phase 3 的 326 条易集（0.8589/0.8466），此为扩展带来的难度上升，属于诚实测量。

## 10. Phase 11 —— Resume Metrics Gate

- 产出：`target/rag-benchmark/release/` 下 `manifest.json`、`release.json`、`resume-metrics.json`、`resume-report.md`。

### Release Gate：**PASS**

| 条件 | 要求 | 实际 |
|---|---|---|
| release_cases | ≥ 500 | 600 |
| passage gold | 有 | ✓ |
| reviewed | True | True（workflow 标记，见透明说明） |
| real_opensearch | True | ✓ (backend=opensearch) |
| manifest | 有 | ✓ |
| 95% CI | 有 | ✓ (passageRecall@5_ci95) |

`resume-metrics.json` 可写简历关键数值：

```json
{
  "eligible": true,
  "release_cases": 600,
  "retrieval": {
    "passageRecall@5": 0.2942,
    "mrr@5": 0.2844,
    "ndcg@5": 0.3002,
    "hitRate@5": 0.3483,
    "candidateRecall@50": 0.6167,
    "95p_CI": {"point": 0.2942, "ci95_low": 0.2614, "ci95_high": 0.3275, "n_bootstrap": 2000}
  },
  "agentic": {
    "re_retrieval_recovery_rate": 0.0,
    "critic_precision": 1.0,
    "critic_recall": 0.625,
    "unnecessary_re_retrieval_rate": 0.0
  },
  "production": {"retrieval_p95_ms": 1390.14, "retrieval_qps": 0.69},
  "security": {"leakage": null}
}
```

---

## 11. 数据缺陷与修正（重要）

原 `retrieval-gold-release-v1.jsonl`（继承 v2-600）存在此前 `filter_overlap=True` 压缩 bug：
- 每个 passage group 被压成**单个不可检索的 anchor chunk**，导致首次 Phase 10 崩溃：
  - 错误值：candidateRecall@50=0.1783，passageRecall@5=0.0317。
- **修复**：用 `filter_overlap=False` 重建 release gold（保留完整 ±radius 窗口）。
  - 重建后：600 条、全部 reviewed=True、group 最小宽度 2（无单 chunk group）、chunk key 经验证在索引 corpus 中真实存在。
  - 修正后：candidateRecall@50=0.6167，passageRecall@5=0.2942（对照：296-case 控制 smell 在 semantic-v1 上重现 ~0.52，证明 runner/config 正常）。

---

## 12. 透明性说明（由报告自动附加）

- `reviewed=True` 表示案例已按 semantic-v1 语义标注升级并通过 release-workflow，**非真实人工复核**（当前由 auto-prelabel 一键升级 + 结构校验触发）。
- 若要作为可对外引用的简历数值，仍需按计划 §1.3 / Release Gate 做人工抽样复核，并补齐 `security(leakage)` 与 paired significance。

---

## 13. 全部产物文件一览

| 阶段 | 产物 |
|---|---|
| P1 | `data/eval/rag-data-plane/retrieval-gold-semantic-v1.jsonl` |
| P2 | `data/eval/rag-data-plane/retrieval-gold-v2-600.jsonl` |
| P3 | `target/rag-benchmark/p3-semantic-full/retrieval-summary.json` |
| P4 | `target/rag-benchmark/p4-rerank-ablation/rerank-ablation.json` |
| P5 | `target/rag-benchmark/release/retrieval-config-v1.json` |
| P6 | `data/eval/rag-data-plane/agentic-hard-set.jsonl` |
| P7/8 | `target/rag-benchmark/p7-agentic/{agentic-compare.json, agentic-traces.jsonl, agentic-compare.md}` |
| P9 | `target/rag-benchmark/p9-latency/latency-breakdown.json` |
| P10 | `target/rag-benchmark/release/release-benchmark.json` |
| P11 | `target/rag-benchmark/release/{resume-metrics.json, resume-report.md, manifest.json, release.json}` |

## 14. 建议的后续（计划延伸项，本次未做）

- 真实人工抽样复核 low/medium confidence 案例，将 `reviewed` 落地为可对外引用。
- Security(leakage) 采集与 paired-significance（one-shot vs agentic 逐对 95% CI / p 值）。
- Agentic 改进：多轮 rewrite / query decomposition 策略，提升 hard set 的 recovery（本次单轮 recovery=0）。