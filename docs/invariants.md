# 系统不变量

| 不变量 | 运行时控制 | 最低验收 |
| --- | --- |
| No Valid Contract → No Handoff | 节点入口和出口按 Pydantic schema 校验；registry 检查 producer/consumer | 缺字段、错误类型、非法 tool output 被阻断 |
| No Evidence → No Factual Claim | claim 指向 SQL artifact 或当前 RAG evidence；引用 ID 必须存在 | 数字来源与 citation 注入测试 |
| No Approval → No Publish | `human_review` 为唯一图前驱；publish 运行时再校验批准状态与 reviewer 身份 | 直接调用 publish、伪造批准均失败 |

附加约束：

- Verifier 输出 `PASS / REVISE / BLOCK`；超时、异常或不合法输出一律 `BLOCK`。
- LLM 生成的 SQL 仅允许单条只读 SELECT，并受数据库最小权限与超时保护。
- synthetic 业务数据不得被表述为真实保险公司的生产数据。
- 不将未经来源验证的公开条款与合成产品事实拼接。
- 定向重跑保留未选阶段的 artifact 内容和哈希。

截至 M6，SQL 只读、知识来源、节点/工具输出结构及逐条 claim 的来源 ID、数字和原文引文已有运行时保障和测试；自然语言语义蕴含仍由 LLM Verifier 判断，不能把现有确定性检查说成完整事实证明。审批发布和定向重跑仍是后续阶段的验收目标。
