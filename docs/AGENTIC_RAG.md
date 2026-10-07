# 附件与受控 MCP 工具使用说明

这是本地研究原型，不用于生产医疗服务。现有数据和审核字段保持不变。

## 打开完整界面

在项目目录执行：

```powershell
.\scripts\start_app.ps1
```

浏览器访问 **http://127.0.0.1:8000/**，不要直接双击 HTML。启动脚本优先使用 `.venv-agentic`，不把旧 `.runtime-deps` 放到新环境前面。使用 `-Port 8002` 时脚本同步设置 MCP 的本机地址。

检查状态：

```powershell
.\scripts\check_runtime.ps1
```

### 联网失败不是“模型已连接”能排除的

API 进程存活、密钥已配置、MCP 在线，不代表模型生成或公共取证当下成功。浏览器能上网，也不代表后台进程有相同的出网权限。诊断和重启请在有正常出网权限的 PowerShell 中运行：

```powershell
.\.venv-agentic\Scripts\python.exe -m scripts.diagnose_connectivity
.\scripts\start_app.ps1 -Restart -CheckNetwork
```

诊断使用与应用一致的模型客户端和公共取证传输，仅检查 HTTP 状态、时间和异常类型；不输出 key、不生成回答、不消耗模型请求。`200` 只证明该检查端点可访问，不能代替完整问答验收。脚本停止已登记的本项目 Uvicorn 父/子进程后才重启，不停止 Chroma、翻译或索引任务；若端口仍被占用则报错，不偷偷启动第二份服务。

基础患者教育问题先走受控 `fetch_evidence`：房颤检查、心律失常症状/检查优先获取 NIH/NHLBI 页面正文，相关链接在 `app/tools/patient_sources.py` 中维护。这里只登记取证地址，不硬编码回答；仍经过 SSRF、域名白名单、相关性、引用及安全复核。抓取失败或目标未覆盖时继续走 PubMed（按相关性检索）/已配置网络工具。范围之外的问题仍由受控规划器取证，并不保证所有疾病都有患者教育链接。`PATIENT_EDUCATION_ENABLED=false` 可关闭这一优先路由；`network_mode=off` 关闭全部网络取证。

完整正文已获取且相关时可作一般性介绍的来源，`pending` 不等于“没有来源”；它仍不是经过本项目审核的指南。网络标记、来源网址、文献/页面时间、原文和译文继续在对话内引用详情显示。仅一条明确声明且有效的引用漏写正文编号时可以做格式修复；没有引用 ID、伪造 ID、多来源映射不明、错误编号或安全复核失败不能借此通过。

`/api/v1/health` 表示进程可用；`/api/v1/ready` 的 503 可能表示完整本地索引还在构建，不表示网页或 OCR 不可用。索引完成后，重新启动 API 加载已提交的活动索引；启动不会重新编码语料。不要停止仍在运行的索引或翻译进程。

## 环境和配置

本机验证环境为 Windows / Python 3.13。新机器使用新的虚拟环境，安装 `requirements-agentic-lock.txt`；不要升级覆盖旧翻译/索引环境。

```powershell
py -3.13 -m venv .venv-agentic
.\.venv-agentic\Scripts\python.exe -m pip install -r requirements-agentic-lock.txt
.\.venv-agentic\Scripts\python.exe -m scripts.setup_ocr
```

OCR 使用 RapidOCR 官方 wheel 内附的中文/英文 ONNX 权重。安装脚本复制权重并登记 SHA256；运行时核对文件，不自动下载，不上传图片。解析在独立子进程内运行，支持超时终止。

沿用 `.env.local` 中的模型配置，不需要把 key 放进代码或浏览器。新增配置参考 `.env.local.example`：

| 配置 | 默认值与含义 |
|---|---|
| `AGENTIC_ENABLED` | `true`，启用附件和工具网关 |
| `MCP_ENABLED` | `true`，内置真实 MCP 服务 |
| `MCP_BASE_URL` | `http://127.0.0.1:8000/mcp/` |
| `NETWORK_ENABLED` | `true`；网页还可按请求关闭联网 |
| `NETWORK_DNS_MODE` | `public_doh`；域名通过 Cloudflare 加密 DNS 解析，随后校验并固定公网 IP；普通环境可设 `system` |
| `TAVILY_API_KEY` | 默认为空，通用搜索不可用；与模型 key 不同 |
| `TOOL_MAX_CALLS` / `TOOL_CONCURRENCY` | 每轮最多 8 次工具调用，并发最多 2 |
| `ATTACHMENT_TTL_SECONDS` | 86400，临时保留 24 小时 |
| `ATTACHMENT_TIMEOUT_SECONDS` | 120，解析任务时限 |
| `DEEPSEEK_THINKING_MODE` | `disabled`，显式关闭思考模式以稳定短 JSON；不支持该参数的兼容服务可改 `omit` |
| `DEEPSEEK_MAX_TOKENS` | 4096，结构化输出上限；空输出和截断仍按失败处理 |

Mock 仅表示语言模型离线，不等于关闭网络工具。完全离线研究请同时设置 `NETWORK_ENABLED=false`。评测内部显式使用 `network_mode=off`。

本机医学域名曾被系统 DNS 解析到 `198.18.*` 虚拟地址，工具会拒绝这种非公网地址。采用公共 DoH 的目的不是放宽 SSRF 检查：仍拒绝私有 IP、内网地址、异常重定向和不受信任目标，并保留 HTTPS 校验。DoH 只接收目标域名，不接收患者报告或医学查询。参考 [Cloudflare DoH JSON 文档](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)。

## 从报告到回答

1. 建立研究会话，可先创建并关联病例。
2. 拖入 PNG/JPEG/PDF/TXT；单文件最多 20 MB，一轮最多 3 个文件、合计 20 页。长 PDF 可指定页码。
3. 文字 PDF 直接提取，扫描页或图片使用本地 OCR。原图和转录可对照。
4. 核对药名、剂量、检验数值和单位。首版采用保守的整页确认，文字 PDF/TXT 也需要确认，避免漏识别没有数字的药名；低置信内容额外提示。修订后必须重新确认新版本。
5. 输入问题，按需保留或关闭“联网补证”。急救文本不等待解析、确认或模型。
6. 查看患者资料引用、医学证据账本和工具时间线。患者信息不能充当指南证据。
7. 若要长期保存，选择已确认的原文事实，点击“确认保存所选事实”。事实再次脱敏后追加到病例备注，不自动推断诊断或剂量。

附件确认只是转录核对，不改变 `review_status` 或 `medical_truth_verified`。参考资料只在当前会话可检索，不自动进入公共知识索引。

附件原文、解析结果在运行时目录下独立存储。到期任务清理登记文件和数据库文本；服务重启后也执行过期清理。SQLite 开启 secure-delete 并截断 WAL，但这不等于对 SSD 做法证级安全擦除。浏览器不持久化附件转录和附件派生回答，过期时撤销预览；服务关闭期间的磁盘清理要等下次启动。用户明确保存的病例事实不随附件过期删除。

## Agent 与 MCP 的边界

图中的节点为：急救 → 附件 → 脱敏上下文 → 分类/改写 → 本地取证 → 工具规划/执行（最多两轮）→ 一次重排 → 生成/引用/安全校验。

模型输出受约束的工具计划 JSON。服务端检查工具名、节点权限、作用域、网络开关、参数、重复调用和次数。当前不声称使用模型厂商的原生 function calling；真实工具执行通过官方 MCP 2.2.0 协议。

内置 `/mcp/` 与 FastAPI 共用已有检索实例，避免重复装载语料。客户端经实际 Streamable HTTP 调用；随机内部凭据不发给网页；每次调用还使用短期宿主能力令牌限制作用域。外部 MCP 支持 HTTP 和 stdio，配置文件参考 `config/mcp_servers.example.json`，复制为本地文件后显式启用。仅允许映射已有的只读网络工具，不把患者附件交给外部服务器。

第三方 MCP 工具输出须符合以下契约，否则返回不可用：

```json
{
  "status": "available",
  "evidence": [],
  "leads": [{"url": "https://example.org/document", "title": "来源标题", "snippet": "搜索线索"}]
}
```

`evidence` 每项使用项目 `EvidenceItem` 结构；网络证据需要有效 URL、正文、来源 ID 和标题。搜索线索没有引用资格。外部服务不能自行赋予审核通过状态，服务端重新生成证据身份并保留未验证标记。

内置工具覆盖附件解析、指定页 OCR、本地检索、药品与 DDI、PubMed、openFDA、可选 Tavily、网页/PDF 正文获取。网络正文、附件和工具描述均不得扩大执行权限；不提供 shell、文件任意读取或浏览器控制工具。

MCP 失败时仅本地检索/药品/DDI 可走明确标记的 `native_fallback`；网络和 OCR 不伪造成功。空 DDI 不能解释为安全合用；未配置 Tavily 不代表所有联网能力都不可用。PubMed 摘要不冒充全文或指南，openFDA 标注美国地区和说明书日期。

## 接口摘要

| 接口 | 用途 |
|---|---|
| `POST /api/v1/attachments` | multipart 上传，包含 session_id，可选 case_id/user_id/purpose/selected_pages，返回 202 |
| `GET /api/v1/attachments/{id}` | 读取状态、文本、页码和确认要求 |
| `GET /api/v1/attachments/{id}/preview` | 带作用域读取原文件，不进入静态目录 |
| `PATCH /api/v1/attachments/{id}` | 传 version；确认、修订、修改用途或重试 |
| `DELETE /api/v1/attachments/{id}` | 取消任务并删除附件及派生解析结果 |
| `POST /api/v1/attachments/{id}/save-facts` | 显式保存选中的脱敏原文事实到目标病例 |
| `POST /api/v1/chat` | 增加 attachment_ids、network_mode；其余请求保持兼容 |

附件读取/更新请求携带上传时一致的 session_id、case_id、user_id。跨作用域返回 404，过期返回 410，未完成或未确认返回 409。没有账户认证，所以这是本机隔离机制，不是生产级身份安全保证。

## 验证与证据

```powershell
.\scripts\run_python.ps1 -m pytest -q
.\scripts\run_python.ps1 -m scripts.verify_agentic --browser
.\scripts\run_python.ps1 -m scripts.verify_agentic --network
# 零付费模型调用的真实网页 + 公共网络取证演示：
.\scripts\run_python.ps1 -m scripts.verify_agentic --browser --network
# 全量严格安全样本，离线 Mock，不改金标准：
.\scripts\run_python.ps1 -m scripts.verify_agentic --safety
# 以下命令会调用已配置的真实模型，最多 10 次请求（含重试）：
.\scripts\run_python.ps1 -m scripts.verify_agentic --live --network
```

`verify_agentic` 使用独立临时索引与真实 MCP HTTP，不修改公共索引。模型冒烟使用现有资料片段；界面验收使用合成研究报告。报告和截图写入 `data/runtime/verification/agentic-<时间>/`。这些小样例不代表全语料 Recall 或临床安全验证。

本轮验收持续记录在 `docs/AGENTIC_ACCEPTANCE.md`。不能用接口成功、单元测试通过或规则兜底替代医学准确性评测，也不通过修改安全集金标准凑全绿。
