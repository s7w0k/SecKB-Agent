from app.services.local_ranking import rerank_and_dedupe, stable_key
from app.services.vector_backends.opensearch_backend import PhysicalHit


def _hit(source_key: str, content: str, score: float = 1.0) -> PhysicalHit:
    return PhysicalHit(
        db_id=None,
        source="test",
        source_index=0,
        content=content,
        score=score,
        domain="SERVICE",
        source_key=source_key,
    )


def test_title_section_rank_and_exact_content_alias_dedupe():
    duplicate = "# 产品甲 ## 部署方式 - 支持私有化部署"
    hits = [
        _hit("wrong.md", "# 产品乙 ## 部署方式 - 仅云端"),
        _hit("duplicate-a.md", duplicate),
        _hit("duplicate-b.md", duplicate),
        _hit("other.md", "# 产品甲 ## 故障排查 - 检查日志"),
    ]
    ranked = rerank_and_dedupe(
        "请说明《产品甲》的“部署方式”",
        hits,
        window=4,
        dedupe_exact_content=True,
    )
    assert stable_key(ranked[0]) == "SERVICE:duplicate-a.md:1:0"
    assert ranked[0].equivalent_keys == ("SERVICE:duplicate-b.md:1:0",)
    assert len(ranked) == 3


def test_version_comparison_preserves_original_order():
    hits = [
        _hit("current.md", "# 制度 ## 当前版本 - 新规则"),
        _hit("old.md", "# 制度 ## 历史版本 - 旧规则"),
    ]
    ranked = rerank_and_dedupe("比较当前规则与旧版规则的冲突", hits, window=2)
    assert [hit.source_key for hit in ranked] == ["current.md", "old.md"]
