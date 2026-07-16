# WorkTraceAgent

WorkTraceAgent 是一组可迁移的 Agent Skills：它以只读方式收集 Codex、Claude Code、ZCode 等 Coding Agent 的本机会话，由**当前正在使用的宿主 Agent**生成可回溯的中文工程日报或周报。报告以 OKR 为管理主线，但不会把季度 OKR 当作工作全集；未可靠对齐但有价值的内容进入独立板块。每次生成还会更新私有滚动工作画像，再独立追加与手头工作相关的“外部拓展（非工作证据）”。

这里有两个不同概念：

- **会话来源**：被 Python 扫描的 Codex、Claude Code、ZCode、Qoder、CodeBuddy、Trae、通义灵码、Kimi、Qwen Code、Gemini CLI、OpenCode、Copilot、Cline、Cursor、Windsurf 等记录。
- **宿主 Agent**：用户安装并调用 Skill 的当前 Coding Agent。它直接承担报告合成，不需要根据历史频率再启动另一个 Agent。

此前“按使用频率选择本地 Agent”的能力保留为无交互 CLI 自动化的可选兼容路径，不再是 Skill 的主流程。

## 为什么拆成三个 Skills

```text
skills/
├── worktrace-collect/   # 只读采集、完整拼接、覆盖诊断、Connector 接入
├── worktrace-report/    # 当前宿主生成日报/周报、更新工作画像并执行严格校验
└── worktrace-research/  # 对冻结报告按相关性/时效性追加外部优化建议
```

拆分后，任意支持 Agent Skills 标准的产品可以只安装需要的能力；采集、报告、联网研究也保持明确的权限边界。三者共享仓库根目录的 Python 运行时和 `references/` 合同，不复制业务实现。

## 安装 Skills

Git 不会在 clone 后自动执行脚本。先 clone，再由用户显式运行安装器：

```bash
git clone <repository-url>
cd WorkTraceAgent
python3 scripts/install_skills.py --dry-run
python3 scripts/install_skills.py
```

默认安装器只检测**安装目标**，不会据此选择报告模型：

- 检测到 Codex、Gemini CLI 或 OpenCode时，链接到 `~/.agents/skills/`。
- 检测到 Claude Code 时，另外链接到 `~/.claude/skills/`。
- 没检测到已知产品时，默认使用开放标准目录 `~/.agents/skills/`。

每个目标中只会新增下面三个符号链接：

```text
worktrace-collect  -> <clone>/skills/worktrace-collect
worktrace-report   -> <clone>/skills/worktrace-report
worktrace-research -> <clone>/skills/worktrace-research
```

安装器不使用 `sudo`、不覆盖现有 Skill，写入前会完整打印计划。已有同名文件、目录或指向其它位置的链接会使整次预检失败。常用选项：

```bash
# 明确安装到开放标准目录
python3 scripts/install_skills.py --target universal

# 同时安装到开放标准目录和 Claude Code
python3 scripts/install_skills.py --target all

# 只安装报告 Skill
python3 scripts/install_skills.py --target universal --skill worktrace-report

# 只移除仍指向当前 clone 的链接
python3 scripts/install_skills.py --target all --uninstall
```

因为使用符号链接，`git pull` 后无需重复复制。移动或删除 clone 前应先执行卸载。

### 各产品方式

| 产品 | 推荐方式 | 生效方式 |
|---|---|---|
| Codex | 安装器写入 `~/.agents/skills`，或把仓库作为 Codex plugin 使用 | 通常自动发现；未出现时开启新任务 |
| Gemini CLI | 同一个 `~/.agents/skills` 链接；也可用 `gemini skills link <clone>/skills/<name>` | 执行 `/skills reload` |
| OpenCode | 同一个 `~/.agents/skills` 链接 | 开启新会话 |
| Claude Code | 安装器写入 `~/.claude/skills` | 已存在顶层目录时支持热加载；首次创建该目录时重启 Claude Code |
| ZCode | **Plugin Management → Discover → + → Add plugin marketplace → Select directory**，选择 clone 根目录 | 在市场页安装 `worktrace-agent` |
| Cursor / 其它产品 | 在 clone 中打开项目并让 Agent 遵循 `AGENTS.md`，或直接运行 `skills/*/scripts/worktrace.py` | 取决于产品会话规则 |

仓库同时提供 `.codex-plugin/plugin.json`、`.zcode-plugin/plugin.json`、`marketplace.json`、`AGENTS.md` 和 `CLAUDE.md`。目录依据可参考 [Codex Agent Skills](https://developers.openai.com/codex/skills/)、[Claude Code Skills](https://code.claude.com/docs/en/skills)、[Gemini CLI Agent Skills](https://geminicli.com/docs/cli/using-agent-skills/) 与 [OpenCode Skills](https://opencode.ai/docs/skills/)。

## 项目怎么启动

### 方式一：在 Coding Agent 中使用（推荐）

安装后直接说：

> 使用 worktrace-report，根据本机 Coding Agent 会话生成本周周报。

Skill 的默认流程是：

```text
Python 只读扫描会话
  → 脱敏并完整拼接全部消息
  → 冻结上一版私有工作画像
  → 当前宿主 Agent 读取 prompt + JSON Schema
  → 当前宿主更新画像并生成 OKR 主线 + 其他重要工作分栏的报告 JSON
  → Python 验证周期 / Schema / OKR / 画像 / E-证据锚点
  → 渲染 Markdown
  → worktrace-research 独立检索 AI HOT 与一手信源并追加优化建议
```

也可以直接调用 `/worktrace-report`（产品支持斜杠 Skill 时）。采集诊断和研究分别使用 `worktrace-collect`、`worktrace-research`。

### 方式二：直接从源码运行

```bash
python3 scripts/worktrace.py setup
python3 scripts/worktrace.py doctor

# 只收集、拼接，并让当前 Agent 接管生成
python3 scripts/worktrace.py run --day today --no-model --research off
python3 scripts/worktrace.py weekly --week this-week --no-model --research off
```

`--no-model` 会生成宿主无关的 prompt、JSON Schema 和 `work-profile-context.json`。当前 Agent 只返回一个包含完整 `work_profile` 的 JSON 对象后，用同周期文件执行：

```bash
python3 scripts/worktrace.py finalize \
  --type weekly --week 2026-W29 \
  --context <brief-context.md> \
  --signals <signals.json> \
  --input <model-output.json>
```

也可以安装纯 Python CLI：

```bash
python3 -m pip install .
worktrace setup
worktrace weekly --week this-week --no-model --research off
```

核心链路支持 Python 3.9+。`schedule` 当前使用 macOS `launchd`；其它系统可用自身定时器调用同一 CLI。

## 可选：无交互 CLI 自动生成

如果用户明确需要 cron/launchd 中从采集到生成全自动运行，可省略 `--no-model`：

```bash
python3 scripts/worktrace.py weekly --week this-week --agent codex --model <低成本模型>
```

不指定 `--agent` 时，旧兼容逻辑会从本机已启用的 CLI 后端中，按本周期来源消息量、用户配置的 `cost_rank` 和稳定顺序选择。模型价格和可用性会变化，因此内置值只作为可修改默认，不宣称始终最便宜；用户应在 `~/.config/worktrace-agent/settings.json` 或 `--model` 中选择自己账户可用的性价比模型。详细配置见 [references/generation.md](references/generation.md)。

全文超过单次上下文时，该兼容路径只沿完整 `E-` 证据块无损分片；同一后端默认最多并行处理 4 个分片，再按原始顺序归并。可通过 `generation.max_parallel_chunks` 调整并发度或设为 `1` 恢复串行。单条消息仍放不下时直接失败，不会截断。

## 会话采集保证

- 专用解析：Codex CLI/Desktop、Claude Code、Qoder、CodeBuddy。
- Portable profiles：ZCode、Trae、通义灵码、Comate、Kimi CLI/Code、Qwen Code、Gemini CLI、OpenCode、GitHub Copilot、Cline、Roo Code、Windsurf、Factory Droid。
- 自定义产品：在用户配置中声明产品级 JSON、JSONL 或 SQLite transcript 根目录与 pattern，无需改代码。

进入 `brief-context.md` 的每条已接受 user、assistant、tool 消息都完整保留，不做摘要、抽样、单消息截断或“只留最近几条”；“继续”等短消息也保留。消息正文以 JSON 字符串写入，换行可逆。密钥、凭据、私人绝对路径、系统/开发者指令和 thinking/reasoning 仍按安全边界排除或脱敏。

来源路径、支持等级和保守降级见 [references/connectors.md](references/connectors.md)。`coverage.md` 中的 `partial`、`empty`、`missing` 和 `error` 都不等于完整覆盖。

## 报告与研究边界

- 工作事实必须引用从原始 TraceBundle 重算认证的 `E-xxxxxxxxxxxx` 锚点。
- 周报重新扫描整周证据，不拼接历史日报。
- OKR 用于规划、归类与价值主线，不是全部工作的白名单。模型做语义关联；无法可靠关联的已核实重要工作统一进入“其他重要工作（未可靠对齐当前 OKR）”。
- 动态工作画像只记录工作重点、交付/协作偏好、工具倾向、反复摩擦与学习兴趣；它辅助排序和表达，不证明完成、OKR、风险或 Todo，也禁止推断敏感人格属性。
- 基础报告由当前宿主在离线证据范围内生成；网页研究只能在报告冻结后开始。
- 外部拓展是重要板块，但相关性是准入门槛、时效性只在相关候选之间排序。它覆盖 OKR 与其他重要工作，优先官方文档、标准、论文与原始发布，并给出具体优化建议、可执行下一步和适用边界。
- AI HOT 只调用公开只读接口作为近期发现源：日报使用滚动 24 小时精选，周报使用最近 7 天精选；先判断与手头工作的相关性，再参考发布时间。AI HOT `score` 不是工作相关度，未回到一手来源时必须标为 `discovery_only`。
- 外部拓展固定标为“非工作证据”，不能改变完成状态、OKR、风险或 Todo。

CLI 自动研究链路内置一个最小 AI HOT 发现 provider：它只向固定的 `https://aihot.virxact.com/api/public/*` 发匿名 GET，不接受可配置 base URL，不发送工作主题或画像，拒绝重定向并限制超时与响应体；失败只让外部拓展降级，不影响基础报告。当前宿主环境若已安装 AI HOT Skill，`worktrace-research` 仍按该 Skill 的最新公开只读合同执行。本项目不会替用户覆盖或修改其它 Skill。

## 隐私与产物

默认不读取凭据、认证数据库、浏览器缓存、thinking/reasoning 或加密数据。联网前应把客户名与私有项目名加入 `research.private_terms`。完整工作画像永不发送到网页、搜索引擎或 AI HOT；外发仍只使用当前冻结报告中带证据、严格脱敏的公共技术主题。

产物默认位于 `~/.local/state/worktrace-agent/artifacts`，目录权限 `0700`、文件权限 `0600`。最新滚动画像保存在该私有根目录的 `work-profile.json`；每个周期冻结生成时使用的上一版画像，避免 `finalize` 前后状态漂移。保留策略只清理由 WorkTrace 标记的过期周期目录。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests skills
```
