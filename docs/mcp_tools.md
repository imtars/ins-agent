# M4 FastMCP 工具契约

两个服务使用锁定的 FastMCP 4.0.10，均通过 stdio 暴露。工具只调用 `packages/sql` 与 `packages/knowledge` 中的能力；统计聚合和文档读取的可复用函数也位于这两个包，不在 MCP 层另写业务查询或检索。输入日期是 ISO `YYYY-MM-DD`，期间统一为 `[start_date, end_date)`。无效参数、权限拒绝、缺失文档或依赖服务故障通过 MCP tool error 返回，不伪装为空成功结果。

## mcp-data

仅接受 `M3_READER_DATABASE_URL` 中的 `insurance_reader` 账号；PostgreSQL 角色只拥有 7 张业务表的 SELECT。任意 SQL 仍经过现有 `sqlglot` 单条 SELECT、表/函数白名单、只读事务、5 秒超时及 200 行上限。

| Tool | 输入 | 输出与边界 |
| --- | --- | --- |
| `describe_schema` | 无 | `schema`：实际业务列、外键、枚举值含义。 |
| `execute_readonly_query` | `sql` | `rows`、`row_count`；只读且最多 200 行。非法 SQL 报 tool error。 |
| `get_table_sample` | `table`、`limit=5` | `table`、按 `id` 排序的 `rows`、`row_count`；表名须在白名单，limit 为 1–20。 |
| `compute_claim_rate` | `start_date`、`end_date`、可选 `product_code`、`region` | `value`、理赔数、在保保单年等；单位为每在保保单年理赔数，**不扣结构化等待期**。 |
| `compute_loss_ratio` | 同上，另有 `kind=incurred\|paid` | `value` 为比例小数，包含分子金额及已赚保费。已发生赔款与已支付赔款分别取数。 |
| `compute_growth` | `previous_start/end`、`current_start/end`，可选指标与过滤器 | 两个同长度、不重叠期间的指标及增长比例；上一期为零时 `growth_rate=null`。 |
| `group_statistics` | `start_date`、`end_date`、`group_by=all\|product_code\|region`、可选过滤器 | `rows`、`row_count`；先汇总原始金额与暴露，再统一计算比率，最多 12 产品或 4 区域分组。 |

`region` 只接受 `north/south/east/west`；`product_code` 由既有 analytics 层验证。期间需为正且不超过 366 天。没有在保暴露时，单值指标报错；分组工具返回空 `rows`。增长率使用 `(current - previous) / abs(previous)`，量化为 4 位小数。

## mcp-knowledge

仅使用 `insurance_knowledge` collection。启动时重新校验 BGE-M3、reranker 的本地文件哈希、注册的 12 份合成产品与 5 份公开草案源文件，以及 M2 知识索引 marker；不会暴露独立的 Insur-QA benchmark collection。

| Tool | 输入 | 输出与边界 |
| --- | --- | --- |
| `search_knowledge` | 1–1000 字符的 `query`、可选 `product_code`、`limit=5` | `evidence`、`count`；沿用 BGE-M3 dense/sparse、RRF 和 reranker，limit 为 1–10，证据含来源、章节、chunk ID、文本和 score。 |
| `get_chunk` | 24 位十六进制 `chunk_id` | 单个索引 chunk、`evidence_id` 与来源；校验内容哈希和注册来源，不允许指定其他 collection。 |
| `get_document` | 已注册 `doc_id`、`offset=0`、`limit=20` | 按原始章节顺序返回 `chunks`、总数和 `next_offset`；读取前验证源文件 SHA-256，单页最多 20 个 chunk。 |
| `get_document_metadata` | 已注册 `doc_id` | 标题、来源、URL、原文件 SHA-256、产品码和 `synthetic/draft` 状态；公开草案的产品码为 null。 |

通过 FastMCP `ClientGroup`，11 个工具分别以 `data_`、`knowledge_` 前缀呈现。M4 测试通过实际 FastMCP client 调用检查工具列表、参数验证、结果字段、SQL 权限、错误传递与知识来源；另实际启动两个 stdio 进程作连通性检查。M4 没有实现 Agent 节点、HTTP 公网入口或新的评测分数。
