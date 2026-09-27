# M10 API contract

API 只入队、读取 checkpoint/artifact/event、提交审核；图由独立 worker 执行。`POST /api/runs/{id}/review` 调用与 M8 相同的 `RunControl.review`，审核和发布仍由 `ReviewStore` 执行当前 analysis 版本与哈希检查。API/UI 没有直接执行 `Command(resume)` 或直接插入 publication 的路径。

## Authentication and roles

`POST /api/auth/login` 接收 JSON `username/password`，返回 `access_token`、`refresh_token`、`token_type=bearer`、`role`、`username`。`POST /api/auth/refresh` 接收 `refresh_token`，原 token 在 PostgreSQL 中立即撤销，返回新令牌；重复使用旧 refresh 返回 401。access token 15 分钟，refresh token 7 天，签名算法固定 HS256，校验 issuer/audience/type/时间；角色以数据库当前值为准，不信任客户端声明。`POST /api/runs` 允许 analyst、reviewer、admin；`POST /api/runs/{id}/review` 只允许 reviewer、admin；`POST /api/documents` 和 `POST /api/evaluations/{kind}` 只允许 admin；读取需要登录。角色不足返回 403，缺失/无效 token 返回 401。

## Runs

- `POST /api/runs`: `{ "query": "..." }`，返回 202 和 `run_id/job_id/status=PENDING`。
- `GET /api/runs`: 最近 run 摘要；可过滤 job `status`。
- `GET /api/runs/{id}`: checkpoint 状态、cycle、trace、degraded flags、artifact 引用、审核与发布回执。
- `GET /api/runs/{id}/events?after_id=N`: 按递增 ID 返回持久化事件。事件包含 worker attempt、节点开始/完成、受限重试、审核、暂停和完成。payload 不存原始 prompt、token 或 SQL 结果。
- `GET /api/runs/{id}/stream?after_id=N`: 同样事件的 SSE 流；`id/event/data` 字段支持客户端按 ID 续接；每次迭代重查 access token 和账号状态。
- `GET /api/runs/{id}/artifacts`: 以 checkpoint 当前引用加载并校验内容 SHA-256；包含 SQL/tool attempts、RAG evidence、analysis 和 verification。
- `GET /api/runs/{id}/report`: 当前 analysis 的预览或已发布报告。只有 `published=true` 与 publication 回执同时存在时才表示已发布。
- `POST /api/runs/{id}/review`: `{decision,artifact_id,artifact_version,content_hash,rerun_targets,comment}`。批准不得带重跑目标。当前 run 不在人工审核态、旧 artifact、重复审核均返回 409。拒绝后的定向重跑复用 M8 guard。

## Document and evaluation scope

`GET /api/documents` 列出固定 M2 source registry；`POST /api/documents` 仅由 admin 按 `doc_id` 核对注册来源文件的 SHA-256，返回 `registration=verified_existing_source`。这是已存在文档的核验入口，尚不支持新文件上传或重新建索引。公开草案的 `status=draft`，`product_code=null`。

`GET /api/evaluations/{rag|sql}` 与 admin 的 `POST /api/evaluations/{rag|sql}` 读取仓库现有历史报告并给出文件 SHA-256，状态为 `existing_report`。POST 不启动重算。页面明确显示这一点，避免把历史评测误称为新测量。

## Local deployment boundary

Vite 开发代理和 Uvicorn 示例仅绑定 `127.0.0.1`。浏览器把令牌放在 tab 级 `sessionStorage`；M10 不声称公网部署安全基线。密钥、密码和 DeepSeek key 不进入前端 bundle 或报告。MCP 的结构化业务错误码留待平台化时升级；M10 没有改变 M9 的错误文本分类策略。
