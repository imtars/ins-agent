# 实施计划与验收

本计划将 [PROJECT_SPEC.md](../PROJECT_SPEC.md) 落成可独立验收的任务。M0–M9 已通过本机验收；M10 的 API/UI 正在本机验收，M11 尚未开始。每一步失败都停在本阶段修复，不能以 mock 输出替代外部服务或评测结果。

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

`uv run --locked python -m evaluation.rag.run` 实际完成 dense、sparse、hybrid、hybrid_rerank 四组评测，逐题排名和自动生成的 Recall@1/5/10、MRR@10 见 [报告](../evaluation/reports/rag_ablation.md)与 [参数及哈希](../evaluation/reports/rag_ablation.json)。例如当次 hybrid_rerank 的 Recall@10 为 0.4043、MRR@10 为 0.1947；这是固定 256 题的**本地 query holdout**，原始文件被作者标为训练数据，不能称为官方独立测试集或无污染泛化结果。真实知识检索命令返回 `product_006` 的“等待期 15 天”章节和可追踪证据 ID。设置 M1 数据库、下载文件与 M2 Milvus 环境变量后，当次完整 `pytest -q` 为 **19 passed**；`docker compose config --quiet` 与 `git diff --check` 同时通过。此为 M2 初次验收记录，最终报告以后续 M2.1 强制重建结果为准。

## M2.1 索引来源与引用修正（2026-09-27）

源码审查发现，原 index marker 只覆盖语料和模型权重，无法证明旧 Milvus collection 使用当前索引参数及编码实现；PDF 每页重新设为“前言”，会把跨页条款续文标错章节。已将 schema 版本、向量维度、实际建索引参数、索引源码 SHA-256、全部模型文件 SHA-256 与编码依赖版本纳入 marker 校验，并增加错误 marker 拒绝复用的单元测试。PDF parser 在换页时保留上一条款标题，直到遇到新标题；两页续文测试验证页码与 section 均正确。

在干净的源码提交 `ccd84c9` 上执行 `uv run --locked python -m evaluation.rag.run --rebuild`，两个 collection 均从头建立，实际行数分别为 21,953 和 365；Milvus 返回的 dense HNSW/COSINE (`M=16`, `efConstruction=200`) 与 sparse `SPARSE_INVERTED_INDEX`/IP (`drop_ratio_build=0.0`) 均与 marker 一致。报告记录 `git_worktree_dirty=false`，5 个源码哈希及逐题排名哈希均重新核对一致。完整测试为 **21 passed**。本次 dense Recall@10 为 **0.3047**、hybrid 为 **0.3604**、hybrid_rerank 为 **0.4043**，后者 Hit@10 为 **0.4102**。前两项与初次报告不同；旧 collection 缺少足够的构建 provenance，无法判定差异的唯一原因。当次 HNSW 报告现已由 M2.2 的精确检索报告取代。256 题留出集未用于调参，512 token 限制及训练数据来源仍是已记录的评测限制。M2.1 修正完成，未进入 M3。

## M2.2 模型字节与评测复现收尾（2026-09-27）

审查确认索引 marker 原先只比对模型 manifest 所写的 SHA-256，未重新读取本地模型文件。正式评测及运行时知识检索现在加载模型前校验 BGE-M3 和 BGE-Reranker-v2-m3 的固定 repo/revision、完整文件列表、实际加载路径、文件大小、逐文件 SHA-256；报告保存两组已验证的文件哈希，并纳入验证实现的源码 SHA。单元测试验证字节被改动和 manifest 指向错误路径时均会失败。

先在干净源码提交 `f3e0daf` 上保持 HNSW 配置再次 `--rebuild`：与上一轮相比，256 题中 dense 排名变化 3 题、sparse 0 题、hybrid 1 题、rerank 13 题；完整 rankings SHA 不同。不能将全部重排变化归因于 HNSW，但该配置下的排名级复现未通过。随后仅把评测集合 dense 索引改为精确 `FLAT/COSINE`，运行时知识集合继续用 `HNSW/COSINE`；两者 sparse 都是 `SPARSE_INVERTED_INDEX/IP`。Milvus 实际 `describe_index` 和两集合行数（21,953 / 365）与配置一致。

在干净源码提交 `91addef` 上**连续两次**强制重建并运行四组消融。两次的 `rag_rankings.jsonl` SHA-256 都是 `7dc23f5af2b6aa89dbad69550b4714666bb3ccd8f4563c14da1eae61bb25ea88`，四条路线的逐题排名差异均为 0；报告均记录 `git_worktree_dirty=false`。最终报告以第二次重建为准：dense / sparse / hybrid / hybrid_rerank 的 Recall@10 分别为 **0.3203 / 0.3525 / 0.3682 / 0.4082**，最终重排的 MRR@10 为 **0.1951**、Hit@10 为 **0.4141**。这些是固定 256 条本地 query holdout 的结果，模型预训练及数据集来源的限制仍适用；未调 512 token、候选数或 RRF 参数。M2 评测封存，未进入 M3。

## M3 SQL 构建与当前验收（2026-09-27）

在固定 M1 数据库中实际建立 `insurance_reader` 登录，只授予 7 张业务表 SELECT，重复运行权限配置脚本成功。直接用该账号查询 `has_table_privilege` 得到 SELECT=true、INSERT/UPDATE/DELETE=false；尝试绕过 AST 直接 INSERT 仍由 PostgreSQL 拒绝。SQL 执行器要求专用账号、单条 SELECT/CTE、业务表和函数白名单；在只读事务内设置 5 秒语句超时，最多返回 200 行。危险 SQL、系统 schema、锁定读取及副作用函数均有拒绝测试。schema introspection 从实际 information_schema 生成。

`evaluation/sql/cases.yaml` 由手工设计的参考查询模板及固定数据库实际结果生成，共 110 题，覆盖过滤、聚合、多表连接、日期区间、分组排名、嵌套聚合和季度赔付率；100 题可回答，5 题不可回答，5 题不安全请求。生成前逐表核对 M1 manifest 哈希；实际运行 `python -m evaluation.sql.run --check-gold` 校验全部静态结果；独立 Python oracle 对全部 110 题复核通过。初始题本已保留在 `cases_m3_v1.yaml`，SHA-256 为 `704f344bc6f07b1630c075750eceaf17250fedb0833c8fe58f83df42be5668f6`；M3.1 仅明确 20 题自然语言输出约定，当前题本 SHA-256 为 `2e9a164a588e8b1f88e8e5b4fb7bb6e9469767e1fa31429b4bfeb875714be4b4`。季度已赚保费使用在保天数 / 365；已发生赔款排除 denied，赔付现金按付款日另算。当前 SQL 业务表没有结构化等待期，因此在保暴露指标不能声称是等待期后可出险暴露。

在干净源码提交 `0a59370` 上，以本机 CLIProxyAPI 的 `gpt-6-luna` 执行初始 110 题诊断，结果为 100 道可回答题全部执行成功、85 道严格结果匹配；5 道不可回答题与 5 道危险请求全部拒绝。15 道严格判错中，10 道排名题缺少区域值语义，4 道比率题涉及表示形式或附加列，1 道赔付汇总混淆了已发生与已支付金额。原报告逐字节保存在 `evaluation/reports/sql_evaluation_m3_v1.json`。这次属于本机验收，不能当作 CI 状态。

## M3.1 数据字典与同题回归（2026-09-27）

在 `schema_context()` 提供中文区域名到英文存储码的映射，同时列出产品类型和理赔状态的实际枚举；模型系统提示明确要求用存储码过滤。排名题明确要求 `branch_code`，季度赔付率题明确要求单列比例小数。仅这 20 题的问题文本改变；全部 110 题的 gold、参考 SQL、排序和容差完全一致，严格比较器未改。测试核对数据字典与实际数据库枚举一致，原题本和报告均归档；抽查失败后进一步明确映射方向，再从干净源码提交 `e8c92d7` 实际运行完整 110 题回归。

最终[回归报告](../evaluation/reports/sql_evaluation.json)记录 `git_worktree_dirty=false`、源码/题本/原报告 SHA-256，`requested_model=gpt-6-luna`、`observed_response_model_ids=[gpt-6-luna]`、110 次响应均有模型 ID。可回答题执行成功及严格结果匹配均为 **100/100**，首轮也是 100/100；不可回答与危险请求均为 **5/5** 拒绝。两轮都未触发修复，修复成功率保持 `null`。独立核验 110 个 ID、逐类结果、源码与题本哈希；`--check-gold` 验证 110 题；完整本机测试在 M1 数据、下载文件、Milvus 与 M3 reader 可用时为 **44 passed**。DeepSeek 备用 key 未使用。**100% 是针对已分析错误的同题回归结果，不能称为未见题或泛化成绩**；代理返回的模型 ID 也不能证明底层权重版本。M3 工程验收与 M3.1 回归完成，可以进入 M4，本轮停在 M3.1。

## M4 FastMCP 包装与验收（2026-09-27）

在现有 FastMCP 4.0.10 锁定依赖上实现 `mcp-data` 与 `mcp-knowledge` 共 11 个工具。数据服务构造时拒绝非 `insurance_reader` URL；任意 SQL 继续走 M3 的 AST 校验、数据库只读事务、5 秒超时与 200 行上限。新统计能力放在 `packages/sql/analytics.py`，MCP 仅调用它，先合计原始暴露与金额再计算理赔率、赔付率和增长率。知识服务的检索仍走 M2 的 BGE-M3 dense/sparse、RRF 和 reranker；文档/chunk 读取只使用显式注册的运行时知识源，启动时验证模型文件、源文件与索引 marker，不暴露 Insur-QA benchmark collection。详细输入、输出和错误规则见 [工具契约](mcp_tools.md)。

使用 FastMCP `Client` 通过真实 reader 数据库调用全部 7 个数据工具；使用真实 Milvus 2.6、BGE-M3 与 reranker 调用全部 4 个知识工具，并验证检索证据与 chunk、公开草案 draft 状态、非法参数和未知文档的错误。两个 stdio 进程实际启动并完成工具调用；`ClientGroup` 列出 11 个 `data_` / `knowledge_` 命名空间工具并路由两端调用。完整本机测试在 M1/M2/M3/M4 真实依赖变量齐备时为 **50 passed**；这是 local acceptance，不是 CI status。M4 PASS，停止在 M4，尚未实现 LangGraph 节点。

## M5 单进程 LangGraph 编排与验收（2026-09-27）

主图包含 Planner、SQL/RAG 两个专家子图、Synthesis 和 Verification。Planner 选择 `SQL`、`RAG`、`BOTH`、`REPORT`；后两条由 LangGraph 并行分支和显式 join 合流。Data Analyst 通过 M4 `data_` 工具获得 schema 与业务数值，Knowledge Researcher 通过 `knowledge_` 工具取得条款证据；Synthesis 和 Verification 没有工具调用入口。图在进程内保存本次状态、trace 和产物，不创建 checkpoint、worker、HITL 或持久化 run state。M5 有最小的 Pydantic 输出模型和来源 ID 检查；完整 handoff registry、逐声明证据验证与故障注入仍属于后续里程碑。

四条真实 demo 使用本机 CLIProxyAPI `gpt-6-luna`、M1 synthetic PostgreSQL reader、M2 Milvus/BGE 和 M4 FastMCP `ClientGroup`，完整逐条记录见 [M5 demo 报告](../evaluation/reports/m5_demo.json)。第一轮连续运行发现混合题把“已发生赔付率”错误选择成理赔频率，Verification 返回 `REVISE`；此反馈用于明确两种指标的工具语义，并增加错误工具选择的确定性修复测试。最终验收从修正后的干净源码提交重新运行，保留模型响应中的 `REVISE` 或 JSON 格式重试记录，不把四题 demo 当成准确率 benchmark。所有测试与路由结果均为本机验收，尚无 CI status；代理报告的模型 ID 不能证明底层权重版本。

最终从干净提交 `c4004ed` 连续运行：SQL `1 SQL / 0 RAG`、RAG `0 / 1`、混合 `1 / 1`、简报 `2 / 2`，四题均在预期节点后各执行一次 Synthesis 和 Verification，状态均为 `PASS`。报告记录 `git_worktree_dirty=false`、源码 SHA-256、24 次模型响应（均报告 `gpt-6-luna`，模型 ID 缺失数 0），本次 JSON 解析重试数 0；混合题实际调用 `incurred_loss_ratio`，简报同时调用 `incurred_loss_ratio` 和 `claims_per_in_force_policy_year`。独立脚本复核四题路由、节点集合、产物数量、引用 ID、指标工具、源码哈希及报告基线提交。真实依赖环境下完整 `pytest -q` 为 **60 passed**。这只是四个预设流程示例的本机验收，不能推断回答准确率或泛化能力。后续审查指出下述两个 M5.1 收尾点，因此 `4332091` 保留为 M5 初版历史，不作为最终冻结点。

## M5.1 MCP 工具契约消费与严格 demo 判定（2026-09-27）

审查原 M5 报告发现 4 个 SQL task 的第一次工具参数均被 M4 拒绝，第二次靠错误反馈修正。Data Analyst 当时只接收业务 schema 和手写工具用途，没有收到 MCP 的真实输入契约。M5.1 改为在任务开始时调用 `ClientGroup.list_tools()`，选取五个数据工具的实际 namespaced 名称、描述及完整 `input_schema` 放入 Data Analyst context；缺少任一工具契约直接失败。原有业务指标语义 guard 和有限 repair 仍在，M4 的 Pydantic 工具校验仍是执行边界。真实 `ClientGroup` 集成测试核对 `sql` 必填、`additionalProperties=false`、赔付率工具没有 `metric` 入参；单元测试验证五份契约确实传入模型。

验收脚本现在要求每条 demo 的 Verification 状态严格为 `PASS`，`REVISE` 与 `BLOCK` 都使脚本失败，且不会写入新的成功报告；状态判定有独立回归测试。原 [M5 报告](../evaluation/reports/m5_demo.json)不覆盖，M5.1 另写 [新报告](../evaluation/reports/m51_demo.json)。不把 demo 当成准确率 benchmark，也不宣称 CI 通过。

从干净提交 `edcb75e` 连续重跑真实四题：SQL `1 SQL / 0 RAG`、RAG `0 / 1`、混合 `1 / 1`、简报 `2 / 1`，路由、合流和最终状态均正确，四题严格 `PASS`。共 4 个 SQL task，**4/4 第一次工具调用成功**，修复次数为 0；前轮 4/4 第一次参数拒绝的问题未再出现。报告记录 `git_worktree_dirty=false`、7 份源码 SHA-256、19 次模型响应均报告 `gpt-6-luna`、模型 ID 缺失 0、JSON 解析失败 0。独立复核四题节点集合、引用 ID、业务指标、源码哈希与基线提交，原 M5 报告 SHA-256 保持 `de6943c9e67bc7aa23d58b56b11f623948d0b3ee05c1ca50499d4068c869a608`。真实依赖环境下完整本机 pytest 为 **64 passed**。这些是固定四题的流程验收事实，不能据此声称新题泛化或底层模型权重版本。M5/M5.1 PASS，可以冻结并进入 M6；本轮未实现 M6。

## M6 Handoff contract 与验收（2026-09-27）

实现 `NodeContract` 注册表，为主图和两个专家子图的所有节点声明必需/可选输入、输出、Pydantic schema 及上游关系。构图时核对实际节点集合、类型匹配的 producer 和 Verifier 对 `AnalysisResult` 的消费；每个节点执行前后统一验证，失败抛 `ContractViolation`，下游不运行。SQL 五种工具结果、知识搜索结果及 Evidence 也按 Pydantic 验证，M4 返回缺字段、错误类型、数量不符、内容哈希不符等直接阻断，不把服务端坏输出当作 LLM 参数错误进行修复。

Verifier 批准前逐条校验 claim 的当前来源 ID：SQL 数字必须在所引工具产物/参数中出现；RAG claim 必须在正文与 `evidence_quote` 放入所引 evidence 的同一段原文，且数字来自该证据；摘要不能新增结果数字。确定性注入测试覆盖注册表缺项、无 producer、缺字段、错误 schema、无效工具输出、未知引用 ID、错误数字和虚构引文。详细边界与语义限制见 [M6 契约说明](handoff_contracts.md)。固定四题的真实依赖验收独立写入 [M6 报告](../evaluation/reports/m6_contract_demo.json)，不覆盖 M5/M5.1 历史报告。

首轮真实连续试跑在混合题由 LLM Verifier 返回 `REVISE`：摘要未明确说明运营数据为合成数据，条款 claim 未标明其来源是合成演示产品。验收脚本按 strict PASS 规则失败，没有写出报告。修正为由当前 SQL/RAG artifact 的已验证来源字段确定性地给摘要和各 claim 加上来源标识；不改数值、引文或 citation 判定。混合题单独复测 `PASS`，随后再从新干净提交执行四题完整验收。

第二轮完整试跑前三题均 `PASS`，简报题被重复 evidence ID 检查误判 `BLOCK`：两个检索任务实际命中同一 chunk。修正为仅在同一 ID 对应不同来源或内容时阻断；相同来源同内容允许重复命中，rerank 分数可因 query 而异。新增相同/冲突两种确定性回归测试，再重新执行完整验收；失败的两轮均未写入 M6 成功报告。

第三轮本机 CLIProxyAPI 在混合题 Synthesis 返回 HTTP 429；立即重试又在 Planner 返回 429，均未写报告。按用户已有的备用密钥配置，改用 DeepSeek `deepseek-flash` 实测，密钥请求有效且响应模型 ID 正确。首次 DeepSeek 连续试跑的 SQL 题 `PASS`，RAG 题的 Synthesis 因输出上限达到 `finish_reason=length` 停止。核对 DeepSeek 官方文档后发现该模型默认开启思考模式；曾试过对 DeepSeek 关闭思考的短 JSON smoke 请求，随后按用户要求改为显式开启思考、`reasoning_effort=high`，并将输出上限设为至少 8,192 token。CLIProxyAPI 请求不变。provider 参数和请求体均有测试，再从干净提交用 DeepSeek 执行四题完整验收。

显式 `high` 的首轮完整 DeepSeek 试跑在纯 SQL 题由 Verifier 返回 `REVISE`：Synthesis 摘要提及公开咨询草案与合成产品的关系，而本轮没有检索任何公开资料。严格验收未写报告。Synthesis 现在只按本轮实际 SQL/RAG 产物列出可描述的来源种类；没有公开草案 evidence 时，不再要求提及公开草案。新增纯 SQL 的 prompt/摘要回归测试后重新运行。

下一轮纯 SQL 题 `PASS`，纯 RAG 题由 Verifier 返回 `REVISE`：草稿把“疾病责任等待期”泛化成了未限定责任的等待期；Verifier 还错误要求 RAG 条款中明确写出的“15 天”必须另有 SQL 查询支持。修正为 RAG claim 对外采用当前 evidence 的确切引文，保留责任限定；Verifier 提示明确区分运营指标数字（SQL）与条款原文数字（RAG），纯 RAG 路径不额外查询 SQL。两项均有确定性测试。失败运行未写 M6 成功报告。

随后连续试跑前三题 `PASS`，简报题先后暴露两个输出问题：Planner 一次返回额外 `type` 字段，严格 `TaskPlan` 校验拒绝；Data Analyst 在同时询问已赚保费、已发生赔款和已发生赔付率时误选分组统计工具。Planner 现在对 schema 校验失败最多请求一次格式修正，第二次仍不合法则失败；Data Analyst 明确使用可一次返回三个值的 `compute_loss_ratio(kind=incurred)`。两处均保留原严格 contract，并增加回归测试。简报单题再次试跑时，摘要中的无引用结果数字被证据检查正确阻断；提示现要求数值仅出现在有引用的 claim 中。

进一步单题诊断发现 SQL claim 写入 `sql:3` 来源标签时，数字检查误把标签序号 `3` 当作业务结果。解析器现在忽略 `sql:<序号>` 标签的序号，但仍检查正文里的真实数字是否来自所引 SQL artifact；新增合法标签和虚构数值的对照测试。上述失败均未写 M6 成功报告。

随后简报单题还暴露 Planner 过度拆分：一次生成 6 个 SQL task、4 个 RAG task，超出问题本身所需，并使 Synthesis 生成没有业务数字的 SQL claim。Planner 现在要求合并单个分析工具可返回的相关指标，每个分支最多两个 task，超限时最多修正一次。Synthesis 对确定性证据检查失败也最多修正一次，只可引用同一批产物；仍失败则由原 Verifier 校验返回 `BLOCK`，不会放宽数值或引用规则。两条界限有确定性测试。

最终从干净基线 `8c3639ef69700c4045314001bc5d4286f2f7ff76` 连续执行四条真实 DeepSeek 路径：SQL `1 SQL / 0 RAG`、RAG `0 / 1`、BOTH `1 / 1`、REPORT `2 / 2`，四条均 `PASS`，每条只经过一次 Synthesis 和 Verifier，确定性 evidence issues 全部为空。报告记录 `git_worktree_dirty=false`、10 个节点的输入/输出 schema hash、8 份源码 SHA-256、20 次模型调用均报告 `deepseek-flash`，model ID 缺失 0、JSON 解析失败 0；独立复算源码与 schema hash、逐题证据校验均吻合。完整本机真实依赖 pytest 为 **72 passed**。这是固定四题的本机流程与契约验收，不是 CI 或新题泛化成绩。M6 PASS，可冻结并进入 M7；本轮未实现 M7。

## M7 PostgreSQL checkpoint、人工审核与发布保护

持久化模式在原 M5/M6 图的 Verifier 后增加 `human_review` 和 `publish`；只有 Verifier `PASS` 才进入 `interrupt()`，其余状态直接结束。图使用 `AsyncPostgresSaver`，`run_id` 是 UUID 并在 Planner、审核、发布节点强制等于 `thread_id`。重启后的服务重新构建同一图，并用同一线程的 `Command(resume={"approval_id": ...})` 恢复；无需重跑 Planner/SQL/RAG/Synthesis/Verifier。原非持久化图及 M6 报告保留。

审核决定写入独立 PostgreSQL 数据库的 `agent_approvals`，唯一键为 `run_id`；`human_review` 在恢复时核对数据库决定和 resume ID。`publish` 是唯一后继，运行时再次检查 Verifier `PASS`、`ApprovalResult`、数据库 reviewer 与审批 ID，并以 `run_id` 为幂等键写入 `agent_publications`，同一内容重复执行返回同一回执。checkpoint 序列化只允许项目实际用到的状态模型。审批表和发布表由 M7 初始化过程创建，业务 M1 数据库不增加表。

`apps/api/m7.py` 仅提供绑定本机的创建、查询、审核入口，审核身份由服务端固定 reviewer ID 与独立 token 确定；它不是 M10 的 JWT/RBAC 公共 API。M7 不提供后台 worker、作业 lease、版本化 artifact、定向重跑或前端。拒绝审核只结束为 `REJECTED`，对应的重跑路由留给 M8。真实进程重启验收脚本与本机 PostgreSQL 集成测试须证明：暂停后新 API 进程能读原 checkpoint；无 token、伪造 resume、伪造 reviewer、非 PASS 和直接 publish 均被拒；批准后只生成一次发布回执。

第一轮真实两进程验收使用 M6 的宽泛综合简报题，Verifier 返回 `REVISE`，因此图正确结束而没有进入人工审核，脚本失败且未写报告。checkpoint 显示 Planner 自行把原题扩展出“平均有效保单数”和“等待期例外”两个额外要求，现有 SQL/条款产物未覆盖；这属于回答计划与证据范围问题，不是 checkpoint 故障。M7 验收题现明确限定为三个已有工具可支持的输出：已发生赔付率、按在保保单年计的理赔频率、疾病责任等待期。M7 用它检验持久化/HITL，不把单题成功解释为工作流泛化成绩；失败运行的 checkpoint 仍保留在本机专用库供诊断。

最终从干净源码提交 `82053a57c2581fd2743f1e754bef68dbda332c7d` 运行真实 DeepSeek、M4 MCP、PostgreSQL reader 与 Milvus/BGE：第一个独立 Uvicorn 进程完成 `REPORT → SQL/RAG → Synthesis → Verifier PASS`，在 `human_review` 中断，返回 `WAITING_APPROVAL`。实际停止该进程后启动第二个进程，按相同 `thread_id` 读回原 checkpoint 和 trace；无效审核 token 得到 HTTP 403，合法 reviewer 批准后 `Command(resume)` 到达 `publish`，状态为 `PUBLISHED`。第二个进程在恢复前后模型响应计数均为 0，trace 中 `human_review` 和 `publish` 各一次，数据库只有 1 条匹配的 approval 和 publication。首次进程 5 次模型响应均报告 `deepseek-flash`，model ID 缺失 0。独立复核 [M7 报告](../evaluation/reports/m7_durable_demo.json)中的 7 份源码哈希、checkpoint 状态、数据库回执与历史 M6 报告哈希；完整本机测试为 **75 passed**，包括真实 PostgreSQL checkpoint、伪造 resume/reviewer、非 PASS、直接 publish、拒绝审核和幂等重试测试。M7 PASS，可冻结并进入 M8；本轮不实现 M8。

## M8 验收记录（2026-09-27）

M8 另建专用图与 `agent_jobs`、`run_artifacts`、`m8_reviews`、`m8_publications`，保留 M7 历史路径。`agent_jobs` 使用 `FOR UPDATE SKIP LOCKED` 和有时限的 lease；worker 在知识服务初始化完成后认领 job，执行期间续租。checkpoint 只保存引用，artifact 按 `(run_id, stage, generation)` 幂等写入，实际内容使用规范化 JSON 的 SHA-256 核验。每个拒绝并重跑的审核轮次增加 generation 与 stage version。审批和发布均绑定当前 analysis 的 ID、版本与哈希。图继续经 M4 ClientGroup 调用 SQL/RAG 工具，Synthesis/Verifier 无工具权限。

第一次真实进程验收暴露出认领 lease 早于 BGE/Milvus 初始化，4 秒 lease 在 Planner 前过期；没有生成成功报告。调整为初始化后认领，并从空 M8 演示库重跑。最终 [M8 报告](../evaluation/reports/m8_runner_replay.json)记录：第一个 worker 在 Planner checkpoint 后主动退出，第二个 worker 租约到期接管同一 `run_id/thread_id`（attempt 2），Planner trace 只有一次；初稿进入 `WAITING_APPROVAL`。Reviewer 拒绝并指定只重跑 RAG 后，RAG/Analysis/Verification 版本变为 v2，SQL ID、版本和 SHA-256 均与 v1 完全一致，Planner 与 SQL 专家 trace 未增加。旧 analysis 审批得到 HTTP 409，新版本批准后生成唯一发布回执。恢复、重跑、发布 worker 分别记录 4、3、0 次 `deepseek-flash` 响应；崩溃进程的 Planner 响应未计入。完整本机测试 **78 passed**，其中 M8 PostgreSQL 测试覆盖租约接管、旧 worker 拒绝、同阶段幂等、artifact 篡改检测、RAG 单阶段重跑及版本绑定审批。此为本机验收，不是 CI、性能指标或新题泛化结果。M8 PASS，停在 M8；M9 尚未实现。

补强发布时对 PostgreSQL 实际 `content_json` 的哈希复核后，第一次重新验收在 RAG 定向重跑后的 Verifier 返回 `BLOCK`：其模型调用抛出 `RuntimeError`，图按 fail-closed 规则终止，没有发布，也没有生成新的成功报告。保留该本机失败记录；再次从空演示库运行同一脚本后通过。当前 M8 不实施 M9 的自动故障重试或降级；单次外部模型失败仍会让该 run 结束为 `BLOCK`。

## M9 故障注入实施与验收

M9 的故障只由 worker 本地 `FAULT_INJECTION_ENABLED=1` 开启，API 不接受故障参数。HTTP 超时、429/部分 5xx、暂时性 MCP 故障和数据库连接错误最多重试 3 次并指数退避；业务参数/契约错误不做传输层重试。现有 SQL 反馈修正、RAG 空结果重查、JSON 解析重试继续属于 Agent 自身恢复。Verifier 故障或无效输出始终 `BLOCK`，不自动降级通过。仅 reranker 失败时允许使用已取得的 dense/sparse RRF 候选，RAG artifact 和 run 状态记录 `reranker_unavailable`。worker 为每个 run 持有 PostgreSQL advisory lock，保证旧 worker 与接管者不会同时写 checkpoint；artifact 写入仍以 generation 去重。

`tests/fault` 覆盖八类配置故障、429 的真实 HTTP 客户端响应路径、真实 PostgreSQL statement timeout、真实 Milvus/BGE reranker 降级、worker 进程崩溃、artifact 写入后的进程崩溃、租约接管竞争和重复 publish。受控故障用 stub 模型/工具验证确定性状态，不把它们称作真实 DeepSeek/Milvus 故障。最终脚本另对真实 DeepSeek Planner 注入一次本地超时，随后继续调用真实 MCP、业务 PostgreSQL 和 Milvus/BGE，并要求人工审核前 `PASS`、审核后唯一发布；成功才写 [M9 故障报告](../evaluation/reports/m9_fault_injection.json)。最终本机验收中，该超时触发 1 次重试，随后 5 次响应均报告 `deepseek-flash`，批准后的 worker 模型响应为 0；`tests/fault` 为 **28 passed**，完整 pytest 为 **106 passed**。故障发生于受控注入点，不能宣称 DeepSeek 或 Milvus 在验收期间实际故障；本机测试也不是 CI。M9 PASS，停在 M9；M10 尚未实现。

## M10 API/UI 实施与验收

M10 通过共用 `RunControl` 复用 M8 的创建、状态读取与版本绑定审核。FastAPI 提供 JWT 登录和一次性 refresh 轮换；角色信息每次从 PostgreSQL 重查。worker 在独立 `run_events` 表保存节点开始/完成、受控重试、审核暂停、恢复和完成事件，事件带 job ID、attempt 和时间戳。API 从当前 checkpoint 引用加载并验证 artifact；SSE 按递增 event ID 推送。Vue 工作台提供 Workbench、Run Detail、Review Queue、Evaluation 四页，审核按钮始终调用服务端审核接口。前端构建不含 JWT secret、DeepSeek key 或 reviewer 密码。

M10 只核验 M2 已注册文档的 SHA-256，保留公开草案 `draft` 状态；新文件上传与重建知识索引尚未实现。评测接口读取已有 RAG/SQL 报告和文件哈希，明确返回 `existing_report`，不会把历史报告伪装成新评测。模型 token 使用量未持久化，UI 当前显示可用的 attempts、tool arguments、results、trace、retry events 和 degraded flags。M9 的文本错误分类未在本阶段修改；将来若接入第三方 MCP 服务，再考虑机器可读业务错误码。

M10 gate：从空专用数据库启动 HTTP API、登录 analyst、创建综合 run、拒绝 analyst 审核、DeepSeek worker 经真实 PostgreSQL/Milvus/BGE/MCP 执行并在注入一次 Planner timeout 后留下持久化 retry event、读取预览/artifact/SSE、reviewer 拒绝过期版本而批准当前版本、再次执行 worker、确认唯一 publication 与报告 `published=true`；并运行完整 pytest 与 Vue build。演示脚本只在全部断言通过后写独立 [M10 报告](../evaluation/reports/m10_api_ui_demo.json)。状态与结果以实际脚本输出和报告为准。

## 关键实施细节

**M1。** 建表顺序为分支、代理、客户、产品、保单、理赔、赔付；外键、日期、保费/赔款非负约束由迁移定义。生成器将批量记录排序后序列化并计算 hash，配置和生成器版本一并写入 manifest。公开文档 URL 必须从来源页核实，不能只从聊天摘录复制。

**M2。** `pos/neg` 可能重复，先规范化再映射稳定 ID，并保存一个 query 的多正例集合；评测必须在完整去重语料上检索。公共 PDF/DOCX 文档走解析与条款切块，Insur-QA 已有 passage 不再伪造页码。Milvus 2.6 standalone 的 etcd/MinIO 属其官方依赖，采用官方 Compose 后以实际容器启动检查。

**M3。** `sqlglot` 只负责 AST 检查，最终安全还依赖数据库授权、事务只读、超时、行数和表白名单。业务指标在 SQL/Python 计算，Synthesis 不能从结果文本自行心算。gold result 由独立、人工审阅的确定性查询生成，不能从待测 Agent 的输出倒推。

**M5–M8。** 图节点每次只产生明确契约输出，SQL/RAG 并行结果在合流时验证。对 `interrupt()` 的恢复采用同一 `thread_id`；节点恢复可能重复执行，因此所有写入使用幂等键。Checkpoint、job lease、artifact 各有独立职责；三者之间的恢复与一致性需集成测试覆盖。

**M10–M11。** UI 直接呈现状态、证据、SQL、重试和人工审核；前端不能绕过服务端 guard。报告标记 synthetic 与真实公开数据的界线，所有简历指标指向可再生成的 `evaluation/reports` 文件。
