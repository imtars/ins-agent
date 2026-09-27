# 分层评测设计

评测只报告脚本实际生成的结果，不能填写臆测数字或统一“Agent 准确率”。`evaluation/reports/` 中的报告由命令生成，连同代码哈希、数据 revision、固定切分规则、模型 revision 与参数保留 provenance；M1 未进行 AI benchmark，M2 才生成本地检索 holdout 结果。

## RAG（M2）

从 `Insur-QA-Retriever.json` 的 JSON Lines `query/pos/neg` 抽取语料，先做 Unicode NFKC 与空白规范化，再按 SHA-256 去重并生成稳定 passage ID；相同 query 的正例集合取并集。实际源文件 20,857 行中有一行空 query，预处理明确计数后排除。索引包含所有有效行的正负 passage；检索查询完整去重语料，不能只在本题候选 passage 中排序。

固定 holdout：按规范化 query 的 SHA-256 前 64 位对 10 取模为 0 的候选中，选哈希排序最前的 256 题；其余 query 是开发集。切分只用于 query，passage 全语料保持不变。两路各取 top 20，hybrid 用 `k=60` 的 RRF 融合后取 top 20，reranker 重排相同 20 条候选并取 top 10。所有四组使用同一 BGE-M3 编码、同一 Milvus 评测 collection、相同模型输入截断（每 passage 前 1,800 个 Unicode 字符和最多 512 token）。评测 collection 的 dense 索引使用精确 FLAT，运行时知识 collection 保持 HNSW；这是为消除小规模基准中的 ANN 近似变量，并未在 holdout 上调检索超参数。原始完整 passage 保存在未提交的 processed JSONL，报告记录截断数量。

Recall@k 定义为每题 top-k 命中的正例 ID 数除以该题全部正例 ID 数，再对 256 题取平均；MRR@10 取首个正例的倒数排名，10 名外记零；Hit@10 是前十至少命中一个正例的 query 比例。逐题排名 ID、原始文件与模型 revision、本地模型实际文件 SHA-256、索引参数和脚本哈希随报告保存。评测开始时重新验证 embedder 和 reranker 的身份、加载路径、文件大小与实际 SHA-256；复用 Milvus collection 时还要匹配 schema、维度、建索引配置、索引代码及编码依赖，不匹配则要求显式 `--rebuild`。最终报告在干净的源码提交上强制重建两个 collection 后生成；相同配置再强制重建一次，四组逐题排名完全一致。结果由 `python -m evaluation.rag.run` 自动生成；任一模型、数据或 Milvus 缺失时命令失败，不写模拟数字。

**数据集性质：**该文件被作者标为 retriever training data，并非官方独立测试集。项目应以固定哈希划分开发集与本地 holdout，仅在开发集调参，结果写作“Insur-QA 本地 holdout”；报告还需说明公开语料、重复 passage 和模型预训练可能带来的污染风险。若无法验证独立性，不称为无污染泛化分数。`Insur-QA-LLM.json` 的候选材料与答案可用于 evidence selection、citation validity 和 citation-in-context，答案质量另列辅助指标。

InsQABench Clause QA 的已选段落不能冒充开放检索的 Recall@k 评测；它可检验基于给定证据的回答与引用。

## SQL（M3）

以合成数据库的固定版本构造 80–120 条自然语言问题。每题给结构化 gold result、数据类型、排序语义与数值容差；`gold_sql` 可保留作为审计，不以 SQL 字符串相同为通过条件。类别覆盖过滤、聚合、多表 JOIN、日期、比率、嵌套计算、不可回答及不安全请求。报告执行成功率、结果准确率、拒绝不安全 SQL 比率和修复成功率。未运行的类别不能算作通过。

M3 casebook 目前固定为 110 题：100 题可回答、5 题不可回答、5 题不安全请求。参考 SQL 模板独立于 Agent 输出，先验证数据库与 M1 synthetic manifest 的逐表哈希，再执行参考 SQL 写入静态 `expected_result`；`tests/sql/test_gold_cases.py` 用原始合成记录在 Python 中独立计算全部 110 题的预期结果。Agent 只接收问题与数据库 schema，不读取参考 SQL 或期望结果。精确数值按容差比较，需排序的 top-N 题严格比较顺序。修复最多两次，报告同时展示首轮准确率与修复后准确率，不以修复成功掩盖首轮错误。

季度口径：保单区间 `[start_date,end_date)` 与季度区间重叠的天数为在保暴露天数；`annual_premium × overlap_days / 365` 为已赚保费。已发生赔款按 `claim_date` 落入季度、且状态为 settled/open 的 `claim_amount` 合计；拒赔金额不作为已发生赔款。现金赔付按 `payment_date` 单独统计。库中没有条款等待期的结构化字段，SQL 阶段的在保暴露不能冒称为扣等待期的可出险暴露。

M3 初始[题本](../evaluation/sql/cases_m3_v1.yaml)及[报告](../evaluation/reports/sql_evaluation_m3_v1.json)保留 85/100 的严格结果匹配成绩。错误分析发现排名题缺少中文区域到英文存储码的语义说明，且排名题的分支标识与比率题的输出形式不够明确。M3.1 在 schema context 提供 `branches.region`、`products.product_type` 和 `claims.status` 的值字典；仅明确 20 道题的输出要求，所有 reference SQL、gold、容差及严格比较器保持不变。新的[同题回归报告](../evaluation/reports/sql_evaluation.json)为 100/100 严格结果匹配和 10/10 拒绝；这反映已看过错误后的本地回归效果，不代表未见题泛化性能。旧版 15 道错题中，4 道季度比率原本的业务值正确，但被比例/百分数或附加列的严格输出判定所拒；1 道赔付汇总确实混淆了已发生赔款与实际支付金额。两次评测都没有触发修复，因此没有修复有效性的实测分数。报告中的 `requested_model` 是请求模型 ID，`observed_response_model_ids` 是代理响应声称的模型 ID，并不能证明后端权重版本；跨时结果可能变化。

## 工作流（M5–M9）

软件正确性单列：无批准不发布、Verifier 失败阻断、契约错误阻断、不安全 SQL 阻断、worker 崩溃恢复、RAG-only 重跑后 SQL artifact 哈希不变、SQL-only 重跑后 RAG 哈希不变。故障注入报告注入点、期望状态、实际状态和 trace。M11 总报告记录各 suite wall time；当前没有采集调用级延迟分布、token 或模型成本，因此不能给出 p50/p95 调用延迟或费用数字，也不能以本机时间声称实际企业价值。

## 报告生成纪律

每个命令应在缺少模型、Milvus、数据或 API Key 时明确失败，不生成看似正式的零分或模拟成绩。README 和简历只能引用有可复现命令与配置的报告。

M11 使用独立临时 worktree 重跑，不覆盖上述历史报告。[总报告](../evaluation/reports/m11_final.json)包含四组 RAG、同题 SQL、四路 workflow、定向重跑、故障套件、完整 pytest 和 Vue 构建的结果与 SHA-256。SQL 的 8,192 输出上限与 workflow 的 thinking/high、16,384 输出上限是此次评测的显式请求参数；历史默认值和 evaluator 未改。详细失败链、结果限制和复现命令见[最终验收](final_evaluation.md)。
