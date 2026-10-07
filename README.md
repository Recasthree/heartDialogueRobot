# 心血管医疗对话机器人 · 本地研究版

> GitHub 线上展示版请先阅读 [README_GITHUB.md](README_GITHUB.md)；完整的目录和文件职责见 [项目文件地图](docs/PROJECT_MAP.md)。

这是一个本机开发用的 Agent/RAG 研究型 toy。默认 Mock、无 API key 也能运行；设置 `LLM_BACKEND=deepseek` 后可通过 AIHubMix 调用 DeepSeek V4.1 Flash。所有当前语料仍按研究数据处理，不能用于医疗判断。

## 当前能力

若出现“网络证据不可用”或“模型已连接但一直拒答”，先看 [联网诊断与重启说明](docs/AGENTIC_RAG.md#联网失败不是模型已连接能排除的)。最新修复和真实验收范围见 [网络问答恢复记录](docs/NETWORK_RECOVERY.md)。

默认页面现在是免登录的聊天界面：历史侧栏可收起，右侧显示对话时间轴。3 秒内连续点击左上角“心”Logo 5 次展开研究台；用户与研究台 Cookie 独立管理。操作、数据保留边界和验证命令见 [聊天界面与 Cookie 说明](docs/CHAT_BROWSER_UI.md)。

低内存分阶段构建、断点续建及中英文取证见 [磁盘索引与双语检索](docs/LOW_MEMORY_RETRIEVAL.md)。先发布磁盘 BM25，再验证并发布 E5；向量未完成时仍可使用关键词检索。界面分别显示 Chroma 服务、关键词索引、向量索引和构建进度。

新增：**带附件解析和受控 MCP 工具调用的 Agentic RAG**。上传报告、确认 OCR 结果后，可自动调用本地检索和网络证据工具；患者资料与医学证据分开显示。使用方法见 [附件与 MCP 指南](docs/AGENTIC_RAG.md)，本轮验证见 [Agentic 验收记录](docs/AGENTIC_ACCEPTANCE.md)。运行 `.\scripts\start_app.ps1` 后打开 **http://127.0.0.1:8000/**，不要直接双击 HTML。

运行请优先阅读 [本地研究闭环说明](docs/RESEARCH_CONSOLE.md)，当前验收、实测分数及未完成项见 [工程交付记录](docs/IMPLEMENTATION_ACCEPTANCE.md)。现有数据直接使用；以下历史数据构建器不是启动前置步骤，不需要重新生成语料。

- FastAPI API 与研究操作台：病例、附件核对、对话、证据、评测和工具轨迹。
- 本地 CPU OCR、临时附件隔离与24小时清理，确认后的事实可由用户明确保存到病例。
- 官方 MCP 2.x HTTP 客户端/服务端，支持可选 stdio 扩展；联网默认开启，可在界面或配置中关闭。
- LangGraph 显式编排、BM25 + Chroma 查询、RRF 和引用校验。
- Redis 会话记忆与 Chroma 证据/长期记忆的可选连接；不可用时降级到本地内存。
- Mock 或 DeepSeek 意图识别、回答生成、安全审查和 Judge。
- 确定性急救拦截；不会给出阿司匹林剂量或其他用药建议。
- 病例默认 Schema、演示知识条目、引用绑定与无来源拒答。
- Huatuo-Lite 心血管数据筛选脚本：按科室、相关疾病、去重后的心血管概念组和质量分数生成高质量集与人工复核集。

## 数据集总表与历史错误文件

所有数据集的功能、状态和建议用途见 [data/DATASET_CATALOG.md](data/DATASET_CATALOG.md)，机器可读版本见 [data/dataset_catalog.json](data/dataset_catalog.json)。

旧版处理结果如果已经被清理，不会影响当前启动数据。当前合成数据和来源状态统一见 `data/DATASET_CATALOG.md`。

## 快速启动合成数据

不依赖 Claude、DeepSeek 或其他模型 API，直接生成本地测试数据：

```powershell
python scripts/build_startup_datasets.py
python scripts/validate_startup_datasets.py
```

生成内容包括 50 条 CaseRecord、急救和降级测试、32 条 RAGCare 心血管评测、30 条演示证据、40 条演示检索查询、40 条 Judge 样本、60 条安全拒答样本和 40 条隐私脱敏样本。所有合成数据都标记为 `is_demo=true`、`medical_truth_verified=false`，不能当作权威医学知识。

PRIME-CVD 是可选的全合成外部来源。下载后再运行上面的构建命令：

```powershell
python scripts/download_prime_cvd.py --all
python scripts/build_startup_datasets.py
```

如果下载失败，使用 `python scripts/build_startup_datasets.py --without-prime-cvd` 仍可完成本地启动数据生成。

## Huatuo-Lite 心血管数据筛选

安装数据集处理依赖：

```powershell
python -m pip install -e ".[data]"
```

对已通过 `datasets.save_to_disk()` 保存到 `data/raw/huatuo_lite` 的数据运行：

```powershell
python scripts/filter_huatuo_cardio.py `
  --input data/raw/huatuo_lite `
  --output-dir data/processed/huatuo_cardio
```

脚本默认只保留 `内科`、`急诊科`，要求 `related_diseases` 非空。精确为 `高血压` 或 `高血压病` 的记录会被排除；其他含“高血压”的主题要求 score=4 至少命中三个概念组，非高血压主题的 score=4 命中一个概念组即可进入高质量集。score=5 继续使用至少两个概念组的原规则。边界记录写入 `cardio_review.json`，供人工修改 `review_status`、`keep` 和 `review_notes`。

输出记录还会包含 `risk_flags`，用于标记宽泛心血管标签、潜在非心血管回答、具体治疗建议和标签内容不一致等情况。这些标记用于审计，不代表自动判定医学错误。

人工复核后应用结果：

```powershell
python scripts/filter_huatuo_cardio.py `
  --apply-review data/processed/huatuo_cardio/cardio_review.json `
  --output-dir data/processed/huatuo_cardio/approved
```

只有 `review_status=approved`、`keep=true` 且 `review_notes` 非空的记录会进入 `approved.jsonl`。筛选脚本不会翻译、改写或生成医学结论；输出仍需人工复核，不能直接视为医疗知识库。

## Medical-Dialogue-Dataset-Chinese 格式化

规则格式化不会修改原始 TXT，会同时生成病例结构、SFT 对话和异常复核文件：

```powershell
python scripts/format_medical_dialogue.py `
  --input data/raw/Medical-Dialogue-Dataset-Chinese/Medical-Dialogue-Dataset-Chinese.txt `
  --output-dir data/processed/medical_dialogue_chinese `
  --mode rule
```

输出包括：

- `canonical_records.jsonl`：保留原始 id、病例描述、多个 segment、多个 Dialogue block 和 `raw_text`；
- `sft_messages.jsonl`：将明确的 `医生：`、`病人：` 对话转换为 `assistant`、`user` 消息；
- `format_review.jsonl`：无对话、角色异常、多病例拼接和未知区块等待复核数据；
- `format_report.json`：输入哈希、记录数量、区块数量和异常统计。

可选的 Mock 归一化模式不会调用真实 API，也不会补写医学事实：

```powershell
python scripts/format_medical_dialogue.py `
  --input data/raw/Medical-Dialogue-Dataset-Chinese/Medical-Dialogue-Dataset-Chinese.txt `
  --output-dir data/processed/medical_dialogue_chinese `
  --mode hybrid `
  --llm-provider mock
```

后续接入 Claude 或 DeepSeek 时，只替换归一化 provider；规则解析结果、原始文本和输出 schema 保持不变。所有输出默认为 `unverified`，不能直接视为医学知识或直接写入正式知识库。

### 离线结构化增强

如果不配置任何模型 API，可以对已经生成的 `canonical_records.jsonl` 做本地、证据锁定的结构化抽取：

```powershell
python scripts/enrich_medical_dialogue.py `
  --input data/processed/medical_dialogue_chinese/canonical_records.jsonl `
  --output-dir data/processed/medical_dialogue_chinese
```

该脚本不调用 Claude、DeepSeek 或其他外部服务，只抽取原文明确出现的疾病、症状、检查、药物和治疗，并检查每个 `evidence` 是否存在于当前 segment 原文。它不会把症状推断为诊断，也不会把医生提到的药物变成推荐。输出为 `canonical_records_enriched.jsonl`、`normalization_review.jsonl` 和 `normalization_report.json`，所有增强字段均标记为 `llm_enriched_unverified`，在人工审核前不能作为医学真值。

## 指南、药物来源和评测集

### 当前 verified 数据与报告

当前运行时索引来自 `data/verified/`；安全、隐私、Agent 和评测文件会登记但默认不会作为医学回答证据。统一统计由下面的报告脚本重建：

```powershell
python scripts/rebuild_data_reports.py --data-root data
```

Huatuo 的运行时真实候选集以当前 `data/verified/huatuo_cardio/cardio_high_quality.jsonl` 为准。用下面的脚本生成候选集 manifest，记录源快照哈希、规则版本、活动 ID、重复 ID 和审计差异；它不会改写医学审核字段，也不会把删除的记录自动补回：

```powershell
python scripts/maintain_candidate_sets.py --data-root data
```

当前报告必须区分“原始源文件行数、正式完整输出、`.partial` 进度和缓存命中数”。`.partial` 与缓存只做进度审计，不进入正式医学证据索引。

先下载 PMC 开放全文并重建正文分块：

```powershell
python scripts/download_repaired_sources.py --max-docs 40
python scripts/rebuild_repaired_guidelines.py
```

再重建 26 个核心药物、DailyMed 章节和 DDInter 交互对。脚本会排除 RxNorm 的 GPCK/BPCK 包装概念；DDInter 没有提供的机制和管理字段保持 `null`：

```powershell
python scripts/rebuild_repaired_drugs.py --download-labels
```

生成严格来源绑定的评测集、安全集和待审核证据：

```powershell
python scripts/build_repaired_evaluation.py
python scripts/build_repaired_evidence.py
python scripts/translation_pipeline.py --build-queue --batch-size 10
python scripts/validate_repaired_corpus.py --write-report
```

当前实际数据量、Huatuo 清洗差异、药物翻译 `.partial` 进度和知识库状态以 `data/DATASET_CATALOG.md` 和 `data/dataset_catalog.json` 为准；报告脚本不会删除 `.partial` 或缓存。

中文翻译通过 `translation_queue.jsonl` 分批处理。翻译结果必须由当前模型或人工产生后，用 `--apply translated_batch.jsonl` 写回 `chunks_bilingual.jsonl` 和 `drug_labels_bilingual.jsonl`；未翻译内容保持 `content_zh=null`，不会被误标成已翻译：

```powershell
python scripts/translation_pipeline.py --apply translated_batch.jsonl
```

以下是历史 repaired 数据流程的说明，不是当前运行条件。当前 `scripts/ingest_knowledge.py` 直接使用指定资料，保留既有审核字段；不会为启动本地研究版要求修改审核状态。

审核完成后只汇总通过门禁的记录：

```powershell
python scripts/build_repaired_evidence.py --approved-only
python scripts/ingest_knowledge.py `
  --input data/processed/repaired/evidence_items_approved.jsonl `
  --manifest data/processed/repaired/approved_import_manifest.json
```

`validate_repaired_corpus.py` 是导入前门禁。校验失败时不要写入 Chroma；对话、Huatuo 和未审核评测样本也不应直接作为权威医学证据。

历史下载器和评测集生成器仅用于审计；当前默认运行时以 `data/verified/` 的实际文件为准。不要根据旧报告补写已被清洗删除的记录，也不要把机器翻译或合成样本改标成医学真值。

## 启动方式

### 有 Python 环境

```powershell
python -m venv .venv-research
.\.venv-research\Scripts\Activate.ps1
python -m pip install -r requirements-runtime.txt
python -m pip install -e . --no-deps
.\scripts\start_chroma.ps1
python -m scripts.build_index --download-model
python -m uvicorn app.main:app --reload
```

如果电脑上有多个 Python，请确保安装依赖和启动服务使用同一个解释器；可以用 `python -c "import sys; print(sys.executable)"` 检查。

真实 DeepSeek 模式从环境变量或已忽略的 `.env.local` 读取 key，环境变量优先。已有配置无需重复填写；示例文件不含密钥：

```powershell
$env:LLM_BACKEND = "deepseek"
$env:AIHUBMIX_BASE_URL = "https://aihubmix.com/v1"
$env:DEEPSEEK_API_KEY = "<rotated-key>"
$env:DEEPSEEK_MODEL = "deepseek-v4.1-flash"
```

打开 http://127.0.0.1:8000/ 。

当前机器如果没有 Python 依赖，先执行 `pip install -e .`；如果没有 Docker，也可以运行 API 的内存降级模式。生产环境不要直接使用本项目配置。

### Redis + Chroma

本项目支持两种 Chroma 模式。没有 Docker 时，可通过 `CHROMA_ROOT` 指向本机的 Chroma 安装目录，再运行本地 Chroma HTTP 服务：

```powershell
.\scripts\start_chroma.ps1
$env:CHROMA_MODE = "http"
$env:CHROMA_HOST = "127.0.0.1"
$env:CHROMA_PORT = "8001"
```

先运行 `python -m scripts.build_index`，再启动 API。证据使用 E5-small 384维语义向量与持久化 BM25；Chroma 不可用时明确降级为 BM25，不使用内存哈希向量伪造证据。API启动只加载索引，不重新向量化。

如果安装了 Docker Desktop，也可以执行：

```powershell
docker compose up --build
```

服务只绑定本机。Compose 默认仍为 `mock`；设置环境变量后才访问真实模型。

## 主要接口

- `GET /api/v1/health`
- `GET /api/v1/ready`
- `POST /api/v1/cases`
- `GET /api/v1/cases`
- `GET /api/v1/cases/{case_id}`
- `PATCH /api/v1/cases/{case_id}`
- `DELETE /api/v1/cases/{case_id}`
- `POST /api/v1/chat`
- `POST /api/v1/knowledge/ingest`
- `POST /api/v1/evaluations/run`
- `GET /api/v1/evaluations/{run_id}`
- `GET /api/v1/traces/{trace_id}`

`/api/v1/evaluations/run` 支持 `suite=ragcare|safety|judge|all`、`mode=retrieval_only|full` 和 `max_cases`，结果写入 `data/runtime/evaluations/`。

## 安全边界

本项目不是诊断、处方或急救服务。急救文本只建议拨打 120 或前往急诊；回答不会替代专业医生。不要在演示环境中录入真实患者身份信息。

## 测试

```powershell
pytest
```
