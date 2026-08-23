---
name: counselor_handoff_summary
description: Use when generating a structured handoff summary for the counselor after a high-risk or consultation conversation; ensures key information is passed to the professional.
---

# Counselor Handoff Summary

## Workflow

- Generate a structured summary for counselor follow-up after risk or consultation conversations.
- Ensure all key risk indicators, context, and recommended next steps are included.
- The summary template below is filled automatically and handed to the counselor.

```text
应用 skill: counselor_handoff_summary
心理咨询转接摘要
===================
报告ID：{{report_id}}
学生：{{student}}
风险等级：{{risk_level}}
情绪标签：{{emotion}}
置信度：{{confidence}}
摘要：{{summary}}

待办事项：
{{next_steps}}

对话节选：
{{content_excerpt}}
```
