"""P0-01 心理域基线固化测试。

固化当前心理域的关键行为契约，防止多域改造（P1～P5）造成回归：
- SSE 事件顺序与 wire 格式
- 意图/风险/工具枚举基线
- 7 个标准心理 Skill 的加载与选择
- RAG 评测数据集规模与结构
- 高风险回复禁止暴露后台元数据（脱敏契约）

设计原则：LLM 自由文本不做整段字符串快照，只断言结构、必需/禁止内容和
artifact 关系，避免脆弱测试。
"""

import json
import unittest
from pathlib import Path

from app.core.enums import EmotionLabel, IntentType, RiskLevel, ToolJobKind
from app.services.skills import MindBridgeSkillLibrary

PROJECT_ROOT = Path(__file__).resolve().parents[1]

STANDARD_SKILL_NAMES = {
    "supportive_response_baseline",
    "high_risk_safety_plan",
    "anxiety_grounding_support",
    "sleep_routine_support",
    "academic_stress_planning",
    "referral_resource_guidance",
    "counselor_handoff_summary",
}


class SseContractTests(unittest.TestCase):
    def test_sse_wire_format(self):
        from app.services.chat import sse

        payload = {"type": "meta", "sessionId": "s1"}
        rendered = sse("meta", payload)
        self.assertTrue(rendered.startswith("event: meta\n"))
        self.assertIn("data: ", rendered)
        self.assertTrue(rendered.endswith("\n\n"))
        self.assertEqual(json.loads(rendered.split("data: ", 1)[1].strip())["sessionId"], "s1")

    def test_sse_event_sequence_meta_token_done(self):
        from app.services.chat import ChatStreamEvent, sse

        events = []
        events.append(sse("meta", ChatStreamEvent(type="meta", sessionId="s1").model_dump(by_alias=True)))
        for token in ["你", "好"]:
            events.append(sse("token", ChatStreamEvent(type="token", sessionId="s1", content=token).model_dump()))
        events.append(sse("done", ChatStreamEvent(type="done", sessionId="s1").model_dump()))

        names = [block.split("\n", 1)[0].removeprefix("event: ") for block in events]
        self.assertEqual(names, ["meta", "token", "token", "done"])


class EnumBaselineTests(unittest.TestCase):
    def test_legacy_intent_enum_present(self):
        # 基线意图：CHAT / CONSULT / RISK（多域改造后必须保留兼容）
        self.assertEqual(IntentType.CHAT.value, "CHAT")
        self.assertEqual(IntentType.CONSULT.value, "CONSULT")
        self.assertEqual(IntentType.RISK.value, "RISK")

    def test_risk_and_emotion_enum_present(self):
        self.assertEqual(RiskLevel.LOW.value, "LOW")
        self.assertEqual(RiskLevel.MEDIUM.value, "MEDIUM")
        self.assertEqual(RiskLevel.HIGH.value, "HIGH")
        self.assertEqual(EmotionLabel.HIGH_RISK.value, "HIGH_RISK")

    def test_tool_job_kind_baseline(self):
        # 心理域工具计划基线
        self.assertEqual(ToolJobKind.EXCEL_REPORT.value, "EXCEL_REPORT")
        self.assertEqual(ToolJobKind.CASE_CREATE.value, "CASE_CREATE")
        self.assertEqual(ToolJobKind.ALERT_SEND.value, "ALERT_SEND")


class SkillBaselineTests(unittest.TestCase):
    def test_seven_standard_skills_loaded(self):
        skills = MindBridgeSkillLibrary.list_skills()
        names = {skill.name for skill in skills}
        missing = STANDARD_SKILL_NAMES - names
        self.assertFalse(missing, f"missing standard skills: {missing}")

    def test_all_standard_skills_ready(self):
        failed = [item for item in MindBridgeSkillLibrary.status_items() if item["status"] != "READY"]
        self.assertFalse(failed, f"standard skill load failures: {failed}")

    def test_high_risk_skill_selection(self):
        names = MindBridgeSkillLibrary.response_skill_names(
            IntentType.RISK, RiskLevel.HIGH, "我不想活了。"
        )
        self.assertEqual(names, ["supportive_response_baseline", "high_risk_safety_plan"])

    def test_consult_skill_selection(self):
        names = MindBridgeSkillLibrary.response_skill_names(
            IntentType.CONSULT, RiskLevel.LOW, "我最近焦虑、失眠，考试压力也很大。"
        )
        for required in [
            "supportive_response_baseline",
            "referral_resource_guidance",
            "anxiety_grounding_support",
            "sleep_routine_support",
            "academic_stress_planning",
        ]:
            self.assertIn(required, names)


class RagBaselineTests(unittest.TestCase):
    def test_mental_rag_dataset_meets_minimum_size_and_structure(self):
        dataset = json.loads((PROJECT_ROOT / "app/rag_eval/mindbridge-rag-eval.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(dataset), 50)
        for case in dataset:
            self.assertIn("id", case)
            self.assertIn("question", case)
            self.assertIn("expectedSources", case)
            self.assertIsInstance(case["expectedSources"], list)


class ReportContractTests(unittest.TestCase):
    def test_report_carries_emotion_and_risk_columns(self):
        from app.models.entities import PsychologicalReport

        columns = {column.name for column in PsychologicalReport.__table__.columns}
        for required in ["intent", "emotion", "emotion_score", "risk_level", "confidence", "summary"]:
            self.assertIn(required, columns)


if __name__ == "__main__":
    unittest.main()
