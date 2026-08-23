from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.enums import IntentType, KnowledgeDomain, RiskLevel
from app.models.entities import PsychologicalReport, UserAccount


class SkillLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillValidationIssue:
    level: str
    message: str


@dataclass(frozen=True)
class MindBridgeSkill:
    name: str
    description: str
    body: str
    path: Path
    domain: str = "MENTAL"
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def registry_key(self) -> str:
        return f"{self.domain}:{self.name}"

    def prompt_context(self) -> str:
        return f"应用 skill: {self.name}\n{self.body.strip()}"

    def validation_issues(self) -> list[SkillValidationIssue]:
        issues: list[SkillValidationIssue] = []
        valid_domains = {d.value for d in KnowledgeDomain}
        if self.domain not in valid_domains:
            issues.append(SkillValidationIssue("ERROR", f"域 {self.domain} 不是合法 KnowledgeDomain"))
        if self.path.parent.name != self.name:
            issues.append(SkillValidationIssue("WARN", f"目录名 {self.path.parent.name} 与 skill name {self.name} 不一致"))
        if "## Workflow" not in self.body:
            issues.append(SkillValidationIssue("WARN", "建议包含 ## Workflow 小节，便于人工审阅和模型稳定加载"))
        if len(self.description) < 20:
            issues.append(SkillValidationIssue("WARN", "description 太短，可能无法准确表达触发场景"))
        if self.name == "counselor_handoff_summary" and "```text" not in self.body:
            issues.append(SkillValidationIssue("ERROR", "counselor_handoff_summary 必须包含 text 模板"))
        return issues


class MindBridgeSkillRegistry:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).resolve().parents[2] / "skills"

    def _discover_skill_files(self) -> list[Path]:
        """发现两级和一级目录下的 SKILL.md。

        - 两级：skills/<domain>/<skill-name>/SKILL.md（domain 从目录名读取）
        - 一级：skills/<skill-name>/SKILL.md（兼容期默认 MENTAL）
        """
        if not self.root.exists():
            return []
        files: list[Path] = []
        seen: set[Path] = set()
        # 两级目录优先
        for skill_file in sorted(self.root.glob("*/*/SKILL.md")):
            files.append(skill_file)
            seen.add(skill_file.resolve())
        # 兼容一级目录（跳过已被两级目录覆盖的）
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            if skill_file.resolve() not in seen:
                files.append(skill_file)
        return files

    def list_skills(self) -> list[MindBridgeSkill]:
        return [self._load_skill_file(f) for f in self._discover_skill_files()]

    def status_items(self) -> list[dict]:
        if not self.root.exists():
            return []
        items = []
        for skill_file in self._discover_skill_files():
            try:
                skill = self._load_skill_file(skill_file)
                issues = skill.validation_issues()
            except SkillLoadError as exc:
                items.append(
                    {
                        "name": skill_file.parent.name,
                        "domain": _infer_domain_from_path(skill_file, self.root),
                        "status": "FAILED",
                        "description": str(exc),
                        "path": _posix_path(skill_file.relative_to(self.root.parent)),
                        "issues": [{"level": "ERROR", "message": str(exc)}],
                    }
                )
                continue
            has_error = any(issue.level == "ERROR" for issue in issues)
            items.append(
                {
                    "name": skill.name,
                    "domain": skill.domain,
                    "status": "FAILED" if has_error else "READY" if not issues else "WARN",
                    "description": skill.description,
                    "path": _posix_path(skill.path.relative_to(self.root.parent)),
                    "issues": [{"level": issue.level, "message": issue.message} for issue in issues],
                    "metadata": skill.metadata,
                }
            )
        return items

    def get_required(self, name: str, *, domain: str | None = None) -> MindBridgeSkill:
        for skill in self.list_skills():
            if skill.name == name and (domain is None or skill.domain == domain):
                return skill
        raise SkillLoadError(f"required standard skill not found: {name}" + (f" (domain={domain})" if domain else ""))

    def template_for(self, name: str, *, domain: str | None = None) -> str:
        skill = self.get_required(name, domain=domain)
        match = re.search(r"```text\s*\n(?P<template>.*?)\n```", skill.body, re.DOTALL)
        if match is None:
            raise SkillLoadError(f"standard skill {name} does not define a text template")
        return match.group("template").strip()

    def _load_skill_file(self, path: Path) -> MindBridgeSkill:
        text = path.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(text, path)
        name = metadata.get("name") or path.parent.name
        description = metadata.get("description", "")
        if not name.strip():
            raise SkillLoadError(f"{path} is missing frontmatter name")
        if not description.strip():
            raise SkillLoadError(f"{path} is missing frontmatter description")
        if not body.strip():
            raise SkillLoadError(f"{path} is missing skill body")
        domain = _infer_domain_from_path(path, self.root)
        return MindBridgeSkill(
            name=name.strip(),
            description=description.strip(),
            body=body.strip(),
            path=path,
            domain=domain,
            metadata=metadata,
        )


class MindBridgeSkillLibrary:
    @staticmethod
    def registry() -> MindBridgeSkillRegistry:
        return MindBridgeSkillRegistry()

    @staticmethod
    def list_skills() -> list[MindBridgeSkill]:
        return MindBridgeSkillLibrary.registry().list_skills()

    @staticmethod
    def status_items() -> list[dict]:
        return MindBridgeSkillLibrary.registry().status_items()

    @staticmethod
    def response_skill_context(intent: IntentType, risk: RiskLevel, text: str) -> str:
        names = MindBridgeSkillLibrary.response_skill_names(intent, risk, text)
        registry = MindBridgeSkillLibrary.registry()
        return "\n\n".join(registry.get_required(name).prompt_context() for name in names)

    @staticmethod
    def response_skill_names(intent: IntentType, risk: RiskLevel, text: str) -> list[str]:
        if intent == IntentType.CHAT:
            return []

        if risk == RiskLevel.HIGH:
            return ["supportive_response_baseline", "high_risk_safety_plan"]

        lowered = text.lower()
        names = ["supportive_response_baseline", "referral_resource_guidance"]
        if _contains_any(lowered, ["焦虑", "惊恐", "恐慌", "panic", "anxious", "崩溃", "呼吸"]):
            names.append("anxiety_grounding_support")
        if _contains_any(lowered, ["失眠", "睡不着", "睡眠", "熬夜", "sleep", "insomnia"]):
            names.append("sleep_routine_support")
        if _contains_any(lowered, ["考试", "挂科", "绩点", "论文", "作业", "学业", "学习", "academic", "exam"]):
            names.append("academic_stress_planning")
        return _dedupe(names)

    @staticmethod
    def high_risk_safety_plan_prompt() -> str:
        return MindBridgeSkillLibrary.registry().get_required("high_risk_safety_plan").prompt_context()

    @staticmethod
    def counselor_handoff_summary(report: PsychologicalReport, user: UserAccount | None) -> str:
        template = MindBridgeSkillLibrary.registry().template_for("counselor_handoff_summary")
        student = _student_label(user, report.user_id)
        urgency = "立即跟进" if report.risk_level == RiskLevel.HIGH.value else "尽快跟进"
        next_steps = [
            f"{urgency}，确认学生当前位置、身边是否有人陪伴，以及当前是否安全。",
            "联系学生本人或其可用的现实支持人，并记录已采取的联系方式。",
            "必要时联系校园保卫、心理中心值班老师或当地紧急救助。",
            "将后续安排、接手人和下一次复访时间写入个案备注。",
        ]
        return _render_template(
            template,
            {
                "report_id": str(report.id),
                "student": student,
                "risk_level": report.risk_level,
                "emotion": report.emotion,
                "confidence": f"{report.confidence:.2f}",
                "summary": report.summary,
                "next_steps": "\n".join(f"- {step}" for step in next_steps),
                "content_excerpt": _truncate(report.content, 700),
            },
        )


def _infer_domain_from_path(path: Path, root: Path) -> str:
    """从 SKILL.md 路径推断域。

    两级目录 skills/<domain>/<name>/SKILL.md -> <domain>（大小写不敏感匹配枚举）
    一级目录 skills/<name>/SKILL.md          -> MENTAL（兼容期默认）
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return KnowledgeDomain.MENTAL.value
    parts = relative.parts
    valid_domains = {d.value: d.value for d in KnowledgeDomain}
    if len(parts) >= 3 and parts[0].upper() in valid_domains:
        return valid_domains[parts[0].upper()]
    return KnowledgeDomain.MENTAL.value


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise SkillLoadError(f"{path} is missing YAML frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise SkillLoadError(f"{path} has unterminated YAML frontmatter")
    metadata = {}
    for line in text[4:end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise SkillLoadError(f"{path} has invalid frontmatter line: {line}")
        key, value = stripped.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, text[end + len("\n---") :].strip()


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def _posix_path(value: Path) -> str:
    """返回正斜杠分隔的相对路径，保证 API 输出跨平台一致。"""
    return str(value).replace("\\", "/")


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _student_label(user: UserAccount | None, user_id: int) -> str:
    if user is None:
        return f"userId={user_id}"
    if user.display_name:
        return f"{user.display_name} ({user.username})"
    return user.username


def _truncate(text: str, limit: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit - 3]}..."
