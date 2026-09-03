"""Request-level profile selection without mutable process-wide state."""

from collections.abc import Mapping
from typing import Optional
import urllib.parse

from flask import g, has_request_context, request


def append_url_query(url: str, params) -> str:
    """Append query parameters with standards-compliant percent encoding."""
    parts = urllib.parse.urlsplit(url)
    query_items = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query_items.extend(params.items() if isinstance(params, Mapping) else params)
    encoded_query = urllib.parse.urlencode(
        query_items,
        doseq=True,
        quote_via=urllib.parse.quote,
    )
    return urllib.parse.urlunsplit(parts._replace(query=encoded_query))

def profile_api_path(config_data, suffix: str) -> str:
    profile_id = config_data.get("profile_id") if isinstance(config_data, Mapping) else None
    return f"/api/profiles/{profile_id}{suffix}" if profile_id else suffix


def config_api_path(config_data, target: str) -> str:
    profile_id = config_data.get("profile_id") if isinstance(config_data, Mapping) else None
    return f"/api/config/{profile_id}/{target}" if profile_id else f"/api/config/{target}"


def resolve_profile_id(explicit: Optional[str] = None, fallback: Optional[str] = None) -> str:
    """Resolve URL, header, query, active profile in that order."""
    from backend.common.config import get_repository

    repository = get_repository()
    if explicit is not None:
        candidate = explicit
    elif has_request_context():
        route_values = request.view_args or {}
        candidate = route_values.get("profile_id")
        if candidate is None:
            candidate = request.headers.get("X-ConfigFlow-Profile")
        if candidate is None:
            candidate = request.args.get("profile") or request.args.get("profile_id")
    else:
        candidate = None

    candidate = candidate or fallback or repository.active_profile_id()
    repository.validate_profile_id(candidate)
    repository.get_profile(candidate)
    return candidate


def install_profile_context(app) -> None:
    """Install request validation and response observability on a Flask app."""

    @app.before_request
    def _set_profile_context():
        from backend.common.config import reset_config_context

        reset_config_context()
        endpoint = request.endpoint or ""
        if endpoint.startswith("auth.") or endpoint == "profiles.handle_profiles":
            g.configflow_profile_id = None
            return None
        g.configflow_profile_id = resolve_profile_id()

    @app.after_request
    def _sanitize_external_json(response):
        """Scrub ordinary JSON responses without touching internal return objects."""
        from backend.common.config_export import sanitize_external_payload

        endpoint = request.endpoint or ""
        if (
            response.is_json
            and not response.direct_passthrough
        ):
            try:
                payload = response.get_json(silent=True)
            except RecursionError:
                # The standard decoder can overflow before the iterative
                # sanitizer sees an adversarially deep raw JSON response.
                response.set_data(app.json.dumps("[REDACTED]"))
                return response
            if payload is not None:
                sanitized = sanitize_external_payload(payload)
                # A successful self-registration may disclose exactly one top-level
                # credential. Nested Agent records and every other API stay scrubbed.
                if (
                    endpoint == "agents.register_agent"
                    and 200 <= response.status_code < 300
                    and isinstance(payload, dict)
                    and payload.get("success") is True
                    and isinstance(payload.get("token"), str)
                ):
                    sanitized["token"] = payload["token"]
                response.set_data(app.json.dumps(sanitized))
        return response

    @app.after_request
    def _add_profile_header(response):
        from backend.common.config import reset_config_context

        profile_id = getattr(g, "configflow_profile_id", None)
        if profile_id:
            response.headers.setdefault("X-ConfigFlow-Profile", profile_id)
        reset_config_context()
        return response

    @app.teardown_request
    def _clear_profile_context(_error=None):
        from backend.common.config import reset_config_context

        reset_config_context()
