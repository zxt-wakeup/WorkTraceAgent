# Changelog

WorkTraceAgent 的重要变化记录在这里。版本号遵循
[Semantic Versioning](https://semver.org/)。

## [Unreleased]

## [1.4.0] - 2026-07-17

### Added

- 新增项目目录内的零安装使用入口：克隆后让 Coding Agent 打开仓库，即可按对应 `SKILL.md` 工作。
- 安装器新增只读 `--status`，可区分当前仓库链接、其他安装、未链接与断裂链接。
- CLI 新增 `--version`；`doctor` 现在会展示运行模式与用户级 Skill 注册健康状态。
- 新增 GitHub Actions，覆盖 Python 3.9 与 3.12 的测试、编译及发布版本一致性检查。
- 新增 Ruff 静态检查与可选 `dev` 依赖组，统一本地与 CI 质量门禁。
- 外部研究 Schema 升级为 `1.1`；每条建议必须精确绑定冻结报告中的工作 ID、脱敏摘要和完整 `E-` 证据。
- AI HOT 日报使用滚动 24 小时、周报使用滚动 7 天；最新窗口不足时，可补充研究时点前 365 天内的强相关一手成果。
- 新增信源时效性、AI HOT 最近 7 天公开池上限、discovery-only 与 evergreen 安全校验。
- 当前宿主研究新增 `prepare → result` 两阶段交接；每轮唯一的 `research_run_id` 与私有 manifest 会冻结检索时点，并绑定报告、证据、Prompt、当前 Schema 与授权工作项，拒绝复用旧研究结果。
- 项目正式采用 MIT License，并把 SPDX 许可证表达式与许可证文件写入 Python 发布物元数据。

### Changed

- 将用户级符号链接明确定位为可选的 `link` / development 模式；保留原有无参数命令兼容性。
- 改进 Python 包元数据，增加项目主页、问题反馈、变更记录和检索关键词。
- 明确 Python wheel 只提供 CLI 运行时；项目目录或完整插件才提供 Agent Skill 体验。
- 手工宿主研究的 `research --result` 现在必须携带同一轮 `--prepare` 生成的 manifest；旧的一步式 CLI 自动研究入口保持兼容。

### Fixed

- 将 Python 包、`pyproject.toml`、Codex/ZCode 插件及 marketplace 版本统一为 `1.4.0`。
- 增加自动校验，防止后续发布时多个 manifest 的版本再次漂移。
- 移除仓库中过期的 1.1/1.2 构建产物，并由 CI 校验实际 wheel/sdist 及安装后命令。
- 近一年来源若提供完整时间戳，现在按精确 UTC 瞬时执行 365 天边界；仅日期型来源继续使用明确的日期精度语义。

[Unreleased]: https://github.com/zxt-wakeup/WorkTraceAgent/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/zxt-wakeup/WorkTraceAgent/compare/v1.3.0...v1.4.0
