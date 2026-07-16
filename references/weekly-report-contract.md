# 周报合同

## 证据与周期

- 只总结调用方给定 ISO 周期内的工作，并让所有事实字段引用上下文中的 `E-xxxxxxxxxxxx` 证据锚点。
- 跨日合并同一事项；较新且更强的证据决定周末最终状态。周一提出、周三完成、周五回滚是一项工作的状态演进，不是三项成果。
- 不用会话数、消息数或工具调用数包装生产力；它们只能说明采集覆盖。
- 当前周尚未结束时令 `partial_period=true`，并在置信度说明中避免声称覆盖整周。
- 覆盖为 partial、empty 或 error 时明确 caveat，并降低没有强证据的结论。

## 价值结构

- `executive_summary` 以有效 OKR 的推进为主线，同时概括独立板块中的高价值其他工作；OKR 是季度规划且不一定覆盖整周全部工作。摘要先说明真正交付的价值和结论置信度，不堆过程；`evidence` 必须引用支撑摘要的真实 `E-` 锚点。
- 若所选周完全没有 `E-` 证据，管理摘要必须精确使用：`headline=本周没有足够证据确认工作成果`、`value_delivered=没有可核验的交付价值`、`confidence_note=所选周期内没有可用的 E- 证据锚点`、`evidence=无可用工作证据`；不得宣称成果、价值或确定性。
- `weekly_highlights` 只保留最多 5 条高价值结果；仅讨论、仅提出或无结果的排查通常不算 highlight。
- `project_progress` 按项目归并动作和最终状态，保留成果、进行中事项及无法确认项。
- `decisions_and_learnings` 只记录会改变后续实现、验证或协作方式的决定与可复用经验。
- `risks_and_actions` 区分风险状态；没有已执行行动时写“未发现已执行动作”，不得编造。
- `next_week_priorities` 只从明确未完成、阻塞、风险或用户承诺中推导，写可验证的 `done_when`；已完成事项不得再次出现。
- `work_patterns` 至少需要跨两个独立证据点才能称为模式；单次偶发问题不包装成趋势。

## OKR 分流

- 先从证据还原事实，再判断其是否直接推进或可靠支撑有效 KR；不得从 OKR 反推做过什么。
- 没有有效 OKR 时 `okr_summary` 为空，其他条目的 `okr_refs` 为空，全部已核实工作进入 `non_okr_work`。
- `okr_summary` 不量化 KR 增量或完成率，除非证据明确提供可核验数字。
- `weekly_highlights`、`project_progress`、`decisions_and_learnings`、`risks_and_actions` 与 `next_week_priorities` 是 OKR 主线字段，每条都必须有非空 `okr_refs`。无法可靠映射到 KR 的工作只放 `non_okr_work`，不要为了周报好看强行对齐。
- 未对齐不等于低价值、无关或不应汇报。`non_okr_work` 是“其他重要工作（未可靠对齐当前 OKR）”独立板块，而不是低优先级垃圾桶。

## 动态工作画像

- `schema_version` 固定返回 `2.0`，并按动态工作画像合同生成 `work_profile` 完整快照。
- 先基于本周会话和上一版已验证画像更新工作画像，再用它辅助选择管理摘要重点、工作价值表达和建议排序。
- 画像不能证明本周做过什么、不能创建 OKR 关联，也不能单独生成风险或下周任务；这些内容仍必须引用本周真实 `E-` 锚点。

## 外部内容隔离

- 本合同只生成内部事实周报。网页、AI HOT、论文、新闻和文档均不能进入本 JSON。
- 外部拓展由报告冻结后的独立研究阶段追加；它不能更改工作状态、OKR 进度或下周优先级。

最终只返回符合 Schema 的单个 JSON 对象，不附加 Markdown 或解释。
