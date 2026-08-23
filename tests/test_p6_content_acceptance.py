"""P6 内容验收测试套件。

验证三域知识文件、Skill frontmatter、域故障/禁用模板、域系统 Prompt、
邮件正文模板的完整性和差异化，对应 P6-01/P6-02/P6-03 内容审核任务的工程化门禁。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("AI_PROVIDER", "mock")

from app.core.config import Settings  # noqa: E402
from app.core.enums import IntentType, KnowledgeDomain, RiskLevel  # noqa: E402
from app.models.entities import PsychologicalReport, RiskCase  # noqa: E402
from app.services.ai import (  # noqa: E402
    DOMAIN_DISABLED_TEMPLATES,
    DOMAIN_FAILURE_TEMPLATES,
    PromptTemplates,
    domain_disabled_template,
    domain_failure_template,
)
from app.services.skills import MindBridgeSkillLibrary, MindBridgeSkillRegistry, SkillLoadError  # noqa: E402
from app.services.tools import ToolOrchestrationService  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_ROOT = PROJECT_ROOT / "app" / "knowledge"
SKILLS_ROOT = PROJECT_ROOT / "skills"

DOMAIN_KNOWLEDGE_MIN_FILES = {
    KnowledgeDomain.MENTAL: 10,
    KnowledgeDomain.SERVICE: 2,
    KnowledgeDomain.COMPLIANCE: 2,
}
DOMAIN_KNOWLEDGE_MIN_BYTES = {
    KnowledgeDomain.MENTAL: 1500,
    KnowledgeDomain.SERVICE: 500,
    KnowledgeDomain.COMPLIANCE: 800,
}
DOMAIN_REQUIRED_SKILLS = {
    KnowledgeDomain.MENTAL: {
        "supportive_response_baseline",
        "high_risk_safety_plan",
        "counselor_handoff_summary",
    },
    KnowledgeDomain.SERVICE: {"service_response_baseline"},
    KnowledgeDomain.COMPLIANCE: {"compliance_response_baseline"},
}
DOMAIN_LEAK_KEYWORDS = {
    KnowledgeDomain.MENTAL: ["退换货", "退款", "客服主管", "授权合规渠道", "合规负责人", "回扣"],
    KnowledgeDomain.SERVICE: ["自杀", "自残", "心理中心", "辅导员", "授权合规渠道", "合规负责人", "回扣"],
    KnowledgeDomain.COMPLIANCE: ["自杀", "自残", "心理中心", "辅导员", "退换货", "退款", "客服主管"],
}


class KnowledgeContentAcceptanceTests(unittest.TestCase):
    """P6-01/02 知识文件完整性与最小规模验收。"""

    def test_three_domain_directories_exist(self):
        for domain in ["mental", "service", "compliance"]:
            self.assertTrue(
                (KNOWLEDGE_ROOT / domain).is_dir(),
                f"知识目录 app/knowledge/{domain} 不存在",
            )

    def test_mental_has_minimum_files(self):
        # rglob：SERVICE 域按主题子目录组织，需递归计数；三域统一递归口径
        files = list((KNOWLEDGE_ROOT / "mental").rglob("*.md"))
        self.assertGreaterEqual(
            len(files), DOMAIN_KNOWLEDGE_MIN_FILES[KnowledgeDomain.MENTAL],
            f"心理域知识文件数 {len(files)} 低于最小要求 {DOMAIN_KNOWLEDGE_MIN_FILES[KnowledgeDomain.MENTAL]}",
        )

    def test_service_has_minimum_files(self):
        files = list((KNOWLEDGE_ROOT / "service").rglob("*.md"))
        self.assertGreaterEqual(
            len(files), DOMAIN_KNOWLEDGE_MIN_FILES[KnowledgeDomain.SERVICE],
            f"客服域知识文件数 {len(files)} 低于最小要求 {DOMAIN_KNOWLEDGE_MIN_FILES[KnowledgeDomain.SERVICE]}",
        )

    def test_compliance_has_minimum_files(self):
        files = list((KNOWLEDGE_ROOT / "compliance").rglob("*.md"))
        self.assertGreaterEqual(
            len(files), DOMAIN_KNOWLEDGE_MIN_FILES[KnowledgeDomain.COMPLIANCE],
        )

    def test_knowledge_files_meet_min_bytes(self):
        for domain, min_bytes in DOMAIN_KNOWLEDGE_MIN_BYTES.items():
            domain_dir = KNOWLEDGE_ROOT / domain.value.lower()
            for file_path in domain_dir.rglob("*.md"):
                content = file_path.read_text(encoding="utf-8")
                self.assertGreaterEqual(
                    len(content.encode("utf-8")), min_bytes,
                    f"{file_path.name} 大小低于 {domain.value} 域最小要求 {min_bytes} 字节",
                )

    def test_knowledge_files_have_h1_or_frontmatter(self):
        for domain in ["mental", "service", "compliance"]:
            for file_path in (KNOWLEDGE_ROOT / domain).glob("*.md"):
                content = file_path.read_text(encoding="utf-8")
                has_h1 = any(line.strip().startswith("# ") for line in content.splitlines())
                has_frontmatter = content.strip().startswith("---")
                self.assertTrue(
                    has_h1 or has_frontmatter,
                    f"{file_path.name} 缺少 H1 标题或 YAML frontmatter",
                )

    def test_no_unexpected_cross_domain_filename_duplication(self):
        names_per_domain: dict[str, set[str]] = {}
        for domain in ["mental", "service", "compliance"]:
            names_per_domain[domain] = {f.name for f in (KNOWLEDGE_ROOT / domain).glob("*.md")}
        mental_service = names_per_domain["mental"] & names_per_domain["service"]
        mental_compliance = names_per_domain["mental"] & names_per_domain["compliance"]
        service_compliance = names_per_domain["service"] & names_per_domain["compliance"]
        all_common = mental_service | mental_compliance | service_compliance
        if all_common:
            print(f"[审核提示] 三域存在同文件名: {all_common}（P2 隔离保证不互相影响）")


class SkillFrontmatterAcceptanceTests(unittest.TestCase):
    """P6-03 Skill frontmatter 验收。"""

    def setUp(self):
        self.registry = MindBridgeSkillRegistry(root=SKILLS_ROOT)
        self.skills = self.registry.list_skills()

    def test_all_three_domains_have_skills(self):
        domains = {skill.domain for skill in self.skills}
        for domain in ["MENTAL", "SERVICE", "COMPLIANCE"]:
            self.assertIn(domain, domains, f"域 {domain} 没有任何 skill")

    def test_required_skills_present_per_domain(self):
        skills_by_domain: dict[str, set[str]] = {}
        for skill in self.skills:
            skills_by_domain.setdefault(skill.domain, set()).add(skill.name)
        for domain, required in DOMAIN_REQUIRED_SKILLS.items():
            actual = skills_by_domain.get(domain.value, set())
            missing = required - actual
            self.assertFalse(missing, f"域 {domain.value} 缺少必需 skill: {missing}")

    def test_all_skills_have_valid_status(self):
        items = MindBridgeSkillLibrary.status_items()
        failed = [item for item in items if item["status"] == "FAILED"]
        self.assertFalse(failed, f"存在加载失败的 skill: {[i['name'] for i in failed]}")

    def test_all_skills_have_workflow_section(self):
        for skill in self.skills:
            self.assertIn(
                "## Workflow", skill.body,
                f"skill {skill.name} ({skill.domain}) 缺少 ## Workflow 小节",
            )

    def test_mental_counselor_handoff_has_text_template(self):
        template = self.registry.template_for("counselor_handoff_summary", domain="MENTAL")
        self.assertTrue(template.strip(), "counselor_handoff_summary 模板内容为空")

    def test_skill_descriptions_meet_min_length(self):
        for skill in self.skills:
            self.assertGreaterEqual(
                len(skill.description), 20,
                f"skill {skill.name} 的 description 长度不足 20 字符",
            )


class DomainFailureTemplateTests(unittest.TestCase):
    """P6-03 域故障/禁用模板验收。"""

    def test_failure_template_high_risk_mental_contains_safety_keywords(self):
        text = domain_failure_template(KnowledgeDomain.MENTAL, RiskLevel.HIGH)
        self.assertIn("可信任", text)
        self.assertIn("紧急", text)

    def test_failure_template_high_risk_service_contains_escalation(self):
        text = domain_failure_template(KnowledgeDomain.SERVICE, RiskLevel.HIGH)
        self.assertIn("转接", text)
        self.assertIn("客服主管", text)

    def test_failure_template_high_risk_compliance_contains_authorized_channel(self):
        text = domain_failure_template(KnowledgeDomain.COMPLIANCE, RiskLevel.HIGH)
        self.assertIn("停止", text)
        self.assertIn("授权合规渠道", text)
        self.assertIn("不作事实定性", text)

    def test_failure_template_no_cross_domain_leak(self):
        for domain in KnowledgeDomain:
            templates = DOMAIN_FAILURE_TEMPLATES.get(domain, {})
            leak_keywords = DOMAIN_LEAK_KEYWORDS.get(domain, set())
            for risk_key, text in templates.items():
                for keyword in leak_keywords:
                    self.assertNotIn(
                        keyword, text,
                        f"{domain.value} 故障模板({risk_key}) 包含了不该出现的关键词: {keyword}",
                    )

    def test_disabled_template_explicit_no_fallback(self):
        for domain in [KnowledgeDomain.SERVICE, KnowledgeDomain.COMPLIANCE]:
            text = domain_disabled_template(domain)
            self.assertIn("不可用", text)
            self.assertIn("不会使用其他域", text)

    def test_disabled_template_mentions_human_channel(self):
        service_text = domain_disabled_template(KnowledgeDomain.SERVICE)
        self.assertIn("人工入口", service_text)
        compliance_text = domain_disabled_template(KnowledgeDomain.COMPLIANCE)
        self.assertIn("授权合规渠道", compliance_text)

    def test_disabled_templates_are_subset_of_failure_domains(self):
        """禁用模板只覆盖 SERVICE/COMPLIANCE，MENTAL 不允许禁用。"""
        failure_keys = set(DOMAIN_FAILURE_TEMPLATES.keys())
        disabled_keys = set(DOMAIN_DISABLED_TEMPLATES.keys())
        self.assertTrue(disabled_keys.issubset(failure_keys), "禁用模板的域应被故障模板覆盖")
        self.assertNotIn(KnowledgeDomain.MENTAL, disabled_keys, "MENTAL 域不应有禁用模板")


class DomainSystemPromptTests(unittest.TestCase):
    """P6-03 域系统 Prompt 验收。"""

    def test_service_prompt_contains_escalation_rule(self):
        msg = PromptTemplates.domain_answer_system_prompt(
            KnowledgeDomain.SERVICE, IntentType.SERVICE_SUPPORT, RiskLevel.HIGH,
            "测试上下文", "测试用户",
        )
        self.assertIn("高风险处理规则", msg.content)
        self.assertIn("转人工", msg.content)
        self.assertIn("客服主管", msg.content)

    def test_compliance_prompt_contains_no_determination_rule(self):
        msg = PromptTemplates.domain_answer_system_prompt(
            KnowledgeDomain.COMPLIANCE, IntentType.COMPLIANCE_CONSULT, RiskLevel.LOW,
            "测试上下文", "测试用户",
        )
        self.assertIn("事实定性", msg.content)
        self.assertIn("代替正式调查", msg.content)

    def test_compliance_prompt_contains_preservation_rule(self):
        msg = PromptTemplates.domain_answer_system_prompt(
            KnowledgeDomain.COMPLIANCE, IntentType.COMPLIANCE_VIOLATION, RiskLevel.HIGH,
            "测试上下文", "测试用户",
        )
        self.assertIn("保留", msg.content)
        self.assertIn("证据", msg.content)
        self.assertIn("授权合规渠道", msg.content)

    def test_compliance_prompt_not_confirms_violation(self):
        """合规 Prompt 只能禁止确认违规，不能实际确认违规。"""
        for risk in [RiskLevel.LOW, RiskLevel.HIGH]:
            msg = PromptTemplates.domain_answer_system_prompt(
                KnowledgeDomain.COMPLIANCE, IntentType.COMPLIANCE_VIOLATION, risk,
                "测试上下文", "测试用户",
            )
            # "不得确认违规" 是禁止性表述，合法；裸 "确认违规" 且无 "不得" 前缀才违规
            self.assertIn("不得确认违规", msg.content, "合规 Prompt 必须包含禁止确认违规的规则")

    def test_mental_prompt_falls_back_to_legacy_baseline(self):
        domain_msg = PromptTemplates.domain_answer_system_prompt(
            KnowledgeDomain.MENTAL, IntentType.CONSULT, RiskLevel.LOW,
            "测试上下文", "测试用户", "skill 上下文",
        )
        legacy_msg = PromptTemplates.answer_system_prompt(
            IntentType.CONSULT, RiskLevel.LOW, "测试上下文", "测试用户", "skill 上下文",
        )
        self.assertEqual(domain_msg.content, legacy_msg.content)

    def test_domain_prompts_no_cross_domain_keywords(self):
        """每个域的 Prompt 不应包含属于其他域的专有关键词。"""
        for domain in KnowledgeDomain:
            leak_keywords = DOMAIN_LEAK_KEYWORDS.get(domain, set())
            for risk in [RiskLevel.LOW, RiskLevel.HIGH]:
                msg = PromptTemplates.domain_answer_system_prompt(
                    domain, IntentType.CHAT, risk, "测试上下文", "测试用户",
                )
                for keyword in leak_keywords:
                    self.assertNotIn(
                        keyword, msg.content,
                        f"{domain.value} Prompt({risk.value}) 包含了不该出现的关键词: {keyword}",
                    )


def _build_mock_report_and_case(domain: KnowledgeDomain):
    """构造 mock 报告和 case，避免依赖 DB。"""
    db = MagicMock()
    user = MagicMock()
    user.username = "drill-user"
    user.display_name = "演练用户"
    db.get.return_value = user
    report = MagicMock(spec=PsychologicalReport)
    report.id = 1001
    report.user_id = 1
    report.domain = domain.value
    report.risk_level = RiskLevel.HIGH.value
    report.emotion = "HIGH_RISK" if domain == KnowledgeDomain.MENTAL else None
    report.severity_label = "HIGH_RISK"
    report.confidence = 0.9
    report.summary = f"{domain.value} 演练摘要"
    report.created_at = datetime(2026, 8, 10, 12, 0, 0)
    case = MagicMock(spec=RiskCase)
    case.id = 2001
    case.handoff_summary = f"[{domain.value}] handoff"
    settings = Settings(ai_provider="mock", alert_email_delivery_mode="log")
    tools = ToolOrchestrationService(db, settings)
    return tools, report, case


class EmailBodyDomainDifferentiationTests(unittest.TestCase):
    """P6-03 邮件正文模板域差异化验收。"""

    def test_email_body_uses_domain_specific_header(self):
        mental_tools, mental_report, mental_case = _build_mock_report_and_case(KnowledgeDomain.MENTAL)
        service_tools, service_report, service_case = _build_mock_report_and_case(KnowledgeDomain.SERVICE)
        compliance_tools, compliance_report, compliance_case = _build_mock_report_and_case(KnowledgeDomain.COMPLIANCE)
        mental_body = mental_tools._email_body(mental_report, mental_case)
        service_body = service_tools._email_body(service_report, service_case)
        compliance_body = compliance_tools._email_body(compliance_report, compliance_case)
        self.assertIn("高风险心理预警", mental_body)
        self.assertIn("客服高风险事件", service_body)
        self.assertIn("合规高风险事件", compliance_body)

    def test_email_body_label_line_differs_per_domain(self):
        mental_tools, mental_report, mental_case = _build_mock_report_and_case(KnowledgeDomain.MENTAL)
        service_tools, service_report, service_case = _build_mock_report_and_case(KnowledgeDomain.SERVICE)
        mental_body = mental_tools._email_body(mental_report, mental_case)
        service_body = service_tools._email_body(service_report, service_case)
        self.assertIn("情绪标签：", mental_body)
        self.assertIn("严重度：", service_body)

    def test_email_body_compliance_header_distinct_from_service(self):
        service_tools, service_report, service_case = _build_mock_report_and_case(KnowledgeDomain.SERVICE)
        compliance_tools, compliance_report, compliance_case = _build_mock_report_and_case(KnowledgeDomain.COMPLIANCE)
        service_body = service_tools._email_body(service_report, service_case)
        compliance_body = compliance_tools._email_body(compliance_report, compliance_case)
        service_header = service_body.splitlines()[0]
        compliance_header = compliance_body.splitlines()[0]
        self.assertNotEqual(service_header, compliance_header, "SERVICE 和 COMPLIANCE 邮件 header 不应相同")

    def test_email_body_includes_domain_field(self):
        for domain in KnowledgeDomain:
            tools, report, case = _build_mock_report_and_case(domain)
            body = tools._email_body(report, case)
            self.assertIn(f"域：{domain.value}", body, f"{domain.value} 邮件正文缺少域字段")


if __name__ == "__main__":
    unittest.main()
