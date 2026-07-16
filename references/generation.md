# 可选 CLI 生成后端与迁移

Skill 默认不执行本文件中的自动选择。`worktrace-report` 使用当前宿主模型：先运行 `run/weekly --no-model`，再由宿主生成 JSON 并执行 `finalize`。以下机制仅用于用户明确要求的无交互 CLI、cron 或 launchd 自动化。

## 可选自动选择

`draft`、`run` 和 `weekly` 在模型调用前读取匹配周期的 `signals.json`：

1. 用 `shutil.which` 检查 `generation.runners` 中已启用 CLI 是否真实存在。
2. 按 runner 的 `origins` 汇总本周期已接受消息数；会话数、文件数和历史总量不参与排序。
3. 选择消息数最多的本机候选；同频时选择较小的 `cost_rank`，再按 runner key 稳定排序。
4. `--agent` 是严格覆盖，未知、禁用或未安装都会失败。`--model` 优先于全局和 runner 默认模型。
5. 把 agent、adapter、model、usage_messages、cost_rank、reason，以及全文大小、分片数和有效并发度写入 `generation-selection.json`。

内置配置提供低成本模型默认值，但模型可用性、命名和实际费用会变化，并取决于用户账户与 CLI 版本。用户应使用 `--model` 或用户级 settings 选择自己当前可用的性价比模型；WorkTrace 不把内置名称当作长期价格承诺。

## 配置

```json
{
  "generation": {
    "agent": "auto",
    "model": "",
    "timeout_seconds": 900,
    "chunk_chars": 250000,
    "max_parallel_chunks": 4,
    "runners": {
      "codex_cli": {
        "enabled": true,
        "command": "codex",
        "adapter": "codex",
        "model": "gpt-5.4-mini",
        "cost_rank": 10,
        "origins": ["codex", "codex_cli"]
      }
    }
  }
}
```

内置 `codex`、`claude`、`gemini` adapter 以 stdin 传入全文 prompt，在临时空工作区运行并禁用本地/网页工具；只复制对应 CLI 的最小认证文件或允许的 API Key 环境变量，不加载项目规则。

## 接入其它 Coding Agent CLI

CLI 若能从 stdin 接收 prompt，并把最终 JSON 输出到 stdout，可配置 `generic` adapter：

```json
{
  "generation": {
    "runners": {
      "my_agent": {
        "enabled": true,
        "command": "my-agent",
        "adapter": "generic",
        "args": ["run", "--model", "{model}"],
        "response_field": "",
        "env_allowlist": ["MY_AGENT_API_KEY"],
        "model": "vendor-cheap-model",
        "cost_rank": 15,
        "origins": ["my_agent"]
      }
    }
  }
}
```

`{model}` 与 `{schema}` 可用于参数模板。若 stdout 是 JSON wrapper，把 `response_field` 配为点分字段（如 `result`）；为空时 stdout 本身必须是报告 JSON。Generic runner 只继承基础网络/语言环境以及显式 `env_allowlist`，不继承任意项目变量。若产品不能通过 stdin/stdout 完成无工具单轮调用，使用 `--no-model` 的 host-native 流程，不要用 shell 拼接全文参数。

## 全文与上下文限制

Python 不做摘要、抽样或消息截断。`context.max_chars` 默认 `0`（不限）；正数只作为硬失败阈值，绝不改变文本。若 CLI/模型拒绝全文，换用更大上下文模型、缩短报告周期或由用户调整采集范围；不得自动丢弃旧消息、工具输出或短消息。

当完整 prompt 超过 `generation.chunk_chars` 时，Python 只沿会话和完整 `E-` 证据块边界切分；同一个已选 runner 读取全部原文分片、生成候选报告，最后再做一次候选归并。每个原始锚点只进入一个分片，公共证据规则可以重复；分片 prompt/候选写入私有 `generation-chunks/`。若单条完整消息仍超限则失败。这个阶段不改变 `brief-context.md`，也不允许 Python 生成内容摘要。

分片模型调用默认以 `generation.max_parallel_chunks=4` 有限并行，最终候选仍严格按原始分片顺序归并。该值可设为 `1` 恢复串行，最大为 `8`；遇到账号并发限制或 runner 不支持并发调用时应主动调低。调度器只维持配置数量的在途调用，任一分片失败后不再提交新分片，并让整次生成失败，不会拿不完整候选继续归并；`generation.timeout_seconds` 分别作用于每个分片调用。每次运行的中间文件写入独立私有子目录，避免同周期的并发运行互相读取候选。并行只减少等待时间，不减少模型读取的原文或 Token，也不改变最终证据校验。

升级兼容：旧版默认组合 `max_chars=400000`、`per_message_chars=12000` 会在读取时迁移为不限模式，避免历史默认继续截断；其它用户显式正数仍保留为硬失败阈值。
