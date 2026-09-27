# 实施计划与验收

本计划将 [PROJECT_SPEC.md](../PROJECT_SPEC.md) 落成可独立验收的任务。当前已完成 M2 RAG 检索与评测，尚未进入 M3。每一步失败都停在本阶段修复，不能以 mock 输出替代外部服务或评测结果。

| 阶段 | 具体交付 | 进入下一阶段的门槛 |
| --- | --- | --- |
| M0 架构 | 目录、`pyproject.toml`/`uv.lock`、PostgreSQL Compose、配置、设计文档 | `uv sync`、配置 smoke、`docker compose config` 通过 |
| M1 数据 | Alembic schema、固定种子生成器、12 个合成产品文档、公开文档清单、两个 HF 数据集分文件下载脚本 | 重建 DB 后规范化哈希一致；外部下载记录 revision/hash；无私人数据 |
| M2 RAG | 解析与条款切块、BGE-M3 dense/sparse、Milvus 2.6、RRF、reranker | 用固定本地 holdout 运行四组消融，自动产出 Recall@k/MRR；失败不可伪造分数 |
| M3 SQL | schema introspection、生成/验证/执行/最多两次修复、确定性指标 | 只读用户权限与危险 SQL 测试通过；80–120 个 gold result case 自动评测 |
| M4 MCP | 将既有 SQL、analytics、knowledge 能力包装为两个 FastMCP server | 每个 tool 的输入、输出、权限、异常 contract test 通过 |
| M5 Graph | 单进程 LangGraph、五角色、SQL/RAG 子图、分流和合流 | SQL、RAG、混合、报告四条 demo 走到预期节点，结果可追踪 |
| M6 Contract | Pydantic handoff、NodeContract registry、边界验证、自检 | 缺字段、错误 schema、无效 tool 输出阻断；producer/consumer 关系可审计 |
| M7 持久化与 HITL | `AsyncPostgresSaver`、固定 thread ID、interrupt/resume、审批 guard | 进程重启后批准可继续；无批准的直接 publish 调用被拒 |
| M8 Worker 与 Replay | PostgreSQL lease 队列、worker、artifact 版本、定向重跑 | crash/lease expiry 恢复；未选阶段 hash 原样保留 |
| M9 故障注入 | LLM/DB/Milvus/MCP/runner/verifier 故障，重试与降级策略 | 每种故障有预期状态断言；Verifier 失败必定 BLOCK |
| M10 API/UI | JWT/RBAC、REST/SSE、Vue 四页 | 从创建 run 到 review/publish 的完整人工演示可执行 |
| M11 收尾 | 全套 pytest、RAG/SQL/workflow/fault 报告、README 和演示 | 命令可重现报告，Docker Compose 可启动核心环境，已知限制列明 |

## 当前 M0 验收记录

M0 只验证项目结构、安装和配置；不宣称数据、Agent 或端到端 demo 已可运行。安装与检查命令见 README。重依赖 `FlagEmbedding`、PyTorch、Milvus SDK、DOCX/PDF 解析工具在 M2 按实际实现加入并锁定，避免 M0 提前下载模型或引入未经验证的版本。前端依赖在 M10 锁定。

2026-09-27 本地验收：`uv sync --python python3.12` 成功并生成 `uv.lock`；`uv run --locked pytest -q` 为 2 passed；`docker compose config --quiet` 成功；PostgreSQL 17.6 容器启动并达到 healthy，检查后已停止。尚未验证 Alembic 迁移或业务表，因为这些属于 M1。

## M1 验收记录（2026-09-27）

在新建的 `insurance_m1_acceptance` 数据库上执行 `alembic upgrade head`，导入默认规模的合成数据，随后**删除并重新创建同名数据库**，再次执行迁移和生成。第二次建库前查询 `pg_tables` 为 0。两次的 `generator_version`、`schema_revision`、配置、7 张表的规范化 SHA-256、12 份文档 SHA-256 和总 SHA-256 均完全一致：`076771b1ccdea1ee376d2ce4a464ea1ec588acd668970a4c87a8c11b072d1d8a`。此为 M1 原版历史验收值，当前 manifest 已由下述 M1.1 验收取代。

外部文件实际下载并逐个复核：InsQABench 5 个、Insur-QA 2 个、协会草案附件 5 个。HF 源站直连 `Network is unreachable`，镜像站直连超时；镜像 `https://hf-mirror.com` 经系统代理成功，两个 repo revision 与官方 API 当次返回一致。LFS 文件同时比对上游 SHA-256；非 LFS 文件记录本地 SHA-256 与仓库 revision。下载元数据见 `data/manifests/*_download.json`，原始第三方文件未提交。完整 pytest 在 `M1_TEST_DATABASE_URL` 与 `M1_VERIFY_DOWNLOADS=1` 下为 13 passed。

## M1.1 数据语义修正与验收（2026-09-27）

审查发现原版对长短暴露保单使用相同年度出险概率、把内部模拟因子放在 `products` 表，以及 12 份文档实质仅有 4 类相似正文。修正后按有效观察天数缩放年度概率；健康险等待期不计入可出险天数；迁移 `20260927_02` 从业务表移除模拟因子，并把它们及其他模拟参数写入 synthetic manifest 与总哈希；12 款产品的责任、免除、免赔/自付和限额具有明确差异，名称使用中性版本。具体模型及限制见 [合成数据说明](synthetic_assumptions.md)。

在已导入旧数据的库上，`20260927_01 → 20260927_02` 升级成功，旧业务记录仍可查询。随后在 `insurance_m1_acceptance` **两次删除并重建数据库**，每次新库的公共表数为 0；两次均从头运行 `alembic upgrade head` 和默认规模生成器。版本 `1.1.0`、schema `20260927_02`、配置、7 张表的行数与规范化哈希、12 份文档哈希、模拟参数哈希及总哈希完全一致。最终有 12 分支、180 代理、10,000 客户、12 产品、30,000 保单、4,306 理赔、4,297 赔付；模拟参数哈希 `c1cdd9519ec269f486783af076f27da35acdfc7d8236b7ff4f5ccc8442ddfcb7`，总哈希 `0d84ea6e0a252bd3f5212ddd610918bfd69f9ebbc49864b6a9a87aa56b5b3be0`。数据库反读后的逐表哈希与生成结果一致；集成测试核对外键、约束、产品字段、等待期和 12 个 `product_code` 文档映射。

中保协五份草案附件再次直连下载，文件 SHA-256 与静态清单一致，下载 provenance 记录本次抓取时间和使用说明。两个 HF 数据集文件沿用 M1 实际下载结果，本次完整测试重新计算其本地 SHA-256、检查 revision 与 provenance；未重新声称联网下载。完整测试命令：

```bash
M1_TEST_DATABASE_URL='postgresql+asyncpg://insurance_app:change-me-local-only@127.0.0.1:5432/insurance_m1_acceptance' M1_VERIFY_DOWNLOADS=1 uv run --locked pytest -q
```

验收结果：15 passed。数据库和第三方原始下载文件只保存在本机；默认无数据库/下载文件环境变量时，相关集成测试会跳过。本项目尚无 CI 可独立复现上述本地验收。

## M2 RAG 验收记录（2026-09-27）

保持 M1.1 synthetic 总哈希 `0d84ea6e0a252bd3f5212ddd610918bfd69f9ebbc49864b6a9a87aa56b5b3be0` 不变。运行时知识入口明确注册 12 份 synthetic Markdown 和 5 份已核验中保协草案附件，按标题/条款结构切块，长段回退为 900 字符、120 字符重叠；实际解析得到 365 块，带 `doc_id`、章节、页码（DOCX/Markdown 无页码）、来源、`product_code` 和内容哈希。`data/synthetic/manifest.json` 只用于验证文件名与 SHA-256，**不进入向量语料**。公开草案没有 `product_code`；Insur-QA 单独放入 `insur_qa_corpus`，不混进运行时 `insurance_knowledge`。

Insur-QA Retriever 原始文件 SHA-256 与 M1 provenance 一致。20,857 行中实际有 1 行空 query，记录后排除；NFKC/空白规范化与去重得到 19,942 个 query、21,953 个唯一 passage。按固定 query 哈希规则选 256 题本地 holdout，其余 19,686 题为开发集；全部正负 passage 都进入检索语料。300 个超过 1,800 字符的 passage 在模型输入和 Milvus `text` 中截断，完整规范化文本保留在本地 processed JSONL。四组都检索同一 collection：dense 与 BGE-M3 sparse 各取 top 20，RRF `k=60` 融合后取 top 20，再用 BGE-Reranker-v2-m3 重排。每题全部正例 ID 都参与 Recall@k 计算。

Milvus 2.6.24、etcd 和同 release MinIO 实际启动并达到 healthy；两个 collection 的真实记录数分别为 21,953 和 365。官方 2.6.24 Compose 中的 Docker Hub MinIO 镜像目前无法拉取，Quay 同名 tag 在本机返回 unauthorized；使用并固定 `tobi312/minio` 的同 release 镜像与 digest，容器内版本实际核对。BGE 模型源站与 `hf-mirror.com` 直连均超时；经镜像和系统代理按固定 revision 下载，逐文件 SHA-256 已核验。上述真实数据源差异与替代来源见 [核对记录](research_notes.md)。

`uv run --locked python -m evaluation.rag.run` 实际完成 dense、sparse、hybrid、hybrid_rerank 四组评测，逐题排名和自动生成的 Recall@1/5/10、MRR@10 见 [报告](../evaluation/reports/rag_ablation.md)与 [参数及哈希](../evaluation/reports/rag_ablation.json)。例如 hybrid_rerank 的 Recall@10 为 0.4043、MRR@10 为 0.1947；这是固定 256 题的**本地 query holdout**，原始文件被作者标为训练数据，不能称为官方独立测试集或无污染泛化结果。真实知识检索命令返回 `product_006` 的“等待期 15 天”章节和可追踪证据 ID。设置 M1 数据库、下载文件与 M2 Milvus 环境变量后，完整 `pytest -q` 为 **19 passed**；`docker compose config --quiet` 与 `git diff --check` 同时通过。M2 门槛满足，尚未实现 SQL Agent 或后续阶段。

## 关键实施细节

**M1。** 建表顺序为分支、代理、客户、产品、保单、理赔、赔付；外键、日期、保费/赔款非负约束由迁移定义。生成器将批量记录排序后序列化并计算 hash，配置和生成器版本一并写入 manifest。公开文档 URL 必须从来源页核实，不能只从聊天摘录复制。

**M2。** `pos/neg` 可能重复，先规范化再映射稳定 ID，并保存一个 query 的多正例集合；评测必须在完整去重语料上检索。公共 PDF/DOCX 文档走解析与条款切块，Insur-QA 已有 passage 不再伪造页码。Milvus 2.6 standalone 的 etcd/MinIO 属其官方依赖，采用官方 Compose 后以实际容器启动检查。

**M3。** `sqlglot` 只负责 AST 检查，最终安全还依赖数据库授权、事务只读、超时、行数和表白名单。业务指标在 SQL/Python 计算，Synthesis 不能从结果文本自行心算。gold result 由独立、人工审阅的确定性查询生成，不能从待测 Agent 的输出倒推。

**M5–M8。** 图节点每次只产生明确契约输出，SQL/RAG 并行结果在合流时验证。对 `interrupt()` 的恢复采用同一 `thread_id`；节点恢复可能重复执行，因此所有写入使用幂等键。Checkpoint、job lease、artifact 各有独立职责；三者之间的恢复与一致性需集成测试覆盖。

**M10–M11。** UI 直接呈现状态、证据、SQL、重试和人工审核；前端不能绕过服务端 guard。报告标记 synthetic 与真实公开数据的界线，所有简历指标指向可再生成的 `evaluation/reports` 文件。
