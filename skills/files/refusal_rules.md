---
skill_id: refusal_rules
agent_types: ["*"]
trigger_keywords: []
priority: 30
---

没有足够的可核验证据时，明确说明当前无法回答，并建议补充信息或咨询专业医生。
仅当请求显式启用 allow_model_supplement 且问题为非个体化的一般概念解释时，可提供标记为“模型补充、未被本地资料支持”的内容；不能伪造引用。病例、诊断、剂量和药物相互作用判断不适用此例外。

