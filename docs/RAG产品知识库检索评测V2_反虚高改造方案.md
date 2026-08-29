# RAG 产品知识库检索评测 V2：反虚高改造方案

> 目标：把 `product_kb_retrieval_bench.py` 评测从"必然命中的虚高口径"改造成
> "能写进简历、经得起追问的真实水平"。
>
> 结论先行：V1 的 `bm25+dense_rrf+rerank MRR@5=0.975 / Recall@5=1.0` **计算正确，
> 但评测口径虚高**，不能作为"复杂任务/文档下效果好"的证据。本方案通过
> 4 项改造（gold 去泄漏、改写去实体、语料加干扰、新增 hard 集）打回真实水平。

---

## 1. 背景：V1 虚高诊断（已有实证）

在 `app/knowledge`（14 个安全产品 + compliance/mental 干扰语料）上，V1 评测
（40 条 FAQ query、改写问句、416 passages）得到：

| 路 | Recall@5 | MRR@5 | NDCG@5 | Hit@5 |
|---|---|---|---|---|
| bm25 | 1.0 | 0.8667 | 0.9012 | 1.0 |
| dense | 1.0 | 0.9521 | 0.964 | 1.0 |
| bm25+dense_rrf | 1.0 | 0.9396 | 0.9548 | 1.0 |
| dense+rerank | 1.0 | 0.975 | 0.9815 | 1.0 |
| **bm25+dense_rrf+rerank** | **1.0** | **0.975** | **0.9815** | **1.0** |

### 1.1 诊断脚本结论（运行于 V1 数据）

| 检查项 | 结果 | 含义 |
|---|---|---|
| 改写 query 与原问句字符集重合率 | mean **0.89**（min 0.50 / max 1.00） | 改写未打散字面，90% 字符原样保留 |
| 改写后英文实体保留率 | **1.00** | SIEM / Agent / IAM / SSO / Docker 全保留，BM25 靠实体必中 |
| gold 定义 | gold = **含原问句的 chunk** | query 与答案文本本质同源 → 文档内自匹配 |

### 1.2 虚高源汇总

**数据层**
1. **gold 泄漏**：`build_gold` 以 `q in p["content"]` 定位 gold，gold 文本含原问句；
   改写 query 与原问句 89% 字符重合 → 检索变成"找同源段落"，必然命中。
2. **样本太小**：仅 40 条 query，1 例波动 = 2.5 个百分点，统计显著性不足。
3. **语料干扰弱**：416 passages 中真正相关的只有 4 个产品的 FAQ 文档；
   compliance/mental 干扰与安全产品 FAQ 语义距离过远，起不到"语义相近干扰"作用。

**链路层**
4. **FAQ 任务本质是文档内自匹配**：问句原文 → 答案在同一 chunk，不是跨文档语义检索。
5. **无 hard case**：没有多跳、冲突证据、表格/长文档定位、跨文档聚合等复杂任务。

### 1.3 反证

项目内真实有难度的对抗套件（600 case）中 lexical Recall@5=0.943、dense 反而下降
——那才是复杂任务下的真实难度曲线。V1 与对抗套件的差距全部来自评测口径，
不是检索实现差异。

---

## 2. V2 改造目标

1. **gold 去泄漏**：gold 一律为 answer-only chunk（问句文本不进入 gold/语料正文）。
2. **改写去实体**：改写后 query 与原问句字符重合率 < 0.4，英文实体保留率 < 0.5。
3. **语料加干扰**：14 个产品全量进语料 + 语义相近干扰（同产品体系 FAQ、相关合规文档）。
4. **新增 hard 集**：跨产品比较 / 多文档聚合 / 长文档定位，10–15 例。
5. **保留 V1 对照**：同一脚本可复现 V1（原始口径）与 V2（反虚高口径），
   形成"发现虚高 → 重建评测 → 拿到真实水平"的完整故事。

---

## 3. 改造设计

### 3.1 改造一：FAQ 拆块为 answer-only passage（gold 去泄漏）

**现状**：`_chunk_file` 把 `06-common-faq.md` 整个按差异 chunker 切块，
问句与答句同 chunk；`build_gold` 用 `q in p["content"]` 找 gold。

**V2 设计**：

1. 新增 `_chunk_faq(pipeline, faq_fp, root, passages, faq_map)`：
   - 用正则解析 `- 问：Q\n 答：A` 问答对（与 V1 相同）。
   - 每个问答对生成**独立 passage**：
     ```python
     passages.append({
         "id": f"{uri}#faq-{n}",
         "file": uri,
         "content": answer_only,          # 只含答句正文，不含问句
         "meta": {"faq_q": q},            # 记录原始问句（仅用于 gold 映射，不进正文）
     })
     ```
   - `faq_map[q] = passage_id` 记录问句 → answer passage 的映射。
2. `build_corpus` 中 `06-common-faq.md` **不走普通 chunker**，只走 `_chunk_faq`；
   其余 `NN-*.md`（01~05、07~08）仍走 `_chunk_file`。
3. `build_gold` 改为：`gold_ids = [faq_map[q]]`（query=改写或原问句），
   不再用 `q in p["content"]` 子串匹配。

**效果**：gold 与 query 不再共享逐字文本，dense 必须靠语义泛化才能命中。

### 3.2 改造二：改写 prompt 去实体化

**现状**：改写 prompt 只要求"意思相同的自然问句"，导致 89% 字符原样保留。

**V2 设计**：改写 system prompt 改为：

```
你是中文知识库检索测评助手。把给定的用户问题改写成一个意思相同、
但措辞完全不同的自然问句。硬性要求：
1. 必须用同义词替换专业术语/实体，例如：
   令牌→凭证，SIEM→安全事件管理平台，信创→国产化环境，
   Agent→智能体，IAM→身份与访问管理，SSO→单点登录，
   Docker→容器，FAQ→常见问题；
2. 不得保留原文中的英文缩写与专有名词；
3. 不得直接复述原文中的连续词组（4 字及以上）；
4. 只输出改写后的问句本身，不要任何解释。
```

- `temperature=0.7`（增加多样性），`max_tokens=96`。
- 改写缓存换新路径：`output/product_kb_paraphrase_v2_cache.json`（避免污染 V1 缓存）。
- 校验：改写后 `charset_overlap < 0.4`、`entity_keep < 0.5`，不达标自动重试 1 次；
  仍不达标记入 `warn` 列表（输出到报告，供人工复核）。

### 3.3 改造三：语料加干扰

**现状**：`--products 0` 已含 14 产品；compliance/mental 为语义远干扰。

**V2 设计**：
1. 全量产品文档（14 个 `service/*`）保持全量进语料。
2. 新增**语义相近干扰**：把 `service/_retired/*`（同类安全/消费级产品历史文档，
   与 FAQ 同主题但 gold 不在其中）一并并入（新增 `--retired-distractors` 开关）。
3. compliance/mental 保留（语义远干扰，模拟"多域知识库"规模）。
4. 输出语料构成统计：`{products, compliance, mental, retired}` 各 passage 数，
   写进报告 `args.corpus_stats`。

**效果**：检索空间显著增大，且存在与 FAQ 高度相似的语义近邻干扰
（同主题但非 gold），dense/rerank 不再"唯一正确"。

### 3.4 改造四：新增 hard 集（跨产品 / 多文档聚合 / 长文档定位）

**设计**：新增 `HARD_QUERIES` 内嵌定义（10–15 例），按类型标注：

| 类型 | 示例 | gold 策略 |
|---|---|---|
| 跨产品比较 | "agent-iam 与 agent-sandbox 都要求信创部署吗？谁的支持面更广？" | 两个产品的部署/FAQ passage |
| 多文档聚合 | "产品 X 的整体架构、部署方式和故障排查入口分别在哪？" | 01/04/07 等跨文件 passage |
| 长文档定位 | 从 `02-spec-and-architecture.md` 长文中定位特定能力段落 | 该文件特定小节 passage |

**实现**：
- hard query 的 gold 用**关键词 → passage id** 静态映射生成：
  每条 hard 定义含 `queries: [str]` 与 `gold_keywords: [str]`；
  匹配 `gold_keywords` 出现在 `p["content"]` 且 `p["file"]` 落在限定文件集合内的 passage。
- hard 集作为独立评测组运行（`--hard` 开关），输出单独指标块 `routes_hard`，
  不与 FAQ 组混合，避免稀释解读。
- hard 集 gold 需要人工核验（与 human-core 理念一致），
  在报告中输出 gold 匹配明细供复核。

---

## 4. CLI 变更（`product_kb_retrieval_bench.py`）

```text
python scripts/product_kb_retrieval_bench.py \
    --distractors --retired-distractors \
    --paraphrase --paraphrase-cache output/product_kb_paraphrase_v2_cache.json \
    --hard --rerank \
    --out output/product_kb_v2_real.json
```

新增参数：
- `--retired-distractors`：并入 `service/_retired` 历史产品文档（语义相近干扰）。
- `--paraphrase-cache`：V2 改写缓存路径（默认改为 v2 路径，隔离 V1）。
- `--hard`：运行 hard 集（内嵌 `HARD_QUERIES`）。
- `--paraphrase-overlap-threshold`（默认 0.4）：改写重合度校验阈值。

保留参数：`--root / --products / --K / --rerank / --distractors / --overwrite / --out`。

---

## 5. 输出与验收

### 5.1 输出

报告 JSON 增加字段：

```jsonc
{
  "args": { "…": "…", "corpus_stats": {"products": 416, "compliance": …, "mental": …, "retired": …},
            "paraphrase_overlap": {"mean": 0.31, "warned": 2} },
  "routes": { "bm25": …, "dense": …, "bm25+dense_rrf": …,
              "dense+rerank": …, "bm25+dense_rrf+rerank": … },
  "routes_hard": { "bm25": …, "dense": …, "bm25+dense_rrf+rerank": … },
  "warnings": ["改写未达标: …"]
}
```

### 5.2 验收标准（反虚高成功）

| 指标 | V1（虚高） | V2 预期（真实） |
|---|---|---|
| dense MRR@5 | 0.952 | 0.75 – 0.88 |
| bm25+dense_rrf+rerank MRR@5 | 0.975 | 0.80 – 0.90 |
| bm25+dense_rrf+rerank Recall@5 | 1.0 | 0.90 – 0.97 |
| hard 集（完整链路）MRR@5 | 未测 | 0.50 – 0.75 |
| 改写字符重合率 | 0.89 | < 0.40 |
| 改写实体保留率 | 1.00 | < 0.50 |

> 上表区间为经验预期，最终以运行结果为准；若 V2 指标仍 ≥ 0.95，
> 需继续审查 gold/hard 定义（原则：任何"必然命中"都视为评测缺陷）。

### 5.3 简历口径（改造完成后）

> 构建产品知识库检索基准（14 产品、400+ chunk、40+ FAQ 改写查询 + hard 集），
> 识别并修复"query 与 gold 同源"导致的虚高（MRR 虚高至 0.975），
> 重建评测后混合检索链（BM25+Dense+RRF+Rerank）在真实口径下
> Recall@5=0.9x、MRR@5=0.8x；相比纯词法基线（MRR@5≈0.20）提升约 4 倍。

---

## 6. 实施步骤

1. **改造一**：新增 `_chunk_faq` + `faq_map`，`build_corpus/build_gold` 接入。
2. **改造二**：替换改写 prompt，新增校验/重试逻辑，v2 缓存隔离。
3. **改造三**：新增 `--retired-distractors` 与语料构成统计。
4. **改造四**：新增 `HARD_QUERIES` 与 `--hard` 评测组。
5. **回归**：确认 V1 口径仍可复现（`--paraphrase-cache` 指向 v1 缓存、不加
   `--retired-distractors` 与 `--hard` 时结果与 V1 一致），保证"新旧对照"可信。
6. **运行 v2**：全量 `--distractors --retired-distractors --paraphrase --hard --rerank`，
   输出 `output/product_kb_v2_real.json`，与 V1 对比并人工复核 hard gold。

---

## 7. 相关文件

| 文件 | 说明 |
|---|---|
| `scripts/product_kb_retrieval_bench.py` | 改造对象 |
| `output/product_kb_real.json` | V1 结果（含老口径） |
| `output/product_kb_hybrid_real.json` | V1 混合链路结果 |
| `output/product_kb_paraphrase_cache.json` | V1 改写缓存（保留） |
| `output/product_kb_paraphrase_v2_cache.json` | V2 改写缓存（新建） |
| `app/knowledge/service/_retired/*` | 语义相近干扰来源 |
| `app/rag_eval/retrieval_metrics.py` | 指标实现（不变） |
