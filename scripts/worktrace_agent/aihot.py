"""Strict, read-only discovery client for the public AI HOT API.

All response data is untrusted.  This module only validates and returns a small
field allowlist; it never interprets response text as instructions or visits
the third-party URLs contained in an item.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


AIHOT_HOST = "aihot.virxact.com"
AIHOT_VERSION_URL = "https://{}/api/public/version".format(AIHOT_HOST)
AIHOT_ITEMS_URL = "https://{}/api/public/items".format(AIHOT_HOST)
AIHOT_SKILL_VERSION = "0.3.6"
AIHOT_USER_AGENT = (
    "aihot-skill/{} (+https://aihot.virxact.com/aihot-skill/)".format(
        AIHOT_SKILL_VERSION
    )
)

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
HARD_MAX_RESPONSE_BYTES = 1_000_000
VERSION_MAX_RESPONSE_BYTES = 64_000
MAX_TAKE = 50
PUBLIC_POOL_LIMIT_DAYS = 7

ITEM_FIELDS = (
    "title",
    "permalink",
    "url",
    "source",
    "publishedAt",
    "summary",
    "category",
    "score",
)
_TEXT_LIMITS = {
    "title": 500,
    "source": 200,
    "publishedAt": 64,
    "summary": 4_000,
    "category": 100,
}
_URL_LIMIT = 2_048
_CONTROL_AND_BIDI_PATTERN = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]"
)
_SEMVER_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_PRIVATE_HOST_SUFFIXES = (
    ".corp",
    ".home",
    ".internal",
    ".lan",
    ".local",
    ".localdomain",
)

_VERSION_LOCK = threading.Lock()
_VERSION_CHECK_DONE = False
_VERSION_CHECK_STATUS = "not_checked"


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        """Reject every redirect instead of constructing a follow-up request."""

        return None


_DEFAULT_OPENER = build_opener(_NoRedirectHandler())
_Opener = Union[Callable[..., Any], Any]


class _FetchError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AihotDiscoveryResult:
    """A JSON-safe discovery result that cannot alter the base report."""

    report_type: str
    status: str
    detail: str
    as_of: str
    since: str
    window: str
    public_pool_limit_days: int
    version_status: str
    items: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        """Return only documented metadata and sanitized item fields."""

        safe_items = []
        for item in self.items:
            cleaned = _sanitize_item(item)
            if cleaned is not None:
                safe_items.append(cleaned)
        return {
            "report_type": _safe_result_text(self.report_type, 16),
            "status": _safe_result_text(self.status, 32),
            "detail": _safe_result_text(self.detail, 80),
            "as_of": _safe_result_text(self.as_of, 64),
            "since": _safe_result_text(self.since, 64),
            "window": _safe_result_text(self.window, 32),
            "public_pool_limit_days": PUBLIC_POOL_LIMIT_DAYS,
            "version_status": _safe_result_text(self.version_status, 32),
            "items": safe_items[:MAX_TAKE],
        }


def discover_aihot(
    report_type: str,
    *,
    now: Optional[datetime] = None,
    take: int = MAX_TAKE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    opener: Optional[_Opener] = None,
) -> AihotDiscoveryResult:
    """Fetch the rolling AI HOT discovery window for a daily or weekly report.

    Network, protocol, and JSON failures are represented by an ``unavailable``
    result.  They never raise into the base reporting flow.  The first valid
    discovery call in each process also performs a best-effort version check;
    failure of that check does not prevent the items request.
    """

    normalized_type = (
        report_type.strip().lower() if isinstance(report_type, str) else ""
    )
    if normalized_type not in {"daily", "weekly"}:
        return AihotDiscoveryResult(
            report_type=normalized_type,
            status="unavailable",
            detail="unsupported_report_type",
            as_of="",
            since="",
            window="",
            public_pool_limit_days=PUBLIC_POOL_LIMIT_DAYS,
            version_status=_cached_version_status(),
        )

    try:
        current = _normalize_now(now)
    except (OverflowError, TypeError, ValueError):
        return AihotDiscoveryResult(
            report_type=normalized_type,
            status="unavailable",
            detail="invalid_now",
            as_of="",
            since="",
            window=(
                "rolling_24h" if normalized_type == "daily" else "rolling_7d"
            ),
            public_pool_limit_days=PUBLIC_POOL_LIMIT_DAYS,
            version_status=_cached_version_status(),
        )

    bounded_take = _bounded_int(take, MAX_TAKE, 1, MAX_TAKE)
    bounded_timeout = _bounded_float(
        timeout, DEFAULT_TIMEOUT_SECONDS, 0.1, MAX_TIMEOUT_SECONDS
    )
    bounded_body = _bounded_int(
        max_response_bytes,
        DEFAULT_MAX_RESPONSE_BYTES,
        1,
        HARD_MAX_RESPONSE_BYTES,
    )
    delta = timedelta(hours=24) if normalized_type == "daily" else timedelta(days=7)
    as_of = _format_utc(current)
    since = _format_utc(current - delta)
    window = "rolling_24h" if normalized_type == "daily" else "rolling_7d"

    # The self-check is intentionally silent and independent of item discovery.
    version_status = _check_version_once(
        opener=opener,
        timeout=bounded_timeout,
        max_response_bytes=min(bounded_body, VERSION_MAX_RESPONSE_BYTES),
    )

    query = urlencode(
        {
            "mode": "selected",
            "since": since,
            "take": str(bounded_take),
        }
    )
    url = "{}?{}".format(AIHOT_ITEMS_URL, query)
    try:
        payload = _request_json(
            url,
            opener=opener,
            timeout=bounded_timeout,
            max_response_bytes=bounded_body,
        )
    except _FetchError as exc:
        return AihotDiscoveryResult(
            report_type=normalized_type,
            status="unavailable",
            detail=exc.code,
            as_of=as_of,
            since=since,
            window=window,
            public_pool_limit_days=PUBLIC_POOL_LIMIT_DAYS,
            version_status=version_status,
        )
    except Exception:
        # A non-standard injected transport must not break a base report either.
        return AihotDiscoveryResult(
            report_type=normalized_type,
            status="unavailable",
            detail="unexpected_transport_error",
            as_of=as_of,
            since=since,
            window=window,
            public_pool_limit_days=PUBLIC_POOL_LIMIT_DAYS,
            version_status=version_status,
        )

    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return AihotDiscoveryResult(
            report_type=normalized_type,
            status="unavailable",
            detail="invalid_payload",
            as_of=as_of,
            since=since,
            window=window,
            public_pool_limit_days=PUBLIC_POOL_LIMIT_DAYS,
            version_status=version_status,
        )

    raw_items = payload["items"][:bounded_take]
    items = tuple(
        cleaned
        for cleaned in (_sanitize_item(item) for item in raw_items)
        if cleaned is not None
    )
    if not items:
        detail = "no_items" if not raw_items else "no_usable_items"
        return AihotDiscoveryResult(
            report_type=normalized_type,
            status="empty",
            detail=detail,
            as_of=as_of,
            since=since,
            window=window,
            public_pool_limit_days=PUBLIC_POOL_LIMIT_DAYS,
            version_status=version_status,
        )
    return AihotDiscoveryResult(
        report_type=normalized_type,
        status="complete",
        detail="ok",
        as_of=as_of,
        since=since,
        window=window,
        public_pool_limit_days=PUBLIC_POOL_LIMIT_DAYS,
        version_status=version_status,
        items=items,
    )


def _check_version_once(
    *, opener: Optional[_Opener], timeout: float, max_response_bytes: int
) -> str:
    global _VERSION_CHECK_DONE, _VERSION_CHECK_STATUS

    with _VERSION_LOCK:
        if _VERSION_CHECK_DONE:
            return _VERSION_CHECK_STATUS
        try:
            payload = _request_json(
                AIHOT_VERSION_URL,
                opener=opener,
                timeout=timeout,
                max_response_bytes=max_response_bytes,
            )
            remote = payload.get("skillVersion") if isinstance(payload, dict) else None
            remote_semver = _parse_semver(remote)
            local_semver = _parse_semver(AIHOT_SKILL_VERSION)
            if remote_semver is None or local_semver is None:
                _VERSION_CHECK_STATUS = "unavailable"
            elif remote_semver > local_semver:
                _VERSION_CHECK_STATUS = "update_available"
            else:
                _VERSION_CHECK_STATUS = "current"
        except Exception:
            _VERSION_CHECK_STATUS = "unavailable"
        finally:
            _VERSION_CHECK_DONE = True
        return _VERSION_CHECK_STATUS


def _request_json(
    url: str,
    *,
    opener: Optional[_Opener],
    timeout: float,
    max_response_bytes: int,
) -> Any:
    _validate_api_url(url)
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": AIHOT_USER_AGENT,
        },
        method="GET",
    )
    response = None
    try:
        response = _open_request(opener, request, timeout)
        status = _response_status(response)
        if status is not None and 300 <= status < 400:
            raise _FetchError("redirect_refused")
        if status is not None and not 200 <= status < 300:
            raise _FetchError("http_error_{}".format(status))

        geturl = getattr(response, "geturl", None)
        final_url = geturl() if callable(geturl) else None
        if final_url and final_url != url:
            raise _FetchError("redirect_refused")

        headers = getattr(response, "headers", None)
        content_length = headers.get("Content-Length") if headers is not None else None
        if content_length is not None:
            try:
                if int(content_length) > max_response_bytes:
                    raise _FetchError("response_too_large")
            except (TypeError, ValueError):
                pass

        body = response.read(max_response_bytes + 1)
        if not isinstance(body, bytes):
            raise _FetchError("invalid_response_body")
        if len(body) > max_response_bytes:
            raise _FetchError("response_too_large")
        if not body:
            raise _FetchError("empty_response")
        try:
            text = body.decode("utf-8-sig")
            return json.loads(text, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise _FetchError("invalid_json") from exc
    except _FetchError:
        raise
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise _FetchError("redirect_refused") from exc
        raise _FetchError("http_error_{}".format(exc.code)) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise _FetchError("timeout") from exc
    except URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise _FetchError("timeout") from exc
        raise _FetchError("network_error") from exc
    except OSError as exc:
        raise _FetchError("network_error") from exc
    except Exception as exc:
        raise _FetchError("transport_error") from exc
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def _open_request(opener: Optional[_Opener], request: Request, timeout: float) -> Any:
    selected = _DEFAULT_OPENER if opener is None else opener
    open_method = getattr(selected, "open", None)
    if callable(open_method):
        return open_method(request, timeout=timeout)
    if callable(selected):
        return selected(request, timeout=timeout)
    raise TypeError("opener must be callable or expose open()")


def _validate_api_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise _FetchError("invalid_api_endpoint") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != AIHOT_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path not in {"/api/public/version", "/api/public/items"}
    ):
        raise _FetchError("invalid_api_endpoint")
    if parsed.path == "/api/public/version" and parsed.query:
        raise _FetchError("invalid_api_endpoint")


def _response_status(response: Any) -> Optional[int]:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else None
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError) as exc:
        raise _FetchError("invalid_http_status") from exc


def _sanitize_item(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    title = _clean_text(value.get("title"), _TEXT_LIMITS["title"])
    if not title:
        return None

    cleaned: Dict[str, Any] = {"title": title}
    permalink = _clean_https_url(value.get("permalink"), required_host=AIHOT_HOST)
    if permalink:
        cleaned["permalink"] = permalink
    source_url = _clean_https_url(value.get("url"))
    if source_url:
        cleaned["url"] = source_url
    for field in ("source", "publishedAt", "summary", "category"):
        text = _clean_text(value.get(field), _TEXT_LIMITS[field])
        if text:
            cleaned[field] = text
    score = _clean_score(value.get("score"))
    if score is not None:
        cleaned["score"] = score
    return {field: cleaned[field] for field in ITEM_FIELDS if field in cleaned}


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = _CONTROL_AND_BIDI_PATTERN.sub(" ", value)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit].strip()


def _clean_https_url(value: Any, required_host: Optional[str] = None) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = _CONTROL_AND_BIDI_PATTERN.sub("", value).strip()
    if not cleaned or len(cleaned) > _URL_LIMIT or any(char.isspace() for char in cleaned):
        return ""
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme.lower() != "https"
        or not host
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    if required_host is not None:
        if host != required_host:
            return ""
    elif not _is_public_host(host):
        return ""
    return cleaned


def _is_public_host(host: str) -> bool:
    if host == "localhost" or host.endswith(_PRIVATE_HOST_SUFFIXES):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host


def _clean_score(value: Any) -> Optional[Union[int, float]]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not 0 <= value <= 100:
        return None
    return value


def _normalize_now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime):
        raise TypeError("now must be a datetime")
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _parse_semver(value: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    match = _SEMVER_PATTERN.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(minimum, min(maximum, parsed))


def _safe_result_text(value: Any, limit: int) -> str:
    return _clean_text(value, limit) if isinstance(value, str) else ""


def _cached_version_status() -> str:
    with _VERSION_LOCK:
        return _VERSION_CHECK_STATUS


def _reset_version_check_for_tests() -> None:
    """Reset process state for isolated offline tests."""

    global _VERSION_CHECK_DONE, _VERSION_CHECK_STATUS
    with _VERSION_LOCK:
        _VERSION_CHECK_DONE = False
        _VERSION_CHECK_STATUS = "not_checked"
