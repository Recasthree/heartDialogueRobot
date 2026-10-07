# 工程修复与验收记录（2026-09-29）

## 当前交付状态

代码、回归测试、真实语义检索探针、网页操作台和少量真实模型联调已完成。
**完整医学语料的首次 CPU 向量构建仍在运行，尚未激活全量索引；不能把本记录解释为全量最终验收已完成。**

本轮直接使用现有 `data/verified`，未清洗、覆盖或修改源数据，未更改审核字段，未删除 `.partial`、缓存或旧32维集合。

实时状态：[delivery-status.json](../data/runtime/verification/delivery-status.json)。
一次性收尾程序在当前构建成功后运行零模型调用的 RAGCare 检索评测，并请求启动网页；构建中止则记录 failed。
机器睡眠、关闭解释器或关闭电脑会影响长任务；不要重复启动另一份全量构建。

## 已执行的验证

| 验证 | 实际结果 | 证据文件 |
|---|---|---|
| Python 3.13.9 全量离线测试 | 76 passed，6 skipped，0 failed；跳过项为不存在的 repaired corpus | `data/runtime/verification/pytest.xml` |
| 真实 E5 + Chroma | 384维；中文/英文同一主题检索成功；无关问题被过滤；独立 Python 进程重新连接可读证据与隔离记忆 | `data/runtime/verification/semantic.json` |
| 全部32条 RAGCare benchmark | Recall@5 0.8125，MRR@5 0.9594，Hit@5 1.0；API调用0；只允许benchmark context参与召回 | `data/runtime/verification/ragcare.json`；`data/runtime/evaluations/20260929T060201-bea493a2.json` |
| 网页浏览器验收 | 病例创建/绑定、急救、零模型调用、追踪、会话恢复、后台评测、390px布局、无JS错误 | `data/runtime/verification/browser.json`；同目录 `console-*.png` |
| DeepSeek真实来源回答 + Judge | 返回合法来源引用；五维Judge完成；本轮成功复测6次请求 | `data/runtime/verification/live-grounded.json` |
| 首轮真实接口探针 | 8次请求；急救0次，病例与DDI路由触发；冷启动和Judge结构曾降级，后续已修复并复测 | `data/runtime/verification/live-model.json` |
| 安全集离线基线 | 38条，程序动作/引用等契约检查38/38；包含全部必需文字和标记的严格检查仅9/38 | `data/runtime/evaluations/20260929T054525-877fdf86.json` |

真实接口探针累计14次请求，不是14次用户聊天。未进行大批量付费生成或Judge评测。
首轮失败报告保留，未以成功结果覆盖历史失败。

RAGCare成绩来自独立的完整benchmark集合，与主医学索引隔离；不代表28,898条医学来源的全量回答质量。
该金标准没有意图字段，因此按意图统计为 `unlabeled`；独立Agent套件才有真实意图标签。
安全集 `contract_pass_rate` 不评价临床准确性；必需内容采用字面/显式别名匹配，不等于语义评审。9/38明确表明离线保守拒答并未满足所有回答完整性要求。

## 大纲要求—实际实现—验证证据

| 要求 | 实际实现 | 验证 |
|---|---|---|
| 急救先于依赖 | 原始输入只做本地规则识别，命中后不访问病例、记忆、检索、生成；脱敏后再进入常规状态图 | `tests/test_regressions.py`、急救用药复合请求测试、浏览器追踪0调用 |
| 脱敏不破坏医学词 | 姓名边界和医学术语保护；病例结构字段脱敏保留药物名称 | 脱敏回归及原隐私样本测试 |
| 结构化回答 | Pydantic统一契约，最多一次结构/引用修复；纯文本不能自动获得来源 | 非JSON、伪造和部分无效引用测试 |
| 安全复核约束最终输出 | unsafe/unsupported/safe=false/unavailable均拒答；不保留无效引用对应断言 | 三类安全失败回归、真实来源回答 |
| 模型补充边界 | 仅显式许可的一般解释；标注未绑定资料；病例/个人用药/强制本地来源场景不允许 | supplement与local-only测试 |
| 真正语义检索 | E5-small，query/passage前缀，归一化384维；按token偏移切分384/64重叠，保留原文 | 语义探针及分块测试 |
| BM25 / Chroma / RRF | 持久化词频与倒排结构；同范围预过滤，成功通道独立投票；每次最多一次批量重排 | 查询不重建、过滤、单通道权重、批量重排测试 |
| 来源与版本 | 来源命名空间ID、父ID、源文件SHA、内容哈希、章节；相同内容不同来源不覆盖 | 重复来源、增量更新/移除测试 |
| 启动不全量编码 | factory + lifespan加载已构建SQLite；后台只预热模型；独立命令构建 | 重启零embed_documents调用测试；全量加载耗时待构建完成记录 |
| 构建期间来源更新 | JSONL不可变字节快照，翻译进程继续工作；partial仅登记 | 并发写入快照、原审核字段保留测试 |
| 安全切换索引 | 完整成功才写active；后续增量构建分代集合，旧集合保留 | 两次构建不同collection、删除来源与缓存复用测试 |
| LangGraph分工 | 意图驱动通用/药物/指南/组合分支，最多4个子查询、有限并发和超时；注入项目skills/prompts | 路由追踪、真实药物/DDI探针 |
| 药物与工具 | 说明书章节检索；DDI区分found/no_record/unavailable；PubMed与KG可选 | DDI三态、工具失败降级测试；真实DDI为空时拒绝合用结论 |
| 病例业务闭环 | SQLite事务，旧JSON一次性备份导入；PATCH禁身份字段并完整复验；聊天case_id查库、缺失404 | CRUD、嵌套验证、病例上下文、浏览器绑定 |
| 两级记忆 | Redis断线回本地；同会话串行；患者/用户/会话作用域；独立Chroma画像和摘要；每6轮摘要 | Redis断连、用户隔离、跨进程持久化探针 |
| 追踪与降级 | 节点耗时、候选ID、工具状态、有效skills、实际模型/调用数、降级原因；只存脱敏信息 | trace接口与浏览器检查 |
| 真实评测 | 正相关qrels、父文档去重、Recall/Hit/MRR分开；Judge收到正文及参考；禁用/失败无伪分数 | 两相关文档命中一条Recall=.5回归，Judge停用测试，32条实测 |
| 运行与网页 | 应用工厂、存活/就绪、真实状态、病例操作、会话、引用、后台评测和追踪 | lifespan集成测试和浏览器验收 |

## 本机启动与查看

本机不要用系统默认 Python 3.8。包装脚本自动选择支持的解释器：

```powershell
.\scripts\run_python.ps1 -m pytest -q
Get-Content .\data\runtime\verification\delivery-status.json
.\scripts\check_runtime.ps1
```

全量构建与一次性收尾成功后，浏览器访问 `http://127.0.0.1:8000`。
若自行提前启动了旧索引进程，收尾不会强行停止它；需手动执行 `stop_app.ps1` 后再 `start_app.ps1` 加载新版本。
后续翻译新增内容可在当前构建结束后执行：

```powershell
.\scripts\run_python.ps1 -m scripts.build_index
.\scripts\stop_app.ps1
.\scripts\start_app.ps1
```

配置入口仍是项目根目录 `.env.local`；日志、报告和示例均不包含密钥。环境变量覆盖本地配置。

## 明确未验收或不在范围内

- 全量医学索引尚在编码：最终条数、全量启动耗时、活动索引复测以 `delivery-status.json` 为准。
- 32条 full-generation + Judge尚未批量实跑，避免额外消耗额度；当前只完成少量真实模型联调。
- 未重启Chroma服务；验证的是独立Python客户端进程重新连接后的读取，不冒充服务端灾难恢复测试。
- 未配置PubMed/KG远端服务；DDI源为空时正确降级，不能宣称具备完整药物相互作用知识。
- 依赖直接版本已固定，验证平台为本机Python3.13.9；未在所有Python版本和干净机器上重复安装验收。
- 不包含账户认证、OCR、医院对接或生产医疗部署。

网页沿用原深青/纸面风格；frontend-design技能用于约束布局、可读性和移动端验证，没有改造成宣传页。
