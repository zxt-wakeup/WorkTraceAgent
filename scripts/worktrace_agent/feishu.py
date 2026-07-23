from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from worktrace_agent.settings import DEFAULT_CONFIG_PATH, expand_path
from worktrace_agent.storage import write_private_json


STATE_SCHEMA = "worktrace-agent/feishu-publishing-v1"
MAX_REPORT_BYTES = 4 * 1024 * 1024


class FeishuError(RuntimeError):
    """A safe, user-facing Feishu publishing failure."""


@dataclass(frozen=True)
class PublishResult:
    action: str
    report_type: str
    period: str
    title: str
    url: str


class FeishuClient:
    def __init__(self, command: str, timeout_seconds: int = 120):
        self.command = _resolve_command(command)
        self.timeout_seconds = timeout_seconds

    def call(
        self, arguments: List[str], *, input_text: Optional[str] = None
    ) -> Dict[str, Any]:
        environment = os.environ.copy()
        environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        try:
            result = subprocess.run(
                [self.command, *arguments],
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise FeishuError("Feishu CLI timed out") from exc
        except OSError as exc:
            raise FeishuError("Feishu CLI could not be started") from exc

        envelope = _parse_cli_envelope(result.stdout, result.stderr)
        if result.returncode != 0 or envelope.get("ok") is not True:
            error = envelope.get("error") if isinstance(envelope, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            hint = error.get("hint") if isinstance(error, dict) else None
            detail = str(message or hint or "Feishu CLI request failed")
            raise FeishuError(_safe_detail(detail))
        data = envelope.get("data")
        return data if isinstance(data, dict) else {}

    def verify_auth(self) -> Dict[str, Any]:
        return self.call(["auth", "status", "--json", "--verify"])

    def list_files(self, folder_token: str) -> List[Dict[str, Any]]:
        files: List[Dict[str, Any]] = []
        page_token = ""
        while True:
            params: Dict[str, Any] = {
                "folder_token": folder_token,
                "page_size": 200,
            }
            if page_token:
                params["page_token"] = page_token
            data = self.call(
                [
                    "drive",
                    "files",
                    "list",
                    "--as",
                    "user",
                    "--params",
                    json.dumps(params, ensure_ascii=False, separators=(",", ":")),
                    "--format",
                    "json",
                ]
            )
            page = data.get("files")
            if isinstance(page, list):
                files.extend(item for item in page if isinstance(item, dict))
            if not data.get("has_more"):
                break
            next_page = data.get("next_page_token") or data.get("page_token")
            if not isinstance(next_page, str) or not next_page or next_page == page_token:
                raise FeishuError("Feishu Drive returned invalid pagination data")
            page_token = next_page
        return files

    def create_folder(self, name: str, parent_token: str) -> Dict[str, Any]:
        arguments = [
            "drive",
            "+create-folder",
            "--as",
            "user",
            "--name",
            name,
        ]
        if parent_token:
            arguments.extend(["--folder-token", parent_token])
        arguments.extend(["--format", "json"])
        return self.call(arguments)

    def create_document(
        self, title: str, body: str, parent_token: str
    ) -> Dict[str, Any]:
        return self.call(
            [
                "docs",
                "+create",
                "--as",
                "user",
                "--parent-token",
                parent_token,
                "--doc-format",
                "markdown",
                "--title",
                title,
                "--content",
                "-",
                "--format",
                "json",
            ],
            input_text=body,
        )

    def overwrite_document(self, token: str, body: str) -> Dict[str, Any]:
        return self.call(
            [
                "docs",
                "+update",
                "--as",
                "user",
                "--doc",
                token,
                "--command",
                "overwrite",
                "--doc-format",
                "markdown",
                "--content",
                "-",
                "--format",
                "json",
            ],
            input_text=body,
        )

    def fetch_document(self, token: str) -> Dict[str, Any]:
        return self.call(
            [
                "docs",
                "+fetch",
                "--as",
                "user",
                "--doc",
                token,
                "--doc-format",
                "markdown",
                "--detail",
                "simple",
                "--format",
                "json",
            ]
        )


def feishu_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    publishing = settings.get("publishing")
    value = publishing.get("feishu") if isinstance(publishing, dict) else None
    if not isinstance(value, dict):
        raise FeishuError("Feishu publishing settings are missing")
    return value


def resolve_state_path(settings: Dict[str, Any]) -> Path:
    value = feishu_settings(settings).get("state_path")
    declared = Path(str(value)).expanduser()
    if declared.is_symlink():
        raise FeishuError("Feishu publishing state path must not be a symbolic link")
    return expand_path(str(value))


def build_client(settings: Dict[str, Any]) -> FeishuClient:
    value = feishu_settings(settings)
    return FeishuClient(
        str(value["command"]), timeout_seconds=int(value["timeout_seconds"])
    )


def setup_feishu(
    settings: Dict[str, Any], *, client: Optional[FeishuClient] = None
) -> Dict[str, Any]:
    config = feishu_settings(settings)
    active_client = client or build_client(settings)
    active_client.verify_auth()
    state_path = resolve_state_path(settings)
    state = _load_state(state_path)

    root = _ensure_folder(
        active_client, str(config["root_folder_name"]), "", state.get("folders", {}).get("root")
    )
    daily = _ensure_folder(
        active_client,
        str(config["daily_folder_name"]),
        root["token"],
        state.get("folders", {}).get("daily"),
    )
    weekly = _ensure_folder(
        active_client,
        str(config["weekly_folder_name"]),
        root["token"],
        state.get("folders", {}).get("weekly"),
    )
    state["folders"] = {"root": root, "daily": daily, "weekly": weekly}
    state["updated_at"] = _utc_now()
    _write_state(state_path, state)
    return {
        "root_url": root.get("url", ""),
        "daily_url": daily.get("url", ""),
        "weekly_url": weekly.get("url", ""),
        "state_path": str(state_path),
    }


def publish_report(
    settings: Dict[str, Any],
    report_path: Path,
    report_type: str,
    period: str,
    *,
    client: Optional[FeishuClient] = None,
) -> PublishResult:
    _validate_period(report_type, period)
    markdown = _read_report(report_path)
    title, body = _split_title(markdown, report_type, period)
    content_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    active_client = client or build_client(settings)
    active_client.verify_auth()
    state_path = resolve_state_path(settings)
    state = _load_state(state_path)
    folder = state.get("folders", {}).get(report_type)
    if not isinstance(folder, dict) or not _resource_token(folder):
        raise FeishuError("Feishu folders are not initialized; run `worktrace feishu setup`")

    documents = state.setdefault("documents", {})
    document_key = "{}:{}".format(report_type, period)
    saved = documents.get(document_key)
    token = _resource_token(saved)
    action = "updated"

    if token and isinstance(saved, dict) and saved.get("content_sha256") == content_sha256:
        try:
            active_client.fetch_document(token)
        except FeishuError:
            token = ""
        else:
            saved["verified_at"] = _utc_now()
            state["updated_at"] = _utc_now()
            _write_state(state_path, state)
            return PublishResult(
                "unchanged", report_type, period, title, str(saved.get("url") or "")
            )

    if not token:
        matches = _exact_named_files(
            active_client.list_files(_resource_token(folder)), title, "docx"
        )
        if len(matches) > 1:
            raise FeishuError(
                "Multiple Feishu documents have the same managed title: {}".format(title)
            )
        if matches:
            token = _resource_token(matches[0])
            if not token:
                raise FeishuError("Matched Feishu document has no token")
            saved = _resource(matches[0], title)
        else:
            created = active_client.create_document(
                title, body, _resource_token(folder)
            )
            document = created.get("document")
            source = document if isinstance(document, dict) else created
            token = _resource_token(source)
            if not token:
                raise FeishuError("Feishu did not return the created document token")
            saved = _resource(source, title)
            action = "created"

    if action != "created":
        active_client.overwrite_document(token, body)
    active_client.fetch_document(token)

    source_url = str(saved.get("url") or "") if isinstance(saved, dict) else ""
    documents[document_key] = {
        "token": token,
        "url": source_url,
        "title": title,
        "report_type": report_type,
        "period": period,
        "content_sha256": content_sha256,
        "updated_at": _utc_now(),
    }
    state["updated_at"] = _utc_now()
    _write_state(state_path, state)
    return PublishResult(action, report_type, period, title, source_url)


def publishing_status(
    settings: Dict[str, Any], *, client: Optional[FeishuClient] = None
) -> Dict[str, Any]:
    config = feishu_settings(settings)
    state_path = resolve_state_path(settings)
    state = _load_state(state_path)
    authenticated = False
    detail = "not authenticated"
    try:
        (client or build_client(settings)).verify_auth()
    except FeishuError as exc:
        detail = str(exc)
    else:
        authenticated = True
        detail = "authenticated"
    folders = state.get("folders") if isinstance(state, dict) else {}
    documents = state.get("documents") if isinstance(state, dict) else {}
    return {
        "enabled": bool(config.get("enabled")),
        "auto_publish": bool(config.get("auto_publish")),
        "authenticated": authenticated,
        "detail": detail,
        "folders_ready": isinstance(folders, dict)
        and all(_resource_token(folders.get(key)) for key in ("root", "daily", "weekly")),
        "document_count": len(documents) if isinstance(documents, dict) else 0,
        "state_path": str(state_path),
    }


def enable_feishu_config(config_path: Optional[Path], command: str) -> Path:
    declared = (config_path or DEFAULT_CONFIG_PATH).expanduser()
    if declared.is_symlink():
        raise FeishuError("Feishu settings path must not be a symbolic link")
    target = declared.resolve()
    if target.exists() and not target.is_file():
        raise FeishuError("Feishu settings path is unsafe")
    value: Dict[str, Any] = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FeishuError("Existing settings JSON could not be updated") from exc
        if not isinstance(loaded, dict):
            raise FeishuError("Existing settings must contain a JSON object")
        value = loaded
    publishing = value.setdefault("publishing", {})
    if not isinstance(publishing, dict):
        raise FeishuError("Existing publishing settings are invalid")
    feishu = publishing.setdefault("feishu", {})
    if not isinstance(feishu, dict):
        raise FeishuError("Existing Feishu settings are invalid")
    feishu.update({"enabled": True, "auto_publish": True, "command": command})
    write_private_json(target, value)
    return target


def _ensure_folder(
    client: FeishuClient,
    name: str,
    parent_token: str,
    saved: Any,
) -> Dict[str, str]:
    files = client.list_files(parent_token)
    matches = _exact_named_files(files, name, "folder")
    if len(matches) > 1:
        raise FeishuError("Multiple Feishu folders have the same managed name: {}".format(name))
    if matches:
        return _resource(matches[0], name)
    created = client.create_folder(name, parent_token)
    source = created.get("folder") if isinstance(created.get("folder"), dict) else created
    created_token = _resource_token(source)
    verified = _exact_named_files(client.list_files(parent_token), name, "folder")
    if len(verified) != 1 or not _resource_token(verified[0]):
        raise FeishuError("Created Feishu folder could not be verified: {}".format(name))
    if created_token and _resource_token(verified[0]) != created_token:
        raise FeishuError("Created Feishu folder token did not match verification")
    return _resource(verified[0], name)


def _exact_named_files(
    files: List[Dict[str, Any]], name: str, resource_type: str
) -> List[Dict[str, Any]]:
    return [
        item
        for item in files
        if item.get("name") == name and str(item.get("type") or "").lower() == resource_type
    ]


def _resource(value: Any, fallback_name: str) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise FeishuError("Feishu resource response is invalid")
    token = _resource_token(value)
    if not token:
        raise FeishuError("Feishu resource response has no token")
    return {
        "token": token,
        "url": str(value.get("url") or value.get("document_url") or ""),
        "name": str(value.get("name") or value.get("title") or fallback_name),
    }


def _resource_token(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    token = value.get("token") or value.get("folder_token") or value.get("document_id")
    return str(token or "")


def _fresh_state() -> Dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "folders": {},
        "documents": {},
        "updated_at": _utc_now(),
    }


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return _fresh_state()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise FeishuError("Feishu publishing state is not a safe regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeishuError("Feishu publishing state is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise FeishuError("Feishu publishing state version is unsupported")
    if not isinstance(value.get("folders"), dict) or not isinstance(value.get("documents"), dict):
        raise FeishuError("Feishu publishing state structure is invalid")
    return value


def _write_state(path: Path, value: Dict[str, Any]) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise FeishuError("Feishu publishing state path is unsafe")
    write_private_json(path, value)


def _read_report(path: Path) -> str:
    declared = path.expanduser()
    if declared.is_symlink():
        raise FeishuError("Report must be a safe regular Markdown file")
    report = declared.resolve()
    if not report.is_file():
        raise FeishuError("Report must be a safe regular Markdown file")
    if report.suffix.lower() != ".md" or report.stat().st_size > MAX_REPORT_BYTES:
        raise FeishuError("Report must be a Markdown file within the publishing size limit")
    content = report.read_text(encoding="utf-8")
    if not content.strip():
        raise FeishuError("Report is empty")
    return content


def _split_title(markdown: str, report_type: str, period: str) -> tuple[str, str]:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", markdown)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        body = (markdown[: match.start()] + markdown[match.end() :]).lstrip("\r\n")
    else:
        label = "日报" if report_type == "daily" else "周报"
        title = "WorkTrace {} {}".format(label, period)
        body = markdown
    if not title or len(title) > 256:
        raise FeishuError("Report title is missing or too long for Feishu")
    return title, body


def _validate_period(report_type: str, period: str) -> None:
    if report_type == "daily":
        valid = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", period))
    elif report_type == "weekly":
        valid = bool(re.fullmatch(r"\d{4}-W\d{2}", period))
    else:
        valid = False
    if not valid:
        raise FeishuError("Report type and period are invalid")


def _parse_cli_envelope(stdout: str, stderr: str) -> Dict[str, Any]:
    for raw in (stdout, stderr):
        text = raw.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {"ok": False, "error": {"message": "Feishu CLI returned invalid JSON"}}


def _resolve_command(command: str) -> str:
    value = Path(command).expanduser()
    if "/" in command or "\\" in command:
        if value.is_file() and os.access(str(value), os.X_OK):
            return str(value.resolve())
        raise FeishuError("Configured Feishu CLI is not executable")
    resolved = shutil.which(command)
    if not resolved:
        raise FeishuError("Feishu CLI is not installed or not on PATH")
    return resolved


def _safe_detail(value: str) -> str:
    text = re.sub(r"https?://\S+", "[authorization URL omitted]", value)
    return re.sub(r"\s+", " ", text).strip()[:500]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
