# 数据来源与用途

| 数据 | 来源 | 预定用途 | 边界 |
| --- | --- | --- | --- |
| 运营表 | 本项目固定种子 `202609` 生成 | SQL Agent、业务指标、gold result | 全部 synthetic，不含真实客户 |
| 产品条款 | 本项目生成，与产品表共用 `product_code` | SQL + RAG 跨源演示 | 文件顶部明确标记为 synthetic |
| InsQABench | [Hugging Face 数据集卡](https://huggingface.co/datasets/JaneDing2025/InsQABench) | Clause QA、领域 SQL 问题参考 | 原始保险 PDF 未公开分发；DB QA 没有可执行运营底库 |
| Insur-QA | [Hugging Face 数据集页](https://huggingface.co/datasets/FrankRin/Insur-QA) | 检索与引用评测 | `Retriever` 与 `LLM` JSON schema 不同；按文件下载 |
| 中保协公开示范条款草案 | [2026-06 定期/终身寿险征求意见页](https://www.iachina.cn/art/2026/6/10/art_24_109080.html)、[2026-09 分红型征求意见页](https://wap.iachina.cn/art/2026/9/18/art_24_109302.html) | 真实文档解析演示 | 均标记为征求意见稿；仓库不重新分发原始附件 |

InsQABench 的 `clause_train.json`、`clause_objective.json`、`clause_subjective.json`、`db_train.json`、`db_test.json` 将按文件用 `hf_hub_download` 下载并记录 revision、SHA-256 与下载日期。Insur-QA 的 `Insur-QA-Retriever.json` 和 `Insur-QA-LLM.json` 同样分文件下载。两套数据不得通过 `load_dataset()` 自动合并异构 JSON。上述数据规模以数据集卡为准，程序运行时再以文件内实测记录数校验。

原始公共文档清单将在 M1 写入 `data/manifests/public_docs.yaml`，记录出版机构、来源网页、直接文件 URL、文件类型、文件状态（草案/正式）、抓取时间、许可证或使用说明、SHA-256。只下载公开可访问的附件，下载失败和改版须显式报告。原始文件位于忽略目录 `data/raw/public/`。M0 已核验上述来源页，具体附件直链尚未逐个核验，因此当前不填占位 URL。

[中保协 2023 年报道](https://www.iachina.cn/art/2023/8/17/art_22_107111.html) 中的 40,005 款是 **截至 2023-07-31** 的产品库数量，不能描述成当前数量。产品库是查询来源，不等于可批量下载或自由再分发的语料库。

合成数据的固定种子只保证相同生成器版本与配置的确定性。M1 应给生成器版本、配置、排序规则和规范化哈希；任何修改 schema 或数据分布的提交都应更新 gold result。设计上植入客户风险、产品赔付频率与赔付金额、区域和年龄等可解释模式，避免纯随机噪声。

参考源码只学习设计，不复制实现。公开数据和文档的使用仍应遵循原始来源的许可证与使用条款。
