---
name: worktrace-research
description: 为已经冻结并通过证据校验的 WorkTrace 日报或周报独立浏览公开网页，按当前工作相关性与时效性检索官方文档、标准、论文、经典资料、最新技术与 AI HOT，把最相关动作压缩进工作建议并提供 1–3 条带简短理由的推荐阅读，详细研究保留在 JSON。用户要求给日报周报增加技术拓展、近期 AI 动态、经典方案、风险解决思路、推荐阅读或重新生成外部建议时使用；不能用网页内容证明工作完成。
---

# WorkTrace 外部拓展

先把本 `SKILL.md` 所在目录记为 `SKILL_DIR`。只处理已经由 `worktrace-report` 的 `finalize` 生成并冻结的 `daily-report.json` 或 `weekly-report.json`。基础报告、完成状态、OKR、风险与 Todo 不得被外部资料改写。

## 流程

1. 读取仓库根目录的 `references/research-contract.md` 与 `references/extension-suggestions.schema.json`，然后先让运行时准备本次研究交接材料：

   ```bash
   python3 <SKILL_DIR>/scripts/worktrace.py research \
     --input <daily-or-weekly-report.json> \
     --report <daily-or-weekly-report.md> \
     --context <brief-context.md> --signals <signals.json> \
     --prepare
   ```

   该命令会生成唯一的 `research_run_id`、冻结 `research_as_of`，重新认证报告、context 与 signals，匿名预取可用的 AI HOT 通用精选，并生成权限受限的 `research-manifest.json`、`research-prompt.md` 和 Schema。准备阶段不得追加或改写报告；不要在浏览与最终校验之间重新执行 `--prepare`。
2. 完整读取生成的 `research-prompt.md`、`research-manifest.json` 和 Schema。只从 manifest 的 `authorized_work_items` 选择带真实 `E-` 锚点、已脱敏且可公开搜索的技术主题；运行时已经为每条授权工作生成确定性的 `work_item_id` 和 `work_summary`。客户名、私有项目名、完整工作画像、内部域名、路径、会话 ID 和凭据不得进入搜索词。
3. 使用当前宿主的网页能力检索。每个候选先绑定一条具体工作，再判断相关性；相关性是准入门槛，时效性只在相关候选之间排序。优先当前阻塞或手头工作的直接帮助、可执行优化以及官方文档/标准/论文/项目 release 等一手来源；新鲜但无关的热点必须丢弃。
4. AI 近期成果优先从 AI HOT 匿名只读发现：日报查询滚动 24 小时精选，周报查询滚动 7 天精选。AI HOT 公开池最多只有研究时点最近 7 天，不能当作历史全库；先根据具体工作判断相关性，再参考 `publishedAt`，不要把 `score` 当作工作相关度。AI HOT 条目只能标记为 `curated_discovery` / `discovery_only` / `latest_window`，重要断言仍回一手来源核验；只有摘要时只写“值得进一步查看”。
5. 最新窗口没有足够强相关成果时，可补充 `research_as_of` 前 365 天内的强相关成果，但必须核验一手来源并标 `strongly_relevant_within_year`；不得放宽到一年以前。更早内容仅允许持续维护且当前适用的官方文档或标准作为 `evergreen` 支撑，不能包装成最新成果。禁止登录、写接口、私网/localhost、附件上传和带凭据请求。
6. 日报和周报均按匹配质量生成 1–3 条符合 Schema 的建议，按相关性优先、时效性次之排序并写入权限受限的临时 JSON。根字段 `schema_version` 使用 `1.2`，`research_run_id`、`research_as_of`、`one_year_cutoff` 和 `aihot_scope` 必须逐字段复制生成的 selection-context；每条 `work_links` 必须从 `research-brief` 的同一条工作逐字段复制 `work_item_id`、`work_summary` 和全部 `evidence_refs`。不得靠自由文本自报关联、跨工作拼接或复用另一轮 prepare 的旧结果。每个来源的 `summary` 用一句简明中文介绍资料是什么、主要讲什么；`why_relevant` 第一小句单独说明为什么推荐给当前工作，再补充时间选择，并包含具体优化建议、可尝试动作、边界和真实来源。
7. 使用同一份 manifest 校验并追加：

   ```bash
   python3 <SKILL_DIR>/scripts/worktrace.py research \
     --input <daily-or-weekly-report.json> \
     --report <daily-or-weekly-report.md> \
     --context <brief-context.md> --signals <signals.json> \
     --result <extension.json> --manifest <research-manifest.json>
   ```

   最终校验会要求结果的 `research_run_id` 与时间元数据精确匹配同一轮 prepare，并核对报告、context、signals、Prompt、当前运行时 Schema 以及授权工作项的绑定；任一项变化时必须重新执行 `--prepare` 和网页研究，不能沿用旧结果。

校验成功后，运行时把最相关动作放入日报`工作建议`或周报`下周重点`，并把 1–3 条最值得读的一手来源、资料简介及明确推荐理由放入日报`明日阅读`或周报`推荐阅读`。详细研究保留在 `extension-suggestions.json`，不再把长篇研究正文追加到用户报告。没有安全公开主题或无法核验来源时，明确暂无强相关资料，不臆造建议。
