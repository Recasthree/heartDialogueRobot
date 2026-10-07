# Agentic RAG 实施与验收记录

日期：2026-09-29。范围：附件解析、受控工具规划、真实 MCP、网络取证和演示操作台；不清洗数据、不修改审核字段、不重建现有全语料任务。

## 交付状态

核心功能已实现并完成下表验证。不能据此宣称完整语料已就绪、真实模型全流程已稳定验收，或严格安全集全部通过。

| 要求 | 实际实现 | 验证证据 |
|---|---|---|
| 附件解析 | PNG/JPEG/TXT/文字及扫描 PDF；真实 CPU RapidOCR；页码、坐标、置信度、哈希 | `tests/test_agentic_tools.py` 的真实 OCR、文字 PDF、混合 PDF 测试 |
| 确认与修订 | 所有页保守确认；修订提升版本并使确认失效；患者资料与参考资料分开 | 上传接口、未确认 409、版本冲突、纯药名无数字用例 |
| 作用域与删除 | 会话/病例/用户组合隔离；24 小时到期清理；删除终止解析子进程 | 跨会话 404、清理仅登记文件、真实 MCP 解析取消测试 |
| MCP | 官方 Python SDK 2.2.0，真实 Streamable HTTP 工具发现/调用，stdio 扩展 | HTTP 与 stdio 实际连接测试；内部凭据、节点权限和能力令牌 |
| 网络取证 | PubMed 正式摘要、openFDA 说明书、可选 Tavily、受限网页/PDF 抓取 | 真实公共接口均返回 6 个正文片段；未配置 Tavily 不伪装可用 |
| 有限工具循环 | 最多两轮计划、八次工具、并发二、一次最终重排 | 预算、重复调用、非法工具、联网关闭、重排次数回归 |
| 隐私与引用 | 脱敏查询；患者附件不是医学证据；临时附件不自动进入记忆；真实检索 ID 约束 | PII、确认门、案例显式保存、伪造引用与空 DDI 回归 |
| 网页 | 上传、确认、修订、用途、联网开关、引用账本、工具时间线 | 真 Chrome 桌面 1440px 与窄屏 390px；无 JS 异常及横向溢出 |
| 依赖与启动 | 新 `.venv-agentic`、103 项依赖闭包锁、启动入口优先新环境 | Windows Python 3.13；未覆盖旧 `.runtime-deps` 或旧翻译环境 |

本机新 venv 通过 `system-site-packages` 只读复用已有 Torch 等大型依赖，新依赖安装在新 venv 内。锁文件用于新环境重建，但本轮未另做一次从零下载全部依赖的干净机器验收，不宣称跨平台锁可复现。

## 自动化回归

最终运行：**107 passed、6 skipped、0 failed**，用时 123.01 秒。六个跳过项是缺少 repaired corpus 的历史测试；没有改金标准或减少安全断言。

原始记录：[JUnit XML](../data/runtime/verification/agentic-pytest.xml)。存在四类上游弃用警告（LangGraph、websockets、jieba/pkg_resources），不影响本次测试结果。

复现：

```powershell
.\scripts\run_python.ps1 -m pytest -q
node --check web/app.js
node --check web/attachments.js
```

测试使用 Mock、临时目录和本机 HTTP/stdio，不调用付费模型或外网取证。真实 CPU OCR 测试需要先执行 `python -m scripts.setup_ocr`，当前字体夹具使用 Windows Arial。

## 浏览器与真实网络演示

使用 Mock 生成器、五条已有真实资料片段及独立临时索引；不是完整 E5/Chroma 检索验收。公共医学接口与 MCP 传输是真实的。

- 本地带引用回答：5 条引用。
- 上传 TXT 检查报告 → 确认 → 原始急救输入：零模型/工具调用。
- 同一已确认附件 → `阿司匹林 aspirin warnings 联网`：本地药品分区无记录，通用检索后补充 openFDA；返回 5 条说明书引用，保留未验证标记、US 地区和日期。
- 工具时间线实际展示 `lookup_drug`、`search_local`、`search_drug_labels`、`search_pubmed` 的状态和耗时；PubMed 对该中文检索返回无记录，不伪造证据。
- 0 次付费模型调用；浏览器无 JavaScript 错误、窄屏无横向溢出。

报告：[浏览器及网络记录](../data/runtime/verification/agentic-20260929-175354/report.json)。

截图：[上传确认](../data/runtime/verification/agentic-20260929-175354/desktop-attachment.png)、[补证与工具时间线](../data/runtime/verification/agentic-20260929-175354/desktop-network-citations.png)、[窄屏](../data/runtime/verification/agentic-20260929-175354/mobile.png)。

```powershell
.\scripts\run_python.ps1 -m scripts.verify_agentic --browser --network
```

按照 frontend-design 技能保留原研究台的深青、纸色与铜色风格，新增交互围绕上传、确认、来源和执行状态，不另做无关视觉重构。

## 真实模型：总计 10 次请求

1. [首轮完整图冒烟](../data/runtime/verification/agentic-20260929-173104/report.json)：8 次 HTTP 模型请求。分类、路由及工具真实运行，但本地生成出现非法 JSON，安全复核亦不可用；最终拒答而不是附加虚假引用。药物补证还出现一次公开接口 HTTP 错误，保留降级。该轮不能算完整回答链路通过。
2. 修复：显式 `thinking.type=disabled`，输出上限 4096，空输出/截断明确失败，记录结束原因及 token 用量但不记录思考内容。官方接口说明参见 [DeepSeek JSON](https://api-docs.deepseek.com/guides/json_mode/) 和 [思考模式参数](https://api-docs.deepseek.com/api/create-chat-completion/)。同时不向规划器提供未配置的 Tavily/CPubMed 工具，修复本地空候选缺少状态字段问题。
3. [修复后定向生成与复核](../data/runtime/verification/model-json-20260929-174531.json)：2 次请求。实际模型返回合法 `cited` JSON，引用 ID 属于所提供片段；安全复核 `pass/safe=true`；两次均 `finish_reason=stop`，合计 908 tokens。使用已有资料，不发送患者附件。

总计 **10 次**，达到用户给定上限后停止付费测试。修复后的完整 DeepSeek 多节点、附件、联网联合流程尚未再次付费重跑；不能用这两次定向成功代替全链路稳定性结论。`verify_agentic --live` 和 `verify_model_json --live` 是主动付费命令，日后运行会产生新的请求。

## 严格安全集的真实结果

离线 Mock、独立五片段索引、`network_mode=off`、Judge 关闭，执行现有全部 38 条安全样本：

| 指标 | 结果 |
|---|---|
| 严格通过：动作、引用、禁止内容、必需表述与标记全部满足 | **9 / 38（23.68%）** |
| 行为契约：动作、引用契约和禁止内容检查 | 38 / 38 |
| 模型请求 | 0 |

失败项主要缺少具体必需表述和 `borderline`、`ordinary` 等场景标记，普通知识题在这个小索引里常被拒答。严格评分采用现有逐字/显式别名规则，不是语义 Judge；但这不意味着可把所有失败当成文字差异而忽略。部分场景确实缺少针对性的分诊/解释内容，仍需后续在证据充分时补齐并重测。

保留逐条结果：[完整安全评测](../data/runtime/verification/agentic-20260929-175559/evaluations/20260929T095612-c460acc7.json)；[运行摘要](../data/runtime/verification/agentic-20260929-175559/report.json)。没有修改安全样本、扩展别名或放宽拒答条件凑满分。这也不是临床安全认证。

```powershell
.\scripts\run_python.ps1 -m scripts.verify_agentic --safety
```

## 当前运行与尚未完成的验收

- API 已在 `http://127.0.0.1:8000/` 启动，读取原有 `.env.local`。不要直接打开 HTML。
- 既有完整 E5 索引构建仍在运行，尚未提交活动索引；当前 API 显示 0 个活动片段、`index_ready=false`。Chroma 服务本身仍运行，但未绑定活动证据集合，所以证据通道不可用。附件和公共网络工具不依赖该全量构建完成。
- 全量索引完成后，重启 **API** 加载活动索引，再做全语料检索评测；不要停止 Chroma、翻译或索引构建任务。`scripts/stop_app.ps1` 只针对登记的 API 进程。
- 未配置 Tavily；没有实测第三方正式 MCP 供应商，stdio 验收使用项目自有协议夹具。空 DDI 仍不能说明没有相互作用。
- OCR 已测中英文印刷识别路径，不承诺手写、复杂表格、影像诊断。网络 PDF 的低置信/含数值 OCR 页不直接当医学证据，不自动解除确认要求。
- 目前没有生产账户认证；附件 scope 是本机隔离，不是医院级权限系统。

启动和完整配置见 [使用说明](AGENTIC_RAG.md)，外部工具模板见 `config/mcp_servers.example.json`。已有公开数据、审核字段、翻译缓存与 `.partial` 均未因本功能被改写或清理。
