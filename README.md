# WorkTraceAgent

WorkTraceAgent 会只读汇总本机 Codex、Claude Code、ZCode 等 Coding Agent 的会话，生成可回溯的中文工程日报或周报。

报告以 OKR 为主线，但不会把季度 OKR 当成全部工作：无法可靠对齐 OKR、但确实重要的内容会进入“其他重要工作”板块。报告还会更新私有工作画像，并追加与手头工作相关的 AI HOT / 一手信源和优化建议。

## 最快开始

只需克隆项目并执行一次安装命令：

```bash
git clone https://github.com/zxt-wakeup/WorkTraceAgent.git
cd WorkTraceAgent
python3 scripts/install_skills.py
```

然后开启一个新的 Agent 会话，直接输入：

```text
生成日报
```

或：

```text
生成周报
```

默认分别生成今天的日报和本周周报。也可以说“生成昨天的日报”“生成 2026-W28 周报”。第一次生成时，WorkTrace 会自动完成初始化，不需要再手动执行 `setup`。

是的，克隆后直接用 Agent 打开这个项目文件夹，也可以根据仓库中的 `AGENTS.md` 交互使用。执行上面的安装命令，是为了让你以后在其他项目或任意新会话里，也能直接说“生成日报/生成周报”。

> Git 出于安全原因不会在 `git clone` 后自动运行仓库脚本，所以安装命令需要用户亲自执行一次。

### 填写 OKR（首次会话会引导）

如果首次说“生成日报”或“生成周报”时还没有有效 OKR，Agent 会先请你直接粘贴当前 OKR；保存成功后才会重新采集并生成报告。OKR 不会从会话中猜测。

请按 `O1/KR1` 形式提供目标和关键结果，例如：

```markdown
# 当前 OKR

- 周期：2026-Q3
- 状态：启用

## O1：提升 WorkTrace 日报与周报体验

- O1/KR1：用户可在新会话直接生成日报和周报
- O1/KR2：报告准确保留 OKR 外的重要工作
```

OKR 会私密保存在 `~/.config/worktrace-agent/okr.md`，不会发送到网页或 AI HOT。以后季度 OKR 有变化时，可以直接编辑该文件，或在会话中提供新的 OKR；无需重新安装 Skill。

如果暂时没有 OKR，可以明确说“跳过 OKR”；系统会生成非 OKR 报告，并把已核实工作放入“其他重要工作”板块。

## 更新

安装器使用符号链接，更新仓库后不需要重新安装：

```bash
cd WorkTraceAgent
git pull
```

不要随意移动或删除已经安装的 clone；如需移除，先运行：

```bash
python3 scripts/install_skills.py --target all --uninstall
```

## 会生成什么

- OKR 主线：模型根据会话证据判断工作与 OKR 的实际关联，不强行映射。
- 其他重要工作：收录与当前 OKR 无法可靠对齐、但有价值的工作。
- 工作画像：滚动记录工作重点、协作偏好、工具倾向、反复摩擦与学习兴趣，用于后续排序和表达。
- 外部拓展：结合工作相关性和时间，从 AI HOT 与一手信源中筛选真正有帮助的信息，并给出可执行的优化建议。
- 证据锚点：每条工作事实、风险和后续任务都要能回到本机会话证据。

周报会重新扫描整周原始证据，不会简单拼接日报。外部资料只作为建议，不能反过来证明工作已经完成，也不能改变 OKR、风险或 Todo 状态。

## 工作原理

仓库包含三个可迁移的 Agent Skills：

```text
skills/
├── worktrace-collect/   # 只读采集、完整拼接和覆盖诊断
├── worktrace-report/    # 由当前宿主 Agent 生成日报或周报
└── worktrace-research/  # 为冻结报告追加外部优化建议
```

默认流程如下：

```text
只读扫描本机会话
  → 脱敏并完整拼接消息
  → 当前宿主 Agent 生成报告
  → 校验周期、Schema、OKR、画像和证据锚点
  → 渲染 Markdown
  → 追加相关的外部优化建议
```

“当前宿主 Agent”就是你正在对话的 Agent。默认不会因为历史中经常出现另一个 Agent，就自动启动它来写报告。

## 支持范围

内置支持或保守兼容 Codex、Claude Code、ZCode、Qoder、CodeBuddy、Trae、通义灵码、Kimi、Qwen Code、Gemini CLI、OpenCode、GitHub Copilot、Cline、Roo Code、Cursor、Windsurf、Factory Droid 等来源。

默认安装器会自动选择本机可用的用户级 Skill 目录：

- Codex、Gemini CLI、OpenCode：`~/.agents/skills/`
- Claude Code：`~/.claude/skills/`
- 未检测到已知产品：`~/.agents/skills/`

安装器不使用 `sudo`，不会覆盖已有同名文件或 Skill。安装后若当前产品尚未发现 WorkTrace，请开启一个新会话；Claude Code 首次创建 Skill 目录时可能需要重启。

## 隐私

采集是只读的。WorkTrace 不读取凭据、认证数据库、浏览器缓存、thinking/reasoning 或加密数据，也不会把完整工作画像发送到网页、搜索引擎或 AI HOT。

进入报告上下文的已接受 user、assistant、tool 消息会完整保留，不做摘要、抽样或单消息截断；密钥、Cookie、Token、私人绝对路径、系统/开发者指令和 reasoning 会按安全边界排除或脱敏。

产物默认保存在 `~/.local/state/worktrace-agent/artifacts`，目录权限为 `0700`，文件权限为 `0600`。

## 高级用法

普通使用不需要下面这些命令。

<details>
<summary>查看安装计划或指定安装目标</summary>

```bash
# 只查看，不写入
python3 scripts/install_skills.py --dry-run

# 安装到开放标准目录
python3 scripts/install_skills.py --target universal

# 同时安装到开放标准目录和 Claude Code
python3 scripts/install_skills.py --target all

# 只安装报告 Skill
python3 scripts/install_skills.py --target universal --skill worktrace-report
```

</details>

<details>
<summary>直接从源码运行底层 CLI</summary>

```bash
python3 scripts/worktrace.py setup
python3 scripts/worktrace.py doctor
python3 scripts/worktrace.py run --day today --no-model --research off
python3 scripts/worktrace.py weekly --week this-week --no-model --research off
```

`--no-model` 是 Skill 的默认链路：Python 负责采集和校验，当前宿主 Agent 负责合成。只有明确需要 cron/launchd 无交互自动化时，才使用 `--agent` / `--model` 的兼容后端。详细合同见 [`references/`](references/)。

</details>

<details>
<summary>开发验证</summary>

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests skills
```

</details>
