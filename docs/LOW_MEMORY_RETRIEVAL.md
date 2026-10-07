# 磁盘索引与双语取证

当前构建命令不会修改 verified、审核字段、旧 SQLite、旧 Chroma 集合或翻译缓存。请通过 `http://127.0.0.1:8000/` 使用研究台，不要直接打开 HTML。

## 启动和续建

在项目根目录运行 PowerShell（使用项目隔离环境）：

```powershell
# 源文件更新：从当前活动版本派生新版本，只重新处理变化文件
.\.venv-agentic\Scripts\python.exe -m scripts.build_index --incremental --stage all --activate
# 已有任务暂停/失败后恢复：使用其 job_id，不重复创建全量任务
.\.venv-agentic\Scripts\python.exe -m scripts.build_index --stage all --resume <job_id> --activate
.\scripts\start_app.ps1
Invoke-RestMethod http://127.0.0.1:8000/api/v1/knowledge/index/status
```

当前药物翻译更新任务为 `20260930-023258-bf1007a5`；旧任务 `20260929-190800-8c541f23` 保留作回退。请先查状态，不要同时续建并发布两个代次。更新结果和恢复边界见 [药物索引更新记录](DRUG_INDEX_REFRESH.md)。

只有首次迁移旧版数据库才需要 `--recover-from data/runtime/indexes/index-20260929-133923-327bf077.sqlite3`。增量构建复制兼容的活动 SQLite，保留分词/向量缓存；未变化文件不重新分块，变化文件按稳定 ID 更新并清除新版本里的过期记录。旧版本不变。新 Chroma 集合的写入确认从零开始，不能把旧集合的成功标志直接当作新集合已写入。

- `snapshot → lexical → lexical_ready`：逐文件流式快照、逐行分块、SQLite FTS5 BM25。完成和核对后才能发布。此阶段不加载 E5 权重。
- `recovery → vectors → verify → ready`：32 个确定性片段比对旧向量身份；通过才导入旧缓存。float32 BLOB 缓存与文档正文分离，小批量编码；Chroma 确认写入后才提交进度，最后核对每个 ID。
- `paused_memory / paused_disk`：在批次边界保存进度并释放本次编码子进程，不杀其他应用。旧活动索引继续服务。释放资源后手动续建，不会无限重试。
- `failed`：查看最近错误，修正服务/配置后续建；无效缓存不会被当成成功。
- `--limit` 仅用于构建冒烟，不发布为全量活动索引。
- 不使用 `--activate` 时仅生成索引与 manifest，不切换当前服务。

活动 manifest 原子替换；新请求获取新索引句柄，正在进行的请求及其 MCP 调用保持同一代。启动不读取全量正文、不全量向量化；只有候选正文按需从 SQLite 取出。测试资料独立分区，不能因为文件名带 verified 就成为默认医学证据。

FTS5 的排序与 WAL 行为见 [SQLite FTS5](https://www.sqlite.org/fts5.html)、[SQLite WAL](https://www.sqlite.org/wal.html)。磁盘化减少内存，不代表零内存；E5 查询首次按需加载也有延迟。

## 可配置资源阈值

写入本地 `.env.local` 或环境变量，优先级：环境变量 > 本地文件 > 默认值。无需改动现有模型 key。

```dotenv
EMBEDDING_BATCH_SIZE=8
VECTOR_WRITE_BATCH_SIZE=128
BUILD_MIN_FREE_MEMORY_GIB=2
BUILD_MAX_RSS_GIB=3
BUILD_START_FREE_DISK_GIB=10
BUILD_MIN_FREE_DISK_GIB=5
TRANSLATION_TTL_SECONDS=86400
```

内存目标包括构建进程树，不包括独立 Chroma/翻译服务；系统可用内存阈值会同时观察它们的影响。运行记录在 `data/runtime/indexes/jobs/<job_id>.json`，源快照在 `source_snapshots/`。不调整 Windows 分页文件。检查点是批次事务，不是“所有未写入的进度都能恢复”。

## 中英文查询和翻译

`translate_query`、`translate_evidence` 是内置 MCP 服务真实可发现的工具。前者优先术语表，例如房颤→atrial fibrillation，同时保留诊断检查和空腹准备两个目标；第二轮选择不同批准变体。后者仅接受本轮获得的最多 5 个证据 ID，批量翻译展示片段；不是检索来源，不改变审核字段。

数字、单位及已知药名占位保护；还原不一致或明显丢失否定语义则保留原文。语义校验只能发现部分错误，机器翻译仍需审慎阅读，不能成为新增医学依据。原文、URL、ID、哈希保持不变。译文缓存独立保存在运行时 `translation-cache.sqlite3`，默认 24 小时。

`network_mode=off` 不调用外网取证或云翻译工具，本地双语术语扩展仍可用。急救短路仍为零模型、零工具调用。工具最多 8 次、补证最多 2 轮，翻译也计入工具和模型预算。

## 验收

本轮实测结果、恢复进度与未通过/未验证项见 [实施及验收记录](LOW_MEMORY_ACCEPTANCE.md)。

```powershell
.\.venv-agentic\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=data/runtime/test-run-new
.\.venv-agentic\Scripts\python.exe -m scripts.verify_low_memory
# 下列命令才使用真实模型；一次执行共享最多 10 个 HTTP 尝试，包含翻译和重试。
.\.venv-agentic\Scripts\python.exe -m scripts.verify_low_memory --live --browser
```

不要重复执行付费冒烟来突破本次 10 次预算。报告分别记录加载内存、实际召回、回答、引用、工具轨迹、降级及请求次数。`lexical_ready=true` 不等于 `dense_ready=true`；Chroma 服务在线也不等于其向量索引已完成。完整向量验收前不能宣称全混合检索交付。
