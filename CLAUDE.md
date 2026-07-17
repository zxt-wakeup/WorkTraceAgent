# WorkTraceAgent

This cloned checkout is a zero-install entry point. Use the focused Skills in
`skills/` directly; user-level Skill links are optional and are not required when
Claude is opened in this repository. Read the matching `SKILL.md` completely
before acting. For reports, the active Claude session is the host model: run
WorkTrace with `--no-model`, produce the required JSON in this session, and
validate it with `finalize`. Do not launch Codex, Gemini, or another Claude
process based on transcript frequency unless the user explicitly requests CLI
automation.

See `AGENTS.md` for repository-wide boundaries and validation commands.
