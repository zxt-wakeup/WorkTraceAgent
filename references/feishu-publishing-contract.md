# 飞书发布合同

## 定位与授权

- 飞书发布是报告完成后的可选外部写入，不属于只读会话连接器，也不得改变采集覆盖或工作事实。
- 首次接入由当前宿主 Agent 负责安装 Feishu/Lark CLI、初始化应用、发起用户登录并执行 `worktrace feishu setup`；必须使用飞书 CLI 返回的官方浏览器链接完成用户授权，不收集或回显 App Secret、OAuth Token、Cookie。
- 所有 Drive 与 Docs 操作固定使用 `--as user`，只申请文档与云空间所需权限。授权状态失效时停止写入并重新发起官方登录，不绕过授权。

## 目录与文档模型

- 默认目录固定为个人云空间中的 `WorkTrace/日报` 与 `WorkTrace/周报`。setup 先列目录、按精确名称复用唯一结果，没有时才创建；同级出现多个同名目录时停止并请用户消歧。
- 每个日报日期或 ISO 周各对应一个文档。文档标题取最终 Markdown 的一级标题；标题不存在时才使用 `WorkTrace 日报 YYYY-MM-DD` 或 `WorkTrace 周报 YYYY-Www`。
- 创建后将目录 token、文档 token、URL、周期与内容摘要写入本机私有状态。状态默认位于 `~/.local/state/worktrace-agent/feishu-publishing.json`，目录权限为 `0700`、文件权限为 `0600`；其中只保存飞书资源标识，不保存认证凭据。
- 同一周期再次发布时必须更新已保存的同一文档。内容摘要未变化时只做远端存在性校验；状态丢失时先按目标目录和精确标题寻找唯一文档，避免重复创建。

## 写入边界

- 只允许上传已经通过 `finalize` 并渲染完成的 `daily-report.md` 或 `weekly-report.md`。原始会话、`signals.json`、证据上下文、OKR、往届周报样例、完整工作画像、Prompt、Schema、研究 manifest 和研究 JSON 都不得上传。
- Markdown 一级标题映射为飞书文档标题，正文通过标准输入传给 CLI，不把正文或凭据放进命令行参数。
- WorkTrace 创建或明确接管的周期文档是托管文档，后续使用 `overwrite` 更新。用户若在托管文档正文中直接添加内容，下一次发布可能覆盖这些改动；评论、手工附件等不应被当作 WorkTrace 状态。
- 基础报告 finalize 后可以先发布；研究结果通过校验并重绘后，必须用相同 token 更新同一文档，不创建“研究版”副本。

## 校验与故障策略

- 每次创建目录后重新列目录校验；每次创建或更新文档后使用 fresh fetch 校验远端文档存在，只有校验成功才提交本地 token 状态。
- CLI 输出必须是 `ok: true` 的 JSON envelope；仅凭进程退出码不得声明成功。日志与状态命令不得输出资源 token 或授权链接。
- 自动发布默认 `failure_policy: warn`：报告本地生成成功但飞书写入失败时保留报告、输出明确警告，下一次可重试；配置为 `stop` 时发布失败令命令失败。
- 发布配置默认关闭。只有 setup 完成目录校验后，才把 `enabled` 与 `auto_publish` 同时开启；此后日报、周报和研究重绘自动复用同一周期文档。
