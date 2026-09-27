# 数据来源与用途

| 数据 | 来源 | 预定用途 | 边界 |
| --- | --- | --- | --- |
| 运营表 | 本项目固定种子 `202609` 生成 | SQL Agent、业务指标、gold result | 全部 synthetic，不含真实客户 |
| 产品条款 | 本项目生成，与产品表共用 `product_code` | SQL + RAG 跨源演示 | 文件顶部明确标记为 synthetic |
| InsQABench | [Hugging Face 数据集卡](https://huggingface.co/datasets/JaneDing2025/InsQABench) | Clause QA、领域 SQL 问题参考 | 原始保险 PDF 未公开分发；DB QA 没有可执行运营底库 |
| Insur-QA | [Hugging Face 数据集页](https://huggingface.co/datasets/FrankRin/Insur-QA) | 检索与引用评测 | `Retriever` 与 `LLM` JSON schema 不同；按文件下载 |
| 中保协公开示范条款草案 | [2026-06 定期/终身寿险征求意见页](https://www.iachina.cn/art/2026/6/10/art_24_109080.html)、[2026-09 分红型征求意见页](https://wap.iachina.cn/art/2026/9/18/art_24_109302.html) | 真实文档解析演示 | 均标记为征求意见稿；仓库不重新分发原始附件 |

InsQABench 的 `clause_train.json`、`clause_objective.json`、`clause_subjective.json`、`db_train.json`、`db_test.json` 已按文件用 `hf_hub_download` 下载；Insur-QA 的 `Insur-QA-Retriever.json` 和 `Insur-QA-LLM.json` 同样分文件下载。两套数据不得通过 `load_dataset()` 自动合并异构 JSON。M1 实测发现：`Insur-QA-Retriever.json` 实际为 **JSON Lines**（逐行含 `query/pos/neg`），而 `Insur-QA-LLM.json` 是 JSON 数组；`clause_subjective.json` 是按文档键组织的 JSON 对象，不是数组。M2 已按 Retriever 文件真实结构解析；后续处理其他文件时仍须按各自格式实现，不能只凭 `.json` 后缀或数据集卡的概括假设统一结构。

两套 HF 数据的实际 repo revision、每文件大小、SHA-256、抓取时间及 endpoint 位于 `data/manifests/insqabench_download.json` 和 `data/manifests/insur_qa_download.json`。大文件保存在忽略目录 `data/raw/benchmarks/`，仓库不再分发。

M2 的 BGE-M3 与 BGE-Reranker-v2-m3 也按不可变 repo revision 逐文件下载；模型文件位于忽略目录 `data/models/`，SHA-256、下载 endpoint 与 transport 位于 `data/manifests/bge-*_download.json`。运行时知识索引只由 `packages/knowledge/documents.py` 中的显式注册入口构造：12 份 synthetic Markdown 与五份已核验公开附件。`data/synthetic/manifest.json`、下载 provenance、模拟参数和整个 `data/` 树都不能被递归扫描进知识库。Insur-QA passage 单独进入 `insur_qa_corpus` 评测 collection，不能冒充正式产品条款。

已核验的五份协会附件位于 `data/manifests/public_docs.yaml`，均标为 **draft**；源网页及其对应 DOCX/PDF 直链都已验证可返回预期文件头。来源页未明确标注许可证，清单以 `license: not_stated_on_source_page` 和 `usage_note` 如实记录；静态清单仅记核验日期。`scripts/download_public_documents.py` 实际下载后将重定向后的 URL、SHA-256、大小与每次抓取时间写入 `data/manifests/public_docs_download.json`。原始附件位于忽略目录 `data/raw/public/`，不与 synthetic products 自动关联，也不随仓库再分发。

[中保协 2023 年报道](https://www.iachina.cn/art/2023/8/17/art_22_107111.html) 中的 40,005 款是 **截至 2023-07-31** 的产品库数量，不能描述成当前数量。产品库是查询来源，不等于可批量下载或自由再分发的语料库。

合成数据使用 `SEED=202609` 和生成器版本 `1.1.0`。相同版本、配置和种子下，按表名顺序及主键顺序把行规范化为 UTF-8 JSON（金额去掉无意义尾零、日期 ISO 格式），结合文档和模拟参数哈希生成总哈希；`generated_at` 不进入哈希。`data/synthetic/manifest.json` 记录配置、表数、每表和每文档哈希、内部模拟参数及总哈希。规则是：risk_score 越高年度出险概率越高；机动车险在 south 区域概率更高；health/life 产品 55 岁及以上概率更高；四类产品有不同基础频率与赔款基数，产品变体有不同内部频率/金额因子；年度概率再按有效暴露天数缩放。随机波动只用于个体抽样，不代表真实精算模型。具体语义和限制见 [合成数据说明](synthetic_assumptions.md)。任何修改 schema 或分布的提交都应更新生成器版本及后续 gold result。

参考源码只学习设计，不复制实现。公开数据和文档的使用仍应遵循原始来源的许可证与使用条款。
