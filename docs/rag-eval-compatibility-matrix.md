# P0-05 RAGAS / Langfuse 版本兼容性矩阵

> 目标：在 P3(P1 引入 RAGAS) 和 P5(P0 接入 Langfuse) 前，锁定精确版本与 Python 兼容性。
> 依据：PyPI 元数据（`Requires-Python` / classifiers）与 Langfuse 官方 SDK-server 兼容表。

## 运行时基线

| 项 | 值 | 说明 |
|----|----|------|
| Python | 3.12.13 | 容器镜像 `python:3.12-slim` |
| 生成模型 | deepseek-chat | OpenAI-compatible |
| Embedding | qwen3.7-text-embedding（DashScope） | 1024 维 |

## 版本矩阵

| 包 | 建议锁定版本 | 兼容 Python | 备注 |
|----|-------------|-------------|------|
| `ragas` | **0.4.3**（或安装时可用的最新稳定版） | >=3.9（含 3.12） | 离线评测用；放 `requirements-eval.txt`，不污染运行时 |
| `langfuse`（Python SDK） | **4.14.x**（推荐 4.14.2） | >=3.10, <4.0（含 3.12） | v4 于 2026-03 重写；需自托管 server **>=3.63.0** |
| `langfuse`（自托管 Server） | **>=3.63.0**（SDK v4 最低要求） | - | Observations API v2 / Metrics API v2 需 Langfuse v4 |

## 关键兼容性约束

1. **Langfuse SDK v4 重写**：若自托管 server 版本 <3.63.0，SDK v4 将不兼容。
   部署时先确认 server 版本，README 中记录 `LANGFUSE_SERVER_VERSION`。
2. **RAGAS 与 Langfuse 解耦**：RAGAS 离线 runner 不依赖 Langfuse；Langfuse 只做可观测与 dataset/experiment。
   P7 的「原生 evaluator」能力探针是独立验收项（见 ADR-0001）。
3. **依赖隔离**：`ragas` 及其重依赖（dataset、langchain 等）只进 `requirements-eval.txt`；
   生产镜像 `requirements.txt` 仅加 `langfuse`（若 P5 启用）。
4. **每晚/发版前**：用 `pip index versions ragas` / `pip index versions langfuse` 复核最新稳定版，
   升级走独立 PR 并回归离线评测。

## 验证命令（P0 完成定义）

```powershell
# 复核当前可用版本
python -m pip index versions ragas
python -m pip index versions langfuse
# 安装后确认兼容
python -c "import ragas; print(ragas.__version__)"
python -c "import langfuse; print(langfuse.__version__)"
```

## 结论

- 两支依赖均兼容 Python 3.12.13，可锁定。
- Langfuse 采用 v4 SDK，需配套自托管 server >=3.63.0（P5 部署时确认）。
- RAGAS 0.4.3 当前可用，P3 引入时锁定。