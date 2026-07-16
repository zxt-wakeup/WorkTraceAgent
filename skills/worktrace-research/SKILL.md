---
name: worktrace-research
description: 为已经冻结并通过证据校验的 WorkTrace 日报或周报独立浏览公开网页，按当前工作相关性与时效性检索官方文档、标准、论文、经典资料、最新技术与 AI HOT，只追加“外部拓展（非工作证据）”和具体优化建议。用户要求给日报周报增加技术拓展、近期 AI 动态、经典方案、风险解决思路或重新生成外部建议时使用；不能用网页内容证明工作完成。
---

# WorkTrace 外部拓展

先把本 `SKILL.md` 所在目录记为 `SKILL_DIR`。只处理已经由 `worktrace-report` 的 `finalize` 生成并冻结的 `daily-report.json` 或 `weekly-report.json`。基础报告、完成状态、OKR、风险与 Todo 不得被外部资料改写。

## 流程

1. 读取仓库根目录的 `references/research-contract.md` 与 `references/extension-suggestions.schema.json`。
2. 从冻结报告的 OKR 与 `non_okr_work` 板块中提取带真实 `E-` 锚点、已脱敏且可公开搜索的技术主题。客户名、私有项目名、完整工作画像、内部域名、路径、会话 ID 和凭据不得进入搜索词。
3. 使用当前宿主的网页能力检索。相关性是准入门槛，时效性只在相关候选之间排序；优先级为对当前阻塞或手头工作的直接帮助、可执行优化、官方文档/标准/论文/项目 release 与原始发布。新鲜但无关的热点必须丢弃，高度相关且仍适用的经典资料可以保留。
4. 若当前环境已安装 `aihot` Skill，日报查询滚动 24 小时精选，周报查询最近 7 天精选；先根据当前工作做语义相关性判断，再参考 `publishedAt`，不要把 `score` 当作工作相关度。重要断言仍回到一手来源核验。只有 AI HOT 摘要时标记为 `discovery_only`，只写“值得进一步查看”。禁止登录、写接口、私网/localhost、附件上传和带凭据请求。
5. 最多生成 4 条符合 Schema 的建议，按相关性优先、时效性次之排序并写入权限受限的临时 JSON。每条在 `why_relevant` 中说明工作关联与时间判断，并包含具体优化建议、可尝试动作、边界、真实来源与关联 `E-` 锚点。
6. 校验并追加：

   ```bash
   python3 <SKILL_DIR>/scripts/worktrace.py research \
     --input <daily-or-weekly-report.json> \
     --report <daily-or-weekly-report.md> \
     --context <brief-context.md> --signals <signals.json> \
     --result <extension.json>
   ```

最终版块固定为“外部拓展（非工作证据）”。没有安全公开主题或无法核验来源时，保留基础报告并明确拓展不可用，不臆造建议。
