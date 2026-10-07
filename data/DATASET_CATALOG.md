# 心血管项目数据集总表

本表由 `scripts/rebuild_data_reports.py` 根据当前文件重新生成。数据可用于本地 Agent/RAG 流程实验，不等于医学事实已获临床验证。

| 数据集 | 当前文件统计 | 状态 |
|---|---:|---|
| Huatuo 心血管候选 | 370 条；审计筛选结果 374 条；差异 ID 4 个 | 已清洗，保留差异审计 |
| Medical Dialogue | canonical 659 条；SFT 785 条；format review 272 条；normalization review 426 条 | 结构化结果 |
| RAGCare-QA | 原始 420 条；心血管金标准 32 条；qrels 96 条 | 仅评测用途 |
| HeartBioPortal 知识 | chunks 28898；evidence 28898；机器翻译 10516 条 | pending / 未完成医学审核 |
| 药物资料 | 原始 3925 条；当前完整输出 740 条；按正式输出仍缺 3185 条；partial 985 条 | in_progress，不读取 partial 作为正式输入 |
| Judge 评测 | 32 条 | 已生成 |
| 安全评测 | 38 条 | 已生成 |
| 隐私评测 | 76 条 | 已生成 |
| Agent 数据 | 意图实体 105 条；记忆会话 36 组 | 合成测试数据 |

## 处理约束

- 不修改已有 `review_status` 或 `medical_truth_verified`。
- 不删除 `.partial`、缓存或原始文件。
- 机器翻译、合成数据和未审核来源必须在回答和引用中保留未验证标记。
- `data/verified/evaluation`、`privacy`、`safety` 和 `agent` 数据进入索引登记，但默认不作为医学回答证据。
