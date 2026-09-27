# M6 handoff contracts

M6 仍是单进程、内存图。`packages/agent/contracts.py` 的 `NODE_CONTRACTS` 为主图和两个子图的每个节点声明 `requires`、`optional`、`produces`、Pydantic 输入/输出 schema 与上游节点。构图时比较注册表与实际节点，检查必需字段有类型匹配的上游 producer，特别检查 Verifier 消费 `AnalysisResult`。未建的 publish 节点及 `ApprovalResult` 属于 M7，当前注册表不伪造它们。

| 节点 | 必需输入 | 输出 |
| --- | --- | --- |
| planner | `user_query` | `plan`, `trace` |
| fork | `plan` | `trace` |
| SQL 子图 / SQL 分支 | `plan` | `sql_results`（分支另有 `trace`） |
| RAG 子图 / RAG 分支 | `plan` | `rag_results`（分支另有 `trace`） |
| synthesis | `plan`，且路线所需产物齐全 | `analysis`, `trace` |
| verifier | `plan`, `analysis`，且路线所需产物齐全 | `verification`, `status`, `trace` |

每个 LangGraph 节点执行 `validate_input() → node() → validate_output()`。边界会拒绝缺字段、错误类型、未声明输出、产物数量与计划不一致、最终状态与 Verification 不一致，抛出 `ContractViolation`，不会调用下游。M4 工具调用也有输出模型：SQL 五种结果按工具分别验证，知识检索验证查询/产品匹配、结果数量、Evidence 字段、内容 SHA-256 和来源 ID；无效工具返回立即阻断，不能当作 LLM 参数错误进行 repair。

`packages/agent/evidence.py` 在 Verifier 批准前逐条检查：每条 claim 只能引用本轮一个 SQL artifact 或 RAG evidence；SQL claim 的数字必须出现在被引 SQL 结果/参数中；RAG claim 必须把该 evidence 文本中的一段原文同时放在 `evidence_quote` 与 claim 正文，数字也必须出现在该证据文本中。摘要只允许重复计划里已有的数字。任一检查失败即 `BLOCK`，Verifier 自身失败也 `BLOCK`。LLM Verifier 仍负责判断引文与叙述的语义关系、冲突及 synthetic 表述；精确引文和数值核对**不能证明任意自然语言推论都被证据蕴含**。

固定四题真实依赖 demo 与注册表/schema 哈希见 [M6 本机报告](../evaluation/reports/m6_contract_demo.json)。缺字段、错误 schema、无效 SQL/RAG 工具返回、错误数值/引文、引用注入和注册表缺项由 `tests/agent/test_m6_contracts.py` 确定性覆盖。这是本机流程与契约验收，不是回答质量 benchmark 或 CI 结果。
