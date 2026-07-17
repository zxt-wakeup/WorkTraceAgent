# WorkTraceAgent agent guide

This repository contains three portable Agent Skills under `skills/`:

- `worktrace-collect`: read-only collection, diagnostics, and lossless transcript concatenation.
- `worktrace-report`: daily/weekly synthesis with the current host Agent by default.
- `worktrace-research`: separate public-web extensions for a frozen report.

A cloned checkout is the zero-install entry point. When a request made from this
repository matches one of the Skills above, read that Skill's complete `SKILL.md`
and follow it directly; user-level Skill links are optional development
conveniences, not a runtime requirement for project-directory use.

The host Agent and the conversation sources are separate concepts. Never choose or launch another local Agent merely because it appears frequently in collected history. The main Skill must run `run` or `weekly` with `--no-model`, synthesize the emitted JSON with the current host model, and pass it through `finalize`. The older cross-Agent runner remains an explicit CLI automation option only.

Reports are OKR-led, not OKR-exclusive. Quarterly OKRs are planning context rather than a whitelist: the model must make a semantic alignment decision, keep reliably aligned work in the OKR main sections, and preserve other important verified work in the separate `non_okr_work` section. Each run also updates the private rolling `work_profile`; it may personalize ranking and wording but is never work evidence, must not infer sensitive traits, and must not be sent to web or AI HOT.

External research remains post-finalize and non-evidentiary. Cover both OKR and non-OKR work, rank work relevance before timeliness, use AI HOT only as anonymous read-only discovery, and verify material claims against primary sources before giving optimization advice.

Use `python3 scripts/worktrace.py` for the runtime. Do not reimplement collection or rendering inside a Skill. Collection is read-only and must retain all accepted user, assistant, and tool messages without summarization, sampling, or truncation. Preserve the existing privacy exclusions for credentials, private paths, system/developer instructions, and thinking/reasoning.

Before changing connector behavior, read `references/connectors.md`. Before changing report or research behavior, read the matching contract and JSON Schema in `references/`.

Validation commands:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests skills
```
