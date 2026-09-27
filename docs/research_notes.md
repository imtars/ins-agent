# M0 外部资料核对（2026-09-27）

本文件记录会影响实施的已核事实与本项目的推论。动态 API 和公开附件状态在对应里程碑再次核验。

| 主题 | 已核事实 | 项目处理 |
| --- | --- | --- |
| [InsQABench](https://huggingface.co/datasets/JaneDing2025/InsQABench) | 数据集卡列出 Clause/DB QA 文件及数量；原始保险 PDF 未获再分发授权 | Clause 用于回答评测，DB 用于领域 SQL 参考；自建业务库 |
| [Insur-QA](https://huggingface.co/datasets/FrankRin/Insur-QA/viewer) | `Retriever` 与 `LLM` 文件字段不同，自动合并的 viewer 报 schema 错误 | 分文件下载；检索结果标为本地 holdout |
| [DeepSeek 模型](https://api-docs.deepseek.com/quick_start/pricing/) | 当前文档要求模型名 `deepseek-flash`，旧 `deepseek-v4-flash` 作为兼容别名 | 默认配置使用 `deepseek-flash`；Provider 接口保留替换能力 |
| [LangGraph Postgres checkpointer](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-postgres/langgraph/checkpoint/postgres/aio.py) | `AsyncPostgresSaver` 提供异步 checkpoint，首次需 `setup()` | M7 实测重启、interrupt/resume 与幂等；另建 job queue |
| [Milvus hybrid](https://milvus.io/docs/multi-vector-search.md)、[RRF](https://milvus.io/docs/rrf-ranker.md) | 支持 dense/sparse 多向量检索与 RRF | M2 实测四组消融，不能预设 hybrid 必胜 |
| [FastMCP 4](https://blog.gofastmcp.com/3mufbh2vcv22o) | 2026-08-31 已发布稳定版 | M4 再核对 ClientGroup API 后封装现有服务 |
| [中保协 2026 寿险附件](https://www.iachina.cn/art/2026/6/10/art_24_109080.html)、[分红型附件](https://wap.iachina.cn/art/2026/9/18/art_24_109302.html) | 两页均称附件为征求意见稿 | 清单和 UI 标为草案，不能宣称正式生效 |

参考项目也已核到：[insurance-policy-rag](https://github.com/i-hridaysaha/insurance-policy-rag) 使用 Sentence-BERT/FAISS/BM25 的混合检索与引用校验；迁移其评测思路时要适配本项目的 BGE-M3/Milvus，不复制其代码。[enterprise-workflow-agent-platform](https://github.com/smlfy/enterprise-workflow-agent-platform) 有 LangGraph 持久化、HITL 和 trace replay 的设计参考。[superMEW](https://github.com/gioqnc/superMEW) 含 Redis，而本项目规格明确排除 Redis，因此只参考前后端与检索展示方式。

推论：最大的交付风险不在五个 Agent 的 prompt，而在可复现数据、真实检索评测、SQL 权限和 checkpoint/job/artifact 的一致性。里程碑顺序因此先验证这些基础能力，再接入编排。

## M1 实际下载补充

2026-09-27 实际拉取的 `Insur-QA-Retriever.json` 是 JSON Lines，每行独立含 `query/pos/neg`；`Insur-QA-LLM.json` 是 JSON 数组；InsQABench 的 `clause_subjective.json` 是按文档键组织的 JSON 对象。文件格式与扩展名、数据集展示层的统一 schema 不完全一致，解析器应按各文件的真实结构实现。两套 HF repo 的不可变 revision 和逐文件哈希见 `data/manifests/*_download.json`。

中保协两份 2026 年修订寿险草案和三份分红型草案的来源页、附件直链均已实际验证；下载后的 SHA-256、抓取时间见 `data/manifests/public_docs_download.json`。五份文件都仍标为 `draft`。

M1.1 再次直连下载五份附件，固定 SHA-256 未变化。静态 `public_docs.yaml` 增加来源页未标明许可证的记录及本地研究使用说明；实际 `retrieved_at` 只保留在本次下载 provenance 中。未因公开征求意见附件而推断可重新分发的许可。

## M2 实际源站与容器核对

2026-09-27，BGE 模型源站和 `hf-mirror.com` 直连超时；镜像经系统网络代理下载成功，并按文件核对 SHA-256。Milvus 官方 2.6.24 Compose 可从 GitHub 获取，但其中的 Docker Hub `minio/minio:RELEASE.2024-12-18T13-15-44Z` 已无法拉取；[Milvus 上游 issue](https://github.com/milvus-io/milvus/issues/53430) 也记录了该仓库不可用。Quay 同名 tag 在本机匿名拉取返回 unauthorized。项目仅替换为 `tobi312/minio` 同一 release 的镜像；容器内 `minio --version` 实测为 `RELEASE.2024-12-18T13-15-44Z`，etcd 与 Milvus 仍使用官方 Compose 的版本。该镜像不是官方发布渠道，实际部署时应重新评估来源。
