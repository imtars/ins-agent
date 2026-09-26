# Insurance Agent Harness

保险业务知识与运营分析多智能体工作流项目。当前处于 **M0：架构与工程骨架**；尚无 Agent、业务数据、RAG、API 或前端实现，不能用于业务决策。

完整范围见 [PROJECT_SPEC.md](PROJECT_SPEC.md)，分阶段方案见 [docs/implementation_plan.md](docs/implementation_plan.md)。所有运营数据将由固定种子生成，明确标记为 synthetic。

## 本地检查

要求 Python 3.12、uv、Docker Compose。

```bash
uv sync
uv run python -c "from packages.domain.config import get_settings; print(get_settings().llm_model)"
docker compose config --quiet
uv run pytest
```

`docker compose up -d postgres` 可启动 M0 中唯一已配置的依赖。Milvus 及应用服务将在相应里程碑加入。启动前可以复制 `.env.example` 到 `.env` 并修改本地密码；`.env` 不会提交。

## 设计文档

- [架构与阶段边界](docs/architecture.md)
- [数据来源与许可边界](docs/data_sources.md)
- [评测方法](docs/evaluation.md)
- [外部资料核对](docs/research_notes.md)
- [系统不变量](docs/invariants.md)
- [实施计划与验收](docs/implementation_plan.md)
- [ADR 0001：渐进式实施](docs/adr/0001-staged-foundation.md)

项目没有真实保险公司生产验证；任何性能数字都必须由后续评测脚本生成。
