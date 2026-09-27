# M11 最终验收与证据边界

本机最终重跑基线为干净提交 `283bb068bcf41ab73fdda2de19ce046c32cf7cf0`。总索引见 [m11_final.json](../evaluation/reports/m11_final.json)。`scripts/run_m11_final.py` 在每组评测前创建独立临时 Git worktree，复用已验证的本地数据与模型文件，把输出另存为 `m11_*`；M2/M3/M6/M8/M9/M10 的原报告仍与基线 Git 内容逐字节一致。总脚本实际校验逐文件 SHA-256、RAG 逐题排名重算结果、SQL 逐题汇总、workflow 路由和状态、定向重跑版本及发布行数。另用独立命令重新核对总报告的源码哈希、各子报告的基线与哈希，以及 PostgreSQL 中两条不同演示 run 各自唯一的发布回执。

## 最终结果

| 项目 | 本机结果 | 证据与解释 |
| --- | --- | --- |
| RAG | 256 题本地 query holdout，完整去重语料 21,953 passages；hybrid + reranker Recall@10 **0.4082**、MRR@10 **0.1951** | [四组重跑报告](../evaluation/reports/m11_rag_ablation.json)、[逐题排名](../evaluation/reports/m11_rag_rankings.jsonl)；数据源作者将原文件标为训练数据，这不是官方独立测试集 |
| SQL | 100/100 可回答题严格结果匹配；5/5 不可回答与 5/5 不安全请求正确拒绝；110 次响应均报告 `deepseek-flash` | [逐题重跑报告](../evaluation/reports/m11_sql_evaluation.json)；同一已分析过的固定题本，不是未见题泛化成绩。0 次 repair，实测 repair success 为 `null` |
| Workflow | SQL、RAG、BOTH、REPORT 四条固定 demo 均 `PASS`；确定性证据问题为空 | [M11 workflow 报告](../evaluation/reports/m11_workflow_demo.json)；18 次模型调用，显式 DeepSeek thinking/high，输出上限至少 16,384 token |
| Durable replay | Planner checkpoint 后受控进程退出；同一 run/thread 接管，仅重跑 RAG；SQL v1 保留，RAG/analysis/verification 进入 v2；唯一发布 | [M11 replay 报告](../evaluation/reports/m11_replay_demo.json)，旧 artifact 审核 409、无权限审核 403 |
| Fault recovery | 一次**本地注入**的 Planner timeout 被有限重试恢复，最终唯一发布；故障套件 **28 passed** | [M11 fault 报告](../evaluation/reports/m11_fault_injection.json)；不代表真实 DeepSeek 或 Milvus 发生过该故障 |
| 工程检查 | 完整本机 pytest **109 passed**；`npm ci` 和 Vue build 通过；统一 `docker compose up -d --wait` 四个核心容器健康 | [总报告](../evaluation/reports/m11_final.json)记录测试与构建输出；这不是 GitHub CI 结果 |

同一 RAG 设置下，hybrid + reranker 相对 dense 的 Recall@10 增加约 **0.0879**（8.79 个百分点），MRR@10 增加约 **0.0489**。这是固定本地切分上的消融差值，不能外推到新语料。四组检索使用相同完整语料、query、BGE-M3 编码、Milvus 评测索引和候选上限；差别见 [评测方法](evaluation.md)。

M3 历史链保持原样：[初始 Luna 报告](../evaluation/reports/sql_evaluation_m3_v1.json) 为 85/100；M3.1 补齐值语义与输出契约后，在**同题、同 gold、同严格 evaluator** 的 [Luna 回归报告](../evaluation/reports/sql_evaluation.json) 为 100/100；M11 用 DeepSeek 与 8,192 token 上限重跑同题也得到 100/100。三个数字都不是新题泛化分数。报告返回的 model ID 只证明请求和响应声称的 ID，不能证明服务端具体权重版本。

## 复现

要求 Python 3.12、`uv sync --locked`、Node 22、Docker Compose、可用的 NVIDIA GPU、已下载并核验的 HF 数据/公开附件/BGE 模型，以及可用的 DeepSeek key。数据和模型下载方式见 [README](../README.md)；原始大文件不随 Git 仓库分发。先执行 `docker compose up -d --wait`，按 M1 章节建立并导入 synthetic 业务库，按 M3 章节建立只读账号。再创建两个**空的专用演示数据库**：

```bash
docker compose exec -T postgres createdb -U insurance_app insurance_m11_replay_demo
docker compose exec -T postgres createdb -U insurance_app insurance_m11_fault_demo
```

将 `M1_TEST_DATABASE_URL`、`M1_VERIFY_DOWNLOADS=1`、`M2_TEST_MILVUS_URI`、`M3_TEST_READER_DATABASE_URL`、`M4_TEST_MILVUS_URI`、`M7_TEST_DATABASE_URL`、`M8_TEST_DATABASE_URL`、`M9_TEST_DATABASE_URL`、`M10_TEST_DATABASE_URL` 指向各自准备好的测试依赖；另设置：

```bash
export M3_READER_DATABASE_URL='postgresql+asyncpg://insurance_reader:change-me-reader-local-only@127.0.0.1:5432/insurance_m1_acceptance'
export M11_REPLAY_DATABASE_URL='postgresql://insurance_app:change-me-local-only@127.0.0.1:5432/insurance_m11_replay_demo'
export M11_FAULT_DATABASE_URL='postgresql://insurance_app:change-me-local-only@127.0.0.1:5432/insurance_m11_fault_demo'
uv run --locked python -m scripts.run_m11_final
```

`M9_TEST_DATABASE_URL` 同时用于 M9 fault suite。脚本要求干净提交和空演示库；任何 suite 失败都不写新的 `m11_final.json`。DeepSeek key 由既有 provider 配置读取，不写入报告。脚本的 DeepSeek SQL 输出上限是 8,192；workflow thinking/high 的输出上限至少 16,384。两者为应对本次真实截断而显式设置，并写入各自报告。各 suite 的 wall time 在总报告中，依赖本机缓存、GPU 和网络，不是跨机器性能基准；token 使用量没有采集，不能给出 token 成本或 p50/p95 调用延迟。

## 失败案例与修复记录

| 观察 | 处理与证据 | 对结论的影响 |
| --- | --- | --- |
| M3 初版 15/100 可回答题未严格匹配，涉及中文区域值、比例表示和返回列；其中 1 题混淆已发生赔款与现金支付 | [M3 评测说明](evaluation.md)和两版原始报告保留；M3.1 明确数据库值字典与题目输出契约 | 100/100 只能叫同题回归，不叫 unseen holdout |
| M8 最初 worker 在初始化 BGE/Milvus 前领取短 lease，Planner 前已过期 | 将初始化移到领取 job 前；[M8 报告](../evaluation/reports/m8_runner_replay.json)实测崩溃接管 | 说明 lease、checkpoint 与 artifact 有不同职责 |
| M11 首轮 SQL 到第 60 题时 DeepSeek 返回 `finish_reason=length`，没有生成成功报告 | 评测客户端增加显式、可记录的 `--max-tokens 8192`；旧默认 1200 与 gold/evaluator 不变 | 输出上限属于模型请求条件，M11 报告如实记录 |
| M11 第一轮 REPORT Verifier 在 8,192 token 上限下返回 `BLOCK`；单题 16,384 token 诊断曾返回 `REVISE`，指出季度右边界描述的歧义 | Verifier 的 fail-closed 规则保持不变。最终连续四路重跑以显式 16,384 上限取得 4/4 PASS；中间失败仍在本记录中 | 固定 demo 的一次 PASS 不证明稳定成功率，也不应抹去有价值的审核意见 |
| M9 故障用受控注入覆盖暂时性超时、reranker 降级及 Verifier 阻断 | [M11 故障重跑](../evaluation/reports/m11_fault_injection.json)与 28 条故障测试 | 只证明受控故障下的代码路径，不是外部服务可靠性指标 |

## 设计决策与限制

- M4 FastMCP 只包装已有 SQL/知识能力；LangGraph 角色通过工具契约调用，不在 Synthesis/Verifier 中直接查询数据。handoff 通过 Pydantic 和当前 artifact 的证据检查；语义蕴含仍需模型判断。
- SQL 执行由 SELECT-only AST、表/函数白名单、PostgreSQL 只读账号、只读事务、5 秒超时和行数上限共同限制。统计值由工具计算，不由 Synthesis 心算。
- Job lease、LangGraph checkpoint、版本化 artifact、人工审核与唯一 publication 分别负责调度、恢复、结果、决定和发布。`run_events` 只是观测时间线，没有 transactional outbox 保证。
- M10 为角色级 RBAC，没有多租户 run ownership ACL；前端 logout 只清 `sessionStorage`，尚无服务端 refresh 撤销接口。API 文档/评测 POST 仅核验已有资料/报告，没有新增文档入库或即时评测。
- 所有运营表和 12 份产品条款都是 `SEED=202609` 生成的 **synthetic** 数据；它们没有真实个人保险信息，也没有复制真实公司 schema。五份中保协公开附件保持 **draft**，没有自动绑定 synthetic `product_code`。详见[数据来源](data_sources.md)和[合成数据假设](synthetic_assumptions.md)。
- Insur-QA 的 256 题是作者标为训练数据的文件中划出的本地 query holdout；全量去重 passage 在同一评测语料中。公开 benchmark 与模型预训练重叠不能排除。项目没有真实保险企业生产验证、公开部署安全验证或 GitHub CI 结果。

## 简历陈述与代码证据

| 可以陈述 | 直接证据 | 不应延伸为 |
| --- | --- | --- |
| 实现四路 LangGraph 编排、并行 SQL/RAG、定向重跑与 HITL 发布 | [图与 artifact](../packages/agent/m8_graph.py)、[M11 workflow](../evaluation/reports/m11_workflow_demo.json)、[M11 replay](../evaluation/reports/m11_replay_demo.json) | 任意开放问题稳定 100% 正确 |
| 建立 Pydantic handoff 与逐声明证据检查 | [节点契约](../packages/agent/contracts.py)、[证据校验](../packages/agent/evidence.py)、[契约说明](handoff_contracts.md) | 程序能证明一切自然语言语义 |
| 建立 PostgreSQL checkpoint、租约接管、版本化 artifact 和审核发布 guard | [runner](../apps/runner/worker.py)、[review store](../packages/persistence/reviews.py)、[M11 replay](../evaluation/reports/m11_replay_demo.json) | 具事务保证的全链路事件账本 |
| 对受控暂时性故障做有限重试、reranker 降级和 Verifier fail-closed | [faults](../packages/agent/faults.py)、[故障重跑](../evaluation/reports/m11_fault_injection.json) | 外部服务真实故障率或自动修复成功率 |
| 用固定本地切分做 BGE/Milvus 四组检索消融，并对 SQL 固定题本作严格结果评测 | [RAG 报告](../evaluation/reports/m11_rag_ablation.json)、[SQL 报告](../evaluation/reports/m11_sql_evaluation.json) | 官方 benchmark、未见题泛化或真实企业效益 |
