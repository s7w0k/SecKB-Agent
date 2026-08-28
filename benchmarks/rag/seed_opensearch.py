"""Phase 12：构造 load/performance 基准用的 OpenSearch 测试语料（§12.1 数据规模）。

用法::

    python -m benchmarks.rag.seed_opensearch --chunks 10000 --dim 256 \
        --tenants 10 --workspaces 10 --generation G042

在指定 generation 下 bulk 写入 ``N`` 个 chunks（跨 tenant/workspace/classification_level
分布），供 ``run_benchmark.py`` 与 ``locustfile.py`` 压测。复用真实
``RealOpenSearchBackend.bulk_index``（含 knn 向量），首次自动 create_generation 建 mapping。
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace


def make_chunk(i: int, *, domain: str, org: int, ws: int, level: int,
               generation: str, text_template: str) -> SimpleNamespace:
    """确定性构建一个 chunk（与 opensearch._doc 所需字段对齐）。"""
    return SimpleNamespace(
        # 不设 id，让 _doc 以 source:source_key:source_index 派生 db_id
        content=f"{text_template} {i}",
        organization_id=org,
        workspace_id=ws,
        knowledge_space_id=org * 1000 + ws,
        classification_level=level,
        generation_id=generation,
        domain=domain,
        source_key=f"perf:{domain}",
        source=domain,
        source_index=i,
    )


def make_chunks(total: int, *, tenants: int = 10, workspaces: int = 10,
                generation: str = "G042", domains: tuple[str, ...] = ("compliance", "service")) -> list[SimpleNamespace]:
    """按 §13.1 分布构造 ``total`` 个 chunk。

    - ``tenants`` 个组织，每组织 ``workspaces`` 个空间（§13.1: 10 tenants × 10 ws）。
    - clearance 在 0-15 间轮转，便于压不同分类过滤路径。
    """
    chunks: list[SimpleNamespace] = []
    for i in range(total):
        org = i % max(tenants, 1)
        ws = (i // max(tenants, 1)) % max(workspaces, 1)
        level = i % 16
        domain = domains[i % len(domains)]
        chunks.append(make_chunk(
            i, domain=domain, org=org, ws=ws, level=level,
            generation=generation,
            text_template=f"training retention policy for {domain} domain",
        ))
    return chunks


def build_vector(dim: int, scalar: float = 0.02) -> list[float]:
    """确定性单位向量（n 维，简单正弦归一化），避免随机依赖、便于复现。"""
    import math

    raw = [math.sin(scalar * k) for k in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seed_opensearch", description="load 语料种子（§12.1）")
    parser.add_argument("--chunks", type=int, default=10000)
    parser.add_argument("--dim", type=int, default=256)
    parser.add_argument("--tenants", type=int, default=10)
    parser.add_argument("--workspaces", type=int, default=10)
    parser.add_argument("--generation", default="G042")
    args = parser.parse_args(argv)

    from app.core.config import get_settings
    from app.services.vector_backends.factory import _build_opensearch

    backend = _build_opensearch(get_settings())
    backend.create_generation(generation_id=args.generation)
    chunks = make_chunks(args.chunks, tenants=args.tenants,
                         workspaces=args.workspaces, generation=args.generation)
    vectors = [build_vector(args.dim) for _ in chunks]
    written = backend.bulk_index(generation_id=args.generation, chunks=chunks, vectors=vectors)
    print(f"seeded generation={args.generation} chunks={written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())