# Contributing to WorkTraceAgent

感谢你帮助改进 WorkTraceAgent。这个项目把隐私、安全边界和证据完整性视为产品功能；提交改动时，请让这些约束与用户体验一起变得更好。

除非另有明确说明，提交贡献即表示你有权提交相关内容，并同意该贡献按项目的 [MIT License](LICENSE) 授权。

## 本地开发

需要 Python 3.9 或更高版本。克隆仓库后即可运行测试，不需要先安装 Skill：

```bash
git clone https://github.com/zxt-wakeup/WorkTraceAgent.git
cd WorkTraceAgent
python3 -m pip install -e ".[dev]"
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests skills
python3 -m ruff check scripts tests
python3 scripts/check_release_versions.py
```

如果希望在任意项目中调试 Skill 发现机制，可以创建可选的开发链接：

```bash
python3 scripts/install_skills.py --mode link --dry-run
python3 scripts/install_skills.py --mode link
python3 scripts/install_skills.py --status
```

这些链接依赖当前 clone，请勿在未卸载链接时移动或删除仓库。

## 修改边界

- 先阅读根目录的 `AGENTS.md`，再阅读与你改动相关的 Skill 和 `references/` 合同。
- 会话采集必须只读，并保留所有被接受的用户、助手和工具消息；不要采样、截断或用摘要替代原文。
- 不要把凭据、私有路径、系统提示、开发者提示或思考过程写入报告或外部请求。
- 外部研究不是工作证据；AI HOT 只用于匿名只读发现，重要结论需要一手信源验证。
- 用户配置、OKR、工作画像和报告产物不得提交到仓库。

## Pull Request 检查清单

- 为行为变化增加或更新测试，并运行完整验证命令。
- 保持 CLI 和 Skill 的旧入口兼容；必须破坏兼容性时，在 PR 与 `CHANGELOG.md` 中说明迁移方法。
- 若修改版本，统一更新发布声明，并运行 `scripts/check_release_versions.py`。
- 不提交 `dist/`、缓存、虚拟环境或真实用户会话样本。
- PR 说明应包含用户可见结果、验证方式以及隐私或兼容性影响。
