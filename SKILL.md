---
name: worktrace-report
description: 从本机多种 Coding Agent 的只读会话证据生成脱敏、可回溯、以 OKR 为主线但不遗漏其他重要工作的中文工程日报或周报，滚动更新工作画像，并在报告冻结后追加与当前工作相关的独立公开网页研究。用户要求基于本机 Coding Agent 或 WorkTrace 记录生成日报、周报、OKR 对齐或工作复盘时使用。默认由当前宿主 Agent 生成，不启动另一个本地 Agent。
---

# WorkTrace 兼容入口

这是旧版单 Skill 安装的兼容入口。新安装请使用仓库 `skills/` 下的三个独立 Skill：

- `skills/worktrace-collect`：采集、完整拼接和覆盖诊断。
- `skills/worktrace-report`：当前宿主生成日报与周报。
- `skills/worktrace-research`：为冻结报告追加外部拓展。

执行报告任务时完整遵循 `skills/worktrace-report/SKILL.md`。不要根据会话使用频率启动另一个 Agent；默认运行 `scripts/worktrace.py run/weekly --no-model --research off`，由当前宿主生成含动态工作画像的 JSON，再用 `finalize` 校验和渲染。基础报告冻结后按 `worktrace-research` 独立追加 AI HOT/一手信源与工作优化建议。

若旧安装只暴露本入口，仍可直接使用根目录 `scripts/worktrace.py`。只有用户明确要求无交互 CLI 自动化时，才使用 `--agent` / `--model` 的可选跨 Agent 后端。
