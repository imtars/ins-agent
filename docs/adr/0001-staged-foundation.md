# ADR 0001：按依赖顺序实现和验证

- 状态：接受（M0）
- 日期：2026-09-27

## 决策

先建立数据和独立可量化的检索、SQL 能力，再接入 MCP 和 LangGraph。每个阶段都必须有可执行验收，之后才进入下一阶段。M0 仅建立可安装的 Python 工程、PostgreSQL Compose 骨架、配置、目录与架构文档；不写 Agent 占位实现。

核心环境使用 PostgreSQL 与 Milvus。Milvus standalone 需要自身的 etcd 与 MinIO 组件，M2 引入时采用对应 2.6 版本的官方 Compose 文件并核对资源需求；这些仅是 Milvus 的依赖，不作为新增业务基础设施。BGE/FlagEmbedding 的大型模型依赖也推迟到 M2，以保持 M0 安装和检查轻量。

## 原因

InsQABench 没提供可直接运行的业务底库，SQL 的可复现性依赖先构建合成数据。RAG 若没有独立检索评测，很难判断 Agent 表现下降是检索问题还是编排问题。Checkpoint 不解决后台调度，因此持久化图与作业队列分阶段实现。

## 后果

M0 的 `docker compose up` 只启动 PostgreSQL；完整系统的 Definition of Done 到 M11 才满足。每次里程碑结束时更新文档与验收结果，禁止把未实现组件写成已可用。
