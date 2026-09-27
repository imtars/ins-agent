# 架构与边界（M0）

## 产品边界

系统回答保险运营分析与条款知识问题，输入可以分别路由到 SQL、RAG 或两者。所有业务运营记录和与其通过 `product_code` 关联的产品文档均为合成演示数据。公开条款仅用于独立知识检索，除非有明确、已验证的产品映射，不得把公开条款套用到合成产品。

五个依赖 LLM 的角色固定为 Planner、Data Analyst、Knowledge Researcher、Synthesis Analyst、Verification Agent。计算指标、SQL 校验、检索融合由确定性代码执行。M6 已增加节点输入/输出契约和逐条 claim 的来源 ID、数字、原文引文检查；任意叙述的语义蕴含仍由 LLM Verifier 判断，审批发布属于后续阶段。Provider 抽象位于 `packages/llm`；本机优先 CLIProxyAPI `gpt-6-luna`，DeepSeek 为备用。

## 目标流程

```text
Vue 3 UI → FastAPI → PostgreSQL job queue → Runner
                                       ↓
                           LangGraph + Postgres checkpoint
                           Planner → SQL/RAG → Synthesis → Verifier
                                         ↘        ↙              ↓
                                    FastMCP tools             Human review
                                    PostgreSQL/Milvus           ↓
                                                            Publish guard
```

SQL 和 RAG 子图在需要时并行执行，合流前必须各自完成明确的 Pydantic 输出契约。Planner 不直接查询数据；Synthesis 只读取版本化的 SQL/RAG artifact；Verifier 对每个数字和事实声明检查证据，异常时阻断；Publish 必须再次读取已批准的 `ApprovalResult`。

`RunState` 只保存可序列化的小型引用、路由和状态。大结果存储在 `run_artifacts`，每次重跑生成新版本和内容哈希；checkpoint 保存图状态和中断位置，不充当作业队列。`run_id` 始终等于 LangGraph `thread_id`。Runner 用 PostgreSQL lease 和 `FOR UPDATE SKIP LOCKED` 领取作业，崩溃后按同一 thread 恢复。外部副作用需在独立节点里做幂等键或提交检查，因为 LangGraph 在恢复时可能从节点开头重新执行。

## 数据与信任边界

1. SQL 工具使用独立 `insurance_reader` 角色、只读事务、AST 白名单、表白名单、`statement_timeout` 和最大行数。生成 SQL 本身视为不可信输入。
2. 检索文档与工具返回内容视为数据，不得提升为系统指令。每条事实 claim 必须引用当前 artifact 中存在的 evidence ID。
3. `product_code` 是合成 SQL 与合成条款的唯一正式关联键。公开文档记录来源、版本与下载哈希，不自动参加运营数据联表。
4. `run_events` 记录节点耗时、模型、token、重试、错误与输入输出哈希；避免记录密钥或原始个人信息。
5. 人工 review 中断之后，只有被授权 reviewer 的批准结果能通过 publish guard。拒绝时按 `sql`、`rag`、`synthesis` 定向重跑，未选阶段的 artifact 哈希保持不变。

## 技术落点

| 路径 | 职责 | 开始实施 |
| --- | --- | --- |
| `packages/domain` | 配置、Pydantic 领域契约 | M0/M6 |
| `packages/persistence` | 数据模型、作业、artifact | M1/M8 |
| `packages/knowledge`, `evaluation/rag` | 文档解析、BGE-M3、Milvus 检索与评测 | M2 |
| `packages/sql`, `evaluation/sql` | M3 只读 SQL 生成、执行、确定性指标与结果评测 | M3 |
| `packages/agent` | SQL/RAG 子图和主 LangGraph | M5 |
| `services/mcp_data`, `services/mcp_knowledge` | 对既有能力的 FastMCP 包装 | M4 |
| `apps/api`, `apps/runner`, `apps/frontend` | HTTP、worker、Vue UI | M8/M10 |

M7 历史图及其 checkpoint/审核表保留。M8 新图保留五角色和 M4 MCP 专家调用，但 checkpoint 中的 SQL/RAG/Analysis/Verification 只保存 `ArtifactRef`；完整内容和规范化 SHA-256 存入版本化 `run_artifacts`。`agent_jobs` 的租约管理与 checkpoint 分开，worker 通过 `FOR UPDATE SKIP LOCKED` 认领并续租，崩溃后的新 worker 用相同 `run_id/thread_id` 恢复。审核记录绑定具体 analysis artifact ID、版本和哈希，发布路径只接受仍为最新版本的已批准产物。完整 JWT/RBAC 与前端属于 M10，M9 故障注入仍未实现。

## 后续待验证的工程问题

- M3 后续质量判断：初始诊断的错误已用于 M3.1 同题回归修正；回归分数不能冒称独立泛化结果。若未来需要泛化结论，须另设未参与开发的题本。
- M7：真实两进程验收已验证 PostgreSQL checkpoint、`interrupt()` 恢复和发布幂等；详见 [M7 本机报告](../evaluation/reports/m7_durable_demo.json)。
- M8：真实 worker 崩溃、lease expiry 恢复和 RAG 定向重跑见 [M8 本机报告](../evaluation/reports/m8_runner_replay.json)；同一阶段 artifact 写入以 `(run_id, stage, generation)` 幂等。
- M9：暂时性依赖错误在 LLM/MCP 边界有限重试；Verifier 仍 fail closed。Reranker 故障只使用已经取得的 RRF 候选并标记降级。PostgreSQL 会话 advisory lock 串行化同一 run 的 checkpoint writer；详见 [M9 故障报告](../evaluation/reports/m9_fault_injection.json)。
