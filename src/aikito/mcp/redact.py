"""Credential and URL redaction helpers for MCP."""

import ipaddress
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
SENSITIVE_URL_PARAMETERS = frozenset(
    {
        # OAuth / OIDC tokens
        "code",
        "access_token",
        "refresh_token",
        "id_token",
        # Generic secrets
        "token",
        "secret",
        "client_secret",
        "password",
        "pass",
        "credential",
        "signature",
        # API keys (various naming conventions)
        "api_key",
        "apikey",
        "api-key",
        "x-api-key",
        "key",
        # Authorization / bearer
        "authorization",
        "auth",
        # Session / identity
        "session",
        "session_token",
        "user_token",
        "private_token",
        # AWS pre-signed URLs
        "x-amz-signature",
        "x_amz_signature",
        "x-amz-credential",
        "x_amz_credential",
        "x-amz-security-token",
        "x_amz_security_token",
        # Google Cloud signed URLs
        "x-goog-signature",
        "x_goog_signature",
        "x-goog-credential",
        "x_goog_credential",
        # Azure SAS (ONLY sig is the secret credential; spr is protocol constraint)
        "sig",
        # Personal-access / app tokens
        "pat",
        "app_token",
        "app-token",
        "auth_token",
        "auth-token",
        # JWT / bearer literals
        "jwt",
        "bearer",
    }
)

_SENSITIVE_PARAM_SEGMENTS: frozenset[str] = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "credential",
        "credentials",
        "signature",
        "jwt",
        "apikey",
    }
)

_SENSITIVE_PARAM_PREFIXES: tuple[str, ...] = (
    "auth_",
    "oauth_",
)

_SENSITIVE_PARAM_SUFFIXES: tuple[str, ...] = (
    "_key",
    "_token",
    "_secret",
    "_password",
    "_pass",
    "_sig",
    "_signature",
    "_credential",
    "_credentials",
    "_jwt",
    "_pat",
)


def is_sensitive_url_parameter(param_name: str) -> bool:
    """Return True if a URL query-parameter name represents a credential.

    Uses exact naming plus controlled segment and prefix/suffix matching to avoid
    false positives on legitimate parameters such as 'author', 'authority',
    'authentication_mode', or 'private_mode'.
    """
    lowered = param_name.lower()
    normalized = lowered.replace("-", "_").replace(".", "_")

    if lowered in SENSITIVE_URL_PARAMETERS or normalized in SENSITIVE_URL_PARAMETERS:
        return True

    segments = set(normalized.split("_"))
    if segments & _SENSITIVE_PARAM_SEGMENTS:
        return True

    if any(normalized.startswith(prefix) for prefix in _SENSITIVE_PARAM_PREFIXES):
        return True

    if any(normalized.endswith(suffix) for suffix in _SENSITIVE_PARAM_SUFFIXES):
        return True

    return False


def _has_sensitive_parameters(url: str) -> bool:
    parameters = {key.lower() for key in parse_qs(urlsplit(url).query)}
    return any(is_sensitive_url_parameter(p) for p in parameters)


_ENV_REFERENCE_PATTERNS = (
    re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$"),
    re.compile(r"^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$"),
    re.compile(r"^!!js process\.env\.([A-Za-z_][A-Za-z0-9_]*)$"),
)

_CREDENTIAL_HEADER_FRAGMENTS = (
    "authorization",
    "token",
    "secret",
    "password",
    "api-key",
    "api_key",
    "apikey",
    "cookie",
)


def _environment_reference(value: str) -> str | None:
    for pattern in _ENV_REFERENCE_PATTERNS:
        match = pattern.fullmatch(value)
        if match:
            return match.group(1)
    return None


environment_reference = _environment_reference


def _is_credential_header(name: str) -> bool:
    return any(fragment in name.lower() for fragment in _CREDENTIAL_HEADER_FRAGMENTS)


is_credential_header = _is_credential_header


def _is_loopback_url(url: str) -> bool:
    host = urlsplit(url).hostname
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _headers_contain_credentials(headers: dict[str, str]) -> bool:
    return any(_is_credential_header(name) for name in headers)


def _redact_probe_error(text: str, headers: dict[str, str]) -> str:
    """Redact runtime credentials and terminal control characters at the boundary."""
    secrets = set()
    for name, value in headers.items():
        if not value or not _is_credential_header(name):
            continue
        secrets.add(value)
        if name.lower() == "authorization":
            _scheme, separator, credential = value.partition(" ")
            if separator and credential:
                secrets.add(credential)

    redacted = text
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")
    printable = "".join(
        character if character.isprintable() else " " for character in redacted
    )
    return " ".join(printable.split())[:300]


_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "token",
    "secret",
    "password",
    "credential",
    "bearer",
    "api-key",
    "api_key",
    "apikey",
)

_SENSITIVE_PARAM_PATTERN = re.compile(
    r"([?&](?:token|secret|key|api_key|api-key|password|credential|bearer|pat)=)[^&]+",
    re.IGNORECASE,
)


def redact_mcp_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a display-safe MCP entry without exposing runtime credentials."""

    def redact(value: Any, key: str = "", parent: str = "") -> Any:
        if isinstance(value, dict):
            return {
                item_key: redact(item, item_key, key)
                for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [redact(item, key, parent) for item in value]
        if not isinstance(value, str):
            return value

        # Do not redact environment variable references like ${VAR} or {env:VAR}
        if (value.startswith("${") and value.endswith("}")) or (
            value.startswith("{env:") and value.endswith("}")
        ):
            return value

        # env_http_headers stores environment variable names rather than secret values.
        if parent == "env_http_headers":
            return value

        # Redact all string values inside headers containers (e.g. headers, http_headers)
        if parent in ("headers", "http_headers") or parent.endswith("headers"):
            return "<redacted>"

        # Redact values associated with sensitive key names
        key_lower = key.lower()
        sensitive_key = (
            any(fragment in key_lower for fragment in _SENSITIVE_KEY_FRAGMENTS)
            or key_lower in ("key", "pat")
            or key_lower.endswith(("_key", "-key", "_pat", "-pat"))
        )
        if sensitive_key:
            return "<redacted>"

        # Sanitize sensitive query parameters inside URLs or string values
        if "?" in value and "=" in value:
            return _SENSITIVE_PARAM_PATTERN.sub(r"\1<redacted>", value)

        return value

    return redact(entry)


def _urls_in_text(text: str) -> list[str]:
    return [match.rstrip(").,;]") for match in URL_PATTERN.findall(text)]


def _is_authorization_url(url: str) -> bool:
    if _has_sensitive_parameters(url):
        return False
    parsed = urlsplit(url)
    location = f"{parsed.netloc}{parsed.path}".lower()
    parameters = {key.lower() for key in parse_qs(parsed.query)}
    return (
        "authorize" in location
        or "oauth" in location
        or {"client_id", "redirect_uri"} <= parameters
    )


def _redact_sensitive_urls(text: str) -> str:
    return URL_PATTERN.sub(
        lambda match: (
            "[REDACTED CALLBACK URL]"
            if _has_sensitive_parameters(match.group())
            else match.group()
        ),
        text,
    )
