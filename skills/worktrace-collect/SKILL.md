---
name: worktrace-collect
description: 只读采集、诊断和完整拼接本机多种 Coding Agent 的会话记录，生成 signals.json、coverage.md 与 brief-context.md，不调用模型、不总结内容。用户要求检查 WorkTrace 来源覆盖、接入新的 Coding Agent、排查缺失会话、只收集原始对话、查看采集统计或验证完整性时使用。
---

# WorkTrace 会话采集与诊断

先把本 `SKILL.md` 所在目录记为 `SKILL_DIR`。使用其中的 `scripts/worktrace.py` 调用同一仓库的 Python 运行时。

## 操作

- 初始化用户级配置：`python3 <SKILL_DIR>/scripts/worktrace.py setup`
- 环境、来源与可选 CLI 后端诊断：`python3 <SKILL_DIR>/scripts/worktrace.py doctor`
- 采集一天：`python3 <SKILL_DIR>/scripts/worktrace.py scan --day today`
- 采集一周：`python3 <SKILL_DIR>/scripts/worktrace.py scan --week this-week`
- 从匹配的 `signals.json` 构建全文：`python3 <SKILL_DIR>/scripts/worktrace.py context --day <日期> --input <signals.json>`
- 精确选择来源：为 `scan` 增加 `--connectors codex_cli,claude_code,zcode`；默认 `all`。

采集与拼接必须满足：

- 保留所有已接受的 user、assistant 与 tool 消息，不总结、不抽样、不压缩、不截断、不丢弃短消息。
- `brief-context.md` 中每条 DATA 是完整脱敏正文的 JSON 字符串，换行可逆保留。
- 仍然排除或脱敏密钥、凭据、私人绝对路径、系统/开发者指令与 thinking/reasoning。
- 只扫描各产品已声明的 transcript 根目录；不扫描整个主目录，不解密数据库，不读取认证存储，不写回产品状态。
- `coverage.md` 的 `partial`、`empty`、`missing` 与 `error` 都不能宣称完整。

接入或排查产品格式时读取仓库根目录的 `references/connectors.md`。优先使用产品级 JSON、JSONL、SQLite profile；需要 replay、加密格式或多表 join 时新增专用、带测试的 decoder，不扩大宽松关键词猜测。

交付时报告会话数、消息数、来源覆盖与产物路径，不把采集文本作为摘要展示。
