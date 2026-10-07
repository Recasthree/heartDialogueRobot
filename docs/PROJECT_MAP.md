# 项目文件地图

这份文件说明首个公开版本中计划提交到 GitHub 的目录和文件。`data/` 中的原始数据、处理结果和运行时产物不会提交；本地存在但未纳入首个公开版本的数据处理脚本也会保留在开发环境中，不作为线上仓库的一部分。

## 根目录文件

| 文件 | 用途 |
|---|---|
| `README.md` | 项目主说明，包含本地运行、研发背景和较完整的工程操作说明。 |
| `README_GITHUB.md` | 面向 GitHub 访客的精简展示页，介绍项目定位、架构、启动方式、限制和安全边界。 |
| `心血管医疗对话机器人 · 完整项目大纲.md` | 项目设计、目标、模块和阶段性规划的中文总纲。 |
| `.gitignore` | 排除密钥、本地环境、数据集、数据库、日志、缓存和构建产物。 |
| `.env.example` | 基础环境变量模板，不应填写真实密钥。 |
| `.env.local.example` | 本地完整配置模板，复制为 `.env.local` 后按需填写；真实 `.env.local` 不上传。 |
| `Dockerfile` | 构建 API 服务容器镜像。 |
| `docker-compose.yml` | 启动 API、Redis 和 Chroma 的本地编排配置。 |
| `pyproject.toml` | Python 项目信息、依赖分组、构建配置和 pytest 配置。 |
| `requirements-runtime.txt` | 运行时依赖集合。 |
| `requirements-agentic.txt` | Agentic/MCP/OCR 等扩展依赖集合。 |
| `requirements-agentic-lock.txt` | Agentic 依赖的锁定版本记录，用于复现环境。 |

## `app/`：应用核心

| 文件/目录 | 用途 |
|---|---|
| `app/main.py` | FastAPI 应用入口、生命周期管理和路由装配。 |
| `app/config.py` | 环境变量、默认配置和运行模式读取。 |
| `app/models.py` | API、病例、消息、证据、附件和评测相关的数据模型。 |
| `app/providers.py` | Mock、兼容 OpenAI API 的模型提供方和生成调用封装。 |
| `app/store.py` | 本地持久化、病例和会话相关存储抽象。 |
| `app/memory.py` | 对话记忆、摘要和可选长期记忆管理。 |
| `app/privacy.py` | 外发前脱敏、隐私识别和患者资料边界处理。 |
| `app/dialogue_policy.py` | 对话范围、拒答、急救拦截和医疗安全策略。 |
| `app/evaluation.py` | 检索、回答、安全和 Judge 评测逻辑。 |
| `app/skills.py` | 加载和组合项目内的安全、引用和免责声明规则。 |
| `app/browser.py` | 浏览器 Cookie、会话身份和聊天历史相关逻辑。 |
| `app/api/routes.py` | HTTP API 路由，包括聊天、病例、附件、检索、评测和工具轨迹。 |
| `app/attachments/store.py` | 上传附件的临时存储、隔离和清理。 |
| `app/attachments/worker.py` | 附件解析、OCR 和后台处理任务。 |
| `app/core/agentic.py` | Agentic RAG 主流程、工具调用和状态转移。 |
| `app/core/graph.py` | LangGraph 节点、边和对话工作流编排。 |
| `app/retrieval/corpus.py` | 语料读取、分区和证据对象准备。 |
| `app/retrieval/disk.py` | 磁盘 BM25 倒排索引和持久化检索。 |
| `app/retrieval/hybrid.py` | BM25 与向量检索结果合并、RRF 和候选排序。 |
| `app/retrieval/chroma_store.py` | Chroma 向量集合的读写和过滤。 |
| `app/retrieval/encoder_worker.py` | 本地语义向量编码和批处理工作进程。 |
| `app/retrieval/index_manager.py` | 索引版本、构建状态、切换和回滚管理。 |
| `app/retrieval/build_job.py` | 分阶段、低内存索引构建任务。 |
| `app/retrieval/live.py` | 当前活动索引和运行时检索状态查询。 |
| `app/tools/contracts.py` | 工具调用、证据和错误状态的数据契约。 |
| `app/tools/gateway.py` | 受控 MCP 和外部工具调用网关。 |
| `app/tools/fallback.py` | 网络、模型或工具不可用时的降级处理。 |
| `app/tools/network.py` | 网络访问、DNS 和公共来源请求控制。 |
| `app/tools/pubmed.py` | PubMed 证据查询适配器。 |
| `app/tools/cpubmed_kg.py` | CPubMed/知识图谱网关适配器。 |
| `app/tools/patient_sources.py` | 患者提供资料与医学证据的隔离读取。 |
| `app/tools/drug_interaction.py` | 药物相互作用工具接口；不在缺少证据时推断结论。 |
| `app/tools/translation.py` | 翻译服务接口和翻译结果约束。 |
| `app/__init__.py`、各子目录 `__init__.py` | Python 包初始化文件。 |

## `web/`：浏览器界面

| 文件 | 用途 |
|---|---|
| `web/index.html` | 聊天页面结构、研究台入口和基础文案。 |
| `web/styles.css` | 页面布局、颜色、响应式和通用视觉样式。 |
| `web/chat.css` | 聊天消息、时间轴、侧栏和证据卡片样式。 |
| `web/app.js` | 前端启动、会话、消息发送和 API 交互。 |
| `web/browser.js` | 浏览器 Cookie、身份和历史侧栏逻辑。 |
| `web/attachments.js` | 附件上传、OCR 确认和病例保存交互。 |

## `config/`：可公开配置

| 文件 | 用途 |
|---|---|
| `config/mcp_servers.example.json` | MCP 服务配置示例；本地真实配置使用 `mcp_servers.local.json`，不上传。 |
| `config/drug_aliases.json` | 药物别名和规范化映射配置。 |

## `prompts/`：模型提示词

| 文件 | 用途 |
|---|---|
| `generation_with_citation.txt` | 约束回答生成并绑定证据引用。 |
| `intent_classify.txt` | 用户意图和任务类型识别。 |
| `safety_review.txt` | 医疗安全、风险和拒答复核。 |
| `judge.txt` | 评测答案质量和证据表现。 |
| `medical_dialogue_repair.txt` | 医学对话结构修复。 |
| `medical_dialogue_normalize.txt` | 对话字段归一化和结构化。 |
| `repaired_translation.txt` | 受约束的医学文本翻译。 |

## `skills/files/`：确定性安全规则

| 文件 | 用途 |
|---|---|
| `refusal_rules.md` | 拒答范围、风险边界和不应生成的内容。 |
| `emergency_protocol.md` | 急救场景拦截和安全响应流程。 |
| `disclaimer.md` | 医疗研究原型免责声明。 |
| `citation_format.md` | 证据引用格式与来源绑定要求。 |

## `tests/`：自动化验证

| 文件/目录 | 用途 |
|---|---|
| `tests/test_core.py` | 核心对话、模型和策略测试。 |
| `tests/test_api.py` | FastAPI 接口测试。 |
| `tests/test_agentic_tools.py` | Agent 与工具调用测试。 |
| `tests/test_research_workflow.py` | 研究闭环流程测试。 |
| `tests/test_browser_identity.py` | 浏览器身份和 Cookie 隔离测试。 |
| `tests/test_dialogue_scope.py` | 对话范围和安全边界测试。 |
| `tests/test_format_medical_dialogue.py` | 医学对话格式化测试。 |
| `tests/test_enrich_medical_dialogue.py` | 结构化增强测试。 |
| `tests/test_filter_huatuo_cardio.py` | 心血管候选筛选规则测试。 |
| `tests/test_knowledge_pipeline.py` | 知识库转换与证据管线测试。 |
| `tests/test_incremental_index.py` | 增量索引和版本切换测试。 |
| `tests/test_disk_bilingual.py` | 磁盘索引和双语检索测试。 |
| `tests/test_repaired_corpus.py` | 修复语料约束测试。 |
| `tests/test_regressions.py` | 历史回归测试。 |
| `tests/test_startup_synthetic.py` | 合成启动数据测试。 |
| `tests/fixtures/mcp_search_server.py` | 测试用 MCP 搜索服务夹具。 |
| `tests/__init__.py` | 测试包初始化文件。 |

## `docs/`：工程文档

| 文件 | 用途 |
|---|---|
| `RESEARCH_CONSOLE.md` | 本地研究闭环、索引构建和服务启动说明。 |
| `AGENTIC_RAG.md` | 附件、Agent 和 MCP 使用说明。 |
| `AGENTIC_ACCEPTANCE.md` | Agentic RAG 验收记录。 |
| `CHAT_BROWSER_UI.md` | 浏览器界面、Cookie 和历史记录说明。 |
| `NETWORK_RECOVERY.md` | 联网问答恢复和诊断记录。 |
| `LOW_MEMORY_RETRIEVAL.md` | 低内存索引和分阶段构建说明。 |
| `LOW_MEMORY_ACCEPTANCE.md` | 低内存流程验收记录。 |
| `DRUG_INDEX_REFRESH.md` | 药物索引更新说明。 |
| `DIALOGUE_SCOPE_FIX.md` | 对话范围修复记录。 |
| `IMPLEMENTATION_ACCEPTANCE.md` | 工程功能、测试和未完成项汇总。 |
| `GITHUB_UPLOAD_CHECKLIST.md` | GitHub 公开前的数据、隐私和许可证检查清单。 |
| `PROJECT_MAP.md` | 本文件，说明公开版本中的目录和文件职责。 |

## `scripts/`：首个公开版本的范围

首个公开版本只纳入启动、验证和合成数据相关脚本，确保读者可以运行和验证项目：

| 文件 | 用途 |
|---|---|
| `scripts/start_app.ps1` | 启动本地 FastAPI 服务。 |
| `scripts/stop_app.ps1` | 停止本地 FastAPI 服务。 |
| `scripts/start_chroma.ps1` | 启动本地 Chroma 服务。 |
| `scripts/run_python.ps1` | 选择项目 Python 环境并转发命令。 |
| `scripts/check_runtime.ps1` | 检查服务和运行时状态。 |
| `scripts/build_startup_datasets.py` | 生成不含真实患者数据的合成启动数据。 |
| `scripts/validate_startup_datasets.py` | 验证合成启动数据格式和数量。 |
| `scripts/run_evaluation.py` | 执行检索、回答和安全评测。 |
| `scripts/build_index.py` | 构建本地 BM25/向量检索索引。 |
| `scripts/diagnose_connectivity.py` | 诊断模型、网络和外部工具连接。 |
| `scripts/live_smoke.py` | 执行服务级冒烟测试。 |
| `scripts/browser_smoke.py` | 执行浏览器界面冒烟测试。 |
| `scripts/verify_*.py` | 对 Agent、网络、检索、低内存和界面流程做专项验证。 |
| `scripts/setup_ocr.py` | 准备本地 OCR 依赖。 |
| `scripts/lock_agentic_dependencies.py` | 生成 Agentic 依赖锁定记录。 |

以下脚本只服务于本地数据下载、清洗、翻译和医学语料构建，首个公开版本暂不提交：`download_*.py`、`format_*.py`、`translate_*.py`、`enrich_medical_dialogue.py`、`filter_huatuo_cardio.py`、`convert_heartbioportal.py`、`ingest_knowledge.py`、`rebuild_*.py`、`build_project_data.py`、`build_knowledge_evidence.py`、`build_repaired_evidence.py`、`build_repaired_evaluation.py`、`maintain_candidate_sets.py`、`repaired_common.py`、`replay_source_answers.py`、`report_*.py`、`benchmark_encoder.py` 和 `build_evaluation_sets.py`。这些文件保留在本地，不代表它们不能在未来经许可审查后单独发布。

## 明确排除的目录

| 目录 | 排除原因 |
|---|---|
| `data/raw/` | 第三方原始数据、下载包和未完成文件，涉及体积、版权和再分发许可。 |
| `data/processed/` | 数据处理结果、缓存和中间产物，体积大且不保证可公开。 |
| `data/verified/` | 医学语料、评测案例、隐私/安全样本和派生证据，需逐项审查。 |
| `data/runtime/` | 日志、数据库、索引、截图、进程信息和测试运行痕迹。 |
| `.venv-*`、`.runtime-deps/` | 本机 Python 环境和第三方安装包。 |
| `cardio_medical_bot.egg-info/` | Python 构建产生的元数据，不属于源代码。 |

