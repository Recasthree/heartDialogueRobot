# 心血管医疗对话机器人

一个面向本地研究与工程验证的心血管医疗对话机器人原型，采用 Agentic RAG、混合检索、引用校验、隐私脱敏与安全策略，探索如何把医学对话、病例资料和可追溯证据组织成一个可测试的本地系统。

> **重要声明**：本项目是研究型软件原型，不是医疗器械、诊断系统或临床决策支持系统。输出不能替代医生诊疗，也不能用于自行诊断、用药或急救决策。项目内的合成数据、机器翻译结果和未完成人工审核的资料均不应视为医学事实。

## 项目亮点

- FastAPI 服务与浏览器聊天界面
- Agentic RAG：显式编排意图识别、检索、证据整理、安全审查和回答生成
- BM25 关键词检索 + 可选 Chroma 向量检索，并支持 RRF 融合
- 医学回答的来源绑定、引用校验和无来源拒答
- 本地 OCR、附件临时隔离、过期清理和用户确认后的病例保存
- 可选 Redis 会话记忆与 Chroma 长期记忆；依赖不可用时支持本地降级
- 确定性急救拦截与受控拒答策略，不生成阿司匹林剂量等用药建议
- 面向数据处理、隐私、安全、检索和 Agent 工作流的测试与验收脚本

## 系统结构

```text
浏览器聊天界面
        │
        ▼
FastAPI API ── 对话策略 / 隐私 / 安全审查
        │
        ├── LangGraph Agent 编排
        ├── BM25 + Chroma 混合检索
        ├── 本地附件解析与 OCR
        ├── 受控 MCP / PubMed / 网络证据工具
        └── Mock 或兼容 OpenAI API 的模型后端
```

## 本地运行

项目默认使用 Mock 后端，不需要 API Key 即可启动研究版。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[agentic]"
Copy-Item .env.local.example .env.local
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

然后打开 <http://127.0.0.1:8000/>。

如需连接兼容 OpenAI API 的模型，请只在本机 `.env.local` 中填写密钥，并确认该文件没有被 Git 跟踪。不要把密钥写入代码、截图、Issue、日志或提交记录。

也可以使用 Docker Compose：

```powershell
docker compose up --build
```

更完整的运行、联网诊断、附件处理和低内存构建说明见：

- [本地研究闭环](docs/RESEARCH_CONSOLE.md)
- [Agentic RAG 指南](docs/AGENTIC_RAG.md)
- [网络问答恢复记录](docs/NETWORK_RECOVERY.md)
- [磁盘索引与双语检索](docs/LOW_MEMORY_RETRIEVAL.md)
- [工程交付记录](docs/IMPLEMENTATION_ACCEPTANCE.md)
- [项目文件地图](docs/PROJECT_MAP.md)
- [GitHub 上传清单](docs/GITHUB_UPLOAD_CHECKLIST.md)

## 测试

```powershell
python -m pytest -q
```

生成合成启动数据并验证：

```powershell
python scripts/build_startup_datasets.py
python scripts/validate_startup_datasets.py
```

## 目录说明

```text
app/        应用服务、Agent、检索、隐私和工具实现
web/        浏览器聊天界面
config/     可公开的配置示例
prompts/    对话、审查和评测提示词
skills/     安全、拒答、免责声明和引用规则
scripts/    数据处理、索引构建和验证脚本
tests/      单元测试、回归测试和流程测试
docs/       工程设计、运行说明与验收记录
data/       本地数据目录；原始、处理后和运行时数据不随代码仓库发布
```

## 数据与隐私边界

为避免误传密钥、个人信息、受许可限制的数据和大体积运行产物，公开仓库默认不包含：

- `.env.local`、本地 MCP 配置和任何 API Key
- `data/raw/`、`data/processed/`、`data/verified/` 和 `data/runtime/`
- SQLite、Chroma、Redis、日志、缓存、下载中间文件和虚拟环境
- 含患者资料、附件内容、浏览器轨迹或测试运行痕迹的文件

数据集说明和再现方式应通过脱敏后的 manifest、下载脚本和许可说明提供，而不是直接提交原始数据。重新发布第三方数据前，需要分别确认原数据集、指南、论文和药品资料的许可证与再分发条款。

## 当前限制

- 仍是本地研究原型，尚未完成临床验证、安全认证或真实世界部署审查。
- 部分知识库内容来自机器翻译或尚未完成人工审核的来源。
- 网络工具、模型服务、Redis 和 Chroma 均可能不可用；系统会根据配置降级。
- 公开仓库不携带运行所需的完整医学数据，需要按照许可和项目说明自行准备。

## 开源许可

当前仓库尚未选择开源许可证。除非后续添加明确的许可证文件，否则公开代码不等于授予他人复制、修改或商业使用权。发布前请确定许可证，并同步确认第三方数据和文档的授权边界。

## 维护与联系

欢迎围绕工程实现、可复现性、隐私保护和安全评测提出 Issue。涉及真实患者信息、API Key 或其他敏感材料时，请不要直接贴到 Issue、Pull Request 或公开日志中。

