# Insurance Agent Harness

保险业务知识与运营分析多智能体工作流项目。当前完成 **M1：数据基础**；已有可重建的合成运营数据与产品文档，以及公共数据下载 provenance。尚无 RAG、Agent、API 或前端实现，不能用于业务决策。

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

生成器固定 `seed=202609`、版本 `1.0.1`，默认生成 12 个分支、180 个代理、10,000 个客户、12 个产品、30,000 张保单，理赔和赔付按明确规则生成。可通过 `--branches`（4 的倍数）、`--agents`、`--customers`、`--policies` 调整规模；产品固定 12 款以对应 12 份文档。数据库表必须为空；重复运行前创建新库或重建专用数据库。文档和规范化哈希 manifest 在 `data/synthetic/`。

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

完整 pytest 的集成测试需要已迁移、已导入数据的专用数据库，以及已下载的公共文件；不设置两个环境变量时相关集成检查会跳过。`.env.example` 仅供本地演示，公开服务前需改密码。Milvus 及应用服务属于后续阶段。

## 设计文档

- [架构与阶段边界](docs/architecture.md)
- [数据来源与许可边界](docs/data_sources.md)
- [评测方法](docs/evaluation.md)
- [外部资料核对](docs/research_notes.md)
- [系统不变量](docs/invariants.md)
- [实施计划与验收](docs/implementation_plan.md)
- [ADR 0001：渐进式实施](docs/adr/0001-staged-foundation.md)

项目没有真实保险公司生产验证；任何性能数字都必须由后续评测脚本生成。
