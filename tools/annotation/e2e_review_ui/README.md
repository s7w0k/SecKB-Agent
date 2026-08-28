# E2E RAG 人工复核界面

## 启动

在仓库根目录执行：

```powershell
python -m tools.annotation.e2e_review_ui
```

浏览器访问：`http://127.0.0.1:8765`

默认文件：

- Dataset：`data/eval/rag-data-plane/e2e-release-v1/e2e-release-human-core-200-v1.jsonl`
- Corpus：`data/eval/rag-data-plane/e2e-release-v1/e2e-eval-corpus-v1.jsonl`
- 断点记录：`target/rag-benchmark/e2e-human-review/core-200-primary-review-session-v1.jsonl`
- 导出目录：`target/rag-benchmark/e2e-human-review/core-200-primary-export-v1/`

可通过命令行参数覆盖，完整参数见：

```powershell
python -m tools.annotation.e2e_review_ui --help
```

## 审核流程

1. 填写真实姓名或工号作为复核者标识。
2. 阅读问题、场景、预期行为与打乱后的候选证据。
3. 检查问题、分类、证据、答案要点、预期行为五项；确认五项均正确时可点击“一键通过 5 项检查”。
4. 选择“通过”“修改后通过”或“不确定”。
5. 点击“保存并下一条”；结论也会在停止输入 1.5 秒后自动保存。
6. 200 条全部完成且不存在“不确定”后，确认并导出首审 Gold。

“一键通过 5 项检查”只负责勾选五个检查项，不会自动选择最终结论或保存，避免把未确认的数据直接记为人工通过。

快捷键：

- `1`：通过
- `2`：修改后通过
- `3`：不确定
- `←` / `→`：上一条 / 下一条
- `Ctrl + Enter`：保存并下一条

## 数据安全与门禁

- 默认盲审，不展示 proposed evidence roles；“显示原标注”只能辅助处理疑难项。
- Session 绑定 dataset/corpus SHA-256。数据变化后旧 session 会被拒绝，防止错配。
- 首审导出会写 `human_semantic`，但 `source_agreement` 和 `passage_jaccard` 保持为空，因此不会伪装成已完成双人盲审的正式 Release Gold。
- 第二位复核者仍需审核固定 60 条样本，再计算 Source Agreement 与 Passage Jaccard。

## 首审导出文件

```text
core-200-primary-export-v1/
├── human-reviewed-e2e-release-core-200-v1.jsonl
├── e2e-annotation-evidence-core-200-primary-human-v1.json
└── primary-review-export-summary.json
```
