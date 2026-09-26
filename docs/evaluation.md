# 分层评测设计

评测只报告脚本实际生成的结果，不能填写臆测数字或统一“Agent 准确率”。`evaluation/reports/` 中的报告由命令生成，连同代码版本、数据 revision、随机种子、模型 ID 与参数保留 provenance；M1 尚未进行任何 AI benchmark 或生成评测数字。

## RAG（M2）

从 `Insur-QA-Retriever.json` 的 `query/pos/neg` 抽取语料，按规范化文本去重并生成稳定 passage ID，保存每题全部正例 ID。语料包含所有正负 passage；检索时查询全语料，不能只在该题候选 passage 中排序。计算 Recall@1/5/10 与 MRR，四组分别为 dense、sparse、hybrid、hybrid + rerank。固定预处理、top-k、RRF 参数和 split，再运行最终评测。

**数据集性质：**该文件被作者标为 retriever training data，并非官方独立测试集。项目应以固定哈希划分开发集与本地 holdout，仅在开发集调参，结果写作“Insur-QA 本地 holdout”；报告还需说明公开语料、重复 passage 和模型预训练可能带来的污染风险。若无法验证独立性，不称为无污染泛化分数。`Insur-QA-LLM.json` 的候选材料与答案可用于 evidence selection、citation validity 和 citation-in-context，答案质量另列辅助指标。

InsQABench Clause QA 的已选段落不能冒充开放检索的 Recall@k 评测；它可检验基于给定证据的回答与引用。

## SQL（M3）

以合成数据库的固定版本构造 80–120 条自然语言问题。每题给结构化 gold result、数据类型、排序语义与数值容差；`gold_sql` 可保留作为审计，不以 SQL 字符串相同为通过条件。类别覆盖过滤、聚合、多表 JOIN、日期、比率、嵌套计算、不可回答及不安全请求。报告执行成功率、结果准确率、拒绝不安全 SQL 比率和修复成功率。未运行的类别不能算作通过。

## 工作流（M5–M9）

软件正确性单列：无批准不发布、Verifier 失败阻断、契约错误阻断、不安全 SQL 阻断、worker 崩溃恢复、RAG-only 重跑后 SQL artifact 哈希不变、SQL-only 重跑后 RAG 哈希不变。故障注入报告注入点、期望状态、实际状态和 trace。性能和费用报告 p50/p95 延迟、token 与模型调用成本；不以这些数字声称实际企业价值。

## 报告生成纪律

每个命令应在缺少模型、Milvus、数据或 API Key 时明确失败，不生成看似正式的零分或模拟成绩。README 和简历只能引用有可复现命令与配置的报告。
