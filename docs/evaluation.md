# 分层评测设计

评测只报告脚本实际生成的结果，不能填写臆测数字或统一“Agent 准确率”。`evaluation/reports/` 中的报告由命令生成，连同代码哈希、数据 revision、固定切分规则、模型 revision 与参数保留 provenance；M1 未进行 AI benchmark，M2 才生成本地检索 holdout 结果。

## RAG（M2）

从 `Insur-QA-Retriever.json` 的 JSON Lines `query/pos/neg` 抽取语料，先做 Unicode NFKC 与空白规范化，再按 SHA-256 去重并生成稳定 passage ID；相同 query 的正例集合取并集。实际源文件 20,857 行中有一行空 query，预处理明确计数后排除。索引包含所有有效行的正负 passage；检索查询完整去重语料，不能只在本题候选 passage 中排序。

固定 holdout：按规范化 query 的 SHA-256 前 64 位对 10 取模为 0 的候选中，选哈希排序最前的 256 题；其余 query 是开发集。切分只用于 query，passage 全语料保持不变。两路各取 top 20，hybrid 用 `k=60` 的 RRF 融合后取 top 20，reranker 重排相同 20 条候选并取 top 10。所有四组使用同一 BGE-M3 编码、同一 Milvus collection、相同模型输入截断（每 passage 前 1,800 个 Unicode 字符和最多 512 token）。原始完整 passage 保存在未提交的 processed JSONL，报告记录截断数量。

Recall@k 定义为每题 top-k 命中的正例 ID 数除以该题全部正例 ID 数，再对 256 题取平均；MRR@10 取首个正例的倒数排名，10 名外记零；Hit@10 是前十至少命中一个正例的 query 比例。逐题排名 ID、原始文件与模型 revision、哈希、索引参数和脚本哈希随报告保存。复用 Milvus collection 时还要匹配 schema、维度、建索引配置、索引代码及编码依赖；不匹配则要求显式 `--rebuild`。最终报告在干净的源码提交上强制重建两个 collection 后生成。结果由 `python -m evaluation.rag.run` 自动生成；任一模型、数据或 Milvus 缺失时命令失败，不写模拟数字。

**数据集性质：**该文件被作者标为 retriever training data，并非官方独立测试集。项目应以固定哈希划分开发集与本地 holdout，仅在开发集调参，结果写作“Insur-QA 本地 holdout”；报告还需说明公开语料、重复 passage 和模型预训练可能带来的污染风险。若无法验证独立性，不称为无污染泛化分数。`Insur-QA-LLM.json` 的候选材料与答案可用于 evidence selection、citation validity 和 citation-in-context，答案质量另列辅助指标。

InsQABench Clause QA 的已选段落不能冒充开放检索的 Recall@k 评测；它可检验基于给定证据的回答与引用。

## SQL（M3）

以合成数据库的固定版本构造 80–120 条自然语言问题。每题给结构化 gold result、数据类型、排序语义与数值容差；`gold_sql` 可保留作为审计，不以 SQL 字符串相同为通过条件。类别覆盖过滤、聚合、多表 JOIN、日期、比率、嵌套计算、不可回答及不安全请求。报告执行成功率、结果准确率、拒绝不安全 SQL 比率和修复成功率。未运行的类别不能算作通过。

## 工作流（M5–M9）

软件正确性单列：无批准不发布、Verifier 失败阻断、契约错误阻断、不安全 SQL 阻断、worker 崩溃恢复、RAG-only 重跑后 SQL artifact 哈希不变、SQL-only 重跑后 RAG 哈希不变。故障注入报告注入点、期望状态、实际状态和 trace。性能和费用报告 p50/p95 延迟、token 与模型调用成本；不以这些数字声称实际企业价值。

## 报告生成纪律

每个命令应在缺少模型、Milvus、数据或 API Key 时明确失败，不生成看似正式的零分或模拟成绩。README 和简历只能引用有可复现命令与配置的报告。
