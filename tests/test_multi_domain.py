"""P2-07 跨域隔离测试：验证 KnowledgeService 的域过滤、Skill Registry 两级目录、跨域泄露为 0。

验证要点：
1. 知识检索调用缺少 domain 时直接失败（TypeError）。
2. SQL/BM25/向量/相邻块扩展和降级路径均返回同域结果。
3. 三域存在相同文件名时，更新/删除不会互相影响。
4. Skill Registry 两级目录加载、跨域同名 Skill 不互相覆盖。
5. 非法域目录会报错。
"""

import unittest
from pathlib import Path

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.core.enums import KnowledgeDomain
from app.services.knowledge import KnowledgeService
from app.services.skills import MindBridgeSkillRegistry, SkillLoadError


class DomainIsolationTests(unittest.TestCase):
    """跨域知识隔离测试。"""

    def setUp(self):
        self.settings = get_settings()
        self.settings.database_url = "sqlite:///:memory:"
        from sqlalchemy import create_engine

        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        # 绑定到内存引擎
        from app.core.database import engine as default_engine

        self._original_engine = default_engine
        self.db.bind = self.engine
        self.service = KnowledgeService(self.db, self.settings)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_retrieve_requires_domain(self):
        """v2 6.4：domain 不再必填，缺省时由 Scope/status 限定，不默认 MENTAL。"""
        # 写入 MENTAL 域数据后，无 domain 检索不应隐式回退 MENTAL 之外的全库
        self.service.ingest("policy.md", "心理危机干预流程和紧急联系方式", domain=KnowledgeDomain.MENTAL)
        self.service.ingest("policy.md", "商品退换货政策与退款时效说明", domain=KnowledgeDomain.SERVICE)
        # domain=None 时返回所有 scope 内 published chunks（此处无 workspace 限制）
        results = self.service.retrieve("危机干预")  # type: ignore[call-arg]
        # 不再默认 MENTAL：结果应能包含跨域（只要词法匹配）
        all_text = " ".join(r.content for r in results)
        self.assertIn("危机干预", all_text)

    def test_ingest_requires_domain(self):
        """缺少 domain 参数时 ingest 必须报错。"""
        with self.assertRaises(TypeError):
            self.service.ingest("source", "content")  # type: ignore[call-arg]

    def test_cross_domain_retrieval_no_leak(self):
        """心理域查询不会返回客服/合规域结果。"""
        self.service.ingest("policy.md", "心理危机干预流程和紧急联系方式", domain=KnowledgeDomain.MENTAL)
        self.service.ingest("policy.md", "商品退换货政策与退款时效说明", domain=KnowledgeDomain.SERVICE)
        self.service.ingest("policy.md", "数据安全合规举报受理流程", domain=KnowledgeDomain.COMPLIANCE)

        mental_results = self.service.retrieve("危机干预", domain=KnowledgeDomain.MENTAL)
        service_results = self.service.retrieve("退款", domain=KnowledgeDomain.SERVICE)
        compliance_results = self.service.retrieve("举报", domain=KnowledgeDomain.COMPLIANCE)

        # 心理域结果不应包含退换货或合规内容
        for r in mental_results:
            self.assertNotIn("退换货", r.content, "心理域检索泄露了客服域内容")
            self.assertNotIn("合规举报", r.content, "心理域检索泄露了合规域内容")

        # 客服域结果不应包含心理或合规内容
        for r in service_results:
            self.assertNotIn("危机干预", r.content, "客服域检索泄露了心理域内容")
            self.assertNotIn("合规举报", r.content, "客服域检索泄露了合规域内容")

        # 合规域结果不应包含心理或客服内容
        for r in compliance_results:
            self.assertNotIn("危机干预", r.content, "合规域检索泄露了心理域内容")
            self.assertNotIn("退换货", r.content, "合规域检索泄露了客服域内容")

    def test_same_filename_different_domain_no_interference(self):
        """三域存在相同文件名时，更新/删除不会互相影响。"""
        self.service.ingest("guide.md", "心理疏导指南内容", domain=KnowledgeDomain.MENTAL)
        self.service.ingest("guide.md", "客服操作指南内容", domain=KnowledgeDomain.SERVICE)

        # 更新客服域的 guide.md 不应影响心理域
        self.service.ingest("guide.md", "客服操作指南更新版内容", domain=KnowledgeDomain.SERVICE)
        mental_results = self.service.retrieve("疏导", domain=KnowledgeDomain.MENTAL)
        self.assertTrue(any("心理疏导" in r.content for r in mental_results), "更新客服域不应影响心理域数据")

        # 删除客服域的 guide.md 不应影响心理域
        from app.models.entities import KnowledgeChunk

        source_key = "guide.md"
        self.db.query(KnowledgeChunk).filter(
            KnowledgeChunk.domain == KnowledgeDomain.SERVICE.value,
            KnowledgeChunk.source_key == source_key.lower(),
        ).delete()
        self.db.commit()
        mental_results = self.service.retrieve("疏导", domain=KnowledgeDomain.MENTAL)
        self.assertTrue(any("心理疏导" in r.content for r in mental_results), "删除客服域不应影响心理域数据")

    def test_domain_count_isolated(self):
        """count 按域过滤。"""
        self.service.ingest("a.md", "心理内容一", domain=KnowledgeDomain.MENTAL)
        self.service.ingest("b.md", "客服内容一", domain=KnowledgeDomain.SERVICE)
        self.service.ingest("c.md", "合规内容一", domain=KnowledgeDomain.COMPLIANCE)

        self.assertEqual(self.service.count(domain=KnowledgeDomain.MENTAL), 1)
        self.assertEqual(self.service.count(domain=KnowledgeDomain.SERVICE), 1)
        self.assertEqual(self.service.count(domain=KnowledgeDomain.COMPLIANCE), 1)
        self.assertEqual(self.service.count(), 3)

    def test_status_with_domain(self):
        """status 按 domain 过滤返回正确的 chunk 数。"""
        self.service.ingest("test.md", "心理测试内容", domain=KnowledgeDomain.MENTAL)
        mental_status = self.service.status(domain=KnowledgeDomain.MENTAL)
        self.assertEqual(mental_status["databaseChunks"], 1)
        self.assertEqual(mental_status["domain"], "MENTAL")


class SkillRegistryDomainTests(unittest.TestCase):
    """两级 Skill Registry 域校验测试。"""

    def test_two_level_directory_loading(self):
        """两级目录 skills/<domain>/<name>/SKILL.md 正确加载。"""
        root = Path(__file__).resolve().parents[1] / "skills"
        registry = MindBridgeSkillRegistry(root=root)
        skills = registry.list_skills()
        domains = {skill.domain for skill in skills}
        self.assertIn("MENTAL", domains)
        self.assertIn("SERVICE", domains)
        self.assertIn("COMPLIANCE", domains)

    def test_registry_key_format(self):
        """注册键使用 <domain>:<name> 格式。"""
        root = Path(__file__).resolve().parents[1] / "skills"
        registry = MindBridgeSkillRegistry(root=root)
        skills = registry.list_skills()
        for skill in skills:
            self.assertIn(":", skill.registry_key)
            domain, name = skill.registry_key.split(":", 1)
            self.assertEqual(domain, skill.domain)
            self.assertEqual(name, skill.name)

    def test_cross_domain_same_name_no_overwrite(self):
        """跨域同名 Skill 不互相覆盖。"""
        root = Path(__file__).resolve().parents[1] / "skills"
        registry = MindBridgeSkillRegistry(root=root)
        skills = registry.list_skills()
        # 心理域和客服域都有 baseline skill，但名称不同
        # 验证每个域的 skill 独立存在
        mental_skills = {s.name for s in skills if s.domain == "MENTAL"}
        service_skills = {s.name for s in skills if s.domain == "SERVICE"}
        compliance_skills = {s.name for s in skills if s.domain == "COMPLIANCE"}
        self.assertIn("supportive_response_baseline", mental_skills)
        self.assertIn("service_response_baseline", service_skills)
        self.assertIn("compliance_response_baseline", compliance_skills)

    def test_get_required_with_domain(self):
        """get_required 支持按域查找。"""
        root = Path(__file__).resolve().parents[1] / "skills"
        registry = MindBridgeSkillRegistry(root=root)
        skill = registry.get_required("supportive_response_baseline", domain="MENTAL")
        self.assertEqual(skill.domain, "MENTAL")

    def test_get_required_wrong_domain_raises(self):
        """在错误域查找 skill 时报错。"""
        root = Path(__file__).resolve().parents[1] / "skills"
        registry = MindBridgeSkillRegistry(root=root)
        with self.assertRaises(SkillLoadError):
            registry.get_required("supportive_response_baseline", domain="SERVICE")

    def test_status_items_include_domain(self):
        """status_items 返回 domain 字段。"""
        root = Path(__file__).resolve().parents[1] / "skills"
        registry = MindBridgeSkillRegistry(root=root)
        items = registry.status_items()
        for item in items:
            self.assertIn("domain", item)
            self.assertIsNotNone(item["domain"])


if __name__ == "__main__":
    unittest.main()
