# Connector 与迁移参考

## 设计边界

WorkTrace 只在已声明的产品目录内做只读扫描，流程固定为“定位目录 → 识别版本/结构 → 归一化消息 → 脱敏 → 生成覆盖信息”。不得扫描整个用户主目录，不得读取认证、Cookie、Token、配置密钥，不得解密数据库或向产品状态写入数据。`thinking/reasoning` 默认不进入报告证据。

覆盖状态含义：

- `complete`：选定周期内发现稳定格式的明文完整 transcript。
- `partial`：只有部分 transcript、时间回退、格式漂移或产品只公开有限本地数据。
- `empty`：目录存在，但周期内未发现可用消息。
- `missing`：配置目录不存在。
- `error`：只读解析失败；不得把失败自动降级成“没有工作”。

## 专用解码器

### Codex CLI / Desktop

默认根目录 `~/.codex`。读取 threads SQLite、history/session index 与 `sessions`、`archived_sessions` JSONL；完整 transcript 优先，元数据只作发现回退。默认最多扫描 2,000 个 JSONL、单文件 50 MiB、50,000 条消息和 10,000 行 thread 元数据；可用 `connectors.codex_cli.max_jsonl_files`、`max_file_mb`、`max_messages`、`max_thread_rows` 调整，命中上限或遇到坏记录时 coverage 必须降级。

### Claude Code

默认根目录 `~/.claude`，读取 `projects/**/*.jsonl` 与 `sessions/**/*.jsonl`。识别 user/assistant、tool use/result、父消息、分支和 `subagents`；跳过 thinking、快照、附件正文及系统提醒。可用 `CLAUDE_CONFIG_DIR` 的实际路径覆盖 `connectors.claude_code.root`。

### Qoder 与 CodeBuddy CLI

Qoder 的公开 transcript 位于 `~/.qoder/projects/<project>/transcript/*.jsonl`；CodeBuddy CLI 位于 `~/.codebuddy/projects/<project>/*.jsonl`。二者使用与 Claude content-block 相容但独立标识的解码路径；遇到未知事件时跳过并在 coverage 中降级，不假设格式永久相同。

## Portable adapter 默认档案

| key | 只读候选根目录 | 当前策略 |
|---|---|---|
| `zcode` | `~/.zcode`；重点探测 `cli/db/db.sqlite` | 识别 OpenCode 风格 `session/message/part`；格式漂移时 partial |
| `trae` | `~/Library/Application Support/{Trae,Trae CN}`、remote server 根 | 只读明文 export/recognized transcript；不解密数据库 |
| `qoder` | `~/.qoder`、Qoder 应用目录 | 专用 content-block transcript |
| `codebuddy` | `~/.codebuddy`、CodeBuddy 应用目录 | 专用 content-block transcript |
| `tongyi_lingma` | Lingma/Qoder CN extension globalStorage | 仅稳定明文或导出；插件内部格式不稳定时 partial |
| `comate` | `~/.comate`、Comate extension globalStorage | 保守 JSON/JSONL 探测 |
| `kimi_code` | `~/.kimi-code` | session index/state/wire 的宽松 envelope 解码 |
| `kimi_cli` | `~/.kimi` | 兼容旧 context/wire JSONL，按未知事件容错 |
| `qwen_code` | `$QWEN_HOME`（默认 `~/.qwen`） | tree JSONL；文本与工具事件，跳过 reasoning |
| `gemini_cli` | `~/.gemini/tmp/**/chats` | JSONL/旧 JSON；未知 mutation 保守跳过 |
| `opencode` | `~/.local/share/opencode` | `opencode.db` WAL 快照 + 旧 JSON storage |
| `github_copilot` | `~/.copilot/session-state` | events JSONL；不读取 auth/config |
| `cline` / `roo_code` | VS Code-compatible globalStorage | task JSON transcript；结构不匹配时 partial |
| `windsurf` | Windsurf / Codeium 产品目录 | 无稳定公开 schema，best effort 且不宣称完整 |
| `factory_droid` | `~/.factory/{sessions,projects}` | 保守 JSON/JSONL 探测 |

这些路径是定位候选，不是完整性承诺。各产品更新后先看 `doctor` 与 `coverage.md`，不要静默接受空结果。

## 无代码接入任意新产品

在用户设置的 `connectors.agent_sessions.profiles` 中增加一个产品级档案。根目录必须具体到该产品，pattern 必须具体到 transcript、session 或只读数据库；不要使用 `~/**`：

```json
{
  "connectors": {
    "agent_sessions": {
      "profiles": {
        "my_agent": {
          "enabled": true,
          "label": "My Agent",
          "roots": ["~/.my-agent"],
          "patterns": [
            "**/sessions/**/*.jsonl",
            "**/conversations/**/*.json",
            "**/state.sqlite"
          ],
          "support_note": "Vendor schema is experimental"
        }
      }
    }
  }
}
```

Portable adapter 只接受 JSON、JSONL 与 SQLite 候选，限制文件数、单文件大小和总消息数；过滤 settings/auth/credentials/cache/logs/backups。若新产品需要 replay rewind、operation log、加密或多表 join，应新增版本化专用 decoder，不要靠宽松关键词猜测。

## Connector 合同

每个 connector 暴露唯一 `key` 和 `scan(TimeWindow) -> ConnectorResult`，并必须返回 `SourceCoverage`。归一化字段至少包括 source、匿名 session/message identity、role、kind、timestamp、workspace、branch activity、confidence、evidence type。JSONL 允许末行半写；SQLite 通过只读 backup 获得包含已提交 WAL 页的快照；最终按 source + session 合并并按内容去重。
