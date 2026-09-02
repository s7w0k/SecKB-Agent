"""企业级多产品大规模 RAG 真实能力压力验证（enterprise-rag-stress）。

实现 docs/企业级多产品大规模RAG真实能力压力验证_详细实施计划.md 的 P0-P11 全阶段，
以统一命令行 ``python -m scripts.enterprise_rag.cli`` 提供入口。

设计原则（计划 §3）：
- 不覆盖 app/knowledge/ 现有语料；新语料写入 data/enterprise-rag-stress/。
- 独立数据库 mindbridge_enterprise_stress、独立 OpenSearch 前缀 seckb-rag-estress。
- 可恢复执行：output/enterprise-rag-stress/<run_id>/run-state.json。
- 禁止 fake/deterministic embedding；真实 BGE 走配置的 BAAI/bge-m3 API。
"""

__version__ = "0.1.0"