from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlsplit

LOW_SIGNAL_TEXT = {
    "ok",
    "okay",
    "好",
    "好的",
    "继续",
    "继续执行",
    "继续重试",
    "收到",
}

PRIVATE_KEY_BLOCK_PATTERN = re.compile(
    r"-----BEGIN ([A-Z0-9 ]{0,64}PRIVATE KEY)-----"
    r".*?"
    r"(?:-----END \1-----|\Z)",
    re.DOTALL,
)
AWS_ACCESS_KEY_ID_PATTERN = re.compile(
    r"\b(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|A3T[A-Z0-9])"
    r"[A-Z0-9]{16}\b"
)
CREDENTIAL_URI_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,31}:"
    r"//[^\s<>\"'`]+"
)
ASSIGNMENT_PATTERN = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9_.-])
    (?:(?:export|set)\s+)?
    (?P<label_quote>["']?)
    (?P<label>[A-Za-z_][A-Za-z0-9_.-]{0,127})
    (?P=label_quote)
    [ \t]*(?P<separator>[:=])[ \t]*
    (?P<value>
        "(?:\\.|[^"\\\r\n])*"
        |'(?:\\.|[^'\\\r\n])*'
        |(?:Bearer|Basic)[ \t]+[^\s,;}\]]+
        |[^\s,;}\]]+
    )
    """
)
SECRET_HEADER_PATTERN = re.compile(
    r"(?im)^[ \t]*(?:cookie|set-cookie|authorization)[ \t]*[:=][ \t]*[^\r\n]+$"
)
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
]
UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
FILE_URI_PATTERN = re.compile(
    r"\bfile:/{1,3}[^\s`\"'<>，。；;）)\]]+",
    re.IGNORECASE,
)
_POSIX_PATH_TOKEN = r"[^/\s`\"'<>，。；;：:）)\]]+"
_POSIX_DIRECTORY_SEGMENT = _POSIX_PATH_TOKEN + r"(?:[ \t]+" + _POSIX_PATH_TOKEN + r")*"
PRIVATE_PATH_PATTERN = re.compile(
    r"(?<![\w/:])/(?!/)(?:" + _POSIX_DIRECTORY_SEGMENT + r"/)*" + _POSIX_PATH_TOKEN
)
QUOTED_POSIX_PRIVATE_PATH_PATTERN = re.compile(
    r"(?P<quote>[`\"'])/(?!/)[^\r\n`\"']+(?P=quote)"
)
_WINDOWS_PATH_PREFIX = (
    r"(?:[A-Z]:[\\/]"
    r"|\\\\[A-Za-z0-9][A-Za-z0-9._$-]{0,254}[\\/]"
    r"|\\\\\?\\(?:[A-Z]:[\\/]"
    r"|UNC\\[A-Za-z0-9][A-Za-z0-9._$-]{0,254}\\)"
    r"|\\\\\.\\[A-Za-z0-9][A-Za-z0-9._$-]{0,254}\\)"
)
WINDOWS_PRIVATE_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])" + _WINDOWS_PATH_PREFIX + r"[^\s`\"'<>，。；;：:）)\]]+"
)
QUOTED_WINDOWS_PRIVATE_PATH_PATTERN = re.compile(
    r"(?i)(?P<quote>[`\"'])" + _WINDOWS_PATH_PREFIX + r"[^\r\n`\"']+"
    r"(?P=quote)"
)

_SENSITIVE_NAME_PARTS = {
    "credential",
    "credentials",
    "password",
    "passwd",
    "pwd",
    "secret",
}
_AUTH_METADATA_PARTS = {
    "enabled",
    "endpoint",
    "method",
    "methods",
    "mode",
    "provider",
    "scheme",
    "strategy",
    "type",
    "url",
}
_COMBINED_SENSITIVE_NAME_PARTS = {
    "accesstoken",
    "apikey",
    "awssecretaccesskey",
    "clientsecret",
    "encryptionkey",
    "idtoken",
    "privatekey",
    "refreshtoken",
    "sessiontoken",
    "signingkey",
}
_TOKEN_METADATA_PARTS = {"budget", "count", "cost", "length", "limit", "price", "usage"}
_PASSWORD_METADATA_PARTS = {
    "length",
    "max",
    "min",
    "policy",
    "required",
    "rules",
}
_CONTEXT_ID_BASES = {
    "call",
    "chat",
    "conversation",
    "message",
    "parentmessage",
    "parenttooluse",
    "rawsession",
    "request",
    "response",
    "run",
    "session",
    "thread",
    "toolcall",
    "tooluse",
    "trace",
    "turn",
}
_SENSITIVE_URI_PARAMETER_NAMES = {
    "auth",
    "authorization",
    "code",
    "sig",
    "signature",
    "x-amz-credential",
    "x-amz-signature",
    "x-goog-credential",
    "x-goog-signature",
}


def compact_text(value: Any, limit: int = 800) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple)):
        text = " ".join(compact_text(item, limit) for item in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = redact(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: max(0, limit - 3)].rstrip() + "..."
    return text


def redact(text: str) -> str:
    cleaned = PRIVATE_KEY_BLOCK_PATTERN.sub("[REDACTED_PRIVATE_KEY]", text)
    cleaned = CREDENTIAL_URI_PATTERN.sub(_redact_credential_uri, cleaned)
    cleaned = SECRET_HEADER_PATTERN.sub("[REDACTED]", cleaned)
    cleaned = ASSIGNMENT_PATTERN.sub(_redact_sensitive_assignment, cleaned)
    cleaned = AWS_ACCESS_KEY_ID_PATTERN.sub("[REDACTED]", cleaned)
    for pattern in SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    return cleaned


def _normalized_name_parts(name: str) -> list[str]:
    expanded = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", expanded)
    return [part for part in re.split(r"[^A-Za-z0-9]+", expanded.lower()) if part]


def _is_sensitive_assignment_name(name: str) -> bool:
    parts = _normalized_name_parts(name)
    if not parts:
        return False
    combined = "".join(parts)
    if combined in {"auth", "authorization", "apikey", "clientsecret"}:
        return True
    if _COMBINED_SENSITIVE_NAME_PARTS.intersection(parts):
        return True
    if {"auth", "authorization"}.intersection(parts):
        return not bool(_AUTH_METADATA_PARTS.intersection(parts))
    if _SENSITIVE_NAME_PARTS.intersection(parts):
        if "password" in parts and _PASSWORD_METADATA_PARTS.intersection(parts):
            return False
        return True
    if "token" in parts:
        return not bool(_TOKEN_METADATA_PARTS.intersection(parts))
    key_pairs = (
        {"access", "key"},
        {"api", "key"},
        {"encryption", "key"},
        {"private", "key"},
        {"signing", "key"},
    )
    return any(required.issubset(parts) for required in key_pairs)


def _is_context_identifier_name(name: str) -> bool:
    parts = _normalized_name_parts(name)
    if not parts:
        return False
    combined = "".join(parts)
    if combined.endswith("id") and combined[:-2] in _CONTEXT_ID_BASES:
        return True
    return parts[-1] == "id" and "".join(parts[:-1]) in _CONTEXT_ID_BASES


def _redact_sensitive_assignment(match: re.Match[str]) -> str:
    label = match.group("label")
    if _is_sensitive_assignment_name(label):
        return "[REDACTED]"
    if _is_context_identifier_name(label):
        return "[CONTEXT_ID]"
    return match.group(0)


def _redact_credential_uri(match: re.Match[str]) -> str:
    uri = match.group(0)
    authority = uri.split("://", 1)[1].split("/", 1)[0]
    authority = authority.split("?", 1)[0].split("#", 1)[0]
    if "@" in authority:
        return "[REDACTED_URI]"
    raw_parameter_names = re.findall(r"(?:[?&#])([^=?&#]+)=", uri)
    if any(
        _is_sensitive_uri_parameter_name(unquote_plus(name))
        for name in raw_parameter_names
    ):
        return "[REDACTED_URI]"
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return uri
    if "@" in parsed.netloc:
        return "[REDACTED_URI]"
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    fragment_pairs = parse_qsl(parsed.fragment, keep_blank_values=True)
    if any(
        _is_sensitive_uri_parameter_name(key)
        for key, _value in query_pairs + fragment_pairs
    ):
        return "[REDACTED_URI]"
    return uri


def _is_sensitive_uri_parameter_name(name: str) -> bool:
    return _is_sensitive_assignment_name(name) or (
        name.lower() in _SENSITIVE_URI_PARAMETER_NAMES
    )


def sanitize_full_text(value: Any) -> str:
    """Preserve message text without length truncation while removing obvious secrets."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple)):
        text = "\n".join(sanitize_full_text(item) for item in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return redact(text.replace("\x00", "")).strip()


def sanitize_for_model(value: Any) -> str:
    text = sanitize_full_text(value)
    home_paths = {str(Path.home())}
    try:
        home_paths.add(str(Path.home().resolve()))
    except OSError:
        pass
    for home in sorted((item for item in home_paths if item), key=len, reverse=True):
        text = re.sub(
            r"(?<![\w]){}(?=$|[/])".format(re.escape(home)),
            "~",
            text,
        )
    return text


def sanitize_report_text(value: Any) -> str:
    text = sanitize_for_model(value)
    text = UUID_PATTERN.sub("[ID]", text)
    text = FILE_URI_PATTERN.sub("[PRIVATE_PATH]", text)
    text = QUOTED_POSIX_PRIVATE_PATH_PATTERN.sub("[PRIVATE_PATH]", text)
    text = QUOTED_WINDOWS_PRIVATE_PATH_PATTERN.sub("[PRIVATE_PATH]", text)
    text = WINDOWS_PRIVATE_PATH_PATTERN.sub("[PRIVATE_PATH]", text)
    return PRIVATE_PATH_PATTERN.sub("[PRIVATE_PATH]", text)


def neutralize_markdown(value: Any) -> str:
    """Render untrusted text as inert inline Markdown, never links/images/HTML."""

    text = re.sub(r"\s+", " ", sanitize_report_text(value)).strip()
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\\", "\\\\")
    return re.sub(r"([`*_{}\[\]()#!|])", r"\\\1", text)


def is_low_signal(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text.strip().lower())
    normalized = normalized.strip("。.!！?？,，;；:：")
    return not normalized or normalized in LOW_SIGNAL_TEXT


def unique_preserving_order(values):
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
