# MindBridge 检索指标评测报告（SERVICE + COMPLIANCE 域）

- **生成时间**: 2026-08-13
- **评测命令**: `python scripts/run_retrieval_full.py --dataset data/eval/full/rag-full.json --domains SERVICE COMPLIANCE`
- **产物目录**: `target/rag-eval/retrieval-service-compliance.json`
- **检索模式**: 混合检索（Chroma 向量 + BM25 + 语义重排 qwen3-vl-rerank）

---

## 一、评测集说明

| 项 | 值 |
|---|---|
| 总 case 数 | **108** |
| SERVICE 域 | 80 |
| COMPLIANCE 域 | 28 |
| MENTAL 域 | 0（已按要求去除）|
| 检索失败 case | 0 |
| 跨域泄漏 case | 0 |

> 使用大规模评测集（`data/eval/full/rag-full.json`，共 119 case），去除 MENTAL 域 11 个 case 后剩余 108 个，统计意义充分。

---

## 二、整体检索指标（n=108）

| K | Precision@K | Recall@K | MRR | NDCG@K | HitRate |
|---|---|---|---|---|---|
| **1** | 0.907 | 0.907 | 0.907 | 0.907 | 0.907 |
| **3** | 0.330 | 0.991 | 0.948 | 0.959 | 0.991 |
| **4** | 0.248 | 0.991 | 0.948 | 0.959 | 0.991 |
| **10** | 0.100 | 1.000 | 0.949 | 0.962 | 1.000 |

### 指标解读

- **Recall@K 优秀**：@3 即达 0.991，@10 达 1.000——目标文档几乎总能被召回。
- **MRR 良好**：0.907（@1）、0.948（@3）——相关文档总体排名靠前。
- **NDCG@K 良好**：@3 为 0.959——排序质量高。
- **HitRate 优秀**：@3 达 0.991，@10 达 1.000——绝大多数 case 的期望文档在 Top-K 内。
- **Precision@K 随 K 增大下降**：@3 为 0.330、@K10 为 0.100。这属**正常现象**——每个 case 仅有 1 个金标文档（referenceContextIds 通常只含 1 个 chunk），分母 K 增大而命中数不变，Precision 必然下降。这是单一金标标注下的固有特性，不代表检索质量差。

---

## 三、分域检索指标

byDomain 指标为 **跨 K 聚合值**（Precision/Recall 汇总），供分域对比参考：

| 域 | case 数 | Precision | Recall | MRR | NDCG | HitRate |
|---|---|---|---|---|---|---|
| **COMPLIANCE** | 28 | 0.403 | 0.982 | 0.955 | 0.962 | 0.982 |
| **SERVICE** | 80 | 0.394 | 0.969 | 0.932 | 0.941 | 0.969 |

> COMPLIANCE 域整体略优于 SERVICE 域（Recall 0.982 vs 0.969，MRR 0.955 vs 0.932），两域均表现健康。

---

## 四、最差 case（需要关注）

分域 worstCases 中，位于 COMPLIANCE 的以下 case 在部分 K 值下 Recall@K=0：

| case | 场景 | 风险 | optimal Recall@K | MRR | NDCG@K |
|---|---|---|---|---|---|
| full-compliance-data-minimization-policy | 数据最小化 | LOW | 0.0（@1）| 0.0 | 0.0 |
| full-compliance-data-security-and-privacy | 数据安全与隐私 | LOW | 0.0（@1）| 0.0 | 0.0 |

> 这两个 case 在金标文档（referenceContextIds）排位在 Top-1 之外的特定 K 值下 Recall=0。建议核查其金标 chunk 划分与 query 表述是否匹配（可能金标指向的 chunk 未被检索模型置顶，或属于多 chunk 语义重叠场景）。

---

## 五、结论

1. **检索层健康**：SERVICE + COMPLIANCE 两域（108 case）的 Recall@3=0.991、HitRate@3=0.991、MRR=0.948、NDCG@3=0.959，混合检索 + 语义重排链路表现优秀。
2. **无空检索、无跨域泄漏**：所有 case 均成功返回结果，且无跨域噪声。
3. **Precision@K 偏低属标注特性**：单一金标导致 Precision 随 K 下降，不能据此判断检索质量差，应结合 Recall/MRR/NDCG 综合评估。
4. **个别 case 需复核**：数据最小化、数据安全与隐私两个 COMPLIANCE case 存在特定 K 下召回失败，建议核查金标标注。

---

*本报告基于确定性检索指标（无需 LLM judge），数据真实来自检索层实际运行结果。*