"""v2 阶段 1 任务 6.3.4：静态检查 — 生产代码不得绕过 RequestScope 调用检索。

要求：
1. 生产目录（app/api、app/services、app/agents）中直接调用
   `KnowledgeService(...).retrieve(...)` 时，必须携带 workspace_id（Scope 派生），
   禁止回退到无 Scope 的全库检索。
2. 业务路由必须依赖 `get_request_scope` / `get_request_scope_optional` 之外，
   必须显式通过 `scope.workspace_id` 派生检索范围。

本测试用 AST 扫描生产目录，防止未来代码回归。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 允许内部通过 RetrievalService（统一 scope 入口）或 KnowledgeService 内部调用的文件
ALLOWED_FILES_WITH_KNOWLEDGE_RETRIEVE = {
    "app/services/retrieval_service.py",   # 统一检索服务：接收 RequestScope，内部携带 workspace_id
    "app/services/knowledge.py",           # KnowledgeService 自身方法
}

# 这些调用点明确带 workspace_id（Scope 派生），是受控的
SCOPED_RETRIEVE_FILES = {
    "app/services/retrieval_service.py",
}


class StaticScopeEnforcementTests(unittest.TestCase):
    """静态扫描生产目录的检索调用是否携带 Scope。"""

    def _production_py_files(self) -> list[Path]:
        files: list[Path] = []
        for sub in ("app/api", "app/services", "app/agents"):
            d = PROJECT_ROOT / sub
            if d.exists():
                files.extend(d.rglob("*.py"))
        return files

    def _calls(self, tree: ast.AST) -> list[ast.Call]:
        calls: list[ast.Call] = []

        class _Visitor(ast.NodeVisitor):
            def visit_Call(self, node: ast.Call):  # noqa: N802
                calls.append(node)
                self.generic_visit(node)

        _Visitor().visit(tree)
        return calls

    def _method_name(self, node: ast.Call) -> str | None:
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call):
            # 形如 KnowledgeService(...).retrieve(...)
            inner = func.value
            if isinstance(inner.func, ast.Name) and inner.func.id == "KnowledgeService":
                return func.attr
        return None

    def test_production_code_scoped_retrieve(self):
        """生产目录中 KnowledgeService(...).retrieve(...) 必须带 workspace_id。"""
        violations: list[str] = []
        for file in self._production_py_files():
            rel = file.relative_to(PROJECT_ROOT).as_posix()
            if rel in ALLOWED_FILES_WITH_KNOWLEDGE_RETRIEVE:
                continue
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for call in self._calls(tree):
                method = self._method_name(call)
                if method != "retrieve":
                    continue
                kwarg_names = {kw.arg for kw in call.keywords if kw.arg}
                # 直接调用旧 retrieve 必须显式携带 workspace_id 或 organization_id
                has_scope = ("workspace_id" in kwarg_names) or ("organization_id" in kwarg_names)
                if not has_scope:
                    violations.append(f"{rel}: KnowledgeService(...).retrieve(...) 未携带 workspace_id/organization_id")
        self.assertEqual(violations, [])

    def test_routes_depend_on_scope_for_business_endpoints(self):
        """业务路由函数签名必须包含 RequestScope 依赖（get_request_scope）。"""
        routes_file = PROJECT_ROOT / "app/api/routes.py"
        tree = ast.parse(routes_file.read_text(encoding="utf-8"))
        missing: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # 跳过公开只读端点：health/login/profile/agent-status 不需要 tenant 数据
            route_args = node.args.args + node.args.kwonlyargs
            decorators = [d for d in node.decorator_list if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {"post", "get", "put", "delete", "patch"}]
            if not decorators:
                continue
            name = node.name
            if name in {"login", "health", "profile", "agent_status"}:
                continue
            has_scope = any(
                a.annotation is not None and "RequestScope" in ast.unparse(a.annotation)
                for a in node.args.args
            )
            if not has_scope:
                missing.append(f"route '{name}' 未注入 RequestScope")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
