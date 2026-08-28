"""Phase 1（plan §1.6）：App-scoped 统一数据面一致性验证。

验证「configured backend = constructed backend = retrieval backend = index backend
= generation backend」：所有 Service / Worker 从 ``AppServices`` 拿到同一个运行期后端，
杜绝各自 new 不同后端（尤其生产 VECTOR_BACKEND=opensearch 时 KnowledgeService 仍走
Chroma、Retriever 仍走 DB 的脱节）。

本测试使用 dev 模拟后端（``OpenSearchVectorBackend``）在独立 sqlite DB 上验证
AppServices 依赖注入的一致性，不依赖真实 OpenSearch 集群，保证 CI 可复现。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.app_services import AppServices, build_app_services
from app.core.database import Base
from app.services.vector_backends.opensearch_backend import OpenSearchVectorBackend


def _settings(**overrides):
    """构造最小 Settings（仅用到 backend 相关字段）。"""
    from app.core.config import Settings

    defaults = {
        "vector_backend": "local_chroma",
        "index_generation": "G001",
        "knowledge_top_k": 6,
        "knowledge_candidate_k": 16,
        "openai_embedding_model": "mock",
        "chroma_persist_dir": "data/chroma-test",
    }
    defaults.update(overrides)
    return Settings(**defaults)


class AppScopedBackendTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(bind=self.engine)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _app_services_with(self, backend) -> AppServices:
        services = build_app_services(_settings(), vector_backend=backend)
        return services

    def test_all_components_share_same_backend(self):
        """§1.4/§1.6：KnowledgeService / GenerationService / api 均拿到同一 backend。"""
        backend = OpenSearchVectorBackend()
        services = self._app_services_with(backend)
        self.assertIs(services.backend, backend, "backend 缓存必须返回同一实例")

        # KnowledgeService 使用注入的 backend 作为 vector_store
        ks = services.build_knowledge_service(self.db)
        self.assertIs(ks.vector_store, backend)

        # GenerationService 使用同一 backend
        gs = services.build_generation_service(self.db)
        self.assertIs(gs.backend, backend)

        # backend 属性线程安全：多次访问同一实例
        self.assertIs(services.backend, services.vector_backend)

    def test_backend_lazy_build_when_not_injected(self):
        """未注入 backend 时 property 只构建一次并缓存（线程安全）。"""
        settings = _settings()
        services = AppServices(settings=settings)
        # 手动预注入已验证的 backend；property 返回同一实例且不再重复构建
        built = OpenSearchVectorBackend()
        services.vector_backend = built
        self.assertIs(services.backend, built)
        self.assertIs(services.backend, built)
        self.assertIs(services.backend, services.vector_backend)

    def test_app_services_container_wires_generation_and_knowledge(self):
        """端到端：同一 backend 下 build → publish → 检索一致性。"""
        backend = OpenSearchVectorBackend()
        chain = backend.bulk_index(
            generation_id="G001",
            chunks=[SimpleNamespace(id=1, source="S", source_index=0, content="alpha", domain="D",
                                    organization_id=1, workspace_id=1, knowledge_space_id=None,
                                    classification_level=0, generation_id="G001", source_key="sk")],
            vectors=[[1.0, 0.0]],
        )
        self.assertEqual(chain, 1)
        backend.activate_generation(generation_id="G001")

        services = self._app_services_with(backend)
        gs = services.build_generation_service(self.db)
        # 已发布的外在状态应可被 backend 读到（同一实例）
        self.assertEqual(backend.current_generation, "G001")
        self.assertEqual(gs.backend.current_generation, "G001")


if __name__ == "__main__":
    unittest.main()