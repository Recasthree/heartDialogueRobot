# 网络问答恢复记录（2026-09-29）

## 实际原因

1. 用户 23:17、23:18 的 trace 中，模型节点连续出现 `APIConnectionError`，PubMed 出现连接失败；同一轮分别累计 15、18 次失败模型尝试。此前运行的 API 继承了受限执行环境的出网权限。将同项目、同配置、同网络客户端放在正常权限下检查，模型 `/models` 与 PubMed 均返回 200。这不能归因于“key 无效”或“整台电脑无法上网”。
2. 重启脚本未确保旧 Uvicorn 子进程退出，曾留下旧进程继续监听。现已限定停止登记进程及相同命令的子进程，失败即中止，端口仍占用时拒绝启动另一实例。
3. 恢复出网后的真实问答仍失败：PubMed 默认搜索得到过于专门的病例/特定人群论文，没有覆盖基础症状和检查问题。仅成功取到文献不足以通过回答证据检查。
4. 最后两次真实模型调用中，症状回答明确给出了一个合法引用 ID，却漏写正文编号。现只对这种无歧义的单来源声明做格式修复；原始结果保留，未篡改原始记录以制造通过。

本次不是通过重建全部索引、更换 API key、修改数据审核字段或放松医学安全条件来解决。

## 修复范围

- 连接失败在当前轮短路后续模型调用；下一轮可以重新连接。模型故障和网络取证故障有不同提示，不再统一包装成资料不足。
- 基础问题通过实际 MCP `fetch_evidence` 获取 NIH/NHLBI 患者教育正文；链接注册覆盖房颤检查、心律失常症状/检查，不硬编码医学回答。目标不匹配、页面失败或需要论文/最新指南时，仍走有限预算的后续网络取证。
- PubMed 默认按相关性排序。获取的标题、正文、日期、URL、来源类型和未审核状态进入生成上下文。
- 双语相关性门槛保留；只有通过门槛的网络结果可保留候选席位，不能将被重排器明确拒绝的文献强行插回去。
- 重排输入使用 `r1` 等临时句柄，服务端映射回真实证据 ID，降低长 ID 复制错误；非法输出仍降级，不向聊天抛出 500。
- 单条有效来源漏写 `[1]` 时允许确定性格式化，仍要通过安全复核。无声明来源、伪造 ID、多来源缺编号、错误编号或不受证据支持的内容仍拒绝/重新生成。
- 关闭联网、急救短路、SSRF、患者资料与公共资料隔离等原有边界保持。

## 验证证据与边界

| 检查 | 实际结果 |
|---|---|
| 正常权限下 AIHubMix 认证检查、应用 PubMed 传输 | 均 HTTP 200；不调用生成模型 |
| 第一轮真实恢复测试 | 8 次模型请求；基础问题仍因文献不合适失败，保留报告 |
| 新官方来源经真实 MCP 获取 | 房颤检查 4 个正文片段、心律失常症状 1 个片段；离线相关性筛选均保留 |
| 最后 2 次付费请求 | 一次两题批量生成、一次两题安全复核；两题安全复核通过，房颤引用直接通过，症状题原始结果漏编号 |
| 已保存真实回答的离线节点回放 | 格式修复后两题均通过生成契约、引用、安全及最终输出节点；新增模型请求为 0 |
| 全量自动回归 | **150 passed、6 skipped、0 failed**；210.68 秒。6 个缺少 repaired corpus 的既有测试继续跳过 |
| 已重启的正式 API | 网页 HTTP 200；急救测试 `model_calls=0`、工具列表为空；磁盘 BM25 可用、向量继续构建 |
| 最终配置下全链路多模型在线复测 | **未进行**：本轮真实请求已达 10 次（包括前面的失败验证） |

原始、不可混淆的报告：

- [首次真实恢复测试](../data/runtime/verification/network-recovery-20260929-233037/report.json)
- [只取证、零付费测试](../data/runtime/verification/patient-sources-20260929-234622/report.json)
- [两次批量模型验证原始结果](../data/runtime/verification/patient-sources-20260929-234842/report.json)
- [格式修复后的离线节点回放](../data/runtime/verification/patient-sources-20260929-234842/offline-replay.json)
- [最终回归测试 XML](../data/runtime/verification/network-recovery-tests.xml)

回放不等于重新请求模型，也不等于最终全链路在线验收。HTTP/MCP 全链路行为另有离线可重复测试，覆盖优先取证、失败回退、关闭联网和保留安全拒答。全量测试中的 4 条警告为现有依赖弃用提示，并非测试失败。

来源：[NHLBI 房颤检查](https://www.nhlbi.nih.gov/health/atrial-fibrillation/diagnosis)、[NHLBI 心律失常症状](https://www.nhlbi.nih.gov/health/arrhythmias/symptoms)。获取时间和页面日期均保存在证据中，不能因为现在获取就称为“最新指南”。

## 使用与复现

```powershell
.\scripts\start_app.ps1 -Restart -CheckNetwork
.\.venv-agentic\Scripts\python.exe -m pytest -q
.\.venv-agentic\Scripts\python.exe -m scripts.verify_patient_sources
.\.venv-agentic\Scripts\python.exe -m scripts.replay_source_answers data/runtime/verification/patient-sources-20260929-234842/report.json
```

启动/真实取证命令应在正常出网权限的本机 PowerShell 运行；普通回归和回放不需要外网。最后两条命令不调用付费生成。单独启用 `--live-two` 会消耗请求，不要将原来已用完预算的报告用于重复验证。

打开 http://127.0.0.1:8000/，刷新后新建对话并保持“联网补证”开启。历史失败消息不会自动重算；成功取证时本轮工具中应出现 `fetch_evidence / available`，引用仍在对话内展开。
