# Insurance Agent Harness

保险业务知识与运营分析多智能体工作流项目。**M0–M5.1 已冻结，M6 handoff contract 已完成本机验收**；M1 合成数据基线保持不变。已有可重建的运营数据、条款知识索引、四组检索消融、SQL 真实模型评测、两个 MCP 工具服务和单进程 Agent 图。API 和前端尚未实现，不能用于业务决策。

完整范围见 [PROJECT_SPEC.md](PROJECT_SPEC.md)，分阶段方案见 [docs/implementation_plan.md](docs/implementation_plan.md)。所有运营数据均由固定种子生成，明确标记为 synthetic。

## 本地构建 M1

要求 Python 3.12、uv、Docker Compose。

```bash
uv sync
docker compose up -d postgres
docker compose exec -T postgres createdb -U insurance_app insurance_m1_demo
DATABASE_URL='postgresql+asyncpg://insurance_app:change-me-local-only@127.0.0.1:5432/insurance_m1_demo' uv run --locked alembic upgrade head
uv run --locked python -m scripts.generate_synthetic --database-url 'postgresql+asyncpg://insurance_app:change-me-local-only@127.0.0.1:5432/insurance_m1_demo'
```

生成器固定 `seed=202609`、版本 `1.1.0`，默认生成 12 个分支、180 个代理、10,000 个客户、12 个产品、30,000 张保单，理赔和赔付按明确规则生成。年度出险概率按有效观察天数缩放，产品模拟参数只写入 synthetic manifest，不写入业务产品表。可通过 `--branches`（4 的倍数）、`--agents`、`--customers`、`--policies` 调整规模；产品固定 12 款以对应 12 份文档。数据库表必须为空；重复运行前创建新库或重建专用数据库。文档和规范化哈希 manifest 在 `data/synthetic/`。

下载公共数据（约 1.58 GB；原始文件被 `.gitignore` 排除）：

```bash
uv run --locked python -m scripts.download_insqabench
uv run --locked python -m scripts.download_insur_qa
uv run --locked python -m scripts.download_public_documents
```

Hugging Face 下载器默认**直连、禁用代理**。若直连不可用，可先试 `--endpoint https://hf-mirror.com`；只有镜像直连也失败时才用 `--endpoint https://hf-mirror.com --transport system`。本机 2026-09-27 的直连尝试失败，实际下载经镜像与系统代理完成；manifest 如实记录 endpoint、transport、revision、文件大小、SHA-256 和抓取时间。已有文件可用 `--verify` 重新核验：

```bash
uv run --locked python -m scripts.download_insqabench --verify
uv run --locked python -m scripts.download_insur_qa --verify
M1_TEST_DATABASE_URL='postgresql+asyncpg://insurance_app:change-me-local-only@127.0.0.1:5432/insurance_m1_demo' M1_VERIFY_DOWNLOADS=1 uv run --locked pytest -q
```

M1 集成测试需要已迁移、已导入数据的专用数据库，以及已下载的公共文件；不设置两个环境变量时相关集成检查会跳过。`.env.example` 仅供本地演示，公开服务前需改密码。

## 本地构建与评测 M2

要求可运行 BGE-M3 与 BGE-Reranker-v2-m3 的 NVIDIA GPU、约 5 GB 模型下载空间，以及已按上节下载的 Insur-QA 和五份公开草案附件。`data/synthetic/manifest.json` 是开发者 provenance，**不会进入运行时知识库**。知识库只读取 `data/synthetic/documents/*.md` 中 manifest 列出的 12 份文件及 `public_docs.yaml` 明确列出的 5 份附件；Insur-QA 完整去重语料进入独立评测 collection。

```bash
uv sync --locked
docker compose -f deploy/milvus-compose.yml -p ins-agent-milvus up -d
uv run --locked python -m scripts.download_m2_models
uv run --locked python -m evaluation.rag.run
```

模型下载默认禁用代理。直连源站失败后可试 `--endpoint https://hf-mirror.com`；本机镜像直连也超时，实际使用 `--endpoint https://hf-mirror.com --transport system` 下载，revision 和逐文件 SHA-256 见 `data/manifests/bge-*_download.json`。已有模型可执行 `uv run --locked python -m scripts.download_m2_models --verify`。Milvus Compose 采用官方 2.6.24 配置；官方 MinIO 镜像已不可拉取，本项目使用固定 digest 的第三方同 release 重新打包镜像，详见 [实施记录](docs/implementation_plan.md)。服务端口仅绑定本机。

首次 `evaluation.rag.run` 会对本地 BGE-M3 与 reranker 文件重新计算 SHA-256，验证原始 Insur-QA 文件，生成完整去重语料与固定 256 题本地 holdout，解析文档，建立两个 Milvus collection，然后运行 dense、sparse、RRF hybrid、hybrid 加 reranker。评测集合使用精确 FLAT dense 索引；运行时知识集合使用 HNSW。后续运行仅在语料、模型文件、编码依赖、索引配置与索引代码哈希都匹配时复用索引；需要重建时传 `--rebuild`。自动生成的 [评测表](evaluation/reports/rag_ablation.md)、[完整参数与结果](evaluation/reports/rag_ablation.json)和逐题排名 ID 都在 `evaluation/reports/`。这是从作者标为训练数据的文件划出的本地 holdout，不能称为官方独立测试集。

<!-- RAG_ABLATION_START -->
本地 holdout：256 题；完整语料：21953 passages。

| Pipeline | Recall@1 | Recall@5 | Recall@10 | MRR@10 | Hit@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| dense | 0.0820 | 0.2314 | 0.3203 | 0.1461 | 0.3281 |
| sparse | 0.0742 | 0.2461 | 0.3525 | 0.1485 | 0.3555 |
| hybrid | 0.0938 | 0.2539 | 0.3682 | 0.1648 | 0.3750 |
| hybrid_rerank | 0.1172 | 0.2773 | 0.4082 | 0.1951 | 0.4141 |
<!-- RAG_ABLATION_END -->

可直接检查带来源、章节和 chunk ID 的检索证据：

```bash
uv run --locked python -m scripts.search_knowledge '健康尊享版等待期是多少天？' --product-code product_006
M2_TEST_MILVUS_URI=http://127.0.0.1:19530 uv run --locked pytest -q
```

完整集成测试同时设置 M1 的两个环境变量和 `M2_TEST_MILVUS_URI`。不设置时依赖外部数据库、下载文件或 Milvus 的检查会跳过。

## M3 SQL 安全与评测

使用已迁移并导入固定 M1 数据的数据库，由管理员创建权限受限的 `insurance_reader` 登录。下面的口令仅供本机示例；实际使用时自行替换。`M3_READER_DATABASE_URL` 必须以 `insurance_reader` 连接，不能传管理员账号。

```bash
export M3_READER_PASSWORD='change-me-reader-local-only'
uv run --locked python -m scripts.setup_m3_reader --database-url 'postgresql+asyncpg://insurance_app:change-me-local-only@127.0.0.1:5432/insurance_m1_demo'
export M3_READER_DATABASE_URL='postgresql+asyncpg://insurance_reader:change-me-reader-local-only@127.0.0.1:5432/insurance_m1_demo'
uv run --locked python -m evaluation.sql.prepare_cases
uv run --locked python -m evaluation.sql.run --check-gold
M3_TEST_READER_DATABASE_URL="$M3_READER_DATABASE_URL" uv run --locked pytest -q tests/sql
```

`cases.yaml` 含 110 条固定问题（100 条可回答、5 条不可回答、5 条不安全请求）及由独立参考查询产生的期望结果；测试另用 Python 对全部 gold 结果进行独立计算。reader 只获 7 张业务表的 SELECT 权限；执行器还要求单条 SELECT、业务表白名单、函数白名单、只读事务、5 秒超时和最多 200 行。季度已赚保费按保单在季度内的有效天数占 365 天的比例计算；已发生赔款排除拒赔，赔付现金按付款日期另计。`claims_per_in_force_policy_year` 使用在保天数，不能称为扣除等待期后的可出险频率。

真实 SQL Agent 评测优先连接本机 CLIProxyAPI（默认读取 `~/.cli-proxy-api/config.yaml` 的端口及 API key），固定使用 `gpt-6-luna`。`--provider auto` 在启动前发现本机服务不可用时才使用 `.env` 中的 `DEEPSEEK_API_KEY` 或 `/home/xubei/projects/jobs/dsv4_key` 首行；一次评测不会中途换模型。也可显式使用 `--provider proxy` 或 `--provider deepseek`。密钥不会写入报告。

```bash
uv run --locked python -m evaluation.sql.run --provider proxy
```

本机 CLIProxyAPI + `gpt-6-luna` 的[初始诊断报告](evaluation/reports/sql_evaluation_m3_v1.json)保留 100 道可回答题中 85 道严格结果匹配的原始成绩。M3.1 补充了业务字段值字典，明确 10 道排名题的 `branch_code` 输出和 10 道比率题的比例小数输出；gold、严格比较规则及其余 90 道题均未改变。在**同一 110 题回归集**上重新运行的[逐题报告](evaluation/reports/sql_evaluation.json)记录可回答题 100/100 执行成功且严格结果匹配，5/5 不可回答题与 5/5 危险请求均拒绝。没有触发修复，修复成功率为 `null`。这次 100% 是已分析过错误后的回归成绩，**不是未见题或泛化准确率**。报告记录请求的模型 ID、代理响应提供的模型 ID、源码和题本哈希；服务端版本未锁定，跨时结果可能变化。

## M4 FastMCP 工具服务

先按 M3 建好 `insurance_reader`，按 M2 建好运行时知识索引并下载模型。以下两个 stdio 服务可由 MCP 客户端分别启动；数据服务只接受 reader URL，知识服务启动时校验模型字节、注册文档和已有索引。

```bash
M3_READER_DATABASE_URL='postgresql+asyncpg://insurance_reader:change-me-reader-local-only@127.0.0.1:5432/insurance_m1_demo' uv run --locked python -m services.mcp_data.server
MILVUS_URI=http://127.0.0.1:19530 uv run --locked python -m services.mcp_knowledge.server
```

数据服务提供 7 个工具，知识服务提供 4 个工具；参数、返回字段、边界和错误规则见 [M4 工具契约](docs/mcp_tools.md)。M4 已通过真实 PostgreSQL、Milvus、BGE 模型和 stdio/ClientGroup 本机验收。完整本机测试需要设置 `M1_TEST_DATABASE_URL`、`M1_VERIFY_DOWNLOADS=1`、`M2_TEST_MILVUS_URI`、`M3_TEST_READER_DATABASE_URL`、`M4_TEST_MILVUS_URI`，然后执行 `uv run --locked pytest -q`；不设置外部依赖变量时相应集成测试会跳过。

## M5 LangGraph 编排

`packages/agent` 实现五个角色和四条路由：纯 SQL、纯条款检索、SQL+RAG、综合简报。混合及简报路由并行执行 SQL/RAG 子图，待两侧结果完成后才进入 Synthesis，然后由 Verification 给出 `PASS`、`REVISE` 或 `BLOCK`。Data Analyst 与 Knowledge Researcher 仅通过 M4 `ClientGroup` 的命名空间工具访问数据；Data Analyst 从 `list_tools()` 取得真实工具名称、描述和 `input_schema`。Synthesis 和 Verification 没有工具调用入口。当前图只在单进程内存中运行，没有 checkpoint、worker、人工审批或持久化 run state；严格的跨节点契约注册与完整证据校验留给 M6。

在已建好的 M1 数据库、M2 知识索引和 M3 reader 上，运行四条真实依赖 demo：

```bash
export M3_READER_DATABASE_URL='postgresql+asyncpg://insurance_reader:change-me-reader-local-only@127.0.0.1:5432/insurance_m1_demo'
uv run --locked python -m scripts.run_m5_demo --provider proxy
```

脚本连接本机 CLIProxyAPI 的 `gpt-6-luna`，通过 M4 FastMCP `ClientGroup` 调用真实 PostgreSQL 与 Milvus/BGE。默认 `--provider auto` 在启动前检测本机模型服务，不可用时才使用 DeepSeek 备用 key；不会将 key 写入报告。四条路径的计划、节点 trace、工具产物、引用、验证状态、模型响应 ID 和代码 SHA-256 记录在 [M5.1 本机 demo 报告](evaluation/reports/m51_demo.json)；[原 M5 报告](evaluation/reports/m5_demo.json)保留历史。验收脚本仅在四题均 `PASS` 时成功，`REVISE` 也视为未通过。这是流程验收，不是工作流准确率或未见题 benchmark。

## M6 Handoff contract

每个图节点在执行前后校验注册的 Pydantic 输入/输出模型。M4 SQL/知识工具返回也按类型和来源元数据校验；缺字段、类型错误或无效工具返回触发 `ContractViolation`，下游节点不会运行。Planner 的格式/任务拆分及 Synthesis 的证据草稿各最多修正一次，最终仍必须通过原契约。Synthesis 根据产物来源确定性地标注合成数据或公开草案；Verifier 批准前逐条检查 SQL 数字与被引产物、RAG 原文引文与当前 evidence。完整规则及其语义限制见 [M6 契约说明](docs/handoff_contracts.md)。当前仍无 checkpoint、审批或持久化运行状态。

在 M5 所需真实依赖可用时运行四题 M6 验收：

```bash
M3_READER_DATABASE_URL='postgresql+asyncpg://insurance_reader:change-me-reader-local-only@127.0.0.1:5432/insurance_m1_demo' uv run --locked python -m scripts.run_m6_demo --provider deepseek
```

该脚本只在四题均 `PASS` 且所有 claim 的确定性证据检查通过后生成 [M6 本机报告](evaluation/reports/m6_contract_demo.json)；原 M5/M5.1 报告不覆盖。本机 CLIProxyAPI 当前返回 429，因此 M6 使用此前配置的 DeepSeek 备用 key；JSON 请求显式设置 `reasoning_effort=high`，并给 DeepSeek 至少 8,192 token 的输出上限。设齐上述测试依赖变量后，`uv run --locked pytest -q` 执行完整本机测试。

本次从干净提交连续验收结果：SQL、RAG、BOTH、REPORT 四路全部 `PASS`，确定性证据问题均为空；DeepSeek 返回 20 次 `deepseek-flash` model ID，缺失 0、JSON 解析失败 0。完整本机真实依赖测试为 **72 passed**。这些是本机验收结果，不是 CI 或新题泛化成绩。

## 设计文档

- [架构与阶段边界](docs/architecture.md)
- [数据来源与许可边界](docs/data_sources.md)
- [合成数据的语义与限制](docs/synthetic_assumptions.md)
- [评测方法](docs/evaluation.md)
- [M4 MCP 工具契约](docs/mcp_tools.md)
- [M5.1 本机 demo 报告](evaluation/reports/m51_demo.json)
- [原 M5 本机 demo 报告](evaluation/reports/m5_demo.json)
- [M6 契约说明](docs/handoff_contracts.md)
- [M6 本机报告](evaluation/reports/m6_contract_demo.json)
- [外部资料核对](docs/research_notes.md)
- [系统不变量](docs/invariants.md)
- [实施计划与验收](docs/implementation_plan.md)
- [ADR 0001：渐进式实施](docs/adr/0001-staged-foundation.md)

项目没有真实保险公司生产验证；任何性能数字都必须由对应评测脚本生成。
