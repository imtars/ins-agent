# 保险业务知识与运营分析多智能体 Harness

## 0. 项目目标

实现一个可以作为求职作品集展示的企业级 Agent 工程项目。

项目不是“保险聊天机器人”，而是一套面向保险业务分析场景的：

**Multi-Agent Workflow Harness**

核心任务是同时处理：

- 结构化保险运营数据查询与分析；
- 非结构化保险条款 / 制度知识检索；
- SQL + RAG 跨源综合分析；
- 多 Agent 阶段编排；
- 阶段间数据契约；
- 持久化执行与故障恢复；
- Human-in-the-loop 审批；
- 定向重跑；
- Tool/MCP 解耦；
- 分层 Evaluation Harness。

最终应达到：

1. GitHub 仓库结构完整；
2. Docker Compose 可以启动核心依赖；
3. Web UI 可以完整演示一个 Agent Run；
4. 有真实公开保险 Benchmark；
5. 有可重复的模拟业务数据库；
6. 有真实可复现的评测结果；
7. 有 checkpoint / HITL / fault injection 自动化测试；
8. README 足以让面试官理解整个系统设计。

项目运行时默认 LLM：

`deepseek-flash`

LLM Provider 必须抽象，禁止业务代码直接依赖具体模型。

---

# 1. 固定技术栈

后端：

```text
Python 3.12
FastAPI
Pydantic 2
SQLAlchemy 2
Alembic
PostgreSQL
LangGraph
langgraph-checkpoint-postgres
sqlglot
```

Agent / AI：

```text
LangGraph
DeepSeek API
FastMCP 4.x
BGE-M3
BGE-Reranker-v2-m3
Milvus 2.6+
FlagEmbedding
```

前端：

```text
Vue 3
TypeScript
Vite
Pinia
Axios
SSE
```

工程：

```text
uv
pytest
Docker Compose
GitHub Actions
```

明确不加入：

```text
Redis
Kafka
Celery
Kubernetes
Neo4j
GraphRAG
多模态
模型微调
复杂微服务
```

除非后续有明确需求，否则禁止扩大技术栈。

---

# 2. 数据体系

整个项目必须区分四种数据。

## 2.1 Synthetic Operational Data

这是 Agent 产品本身使用的结构化业务数据库。

原因：

真实保险公司的：

- 客户；
- 保单；
- 理赔；
- 代理人；
- 销售业绩；
- 分公司经营数据

通常不可公开获得，更不能使用前公司数据。

因此建立一个完全可复现的 synthetic insurance company。

固定：

```python
SEED = 202609
```

核心表：

```text
branches
agents
customers
products
policies
claims
claim_payments
```

建议默认规模：

```text
branches          12
agents            150~250
customers         10,000
products          12
policies          25,000~40,000
claims            4,000~8,000
claim_payments    根据 claims 生成
```

具体数字做成配置。

### 数据不能纯随机

生成器必须人为植入可分析的业务模式。

例如：

```text
高 risk_score 客户 → 更高 claim probability

不同产品
→ 不同 claim frequency
→ 不同 claim severity

部分区域
→ 更高机动车事故率

部分产品
→ 较高 loss ratio

不同年龄段
→ 不同出险概率
```

否则 SQL Agent 最后分析出来的只是随机噪声。

必须保证固定 seed 下结果稳定。

---

## 2.2 Synthetic Insurance Documents

为了让 SQL 和 RAG 可以真正跨源关联，项目自己生成一套**明确标记为 synthetic** 的保险文档。

例如 12 个产品对应：

```text
product_001.md
product_002.md
...
product_012.md
```

包含：

```text
保险责任
责任免除
等待期
犹豫期
保险期间
赔付条件
理赔申请材料
特殊约定
```

documents 与 products 表通过：

```text
product_code
```

关联。

目的不是模拟真实产品，而是构建：

```text
SQL operational data
+
RAG policy knowledge
```

完整演示环境。

所有 synthetic 文件顶部必须声明：

```text
Synthetic demo document.
Not a real insurance product or policy.
```

---

# 3. 公共保险 Benchmark

## 3.1 InsQABench

来源：

```text
GitHub:
jingjingjing-ding/InsQABench

Hugging Face:
JaneDing2025/InsQABench
```

使用以下文件：

```text
clause_train.json
clause_objective.json
clause_subjective.json

db_train.json
db_test.json
```

用途：

Clause 数据：

```text
RAG evaluation
保险领域回答测试
Evidence grounding
```

DB 数据：

```text
保险领域 Text-to-SQL 问题参考
SQL prompt case
领域问题生成参考
```

注意：

**不得假定 InsQABench 提供完整 SQL 数据库。**

它只作为 QA Benchmark / reference dataset。

下载时使用：

```python
huggingface_hub.hf_hub_download()
```

而不是依赖 Git clone，因为 GitHub 中部分大文件通过 LFS 托管。

---

# 4. RAG 核心 Benchmark

优先使用：

```text
Hugging Face:
FrankRin/Insur-QA
```

关键文件：

```text
Insur-QA-Retriever.json
Insur-QA-LLM.json
```

Retriever 文件结构：

```text
query
pos
neg
```

因此可以构造真正的 retrieval benchmark。

实现：

```text
所有 pos/neg passage
        ↓
deduplicate
        ↓
benchmark corpus
        ↓
Milvus
```

对于每一个：

```text
query
```

已知：

```text
gold positive passage
```

所以可以客观计算：

```text
Recall@1
Recall@5
Recall@10
MRR
```

不需要 LLM Judge。

这应该成为项目最重要的一组真实 AI 指标。

注意：

FrankRin/Insur-QA 两个 JSON schema 不一致，因此不要：

```python
load_dataset("FrankRin/Insur-QA")
```

直接自动合并。

使用：

```python
hf_hub_download(
    repo_id="FrankRin/Insur-QA",
    filename="Insur-QA-Retriever.json",
    repo_type="dataset"
)
```

分别读取。

---

# 5. 公开真实保险文档

用于展示真正的：

```text
PDF/DOCX
→ parsing
→ chunking
→ embedding
→ Milvus
```

数据源优先级：

### Source A

中国保险行业协会：

```text
中国人身保险产品信息库
```

该平台存在：

```text
保险条款
产品类型
销售状态
费率文件
现金价值表
产品说明书
```

### Source B

中国保险行业协会公开发布的示范条款。

V1 推荐获取：

```text
2026 定期寿险示范条款
2026 终身寿险示范条款

2026 终身寿险（分红型）示范条款
2026 两全保险（分红型）示范条款
2026 年金保险（分红型）示范条款
```

这些网页存在公开 DOCX/PDF 附件。

实现：

```text
data/manifests/public_docs.yaml
```

记录：

```yaml
- id:
  title:
  publisher:
  source_page:
  file_url:
  file_type:
  retrieved_at:
```

然后：

```text
scripts/download_public_documents.py
```

自动下载。

### Copyright 处理

GitHub repository 默认：

```text
不提交第三方原始 PDF
```

`.gitignore`：

```text
data/raw/public/*
```

只提交：

```text
manifest
download script
source metadata
```

避免重新分发版权不明确的第三方材料。

---

# 6. 可选真实结构化数据

V1 不依赖。

可以后续加入：

```text
UCI COIL 2000
```

它有：

```text
9000 customers
86 variables
CC BY 4.0
```

可以作为真实保险客户数据分析扩展。

另外国家金融监督管理总局公开：

```text
保险业经营情况
人身险公司经营情况
财产险公司经营情况
地区原保险保费收入
```

可作为行业统计扩展。

但是：

**V1 SQL Agent 的核心数据库仍然必须是 synthetic operational DB。**

原因：

它的 schema 和 ground truth 完全可控，更适合：

```text
JOIN
GROUP BY
时间分析
业务指标
SQL evaluation
```

---

# 7. 参考开源项目

禁止直接 fork 后改名字。

只学习设计。

## 7.1 InsQABench

学习：

```text
Insurance DB QA
Insurance Clause QA
SQL-ReAct
RAG-ReAct
Benchmark 组织方式
```

不要复制其旧模型代码。

---

## 7.2 insurance-policy-rag

仓库：

```text
i-hridaysaha/insurance-policy-rag
```

重点学习：

```text
clause-aware chunking
hybrid retrieval
RRF
citation verification
refusal
retrieval ablation
deterministic evaluation
```

该项目的价值尤其在于：

```text
Dense
Sparse
Hybrid
```

分别测 Recall@k / MRR，而不是只声称“用了 Hybrid RAG”。

同时学习：

```text
引用必须验证是否真的出现在 retrieved context 中
```

---

## 7.3 enterprise-workflow-agent-platform

仓库：

```text
smlfy/enterprise-workflow-agent-platform
```

重点学习：

```text
Supervisor
LangGraph fan-out/fan-in
HITL
checkpoint resume
task/handoff
trace
audit log
workflow state
fault/recovery
```

这个项目是整个工程层最重要的参考。

---

## 7.4 SuperMew

仓库：

```text
gioqnc/superMEW
```

重点学习：

```text
FastAPI
Vue3
PostgreSQL
Milvus
BGE-M3
hybrid retrieval
RRF
rerank
JWT
SSE
```

它与本项目基础技术栈高度接近。

但我们的核心区别必须是：

```text
SuperMew → Agentic RAG

本项目 → Durable Multi-Agent Workflow Harness
```

---

## 7.5 τ-bench / τ³-bench

仅作为 Evaluation / Agent Environment 设计参考。

学习：

```text
Policy
Database
Tools
Environment State
Outcome
Agent Trace
```

不要求 V1 接入 benchmark。

---

# 8. 系统最终架构

```text
                         Vue3
                           │
                      REST + SSE
                           │
                        FastAPI
                           │
                    Run Management
                           │
                  PostgreSQL Job Queue
                           │
                   Background Runner
                           │
                     LangGraph Main
                           │
                      Planner Agent
                           │
                 ┌─────────┴─────────┐
                 │                   │
            SQL Subgraph        RAG Subgraph
                 │                   │
          Data Analyst        Knowledge Agent
                 │                   │
             FastMCP             FastMCP
                 │                   │
          PostgreSQL              Milvus
                 │                   │
                 │             BGE-M3
                 │                   │
                 │              Reranker
                 │                   │
                 └─────────┬─────────┘
                           │
                    Synthesis Agent
                           │
                     Verifier Agent
                       /       \
                   REVISE      PASS
                     │           │
              Revision Router   HITL
                     │           │
               SQL / RAG      interrupt()
                                 │
                           Human Reviewer
                              /        \
                          reject      approve
                            │            │
                     Revision Router   Publish
                                         │
                                       END
```

---

# 9. 五个 Agent

只允许五个真正依赖 LLM 推理的角色。

## Planner Agent

职责：

```text
理解任务
判断 SQL / RAG / BOTH
拆解任务
定义输出要求
```

输出：

```python
TaskPlan
```

字段：

```text
intent
route
sql_tasks
rag_tasks
required_outputs
```

禁止 Planner：

```text
直接查数据库
直接生成最终报告
```

---

## Data Analyst Agent

输入：

```text
TaskPlan.sql_tasks
```

负责：

```text
理解 schema
生成 SQL
执行
根据数据库错误自修复
调用 deterministic analytics
```

禁止 LLM：

```text
自行计算重要统计数字
```

例如：

```text
claim_rate
loss_ratio
average_claim
growth_rate
```

必须由 Python / SQL 计算。

原则：

```text
Numbers = deterministic code
Narrative = LLM
```

---

## Knowledge Researcher Agent

负责：

```text
query rewrite
retrieve
rerank
evidence sufficiency
retry
```

输出：

```python
RagResult
```

不能直接输出最终分析报告。

---

## Synthesis Analyst Agent

只能读取：

```text
SqlResult
RagResult
AnalyticsResult
```

负责：

```text
综合分析
形成 claims
组织报告
绑定 citation
```

禁止：

```text
再次偷偷调用数据源
```

---

## Verification Agent

检查：

```text
每个数字是否来源于 SQL artifact
每个事实 claim 是否存在 RAG evidence
citation 是否存在
是否出现没有证据的结论
是否把 synthetic 数据描述成真实生产数据
是否存在数据冲突
```

输出：

```python
VerificationResult
```

状态：

```text
PASS
REVISE
BLOCK
```

Verifier 不能降级。

Verifier 自身失败：

```text
BLOCK
```

禁止：

```text
默认 PASS
```

---

# 10. Graph State

主状态不得使用自由 dict 四处塞数据。

定义：

```python
class RunState(TypedDict):
    run_id: str
    user_query: str

    plan: TaskPlan | None

    sql_results: list[SqlResult]
    rag_results: list[RagResult]

    analysis: AnalysisResult | None
    verification: VerificationResult | None

    approval: ApprovalResult | None

    revision_targets: list[str]

    degraded_flags: list[str]
    errors: list[WorkflowError]

    status: RunStatus
```

LLM 生成的数据对象必须全部 Pydantic 化。

---

# 11. Handoff Contract

核心对象：

```text
TaskPlan
SqlTask
SqlResult
Evidence
RagResult
AnalysisClaim
AnalysisResult
VerificationIssue
VerificationResult
ApprovalResult
```

每一个 node：

```text
声明 requires
声明 produces
声明 input schema
声明 output schema
```

统一执行：

```text
validate_input()
node()
validate_output()
```

非法：

```text
ContractViolation
```

禁止 downstream Agent：

```text
发现字段不存在后自己猜
```

---

# 12. Contract Registry

实现：

```python
NodeContract
```

例如：

```text
planner
requires:
    user_query
produces:
    plan

synthesis
requires:
    plan
optional:
    sql_results
    rag_results
produces:
    analysis
```

启动测试检查：

```text
所有 node 已注册
所有 required field 有 producer
Verifier 必须消费 AnalysisResult
Publish 必须消费 ApprovalResult
```

---

# 13. SQL Subgraph

流程：

```text
START
 ↓
schema_context
 ↓
generate_sql
 ↓
validate_sql
 ↓
execute_sql
 ↓
success?
 ├── yes → analytics → END
 └── no
       ↓
    repair_sql
       ↓
    validate
```

最大修正次数：

```text
2
```

---

# 14. SQL 安全

LLM 使用的 PostgreSQL 用户：

```text
insurance_reader
```

只授予：

```text
SELECT
```

禁止：

```text
INSERT
UPDATE
DELETE
DROP
ALTER
TRUNCATE
CREATE
```

同时用：

```text
sqlglot
```

进行 AST validation。

要求：

```text
只允许单语句
只允许 SELECT / WITH ... SELECT
强制 statement_timeout
强制最大返回行数
禁止访问系统 schema
```

因此形成：

```text
LLM constraint
+
AST constraint
+
Database permission
```

三层安全。

---

# 15. SQL Evaluation

因为 synthetic database 是固定 seed，可以建立真正的 gold result。

文件：

```text
evaluation/sql/cases.yaml
```

初始设计 80~120 个测试 case。

覆盖：

```text
single table filter
aggregation
GROUP BY
JOIN
multi-table JOIN
date range
sorting / top N
ratio
nested aggregation
unanswerable question
unsafe SQL
```

每个 case：

```yaml
id:
question:
expected_result:
tolerance:
category:
```

可选保存：

```text
gold_sql
```

但最终评价主要看：

```text
Execution Success
Result Accuracy
Unsafe Query Rejection
Repair Success
```

不要求生成 SQL 字符串与 gold SQL 完全一致。

---

# 16. RAG Pipeline

Public / Synthetic documents：

```text
Document
 ↓
Parse
 ↓
Normalize
 ↓
Section-aware Chunk
 ↓
BGE-M3
 ├── dense
 └── sparse
 ↓
Milvus
```

Query：

```text
Query
 ↓
BGE-M3
 ├── Dense Search
 └── Sparse Search
 ↓
RRF
 ↓
Top 20
 ↓
BGE-Reranker-v2-m3
 ↓
Top 5
 ↓
Evidence
```

---

# 17. Chunking

对于真实保险条款：

优先按照：

```text
第X条
X.X
章节标题
```

切块。

只有识别不到结构时，才 fallback：

```text
600~1000 Chinese characters
overlap 100~150
```

每个 chunk 必须携带：

```text
doc_id
chunk_id
title
section
page
source_type
source_name
source_url
product_code
content_hash
```

Citation 使用：

```text
doc_id + section + chunk_id
```

禁止让模型自己生成不存在的 citation。

---

# 18. Milvus Collection

```text
insurance_knowledge
```

字段：

```text
pk
doc_id
chunk_id

title
section
page

text

dense_vector
sparse_vector

source_type
source_name
source_url

product_code

content_hash
created_at
```

---

# 19. RAG Evaluation Harness

必须支持四种 pipeline：

```text
dense
sparse
hybrid
hybrid_rerank
```

统一执行：

```text
evaluation/rag/run.py
```

输出：

```text
Recall@1
Recall@5
Recall@10
MRR
```

主要测试集：

```text
FrankRin/Insur-QA
Insur-QA-Retriever.json
```

生成结果：

```text
evaluation/reports/rag_ablation.json
evaluation/reports/rag_ablation.md
```

最终 README 自动读取生成结果。

禁止手动填写 benchmark 数字。

---

# 20. Answer / Citation Evaluation

使用：

```text
Insur-QA-LLM.json
```

因为它提供：

```text
candidate passages
正确 evidence
回答
```

优先评价：

```text
correct evidence selection
citation validity
citation-in-context
```

回答文本质量可辅助使用：

```text
ROUGE
RAGAS
LLM Judge
```

但不得把 LLM Judge 作为唯一评价方式。

---

# 21. FastMCP

使用 FastMCP 4。

实现两个 MCP Server。

## mcp-data

Tools：

```text
describe_schema
execute_readonly_query
get_table_sample
compute_claim_rate
compute_loss_ratio
compute_growth
group_statistics
```

## mcp-knowledge

Tools：

```text
search_knowledge
get_chunk
get_document
get_document_metadata
```

Agent 侧使用：

```text
ClientGroup
```

统一获得 namespaced tools。

例如：

```text
data_execute_readonly_query
knowledge_search_knowledge
```

LangGraph Node 不允许：

```text
直接 import SQL repository
直接 import Milvus client
```

Agent 与外部能力之间必须经过：

```text
Tool Adapter / MCP boundary
```

---

# 22. Checkpoint

生产开发环境使用：

```text
AsyncPostgresSaver
```

`run_id`：

```text
UUID
```

并作为：

```text
thread_id
```

同一个 run 永远使用相同 thread_id。

Checkpoint 负责：

```text
graph state
interrupt state
resume
```

注意：

```text
Checkpoint != Background Scheduler
```

因此必须另外实现 runner。

---

# 23. Background Runner

禁止：

```python
asyncio.create_task(...)
```

作为唯一后台机制。

实现 PostgreSQL：

```text
agent_jobs
```

字段：

```text
id
run_id
status
attempt
locked_by
locked_until
created_at
updated_at
last_error
```

worker 使用：

```sql
FOR UPDATE SKIP LOCKED
```

获取 job。

同时采用 lease：

```text
locked_until
```

如果 worker crash：

```text
lease expiry
↓
另一 worker 接管
↓
相同 run_id/thread_id
↓
从 LangGraph checkpoint 恢复
```

---

# 24. Human-in-the-loop

Graph：

```text
Verifier PASS
 ↓
human_review
 ↓
interrupt()
```

API：

```text
POST /api/runs/{run_id}/review
```

请求：

```json
{
  "decision": "approve | reject",
  "rerun_targets": [],
  "comment": ""
}
```

Approve：

```text
Command(resume=...)
↓
Publish
```

Reject：

```text
revision_targets
↓
Revision Router
```

---

# 25. Publish Guard

系统核心不变量：

```text
No Approval → No Publish
```

实现两层。

Graph：

```text
human_review
```

是 `publish` 的唯一 predecessor。

Runtime：

```python
if approval.status != APPROVED:
    raise PublishGuardViolation
```

测试必须覆盖绕过攻击。

---

# 26. Targeted Replay

Reviewer 可以：

```text
rerun SQL
rerun RAG
rerun Synthesis
```

例如：

```text
revision_targets=["rag"]
```

则：

```text
已有 SQL artifact 保留

只重新执行：
RAG
→ Synthesis
→ Verifier
```

禁止默认：

```text
从 Planner 全流程重跑
```

---

# 27. Artifact Versioning

表：

```text
run_artifacts
```

字段：

```text
artifact_id
run_id
stage
version
content_json
content_hash
created_at
```

例如：

```text
sql_result v1
rag_result v1
analysis v1
```

RAG 重跑：

```text
sql_result v1
rag_result v2
analysis v2
```

用于证明：

```text
未被要求重跑的阶段没有漂移
```

---

# 28. Error Strategy

三层。

### Retry

适用：

```text
HTTP timeout
429
temporary DB connection error
temporary MCP error
```

使用：

```text
RetryPolicy / exponential backoff
```

---

### Agent Recovery

适用：

```text
SQL syntax error
query evidence insufficient
invalid structured output
```

例如：

```text
SQL error
→ feedback
→ SQL Agent regenerate
```

RAG：

```text
insufficient evidence
→ query rewrite
→ retrieve again
```

---

### System Fallback

例如：

```text
Reranker unavailable
```

允许：

```text
hybrid top-k directly
```

并设置：

```text
degraded_flags=["reranker_unavailable"]
```

但：

```text
Verifier unavailable
```

必须：

```text
BLOCK
```

---

# 29. Fault Injection

配置：

```text
FAULT_INJECTION_ENABLED
```

支持：

```text
llm_timeout
llm_invalid_json

postgres_timeout

milvus_timeout
reranker_failure

mcp_failure

runner_crash

verifier_failure
```

必须有：

```text
tests/fault/
```

---

# 30. Workflow Tests

关键 invariant 必须 100% 测试通过。

包括：

```text
No Approval → No Publish

Verifier Failure → Block

Invalid Handoff → Block

Unsafe SQL → Block

Runner Crash → Resume

RAG-only Replay
→ SQL artifact hash unchanged

SQL-only Replay
→ RAG artifact hash unchanged
```

这些不是 AI benchmark。

属于：

```text
software correctness
```

---

# 31. Evaluation Philosophy

本项目不要创造：

```text
Agent 总准确率 = XX%
```

项目不存在可信的统一公开端到端 benchmark。

因此正式评价体系分层：

```text
RAG Retrieval
SQL Agent
Routing / Tool Use
Generation / Citation
Workflow Reliability
Performance / Cost
```

对于最终报告本身：

承认个人项目没有：

```text
真实企业用户
生产数据
业务采纳率
人工修改率
生产事故数据
```

因此不声称：

```text
“已经验证企业价值”
```

---

# 32. Observability

表：

```text
run_events
```

字段：

```text
run_id
node
event_type
started_at
finished_at
latency_ms

model
input_tokens
output_tokens

tool_name
retry_count

error_code

input_hash
output_hash
```

前端可以通过 SSE 实时显示。

---

# 33. API

核心：

```text
POST /api/auth/login
POST /api/auth/refresh

POST /api/runs
GET  /api/runs
GET  /api/runs/{run_id}

GET  /api/runs/{run_id}/events

POST /api/runs/{run_id}/review

GET  /api/runs/{run_id}/artifacts
GET  /api/runs/{run_id}/report

POST /api/documents
GET  /api/documents

POST /api/evaluations/rag
POST /api/evaluations/sql
GET  /api/evaluations/{id}
```

SSE：

```text
GET /api/runs/{run_id}/stream
```

事件：

```text
planner.started
planner.completed

sql.started
sql.completed

rag.started
rag.completed

synthesis.completed

verification.completed

workflow.interrupted

workflow.resumed

workflow.completed
```

---

# 34. JWT / RBAC

角色：

```text
analyst
reviewer
admin
```

Analyst：

```text
run
view
```

Reviewer：

```text
run
view
approve
reject
```

Admin：

```text
document ingestion
evaluation
system administration
```

---

# 35. Vue UI

不做 ChatGPT clone。

实现四个核心页面。

## Workbench

输入分析问题。

实时展示：

```text
Planner          ✓
SQL Analysis     ✓
Knowledge RAG    ●
Synthesis        ○
Verification     ○
Human Review     ○
```

---

## Run Detail

展示：

```text
Graph Timeline
Tool Calls
Generated SQL
SQL Result
Retrieved Evidence
Agent Artifacts
Retries
Errors
Token Usage
```

---

## Review Queue

展示：

```text
Report Preview
Verification Issues
Evidence

Approve

Reject
☑ rerun SQL
☑ rerun RAG
☑ rerun Synthesis
```

---

## Evaluation

展示：

```text
RAG Recall@k
MRR

SQL Execution Rate
SQL Result Accuracy

Workflow Test Results
Latency
Token Usage
```

---

# 36. Demo 必须准备四条

### Demo A — SQL

```text
统计 2026 年第二季度华南区域不同产品的赔付率，并按从高到低排序。
```

展示：

```text
SQL generation
SQL validation
Tool call
Result
```

---

### Demo B — RAG

询问公开或 synthetic 条款问题。

展示：

```text
Dense/Sparse retrieval
RRF
Reranker
Evidence
Citation
```

---

### Demo C — Hybrid

```text
找出本季度赔付率最高的三个产品，并结合对应产品条款中的责任免除内容生成风险分析。
```

必须：

```text
SQL + RAG
→ Synthesis
→ Verification
```

---

### Demo D — HITL

生成：

```text
季度保险运营风险报告
```

Verifier PASS 后：

```text
WAITING_APPROVAL
```

只有 Reviewer approve：

```text
PUBLISHED
```

---

# 37. Repository Structure

```text
insurance-agent-harness/
│
├─ apps/
│  ├─ api/
│  ├─ runner/
│  └─ frontend/
│
├─ packages/
│  ├─ agent/
│  │  ├─ graph.py
│  │  ├─ state.py
│  │  ├─ contracts.py
│  │  ├─ registry.py
│  │  │
│  │  ├─ agents/
│  │  │  ├─ planner.py
│  │  │  ├─ data_analyst.py
│  │  │  ├─ researcher.py
│  │  │  ├─ synthesis.py
│  │  │  └─ verifier.py
│  │  │
│  │  └─ subgraphs/
│  │     ├─ sql_graph.py
│  │     └─ rag_graph.py
│  │
│  ├─ llm/
│  ├─ domain/
│  ├─ persistence/
│  ├─ retrieval/
│  └─ observability/
│
├─ services/
│  ├─ mcp_data/
│  └─ mcp_knowledge/
│
├─ data/
│  ├─ synthetic/
│  ├─ manifests/
│  └─ raw/
│
├─ evaluation/
│  ├─ rag/
│  ├─ sql/
│  ├─ generation/
│  └─ reports/
│
├─ scripts/
│
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  ├─ workflow/
│  └─ fault/
│
├─ docs/
│  ├─ architecture.md
│  ├─ data_sources.md
│  ├─ evaluation.md
│  ├─ invariants.md
│  └─ adr/
│
├─ alembic/
├─ docker-compose.yml
├─ pyproject.toml
├─ uv.lock
└─ README.md
```

---

# 38. 实施 Milestones

Codex 不允许一次完成整个项目。

必须顺序实施。

## M0 — Architecture & Skeleton

只完成：

```text
repo skeleton
pyproject
docker-compose skeleton
configuration

architecture.md
data_sources.md
evaluation.md
invariants.md

ADR
```

禁止写 Agent 大量代码。

验收：

```text
目录正确
依赖可以安装
docs 已定义边界
```

---

## M1 — Data Foundation

实现：

```text
PostgreSQL
Alembic

synthetic DB generator
synthetic documents generator

public document manifest

InsQABench downloader
Insur-QA downloader
```

验收：

```text
相同 seed 数据 hash 相同

数据库可重建

数据下载脚本可执行

不包含私人/敏感数据
```

---

## M2 — RAG

实现：

```text
parsing
chunking
BGE-M3
Milvus

dense
sparse
hybrid
RRF
rerank
```

然后先完成 evaluation。

验收：

```text
uv run python -m evaluation.rag.run
```

生成：

```text
Recall@1
Recall@5
Recall@10
MRR
```

四组 ablation 都必须输出。

在 RAG 评测没有跑通之前：

**禁止进入 Agent 编排。**

---

## M3 — SQL Agent

实现：

```text
schema introspection
SQL generation
sqlglot validation
read-only execution
repair
analytics
SQL evaluation
```

验收：

```text
pytest tests/sql
python -m evaluation.sql.run
```

必须输出真实结果。

---

## M4 — FastMCP

把已经实现好的：

```text
SQL
Analytics
Knowledge Search
```

包装成：

```text
mcp-data
mcp-knowledge
```

禁止为了 MCP 重写底层逻辑。

验收：

```text
所有 MCP tools 有独立 contract test
```

---

## M5 — LangGraph Harness

实现五 Agent。

首先：

```text
in-memory
single process
```

把 Graph 跑通。

需要：

```text
Planner
conditional routing
SQL subgraph
RAG subgraph
Synthesis
Verifier
```

验收：

四条 demo query 能正常到达对应节点。

---

## M6 — Handoff Contract

加入：

```text
Pydantic contracts
contract registry
boundary validation
contract self-test
```

必须主动注入：

```text
missing field
invalid schema
invalid tool output
```

并证明流程阻断。

---

## M7 — Durable Execution + HITL

加入：

```text
AsyncPostgresSaver
thread_id
interrupt
Command(resume)

approval
publish guard
```

验收：

```text
任务暂停
↓
重启 API
↓
重新连接
↓
approve
↓
继续执行
```

---

## M8 — Background Runner + Replay

实现：

```text
Postgres jobs
lease
worker

artifact versioning

targeted replay
```

验收：

```text
worker crash
↓
restart
↓
resume
```

以及：

```text
只 rerun RAG
↓
SQL hash 不变
```

---

## M9 — Fault Injection

实现：

```text
retry
agent recovery
fallback
fail closed

fault cases
```

Verifier failure 必须：

```text
BLOCK
```

---

## M10 — API / JWT / Vue

核心流程稳定以后再实现前端。

禁止先把时间花在 UI。

完成：

```text
Workbench
Run Detail
Review Queue
Evaluation Dashboard
```

---

## M11 — Final Evaluation & Documentation

最终运行：

```text
pytest

RAG benchmark
SQL benchmark
fault suite
workflow suite
```

生成：

```text
evaluation/reports/*
```

README 包含：

```text
Architecture
Data Sources
Demo
Evaluation
Ablation
Failure Cases
Design Decisions
Limitations
```

并明确：

```text
所有运营数据为 synthetic
公开 Benchmark 来源
公开文档来源
项目没有真实保险企业生产验证
```

---

# 39. Codex 全局约束

在任何 Milestone 中都遵守：

1. 不修改 scope，除非当前规格存在明确技术阻塞。
2. 不未经批准增加新的基础设施。
3. 每个 Milestone 必须独立可测试。
4. 所有 LLM structured output 必须 Pydantic 校验。
5. 所有 Agent handoff 必须显式 schema。
6. 统计和财务数字不得由 LLM 心算。
7. 所有 SQL 必须只读。
8. 最终事实必须可追踪到 SQL artifact 或 RAG evidence。
9. Verifier 不允许自动降级通过。
10. 未人工批准不得发布。
11. 不允许使用真实个人保险数据。
12. Benchmark 数字只能从 evaluation scripts 自动生成。
13. 不允许把 synthetic 数据描述成真实保险公司数据。
14. 不把测试写成只为了让当前实现通过。
15. 修复 bug 后必须补 regression test。
16. 每完成一个 Milestone，更新对应 docs。

---

# 40. 每个 Milestone Codex 输出格式

每次任务完成必须报告：

```text
1. Implemented
2. Files Changed
3. Architecture Decisions
4. Tests Added
5. Test Results
6. Known Limitations
7. Ready for Next Milestone?
```

禁止只回复：

```text
Done
```

---

# 41. Definition of Done

项目最终完成需要：

```text
docker compose up
```

可启动核心环境。

同时：

```text
pytest
```

通过。

拥有真实：

```text
RAG Recall@k / MRR
SQL evaluation metrics
workflow tests
fault tests
```

可以完整演示：

```text
SQL only
RAG only
SQL + RAG
HITL / resume
targeted replay
fault recovery
```

并且所有指标：

```text
可通过命令重新生成
```

---

# 42. 简历反向验收

项目做完以后，目标简历应能支撑类似以下五类内容。

### 编排运行时

代码证据：

```text
LangGraph
subgraph
conditional routing
revision edge
HITL
```

---

### Handoff Contract

代码证据：

```text
Pydantic
NodeContract
boundary validator
contract self-tests
```

---

### Durable Execution

代码证据：

```text
AsyncPostgresSaver
Postgres job runner
crash recovery
resume test
```

---

### Fault Tolerance

代码证据：

```text
retry
agent recovery
fallback
fail-closed verifier
fault injection tests
```

---

### Tool / RAG / Evaluation

代码证据：

```text
FastMCP
Milvus
BGE-M3
BGE-Reranker
Insur-QA
Recall@k
MRR
SQL evaluation
```

最终简历中的所有数字必须在：

```text
evaluation/reports/
```

找到对应结果。

任何找不到证据的数字：

**不得写入简历。**

---

# 43. 项目核心设计原则

整个项目最终围绕三个 invariant：

```text
No Valid Contract
→ No Handoff
```

```text
No Evidence
→ No Factual Claim
```

```text
No Approval
→ No Publish
```

如果这三个原则在代码、测试、UI 和文档中都能被清楚证明，则该项目达到作品集目标。
