# 本地研究闭环运行说明

现有 `data/verified` 直接用于工程运行。本轮不改变数据内容、审核字段、翻译缓存或 `.partial` 文件。

## 环境与配置

使用 Python 3.10–3.13。不要使用旧 `.venv-project` 的 Python 3.8。

```powershell
python -m venv .venv-research
.\.venv-research\Scripts\python.exe -m pip install -r requirements-runtime.txt
.\.venv-research\Scripts\python.exe -m pip install -e . --no-deps
```

如果系统中有多个 Python 解释器，请显式使用 Python 3.10–3.13；本轮额外依赖可隔离在项目 `.runtime-deps`。启动脚本会优先识别项目虚拟环境；手动调用时设置：

```powershell
$env:PYTHONPATH = "$PWD\.runtime-deps"
$env:HF_HOME = "$PWD\data\runtime\hf"
```

`.env.local` 是唯一需要填写密钥的本地配置；示例 `.env.local.example` 不包含密钥。
读取优先级：进程环境变量 > `.env.local` > 默认值。默认模型后端为 Mock；已有本地配置选择 DeepSeek 时会使用真实接口。

## 构建与启动

```powershell
.\scripts\start_chroma.ps1
python -m scripts.build_index --download-model
.\scripts\start_app.ps1
.\scripts\check_runtime.ps1
```

浏览器打开 http://127.0.0.1:8000 。停止 API 使用 `scripts/stop_app.ps1`；不会停止 Chroma 或翻译进程。

首次构建下载 E5-small 模型并批量编码。后续构建复用缓存，只编码新增或修改内容；启动 API 不执行语料编码。
本机请用下方包装脚本调用正确解释器，避免系统默认 Python 3.8：

```powershell
.\scripts\run_python.ps1 -m scripts.build_index
.\scripts\run_python.ps1 -m pytest -q
.\scripts\run_python.ps1 -m scripts.run_evaluation --suite ragcare --mode retrieval_only --max-cases 32
```

完整语料首次 CPU 编码是长任务，不能以小样本冒烟时间估计。完成前 `active.json` 不会切换。
本次构建与收尾的实时状态记录在 `data/runtime/verification/delivery-status.json`；收尾程序只做本地检索评测并启动网页，不调用付费模型。
`--limit 8` 仅构建隔离的冒烟集合，不切换活动索引。

新索引元数据在 `data/runtime/indexes/active.json`，文档、词频和向量缓存存入版本化 SQLite。旧32维集合保留。
模型、维度、切分版本和构建版本决定集合命名；源文件 SHA、父证据 ID、分块位置及内容哈希可在数据库中追溯。
后续增量构建使用独立集合，缓存向量直接复制写入，验证后切换；旧集合保留以供回退。
完整 JSONL 为输入；缓存与部分文件只登记、不索引。医学证据、benchmark context、测试数据分区；默认回答只查 evidence 分区。
构建按文件先保存不可变字节快照到 `data/runtime/source_snapshots`，再进行切分。翻译进程继续更新正式文件不会混入当前快照；manifest 会标记 `changed_after_snapshot`，下次构建再接入新内容。
证据和 benchmark 分区有 BM25 与 Chroma 两个通道；调试分区保留在持久化 BM25 与清单中，不为问题/答案/隐私样本额外计算医学向量。相同正文可复用编码，但不同来源保留不同 ID。
模型使用 [E5 官方要求的 query/passage 前缀与归一化](https://huggingface.co/intfloat/multilingual-e5-small)。

## 检索与回答

BM25 查询复用倒排表；Chroma 在查询前执行同范围过滤；两个成功通道做 RRF。
每路召回20，合并最多20，一次批量重排，返回最多5。失败有降级状态，不使用哈希邻居冒充语义召回。
`.78` 是小样本跨语言探针校准后的可配置 E5 相似度门槛，不是医学可信度概率；应依据完整本地评测继续调整。重排低相关候选被过滤，生成后仍接受引用与安全校验。

LangGraph 负责上下文、意图、改写、最多4个子查询、工具分支、生成、引用和安全复核。急救规则在原始输入上本地运行，先于脱敏和所有依赖。
病例及历史在外发前脱敏。画像只保存用户陈述和病例事实，每6轮做一次确定性摘要与引用历史记录，不添加模型医学结论。
画像和摘要使用独立 Chroma 集合、作用域精确过滤；不是医学证据。没有用户或病例标识时仅限当前会话。
有意保留本地规则与模型审查两道边界；模型审查失败不能解释为通过。

PubMed 仅在 `PUBMED_ENABLED=true` 时访问 NCBI；`CPUBMED_URL` 为可选本地网关，接收 `{"query":"..."}`，返回 `{"evidence":[EvidenceItem]}`。未配置时显示 not_configured。没有 DDI 数据时不推断安全合用。

## 评测

```powershell
python -m pytest -q
python -m scripts.run_evaluation --suite ragcare --mode retrieval_only --max-cases 32
python -m scripts.run_evaluation --suite all --mode full --max-cases 5 --judge
```

最后一条在 DeepSeek 后端会消耗额度。结果保存到 `data/runtime/evaluations`，不覆盖金标准。
Recall、Hit、MRR 分开计算；分块映射回父证据 ID。相关性0不计入，无相关标注不参与均值。
Judge停用、Mock和错误不会生成评分；检索模式不调用模型生成、在线重排或Judge。
安全套件逐条报告缺失必需内容、禁止内容、动作、标记和引用契约；不把存在响应当作通过。
`contract_pass_rate` 仅表示程序动作/引用/禁止文本检查，`pass_rate` 还要求全部标记和必需内容匹配。
必需内容目前采用字面/显式别名匹配，不是语义 Judge；应阅读逐条失败原因，不能把任一分数解释为临床安全性。
RAGCare 原文件没有意图标签，按意图统计明确列为 unlabeled；独立 Agent 意图集提供分类准确率和混淆矩阵，不伪造金标签。

网页提供后台评测与run_id恢复。追踪接口只输出脱敏后的路由、工具状态、候选ID、耗时和模型调用数。

## 页面设计约束

使用 frontend-design 技能保留现有身份：深青 `#102d38`、文字 `#102331`、面板 `#f7f4ed`、纸面 `#fffdf8`、铜红 `#c85a43`、薄荷 `#b9d6c8`。
沿用中文系统字体和原有标题字体；左侧导航，中央对话/病例/评测，右侧证据账本。
布局以左对齐阅读和可追溯信息为主，不新增宣传大数字、无意义动效或装饰组件。

```text
导航与状态 | 病例选择 / 会话
           | 对话与执行追踪 | 引用、来源、片段
           | 病例档案 或 评测表格
```

## 范围边界

本地研究原型，无账户授权、生产部署、医院系统或OCR。不要暴露到公网，不要录入真实身份数据。
服务端会拒绝修改病例身份字段；SQLite自动备份并一次性导入旧cases.json。用户主动删除的病例不可从页面撤销。
客户端取消等待不承诺取消已发出的上游模型调用；同会话请求在服务端串行执行。
