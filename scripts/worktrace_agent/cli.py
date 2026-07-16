from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Dict, List, Optional

from worktrace_agent.agent_runner import (
    GenerationSelection,
    detect_local_agents,
    run_generation_draft,
    select_generation_agent,
)
from worktrace_agent.aihot import discover_aihot
from worktrace_agent.codex_runner import run_codex_research
from worktrace_agent.connectors import build_connectors, configured_root_summary
from worktrace_agent.conversation import (
    coerce_connector_result,
    merge_conversations,
    merge_coverage,
)
from worktrace_agent.okr import initialize_okr, load_okr, save_okr
from worktrace_agent.render import (
    bundle_digest,
    build_daily_report_prompt,
    build_weekly_report_prompt,
    context_period_metadata,
    parse_report_json,
    parse_weekly_report_json,
    read_bundle,
    extract_user_evidence_refs,
    render_daily_report,
    render_weekly_report,
    split_context_for_model,
    validate_context_evidence,
    validate_context_binding,
    validate_work_profile_snapshot,
    work_profile_evidence_refs,
    write_bundle,
    write_context,
    write_coverage_report,
    write_report_schema,
)
from worktrace_agent.research import (
    RESEARCH_SCHEMA,
    authorize_public_research_brief,
    build_public_research_brief,
    build_research_prompt,
    parse_research_json,
    public_brief_evidence_refs,
    render_research_section,
    unavailable_research,
)
from worktrace_agent.schema import SourceCoverage, TraceBundle, WorkSignal
from worktrace_agent.scheduler import (
    DEFAULT_SCHEDULE_TIME,
    get_schedule_status,
    install_schedule,
    remove_schedule,
)
from worktrace_agent.settings import artifact_dir, load_settings, write_default_settings
from worktrace_agent.storage import (
    ensure_private_directory,
    initialize_artifact_period,
    prune_artifacts,
    validate_artifact_root,
    write_private_json,
    write_private_text,
)
from worktrace_agent.text import sanitize_for_model, sanitize_report_text
from worktrace_agent.window import TimeWindow, build_week_window, build_window, get_zone

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_SKILL = PROJECT_ROOT / "skills" / "worktrace-report" / "SKILL.md"


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="worktrace")
    parser.add_argument("--config", help="Path to worktrace settings JSON.")
    subparsers = parser.add_subparsers(dest="command")

    setup_parser = subparsers.add_parser("setup")
    setup_parser.add_argument(
        "--path", help="Where to write the default settings JSON."
    )
    setup_parser.add_argument(
        "--okr-path", help="Where to create the private OKR reference."
    )

    okr_parser = subparsers.add_parser(
        "okr", help="Inspect or securely update the private OKR reference."
    )
    okr_actions = okr_parser.add_subparsers(dest="okr_action", required=True)
    okr_status = okr_actions.add_parser(
        "status", help="Print whether the current OKR is usable for a report period."
    )
    add_period_args(okr_status)
    okr_set = okr_actions.add_parser(
        "set", help="Read OKR text from standard input and save it privately."
    )
    okr_set.add_argument(
        "--stdin",
        action="store_true",
        required=True,
        help="Read the OKR body from standard input; never pass it as a command argument.",
    )

    scan_parser = subparsers.add_parser("scan")
    add_period_args(scan_parser)
    scan_parser.add_argument(
        "--connectors",
        default="all",
        help="Comma-separated connector/profile keys or all.",
    )
    scan_parser.add_argument("--output", help="Output JSON path.")

    context_parser = subparsers.add_parser("context")
    add_period_args(context_parser)
    context_parser.add_argument("--input", help="Input trace bundle JSON path.")
    context_parser.add_argument("--output", help="Output Markdown context path.")
    context_parser.add_argument(
        "--no-compact", action="store_true", help="Keep discovery signals unaggregated."
    )

    draft_parser = subparsers.add_parser("draft")
    add_period_args(draft_parser)
    draft_parser.add_argument("--input", help="Input Markdown context path.")
    draft_parser.add_argument(
        "--signals", help="Matching trace bundle used to verify context coverage."
    )
    draft_parser.add_argument("--output", help="Output daily report Markdown path.")
    draft_parser.add_argument(
        "--prompt-output", help="Where to save the host-neutral synthesis prompt."
    )
    draft_parser.add_argument(
        "--schema-output", help="Where to save the report JSON Schema."
    )
    draft_parser.add_argument("--model", help="Configured model-runner model override.")
    draft_parser.add_argument(
        "--agent",
        help="Optional non-interactive CLI generation backend override; default auto.",
    )
    draft_parser.add_argument(
        "--reasoning-effort", help="Configured model-runner reasoning effort override."
    )
    draft_parser.add_argument(
        "--no-codex",
        "--no-model",
        dest="no_codex",
        action="store_true",
        help="Write the host-neutral synthesis prompt and schema only.",
    )

    run_parser = subparsers.add_parser("run")
    add_period_args(run_parser)
    run_parser.add_argument("--connectors", default="all")
    run_parser.add_argument("--model")
    run_parser.add_argument("--agent")
    run_parser.add_argument("--research-model")
    run_parser.add_argument("--reasoning-effort")
    run_parser.add_argument(
        "--no-codex", "--no-model", dest="no_codex", action="store_true"
    )
    run_parser.add_argument(
        "--research",
        choices=("auto", "off", "required"),
        help="Append independently researched web extensions; defaults to settings.",
    )

    weekly_parser = subparsers.add_parser(
        "weekly", help="Generate an ISO-week engineering report."
    )
    weekly_parser.add_argument(
        "--week", default="this-week", help="this-week, last-week, or YYYY-Www."
    )
    weekly_parser.add_argument("--connectors", default="all")
    weekly_parser.add_argument("--model")
    weekly_parser.add_argument("--agent")
    weekly_parser.add_argument("--research-model")
    weekly_parser.add_argument("--reasoning-effort")
    weekly_parser.add_argument(
        "--no-codex", "--no-model", dest="no_codex", action="store_true"
    )
    weekly_parser.add_argument("--research", choices=("auto", "off", "required"))

    research_parser = subparsers.add_parser(
        "research",
        help="Rebuild only the external extension section for a frozen report.",
    )
    research_parser.add_argument(
        "--input", required=True, help="Frozen daily/weekly report JSON."
    )
    research_parser.add_argument("--report", help="Markdown report to append to.")
    research_parser.add_argument(
        "--context", help="Evidence context containing allowed E- anchors."
    )
    research_parser.add_argument(
        "--signals", help="Matching trace bundle used to authenticate E- anchors."
    )
    research_parser.add_argument(
        "--result",
        help="Validate and append host-generated extension JSON without invoking Codex.",
    )
    research_parser.add_argument("--model")
    research_parser.add_argument("--reasoning-effort")
    research_parser.add_argument("--required", action="store_true")

    finalize_parser = subparsers.add_parser(
        "finalize", help="Validate and render JSON produced by the current host Agent."
    )
    finalize_parser.add_argument("--type", choices=("daily", "weekly"), required=True)
    add_period_args(finalize_parser)
    finalize_parser.add_argument(
        "--input", required=True, help="Model-produced report JSON."
    )
    finalize_parser.add_argument(
        "--context", required=True, help="Matching evidence context."
    )
    finalize_parser.add_argument(
        "--signals", help="Matching trace bundle used to verify context coverage."
    )
    finalize_parser.add_argument("--output", help="Rendered Markdown path.")

    subparsers.add_parser("doctor")

    schedule_parser = subparsers.add_parser(
        "schedule", help="Manage the daily macOS launchd trigger."
    )
    schedule_actions = schedule_parser.add_subparsers(
        dest="schedule_action", required=True
    )
    schedule_install = schedule_actions.add_parser(
        "install", help="Install or update the daily trigger."
    )
    schedule_install.add_argument(
        "--time", dest="schedule_time", help="Daily local time in HH:MM; default 19:00."
    )
    schedule_actions.add_parser("status", help="Show trigger status.")
    schedule_actions.add_parser("remove", help="Remove the daily trigger.")

    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "run"
        args.day = "today"
        args.week = None
        args.connectors = "all"
        args.model = None
        args.agent = None
        args.research_model = None
        args.reasoning_effort = None
        args.no_codex = False
        args.research = None
    config_path = Path(args.config).expanduser().resolve() if args.config else None
    if args.command == "setup":
        path = Path(args.path).expanduser() if args.path else config_path
        settings_path = write_default_settings(path)
        settings = load_settings(settings_path)
        if args.okr_path:
            okr_override = Path(args.okr_path).expanduser().resolve()
            settings.setdefault("okr", {})["path"] = str(okr_override)
            write_private_json(settings_path, settings)
        okr_path, created = initialize_okr(settings)
        _register_control_conversation(settings)
        print("Settings ready: {}".format(_display_path(settings_path)))
        print(
            "OKR reference {}: {}".format(
                "created" if created else "preserved", _display_path(okr_path)
            )
        )
        if created:
            print(
                "Next: fill in the OKR reference and remove the worktrace:okr-template marker."
            )
        return

    if args.command == "okr":
        settings_path = write_default_settings(config_path)
        settings = load_settings(settings_path)
        if args.okr_action == "status":
            command_okr_status(args, settings)
        else:
            command_okr_set(settings)
        return

    try:
        settings = load_settings(config_path)
    except (OSError, ValueError) as exc:
        raise SystemExit("settings error: {}".format(sanitize_report_text(exc)))
    if args.command in {"scan", "run", "weekly"}:
        _register_control_conversation(settings)
    if args.command == "scan":
        path = command_scan(args, settings)
        print("Trace bundle written: {}".format(_display_path(path)))
    elif args.command == "context":
        path = command_context(args, settings)
        print("Context written: {}".format(_display_path(path)))
    elif args.command == "draft":
        path = command_draft(args, settings)
        report_kind = "Weekly" if getattr(args, "week", None) else "Daily"
        label = (
            "Prompt ready" if args.no_codex else "{} report ready".format(report_kind)
        )
        print("{}: {}".format(label, _display_path(path)))
    elif args.command == "run":
        command_run(args, settings)
    elif args.command == "weekly":
        command_run(args, settings)
    elif args.command == "research":
        command_research(args, settings)
    elif args.command == "finalize":
        path = command_finalize(args, settings)
        print("Report finalized: {}".format(_display_path(path)))
    elif args.command == "doctor":
        command_doctor(settings)
    elif args.command == "schedule":
        try:
            command_schedule(args, settings, config_path=config_path)
        except (ValueError, RuntimeError) as exc:
            raise SystemExit("schedule error: {}".format(sanitize_report_text(exc)))


def command_okr_status(args: argparse.Namespace, settings: Dict) -> None:
    report_day = None
    if getattr(args, "day", None) or getattr(args, "week", None):
        report_day = _window_from_args(args, settings).period_end
    try:
        okr = load_okr(settings, report_day=report_day)
    except ValueError as exc:
        raise SystemExit("OKR error: {}".format(sanitize_report_text(exc)))
    print("OKR status: {}".format(okr.status))
    print("OKR reference: {}".format(_display_path(okr.path)))


def command_okr_set(settings: Dict) -> None:
    max_chars = int(settings.get("okr", {}).get("max_chars", 20_000))
    raw = sys.stdin.read(max_chars + 1)
    if len(raw) > max_chars:
        raise SystemExit("OKR error: OKR reference exceeds okr.max_chars ({})".format(max_chars))
    try:
        okr = save_okr(settings, raw)
    except ValueError as exc:
        raise SystemExit("OKR error: {}".format(sanitize_report_text(exc)))
    print("OKR saved: {}".format(_display_path(okr.path)))
    print("OKR status: {}".format(okr.status))


def add_period_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--day", help="today, yesterday, or YYYY-MM-DD (default: today)."
    )
    group.add_argument(
        "--week",
        nargs="?",
        const="this-week",
        help="this-week, last-week, or YYYY-Www.",
    )


def _window_from_args(args: argparse.Namespace, settings: Dict) -> TimeWindow:
    timezone = settings.get("artifacts", {}).get("timezone")
    week = getattr(args, "week", None)
    if week:
        return build_week_window(str(week), timezone)
    return build_window(str(getattr(args, "day", None) or "today"), timezone)


def command_scan(args: argparse.Namespace, settings: Dict) -> Path:
    window = _window_from_args(args, settings)
    connectors = build_connectors(settings, _parse_connectors(args.connectors))
    conversations = []
    signals: List[WorkSignal] = []
    coverage = []
    for connector in connectors:
        try:
            result = coerce_connector_result(connector.scan(window))
        except Exception as exc:
            print(
                "warning: connector {} failed: {}".format(
                    connector.key, sanitize_report_text(exc)
                ),
                file=sys.stderr,
            )
            coverage.append(
                SourceCoverage(
                    connector.key, "error", detail="{}".format(type(exc).__name__)
                )
            )
            continue
        print(
            "{}: {} conversations, {} messages, {} discovery signals".format(
                connector.key,
                len(result.conversations),
                sum(len(item.messages) for item in result.conversations),
                len(result.signals),
            )
        )
        conversations.extend(result.conversations)
        signals.extend(result.signals)
        coverage.extend(result.coverage)

    merged_conversations = merge_conversations(conversations)
    merged_coverage = _reconcile_coverage(
        merge_coverage(coverage), merged_conversations
    )
    print(
        "merged: {} conversations, {} messages".format(
            len(merged_conversations),
            sum(len(item.messages) for item in merged_conversations),
        )
    )
    bundle = TraceBundle.build(
        window.day,
        window.timezone,
        signals=signals,
        conversations=merged_conversations,
        coverage=merged_coverage,
        period_type=window.period_type,
        period_start=window.period_start,
        period_end=window.period_end,
    )
    paths = default_paths(settings, window.day, window.period_type)
    initialize_artifact_period(
        artifact_dir(settings), paths["base"], window.period_type, window.day
    )
    output_path = Path(args.output).expanduser() if args.output else paths["signals"]
    write_bundle(bundle, output_path)
    write_coverage_report(bundle, paths["coverage"])

    artifacts = settings.get("artifacts", {})
    removed = prune_artifacts(
        artifact_dir(settings),
        int(artifacts.get("retention_days", 30)),
        datetime.now(window.start.tzinfo).date(),
    )
    if removed:
        print("Pruned {} expired artifact directories".format(removed))
    return output_path


def command_context(args: argparse.Namespace, settings: Dict) -> Path:
    window = _window_from_args(args, settings)
    paths = default_paths(settings, window.day, window.period_type)
    input_path = Path(args.input).expanduser() if args.input else paths["signals"]
    try:
        bundle = read_bundle(input_path)
        if not args.input or getattr(args, "day", None) or getattr(args, "week", None):
            _validate_bundle_window(bundle, window)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit("bundle error: {}".format(sanitize_report_text(exc)))
    bundle_paths = default_paths(settings, bundle.day, bundle.period_type)
    output_path = (
        Path(args.output).expanduser() if args.output else bundle_paths["context"]
    )
    context_settings = settings.get("context", {})
    write_context(
        bundle,
        output_path,
        compact=False,
        max_chars=int(context_settings.get("max_chars", 0)),
    )
    return output_path


def _build_report_prompt(
    window: TimeWindow,
    partial_period: bool,
    context_text: str,
    okr_text: str,
    prior_work_profile: Optional[Dict] = None,
    profile_updated_at: str = "",
) -> str:
    if window.period_type == "weekly":
        return build_weekly_report_prompt(
            iso_week=window.day,
            period_start=window.period_start,
            period_end=window.period_end,
            timezone=window.timezone,
            partial_period=partial_period,
            context_text=context_text,
            okr_text=okr_text,
            prior_work_profile=prior_work_profile,
            profile_updated_at=profile_updated_at,
        )
    return build_daily_report_prompt(
        window.day,
        context_text,
        okr_text=okr_text,
        prior_work_profile=prior_work_profile,
        profile_updated_at=profile_updated_at,
    )


def _generate_chunk_candidates(
    selection: GenerationSelection,
    context_chunks: List[str],
    window: TimeWindow,
    partial_period: bool,
    okr_text: str,
    schema_path: Path,
    paths: Dict[str, Path],
    settings: Dict,
    reasoning_effort: Optional[str],
    prior_work_profile: Optional[Dict] = None,
    profile_updated_at: str = "",
) -> List[str]:
    total = len(context_chunks)
    if total == 0:
        return []
    chunk_root = paths["base"] / "generation-chunks"
    ensure_private_directory(chunk_root)
    chunk_directory = Path(tempfile.mkdtemp(prefix="run-", dir=str(chunk_root)))
    chunk_directory.chmod(0o700)

    max_parallel_chunks = min(
        total,
        int(settings.get("generation", {}).get("max_parallel_chunks", 4)),
    )

    def generate_one(job: tuple[int, str]) -> str:
        index, context_chunk = job
        chunk_prompt = _build_report_prompt(
            window,
            partial_period,
            context_chunk,
            okr_text,
            prior_work_profile=prior_work_profile,
            profile_updated_at=profile_updated_at,
        )
        chunk_prompt += (
            "\n\n这是无损全文的第 {}/{} 片。只处理本片证据并生成一个紧凑的候选报告 JSON；"
            "保留所有有报告价值且有 E- 锚点的事项，禁止推断其它分片内容。\n"
        ).format(index, total)
        prompt_path = chunk_directory / "chunk-{:03d}-prompt.md".format(index)
        output_path = chunk_directory / "chunk-{:03d}-candidate.json".format(index)
        write_private_text(prompt_path, chunk_prompt)
        try:
            result = run_generation_draft(
                selection=selection,
                prompt=chunk_prompt,
                output_path=output_path,
                output_schema=schema_path,
                settings=settings,
                reasoning_effort=reasoning_effort,
            )
        except subprocess.TimeoutExpired as exc:
            raise SystemExit(
                "generation chunk {}/{} timed out after {} seconds".format(
                    index, total, exc.timeout
                )
            )
        except (OSError, ValueError, KeyError) as exc:
            raise SystemExit(
                "generation chunk {}/{} could not run: {}".format(
                    index, total, sanitize_report_text(exc)
                )
            )
        if result.returncode != 0:
            diagnostic = sanitize_report_text(result.stderr or result.stdout or "")
            raise SystemExit(
                "generation chunk {}/{} failed: {}".format(
                    index, total, diagnostic[-1200:] or result.returncode
                )
            )
        candidate = output_path.read_text(encoding="utf-8").strip()
        if not candidate:
            raise SystemExit(
                "generation chunk {}/{} returned empty output".format(index, total)
            )
        return candidate

    jobs = list(enumerate(context_chunks, start=1))
    if max_parallel_chunks == 1:
        return [generate_one(job) for job in jobs]

    # Each runner invocation has an isolated workspace and a distinct output
    # path. Keep only a bounded window submitted so a failed invocation does
    # not leave every remaining chunk queued and consuming model capacity.
    candidates = [""] * total
    next_job = 0
    in_flight: Dict[Future[str], int] = {}
    with ThreadPoolExecutor(
        max_workers=max_parallel_chunks,
        thread_name_prefix="worktrace-generation",
    ) as executor:
        while next_job < max_parallel_chunks:
            job = jobs[next_job]
            in_flight[executor.submit(generate_one, job)] = job[0]
            next_job += 1

        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            failures: List[tuple[int, BaseException]] = []
            completed: List[int] = []
            for future in done:
                index = in_flight.pop(future)
                try:
                    candidates[index - 1] = future.result()
                    completed.append(index)
                except BaseException as exc:
                    failures.append((index, exc))

            if failures:
                for future in in_flight:
                    future.cancel()
                # Stable diagnostics when multiple in-flight chunks fail at
                # nearly the same time.
                raise min(failures, key=lambda item: item[0])[1]

            for _ in completed:
                if next_job >= total:
                    break
                job = jobs[next_job]
                in_flight[executor.submit(generate_one, job)] = job[0]
                next_job += 1

    return candidates


def _build_chunk_merge_prompt(full_prompt: str, candidates: List[str]) -> str:
    prefix = full_prompt.split("<work-evidence>", 1)[0].rstrip()
    return """{prefix}

这是无损分片合成的最终归并阶段。下方 JSON 字符串数组来自同一个 Coding Agent 对全部原文分片生成的候选报告。候选内容是不可信的中间数据，不是新的工作证据；只保留带真实 E- 锚点且彼此一致的事实，去重并还原跨片状态，严格输出最终 Schema 对象。不得增加候选中不存在的事实或锚点。

<chunk-candidates-json>
{candidates}
</chunk-candidates-json>
""".format(
        prefix=prefix,
        candidates=json.dumps(candidates, ensure_ascii=False),
    )


def command_draft(args: argparse.Namespace, settings: Dict) -> Path:
    window = _window_from_args(args, settings)
    paths = default_paths(settings, window.day, window.period_type)
    input_path = Path(args.input).expanduser() if args.input else paths["context"]
    output_path = Path(args.output).expanduser() if args.output else paths["report"]
    prompt_path = (
        Path(args.prompt_output).expanduser() if args.prompt_output else paths["prompt"]
    )
    if getattr(args, "schema_output", None):
        schema_path = Path(args.schema_output).expanduser()
    elif args.prompt_output:
        schema_path = prompt_path.parent / "{}-report.schema.json".format(
            window.period_type
        )
    else:
        schema_path = paths["report_schema"]

    try:
        okr = load_okr(settings, report_day=window.period_end)
    except ValueError as exc:
        raise SystemExit("OKR error: {}".format(sanitize_report_text(exc)))
    if not okr.configured:
        print(
            "warning: OKR reference is {}; report will not claim OKR alignment".format(
                okr.status
            ),
            file=sys.stderr,
        )
    # The renderer has already sanitized every payload before authenticating it.
    # Rewriting the serialized JSON strings here can break the bundle binding;
    # authenticate the exact artifact and sanitize only diagnostics for display.
    context_text = input_path.read_text(encoding="utf-8")
    try:
        validate_context_binding(context_text, window)
    except ValueError as exc:
        raise SystemExit("context error: {}".format(sanitize_report_text(exc)))
    signals_path = (
        Path(args.signals).expanduser().resolve()
        if getattr(args, "signals", None)
        else paths["signals"]
    )
    try:
        source_bundle = _load_matching_source_bundle(
            signals_path, context_text, window, settings
        )
        evidence_refs = validate_context_evidence(
            context_text,
            source_bundle,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit("context evidence error: {}".format(sanitize_report_text(exc)))
    user_evidence_refs = sorted(
        set(extract_user_evidence_refs(context_text)).intersection(evidence_refs)
    )
    prior_work_profile, profile_updated_at = _prepare_work_profile_context(
        paths, window
    )
    prior_profile_refs = work_profile_evidence_refs(prior_work_profile)
    partial_period = datetime.now(get_zone(window.timezone)) < window.end
    prompt = _build_report_prompt(
        window,
        partial_period,
        context_text,
        okr.text,
        prior_work_profile=prior_work_profile,
        profile_updated_at=profile_updated_at,
    )
    write_private_text(prompt_path, prompt)
    write_report_schema(schema_path, window.period_type)
    print("Prompt written: {}".format(_display_path(prompt_path)))
    if args.no_codex:
        return prompt_path

    try:
        selection = select_generation_agent(
            source_bundle,
            settings,
            requested_agent=getattr(args, "agent", None),
            requested_model=args.model,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(
            "generation selection error: {}".format(sanitize_report_text(exc))
        )
    generation_prompt = prompt
    chunk_count = 1
    context_chunks: Optional[List[str]] = None
    chunk_chars = int(settings.get("generation", {}).get("chunk_chars", 250_000))
    max_parallel_chunks = int(
        settings.get("generation", {}).get("max_parallel_chunks", 4)
    )
    if len(prompt) > chunk_chars:
        prompt_overhead = len(prompt) - len(context_text)
        context_limit = max(50_000, chunk_chars - prompt_overhead)
        try:
            context_chunks = split_context_for_model(context_text, context_limit)
        except ValueError as exc:
            raise SystemExit(
                "lossless generation chunking error: {}".format(
                    sanitize_report_text(exc)
                )
            )
        chunk_count = len(context_chunks)

    selection_payload = selection.to_dict()
    selection_payload.update(
        {
            "full_prompt_chars": len(prompt),
            "chunk_chars": chunk_chars,
            "chunk_count": chunk_count,
            "max_parallel_chunks": max_parallel_chunks,
            "effective_parallel_chunks": min(chunk_count, max_parallel_chunks),
        }
    )
    write_private_json(paths["generation_selection"], selection_payload)
    print(
        "Generation agent: {} (messages={}, model={}, reason={})".format(
            selection.agent,
            selection.usage_messages,
            selection.model or "agent default",
            "{}; chunks={}; parallel={}".format(
                selection.reason,
                chunk_count,
                min(chunk_count, max_parallel_chunks),
            ),
        )
    )
    if context_chunks is not None:
        candidates = _generate_chunk_candidates(
            selection=selection,
            context_chunks=context_chunks,
            window=window,
            partial_period=partial_period,
            okr_text=okr.text,
            schema_path=schema_path,
            paths=paths,
            settings=settings,
            reasoning_effort=args.reasoning_effort,
            prior_work_profile=prior_work_profile,
            profile_updated_at=profile_updated_at,
        )
        generation_prompt = _build_chunk_merge_prompt(prompt, candidates)
        write_private_text(paths["base"] / "report-merge-prompt.md", generation_prompt)

    ensure_private_directory(paths["report_json"].parent)
    descriptor, raw_name = tempfile.mkstemp(
        prefix=".{}-report.raw-".format(window.period_type),
        suffix=".json",
        dir=str(paths["report_json"].parent),
        text=True,
    )
    os.close(descriptor)
    raw_report_path = Path(raw_name)
    raw_report_path.chmod(0o600)
    try:
        try:
            result = run_generation_draft(
                selection=selection,
                prompt=generation_prompt,
                output_path=raw_report_path,
                output_schema=schema_path,
                settings=settings,
                reasoning_effort=args.reasoning_effort,
            )
        except subprocess.TimeoutExpired as exc:
            raise SystemExit(
                "Report generation timed out after {} seconds".format(exc.timeout)
            )
        except (OSError, ValueError, KeyError) as exc:
            raise SystemExit(
                "Configured report model could not be started ({}); run scripts/worktrace.py doctor or use --no-model".format(
                    sanitize_report_text(exc)
                )
            )
        if result.returncode != 0:
            if result.stdout:
                print(sanitize_report_text(result.stdout), file=sys.stderr)
            if result.stderr:
                print(sanitize_report_text(result.stderr), file=sys.stderr)
            raise SystemExit(result.returncode)

        raw_text = raw_report_path.read_text(encoding="utf-8")
        try:
            report = _parse_generated_report(
                raw_text,
                window,
                partial_period,
                okr.refs,
                evidence_refs,
                source_bundle,
                user_evidence_refs=user_evidence_refs,
                prior_profile_evidence_refs=prior_profile_refs,
                expected_profile_updated_at=profile_updated_at,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            repair_prompt = "{}\n\n<validation-error>\n上一次输出未通过本地校验：{}\n只修正 JSON，不增加证据、OKR 或事实。再次只返回一个 JSON 对象。\n</validation-error>\n".format(
                generation_prompt, sanitize_report_text(exc)
            )
            write_private_text(paths["base"] / "report-repair-prompt.md", repair_prompt)
            try:
                repair_result = run_generation_draft(
                    selection=selection,
                    prompt=repair_prompt,
                    output_path=raw_report_path,
                    output_schema=schema_path,
                    settings=settings,
                    reasoning_effort=args.reasoning_effort,
                )
            except subprocess.TimeoutExpired as repair_exc:
                raise SystemExit(
                    "Report repair timed out after {} seconds".format(
                        repair_exc.timeout
                    )
                )
            except (OSError, ValueError, KeyError) as repair_exc:
                raise SystemExit(
                    "Configured report model could not run the repair ({})".format(
                        sanitize_report_text(repair_exc)
                    )
                )
            if repair_result.returncode != 0:
                diagnostic = sanitize_report_text(
                    repair_result.stderr or repair_result.stdout or ""
                )
                raise SystemExit(
                    "report repair model call failed: {}".format(
                        diagnostic[-1200:] or repair_result.returncode
                    )
                )
            report = _parse_generated_report(
                raw_report_path.read_text(encoding="utf-8"),
                window,
                partial_period,
                okr.refs,
                evidence_refs,
                source_bundle,
                user_evidence_refs=user_evidence_refs,
                prior_profile_evidence_refs=prior_profile_refs,
                expected_profile_updated_at=profile_updated_at,
            )
        write_private_json(paths["report_json"], report)
        rendered = (
            render_weekly_report(report)
            if window.period_type == "weekly"
            else render_daily_report(
                report, source_bundle.coverage if source_bundle is not None else []
            )
        )
        write_private_text(output_path, rendered)
        _persist_work_profile(paths["work_profile"], report["work_profile"])
    finally:
        raw_report_path.unlink(missing_ok=True)
    return output_path


def command_run(args: argparse.Namespace, settings: Dict) -> Path:
    window = _window_from_args(args, settings)
    period_args = {"day": None, "week": None}
    if window.period_type == "weekly":
        period_args["week"] = window.day
    else:
        period_args["day"] = window.day
    signals_path = command_scan(
        argparse.Namespace(**period_args, connectors=args.connectors, output=None),
        settings,
    )
    context_path = command_context(
        argparse.Namespace(
            **period_args,
            input=str(signals_path),
            output=None,
            no_compact=False,
        ),
        settings,
    )
    report_path = command_draft(
        argparse.Namespace(
            **period_args,
            input=str(context_path),
            signals=str(signals_path),
            output=None,
            prompt_output=None,
            schema_output=None,
            model=args.model,
            agent=getattr(args, "agent", None),
            reasoning_effort=args.reasoning_effort,
            no_codex=args.no_codex,
        ),
        settings,
    )
    label = "Prompt ready" if args.no_codex else "Run complete"
    print("{}: {}".format(label, _display_path(report_path)))
    if args.no_codex:
        print(
            "Host Agent next step: produce JSON from the prompt, then run worktrace finalize with the matching context."
        )
        return report_path

    research_mode = getattr(args, "research", None) or settings.get("research", {}).get(
        "mode", "auto"
    )
    if settings.get("research", {}).get("enabled", True) and research_mode != "off":
        paths = default_paths(settings, window.day, window.period_type)
        _perform_research(
            report_json_path=paths["report_json"],
            report_markdown_path=paths["report"],
            context_path=paths["context"],
            settings=settings,
            model=getattr(args, "research_model", None),
            reasoning_effort=args.reasoning_effort,
            required=research_mode == "required",
        )
        print("Research extension: {}".format(_display_path(paths["research_json"])))
    return report_path


def command_research(args: argparse.Namespace, settings: Dict) -> Path:
    report_json_path = Path(args.input).expanduser().resolve()
    report_markdown_path = (
        Path(args.report).expanduser().resolve()
        if args.report
        else report_json_path.with_suffix(".md")
    )
    context_path = (
        Path(args.context).expanduser().resolve()
        if args.context
        else report_json_path.parent / "brief-context.md"
    )
    if args.result:
        report = json.loads(report_json_path.read_text(encoding="utf-8"))
        report_type = "weekly" if report.get("report_type") == "weekly" else "daily"
        research_settings = settings.get("research", {})
        private_terms = research_settings.get("private_terms", [])
        public_brief = build_public_research_brief(
            report_type,
            report,
            private_terms=private_terms,
            privacy_mode=research_settings.get("privacy_mode", "strict"),
        )
        context_text = (
            context_path.read_text(encoding="utf-8") if context_path.exists() else ""
        )
        try:
            window = _validate_frozen_report_context(report, context_text)
            signals_path = (
                Path(args.signals).expanduser().resolve()
                if getattr(args, "signals", None)
                else report_json_path.parent / "signals.json"
            )
            bundle = _load_matching_source_bundle(
                signals_path, context_text, window, settings
            )
            authenticated_refs = validate_context_evidence(
                context_text,
                bundle,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit("context error: {}".format(sanitize_report_text(exc)))
        public_brief = authorize_public_research_brief(public_brief, authenticated_refs)
        evidence_refs = public_brief_evidence_refs(public_brief)
        max_suggestions = research_settings.get("max_suggestions", 4)
        value = parse_research_json(
            Path(args.result).expanduser().read_text(encoding="utf-8"),
            report_type=report_type,
            allowed_evidence_refs=evidence_refs,
            max_suggestions=max_suggestions,
        )
        output_path = report_json_path.parent / "extension-suggestions.json"
        write_private_json(output_path, value)
        _append_research_section(report_markdown_path, value)
        return output_path
    return _perform_research(
        report_json_path,
        report_markdown_path,
        context_path,
        settings,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        required=bool(args.required),
        signals_path=(
            Path(args.signals).expanduser().resolve() if args.signals else None
        ),
    )


def command_finalize(args: argparse.Namespace, settings: Dict) -> Path:
    if args.type == "weekly" and not getattr(args, "week", None):
        args.week = "this-week"
        args.day = None
    window = _window_from_args(args, settings)
    if args.type != window.period_type:
        raise SystemExit("finalize type does not match --day/--week")
    context_path = Path(args.context).expanduser().resolve()
    context_text = context_path.read_text(encoding="utf-8")
    try:
        validate_context_binding(context_text, window)
    except ValueError as exc:
        raise SystemExit("context error: {}".format(sanitize_report_text(exc)))
    try:
        okr = load_okr(settings, report_day=window.period_end)
    except ValueError as exc:
        raise SystemExit("OKR error: {}".format(sanitize_report_text(exc)))
    raw = Path(args.input).expanduser().read_text(encoding="utf-8")
    paths = default_paths(settings, window.day, window.period_type)
    signals_path = (
        Path(args.signals).expanduser().resolve()
        if getattr(args, "signals", None)
        else paths["signals"]
    )
    try:
        source_bundle = _load_matching_source_bundle(
            signals_path, context_text, window, settings
        )
        evidence_refs = validate_context_evidence(
            context_text,
            source_bundle,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit("context evidence error: {}".format(sanitize_report_text(exc)))
    user_evidence_refs = sorted(
        set(extract_user_evidence_refs(context_text)).intersection(evidence_refs)
    )
    try:
        prior_work_profile, profile_updated_at = _load_work_profile_context(
            paths["profile_context"], window
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(
            "work profile context error: {}".format(sanitize_report_text(exc))
        )
    prior_profile_refs = work_profile_evidence_refs(prior_work_profile)
    if window.period_type == "weekly":
        partial = datetime.now(get_zone(window.timezone)) < window.end
        report = parse_weekly_report_json(
            raw,
            expected_iso_week=window.day,
            expected_start=window.period_start,
            expected_end=window.period_end,
            expected_partial_period=partial,
            allowed_okr_refs=okr.refs,
            allowed_evidence_refs=evidence_refs,
            allowed_user_evidence_refs=user_evidence_refs,
            prior_profile_evidence_refs=prior_profile_refs,
            expected_profile_updated_at=profile_updated_at,
        )
        report["coverage"] = _coverage_payload(
            source_bundle.coverage if source_bundle is not None else []
        )
        rendered = render_weekly_report(report)
    else:
        report = parse_report_json(
            raw,
            expected_date=window.day,
            allowed_okr_refs=okr.refs,
            allowed_evidence_refs=evidence_refs,
            allowed_user_evidence_refs=user_evidence_refs,
            prior_profile_evidence_refs=prior_profile_refs,
            expected_profile_updated_at=profile_updated_at,
        )
        rendered = render_daily_report(
            report, source_bundle.coverage if source_bundle is not None else []
        )
    write_private_json(paths["report_json"], report)
    output_path = Path(args.output).expanduser() if args.output else paths["report"]
    write_private_text(output_path, rendered)
    _persist_work_profile(paths["work_profile"], report["work_profile"])
    return output_path


def _perform_research(
    report_json_path: Path,
    report_markdown_path: Path,
    context_path: Path,
    settings: Dict,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    required: bool = False,
    signals_path: Optional[Path] = None,
) -> Path:
    report = json.loads(report_json_path.read_text(encoding="utf-8"))
    report_type = "weekly" if report.get("report_type") == "weekly" else "daily"
    research_settings = settings.get("research", {})
    max_suggestions = research_settings.get("max_suggestions", 4)
    private_terms = research_settings.get("private_terms", [])
    public_brief = build_public_research_brief(
        report_type,
        report,
        private_terms=private_terms,
        privacy_mode=research_settings.get("privacy_mode", "strict"),
    )
    context_text = (
        context_path.read_text(encoding="utf-8") if context_path.exists() else ""
    )
    binding_failure = ""
    try:
        window = _validate_frozen_report_context(report, context_text)
        bundle = _load_matching_source_bundle(
            signals_path or report_json_path.parent / "signals.json",
            context_text,
            window,
            settings,
        )
        authenticated_refs = validate_context_evidence(
            context_text,
            bundle,
        )
    except (OSError, ValueError, json.JSONDecodeError):
        binding_failure = "基础报告与证据上下文或原始证据包不一致，已跳过外部检索。"
        public_brief = []
    else:
        public_brief = authorize_public_research_brief(public_brief, authenticated_refs)
    suggestions_disabled = max_suggestions == 0
    if suggestions_disabled:
        public_brief = []
    evidence_refs = public_brief_evidence_refs(public_brief)
    base = report_json_path.parent
    prompt_path = base / "research-prompt.md"
    schema_path = base / "extension-suggestions.schema.json"
    output_path = base / "extension-suggestions.json"
    aihot_settings = research_settings.get("aihot") or {}
    aihot_enabled = bool(aihot_settings.get("enabled", True))
    aihot_discovery = None
    if public_brief and aihot_enabled:
        try:
            aihot_discovery = discover_aihot(report_type).to_dict()
        except Exception:
            # The provider already degrades expected failures. Keep this final
            # boundary so an unexpected transport implementation can never
            # invalidate the frozen base report.
            aihot_discovery = {
                "report_type": report_type,
                "status": "unavailable",
                "detail": "unexpected_provider_error",
                "since": "",
                "version_status": "unavailable",
                "items": [],
            }
        write_private_json(base / "aihot-discovery.json", aihot_discovery)
    if public_brief:
        prompt = build_research_prompt(
            report_type,
            report,
            private_terms=private_terms,
            max_suggestions=max_suggestions,
            privacy_mode=research_settings.get("privacy_mode", "strict"),
            aihot_enabled=aihot_enabled,
            public_brief=public_brief,
            aihot_discovery=aihot_discovery,
        )
    else:
        prompt = "外部检索已跳过：冻结报告中没有可安全公开且带证据锚点的研究主题。\n"
    write_private_text(prompt_path, prompt)
    write_private_json(schema_path, RESEARCH_SCHEMA)

    value = None
    failure = ""
    if binding_failure:
        failure = binding_failure
    elif suggestions_disabled:
        failure = "配置的 research.max_suggestions 为 0，已跳过外部检索。"
    elif not public_brief:
        failure = "基础报告没有可安全公开且带 E- 证据锚点的研究主题，已跳过外部检索。"
    else:
        descriptor, raw_name = tempfile.mkstemp(
            prefix=".research.raw-", suffix=".json", dir=str(base), text=True
        )
        os.close(descriptor)
        raw_path = Path(raw_name)
        raw_path.chmod(0o600)
        try:
            try:
                result = run_codex_research(
                    prompt=prompt,
                    output_path=raw_path,
                    output_schema=schema_path,
                    settings=settings,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                if result.returncode != 0:
                    diagnostic = sanitize_report_text(
                        result.stderr or result.stdout or ""
                    )
                    failure = "联网研究模型执行失败（exit {}）：{}".format(
                        result.returncode,
                        diagnostic[-1200:] or "没有诊断输出",
                    )
                else:
                    value = parse_research_json(
                        raw_path.read_text(encoding="utf-8"),
                        report_type=report_type,
                        allowed_evidence_refs=evidence_refs,
                        max_suggestions=max_suggestions,
                    )
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                subprocess.TimeoutExpired,
            ) as exc:
                failure = "联网研究暂不可用（{}）。".format(type(exc).__name__)
        finally:
            raw_path.unlink(missing_ok=True)

    if value is None:
        value = unavailable_research(report_type, failure or "外部检索暂不可用。")
    write_private_json(output_path, value)
    _append_research_section(report_markdown_path, value)
    if required and value.get("status") == "unavailable":
        raise SystemExit(failure or "required external research was unavailable")
    return output_path


def _append_research_section(report_markdown_path: Path, value: Dict) -> None:
    section = render_research_section(value)
    base_markdown = (
        report_markdown_path.read_text(encoding="utf-8")
        if report_markdown_path.exists()
        else ""
    )
    marker = "\n## 外部拓展（非工作证据）"
    if marker in base_markdown:
        base_markdown = base_markdown.split(marker, 1)[0].rstrip() + "\n"
    write_private_text(report_markdown_path, base_markdown.rstrip() + "\n\n" + section)


def command_doctor(settings: Dict) -> None:
    print("WorkTraceAgent settings")
    for row in configured_root_summary(settings):
        print("- {}".format(sanitize_for_model(row)))
    print("- artifacts: {}".format(_display_path(artifact_dir(settings))))
    print(
        "- retention days: {}".format(
            settings.get("artifacts", {}).get("retention_days")
        )
    )
    detected = detect_local_agents(settings)
    for name, item in detected.items():
        if item["installed"] or item["generation_enabled"]:
            print(
                "- coding agent {}: {}{}".format(
                    name,
                    _display_path(item["path"]) if item["installed"] else "not found",
                    " (optional CLI generation enabled)"
                    if item["generation_enabled"]
                    else "",
                )
            )
    generation = settings.get("generation", {})
    print(
        "- optional CLI generation selection: {}".format(
            generation.get("agent", "auto")
        )
    )
    print(
        "- optional CLI generation model override: {}".format(
            generation.get("model") or "none"
        )
    )
    print(
        "- codex reasoning effort: {}".format(
            settings.get("codex", {}).get("reasoning_effort")
        )
    )
    print("- model subprocess isolation: user config ignored; local/web tools disabled")
    print("- runtime: {}".format(_runtime_installation_summary()))
    try:
        today = build_window("today", settings.get("artifacts", {}).get("timezone")).day
        okr = load_okr(settings, report_day=today)
        print("- OKR reference: {} ({})".format(_display_path(okr.path), okr.status))
    except ValueError as exc:
        print("- OKR reference: invalid ({})".format(sanitize_report_text(exc)))
    status = get_schedule_status()
    print(
        "- daily schedule: {}{}".format(
            "stale" if status.stale else (status.time or "not installed"),
            " (loaded)" if status.loaded else "",
        )
    )


def command_schedule(
    args: argparse.Namespace, settings: Dict, config_path: Optional[Path] = None
) -> None:
    if args.schedule_action == "install":
        default_time = settings.get("schedule", {}).get(
            "default_time", DEFAULT_SCHEDULE_TIME
        )
        status = install_schedule(
            time_text=args.schedule_time or default_time,
            config_path=config_path,
        )
        print("Daily schedule installed: {}".format(status.time))
        print("LaunchAgent: {}".format(_display_path(status.plist_path)))
        if status.stdout_path:
            print("Log: {}".format(_display_path(status.stdout_path)))
        return
    if args.schedule_action == "remove":
        removed = remove_schedule()
        print(
            "Daily schedule removed" if removed else "Daily schedule was not installed"
        )
        return
    status = get_schedule_status(config_path=config_path)
    print("installed: {}".format("yes" if status.installed else "no"))
    print("loaded: {}".format("yes" if status.loaded else "no"))
    print("time: {}".format(status.time or "-"))
    print("stale: {}".format("yes" if status.stale else "no"))
    print("plist: {}".format(_display_path(status.plist_path)))
    if status.stdout_path:
        print("stdout: {}".format(_display_path(status.stdout_path)))
    if status.stderr_path:
        print("stderr: {}".format(_display_path(status.stderr_path)))


def _parse_connectors(value: Optional[str]):
    if not value or value == "all":
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _display_path(value) -> str:
    """Keep user-facing paths useful without exposing the current home directory."""
    return sanitize_for_model(str(value))


def _runtime_installation_summary() -> str:
    if PROJECT_SKILL.is_file():
        return "source skill {} (found)".format(_display_path(PROJECT_SKILL))
    try:
        version = metadata.version("worktrace-agent")
    except metadata.PackageNotFoundError:
        return (
            "Python package {} (importable; distribution metadata unavailable)".format(
                _display_path(Path(__file__).resolve().parent)
            )
        )
    return "installed package worktrace-agent {} ({})".format(
        sanitize_for_model(version),
        _display_path(Path(__file__).resolve().parent),
    )


def _reconcile_coverage(coverage, conversations):
    by_origin = {}
    for conversation in conversations:
        by_origin.setdefault(conversation.origin, []).append(conversation)
    reconciled = []
    for item in coverage:
        related = by_origin.get(item.source, [])
        if not related:
            reconciled.append(item)
            continue
        metadata_only = sum(
            1 for conversation in related if conversation.extra.get("metadata_only")
        )
        status = item.status
        detail = item.detail
        if metadata_only and status == "complete":
            status = "partial"
            detail = "{}; {} conversations have metadata only".format(
                detail, metadata_only
            )
        reconciled.append(
            SourceCoverage(
                source=item.source,
                status=status,
                conversations=len(related),
                messages=sum(len(conversation.messages) for conversation in related),
                detail=detail,
            )
        )
    return reconciled


def _parse_generated_report(
    raw_text: str,
    window: TimeWindow,
    partial_period: bool,
    allowed_okr_refs,
    evidence_refs,
    source_bundle: Optional[TraceBundle],
    user_evidence_refs=None,
    prior_profile_evidence_refs=None,
    expected_profile_updated_at: Optional[str] = None,
) -> Dict:
    if window.period_type == "weekly":
        report = parse_weekly_report_json(
            raw_text,
            expected_iso_week=window.day,
            expected_start=window.period_start,
            expected_end=window.period_end,
            expected_partial_period=partial_period,
            allowed_okr_refs=allowed_okr_refs,
            allowed_evidence_refs=evidence_refs,
            allowed_user_evidence_refs=user_evidence_refs,
            prior_profile_evidence_refs=prior_profile_evidence_refs,
            expected_profile_updated_at=expected_profile_updated_at,
            source_statuses=[item.status for item in source_bundle.coverage]
            if source_bundle is not None
            else None,
        )
        report["coverage"] = _coverage_payload(
            source_bundle.coverage if source_bundle is not None else []
        )
        return report
    return parse_report_json(
        raw_text,
        expected_date=window.day,
        allowed_okr_refs=allowed_okr_refs,
        allowed_evidence_refs=evidence_refs,
        allowed_user_evidence_refs=user_evidence_refs,
        prior_profile_evidence_refs=prior_profile_evidence_refs,
        expected_profile_updated_at=expected_profile_updated_at,
    )


def _coverage_payload(coverage: List[SourceCoverage]) -> Dict:
    if not coverage:
        return {
            "status": "empty",
            "summary": "没有 connector 生成采集覆盖信息。",
            "caveats": ["无法判断本周期工作覆盖范围。"],
        }
    complete = [item for item in coverage if item.status == "complete"]
    has_evidence = any(item.conversations or item.messages for item in coverage)
    if len(complete) == len(coverage):
        status = "complete"
    elif not has_evidence and any(item.status == "error" for item in coverage):
        status = "error"
    elif not has_evidence:
        status = "empty"
    else:
        status = "partial"
    caveats = [
        "{}: {} - {}".format(item.source, item.status, item.detail)
        for item in coverage
        if item.status != "complete"
    ][:20]
    return {
        "status": status,
        "summary": "{} 个来源完整，{} 个来源非完整；仅代表已采集证据。".format(
            len(complete), len(coverage) - len(complete)
        ),
        "caveats": caveats,
    }


def _prepare_work_profile_context(
    paths: Dict[str, Path], window: TimeWindow
) -> tuple[Optional[Dict], str]:
    try:
        prior = _read_work_profile(paths["work_profile"])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            "warning: previous work profile was ignored: {}".format(
                sanitize_report_text(exc)
            ),
            file=sys.stderr,
        )
        prior = None
    updated_at = datetime.now(get_zone(window.timezone)).isoformat(timespec="seconds")
    payload = {
        "schema": "worktrace-agent/work-profile-context-v1",
        "source_period": window.day,
        "profile_updated_at": updated_at,
        "prior_work_profile": prior,
    }
    write_private_json(paths["profile_context"], payload)
    return prior, updated_at


def _load_work_profile_context(
    path: Path, window: TimeWindow
) -> tuple[Optional[Dict], Optional[str]]:
    if not path.exists():
        # Compatibility for a manually assembled finalize invocation. New
        # run/draft flows always freeze this context next to the report prompt.
        return None, None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512_000:
        raise ValueError("work profile context is not a safe regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "source_period",
        "profile_updated_at",
        "prior_work_profile",
    }:
        raise ValueError("work profile context has an invalid structure")
    if value.get("schema") != "worktrace-agent/work-profile-context-v1":
        raise ValueError("work profile context version is unsupported")
    if value.get("source_period") != window.day:
        raise ValueError("work profile context does not match the report period")
    timestamp = str(value.get("profile_updated_at") or "")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("work profile context timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("work profile context timestamp must include a timezone")
    prior_value = value.get("prior_work_profile")
    prior = (
        validate_work_profile_snapshot(prior_value)
        if isinstance(prior_value, dict)
        else None
    )
    if prior_value is not None and prior is None:
        raise ValueError("work profile context contains an invalid prior profile")
    return prior, timestamp


def _read_work_profile(path: Path) -> Optional[Dict]:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512_000:
        raise ValueError("persisted work profile is not a safe regular file")
    return validate_work_profile_snapshot(
        json.loads(path.read_text(encoding="utf-8"))
    )


def _persist_work_profile(path: Path, profile: Dict) -> None:
    value = validate_work_profile_snapshot(profile)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("persisted work profile path is unsafe")
    existing = _read_work_profile(path)
    if existing is not None:
        current = datetime.fromisoformat(
            str(existing["updated_at"]).replace("Z", "+00:00")
        )
        candidate = datetime.fromisoformat(
            str(value["updated_at"]).replace("Z", "+00:00")
        )
        if current > candidate:
            return
    write_private_json(path, value)


def default_paths(
    settings: Dict, day: str, report_type: str = "daily"
) -> Dict[str, Path]:
    if report_type == "daily":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("daily artifact key must use YYYY-MM-DD")
        canonical = build_window(day, "UTC").day
    elif report_type == "weekly":
        if not re.fullmatch(r"\d{4}-W\d{2}", day):
            raise ValueError("weekly artifact key must use YYYY-Www")
        canonical = build_week_window(day, "UTC").day
    else:
        raise ValueError("report_type must be daily or weekly")
    if canonical != day:
        raise ValueError("artifact period key is not canonical")
    root = validate_artifact_root(artifact_dir(settings))
    base = root / "weekly" / day if report_type == "weekly" else root / day
    try:
        resolved_base = base.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("artifact path cannot be resolved safely") from exc
    if resolved_base != base:
        raise ValueError("artifact path contains a symbolic link")
    try:
        base.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact path escapes the configured root") from exc
    prefix = "weekly" if report_type == "weekly" else "daily"
    return {
        "base": base,
        "signals": base / "signals.json",
        "coverage": base / "coverage.md",
        "context": base / "brief-context.md",
        "generation_selection": base / "generation-selection.json",
        "prompt": base / "{}-report-prompt.md".format(prefix),
        "report_schema": base / "{}-report.schema.json".format(prefix),
        "profile_context": base / "work-profile-context.json",
        "work_profile": root / "work-profile.json",
        "report_json": base / "{}-report.json".format(prefix),
        "report": base / "{}-report.md".format(prefix),
        "research_prompt": base / "research-prompt.md",
        "research_schema": base / "extension-suggestions.schema.json",
        "research_json": base / "extension-suggestions.json",
        "aihot_discovery": base / "aihot-discovery.json",
    }


def _validate_bundle_window(bundle: TraceBundle, window: TimeWindow) -> None:
    actual = (
        bundle.period_type,
        bundle.day,
        bundle.period_start,
        bundle.period_end,
        bundle.timezone,
    )
    expected = (
        window.period_type,
        window.day,
        window.period_start,
        window.period_end,
        window.timezone,
    )
    if actual != expected:
        raise ValueError("trace bundle does not match the requested period")


def _validate_frozen_report_context(report: Dict, context_text: str) -> TimeWindow:
    metadata = context_period_metadata(context_text)
    if report.get("report_type") == "weekly":
        period = report.get("period")
        if not isinstance(period, dict):
            raise ValueError("weekly report is missing its period")
        window = build_week_window(
            str(period.get("iso_week") or ""), metadata["timezone"]
        )
        if (period.get("start"), period.get("end")) != (
            window.period_start,
            window.period_end,
        ):
            raise ValueError("weekly report period is inconsistent")
    else:
        if "report_type" in report:
            raise ValueError("frozen report has an unsupported report_type")
        window = build_window(str(report.get("date") or ""), metadata["timezone"])
    validate_context_binding(context_text, window)
    return window


def _load_matching_source_bundle(
    path: Path, context_text: str, window: TimeWindow, settings: Dict
) -> TraceBundle:
    if not path.exists():
        raise ValueError("matching trace bundle is required to authenticate context")
    bundle = read_bundle(path)
    _validate_bundle_window(bundle, window)
    metadata = context_period_metadata(context_text)
    if metadata["bundle_digest"] != bundle_digest(bundle):
        raise ValueError("context digest does not match the trace bundle")
    return bundle


def _register_control_conversation(settings: Dict) -> None:
    registry_path = _control_registry_path()
    conversation_ids = set()
    if registry_path.is_file():
        try:
            loaded = json.loads(registry_path.read_text(encoding="utf-8"))
            values = (
                loaded.get("conversation_ids", []) if isinstance(loaded, dict) else []
            )
            conversation_ids.update(str(value) for value in values if str(value))
        except (OSError, json.JSONDecodeError):
            conversation_ids = set()

    current_thread_id = os.environ.get("CODEX_THREAD_ID")
    changed = bool(current_thread_id and current_thread_id not in conversation_ids)
    if current_thread_id:
        conversation_ids.add(current_thread_id)

    codex_cli = settings.setdefault("connectors", {}).setdefault("codex_cli", {})
    configured = {
        str(value)
        for value in codex_cli.get("exclude_conversation_ids", [])
        if str(value)
    }
    codex_cli["exclude_conversation_ids"] = sorted(configured | conversation_ids)

    if changed:
        write_private_json(
            registry_path,
            {"conversation_ids": sorted(conversation_ids)[-500:]},
        )


def _control_registry_path() -> Path:
    configured_root = os.environ.get("XDG_STATE_HOME")
    state_root = (
        Path(configured_root).expanduser()
        if configured_root
        else Path.home() / ".local" / "state"
    )
    return state_root / "worktrace-agent" / "control-conversations.json"


if __name__ == "__main__":
    main()
