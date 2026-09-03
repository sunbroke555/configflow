"""Safe copies of configuration data for external responses and exports."""

import urllib.parse
from collections import deque
from typing import Any, Dict, Iterable, Optional


SENSITIVE_SYSTEM_CONFIG_FIELDS = frozenset(
    {"rule_proxy_token", "retired_rule_proxy_tokens"}
)
_DESENSITIZED = "***已脱敏***"
_REDACTED = "[REDACTED]"


def _repository_rule_proxy_tokens() -> Iterable[str]:
    """Read capability tokens from the authoritative repository, not a response."""
    from backend.common.config import get_repository

    yield from get_repository().rule_proxy_tokens_for_sanitization()


def _repository_agent_tokens() -> Iterable[str]:
    """Collect persisted Agent credentials without exposing manager return values."""
    from backend.common.config import get_repository

    for agent in get_repository().get_system().get("agents", []):
        if isinstance(agent, dict):
            token = agent.get("token")
            if isinstance(token, str) and token:
                yield token


def _contains_encoded_secret(value: str, secrets: set[str]) -> bool:
    """Inspect bounded URL-decoding paths without enumerating encoding depth."""
    pending = deque([value])
    seen = {value}
    max_states = max(1, 2 * len(value) + 1)
    max_steps = 2 * max_states
    states = 0
    steps = 0

    while pending and states < max_states and steps < max_steps:
        current = pending.popleft()
        states += 1
        if any(secret in current for secret in secrets):
            return True
        for decoder in (urllib.parse.unquote, urllib.parse.unquote_plus):
            if steps >= max_steps:
                break
            steps += 1
            decoded = decoder(current)
            if len(decoded) <= len(current) and decoded not in seen:
                seen.add(decoded)
                pending.append(decoded)
    return False


def contains_internal_rule_proxy_token(value: str) -> bool:
    """Detect current/retired internal rule tokens through arbitrary URL encoding."""
    if not isinstance(value, str):
        return False
    secrets = {token for token in _repository_rule_proxy_tokens() if token}
    return _contains_encoded_secret(value, secrets)


def sanitize_external_payload(
    payload: Any, system_config: Optional[Dict[str, Any]] = None
) -> Any:
    """Remove the repository's internal token from arbitrary external payloads."""
    secrets = set(_repository_rule_proxy_tokens())
    secrets.update(_repository_agent_tokens())
    if isinstance(system_config, dict):
        secrets.update(
            system_config.get(field)
            for field in SENSITIVE_SYSTEM_CONFIG_FIELDS
            if isinstance(system_config.get(field), str) and system_config[field]
        )
    secrets = {secret for secret in secrets if isinstance(secret, str) and secret}

    # A response sanitizer is an availability boundary: it must not inherit
    # Python's recursion limit or follow hostile object cycles.  Depth counts
    # the root as zero; a container at the limit is replaced as one subtree.
    max_depth = 128
    root = [None]
    active_container_ids: set[int] = set()
    stack = [("visit", payload, 0, root, 0)]

    while stack:
        operation, *arguments = stack.pop()
        if operation == "leave":
            active_container_ids.remove(arguments[0])
            continue
        if operation == "finish_tuple":
            items, target, target_key = arguments
            target[target_key] = tuple(items)
            continue
        if operation == "finish_dict_item":
            output, item = arguments
            output[item[0]] = item[1]
            continue

        value, depth, target, target_key = arguments
        is_container = isinstance(value, (dict, list, tuple))
        if is_container and (
            depth >= max_depth or id(value) in active_container_ids
        ):
            target[target_key] = _REDACTED
            continue

        if isinstance(value, dict):
            output: Dict[Any, Any] = {}
            target[target_key] = output
            container_id = id(value)
            active_container_ids.add(container_id)
            stack.append(("leave", container_id))
            for key, item_value in reversed(tuple(value.items())):
                if key in SENSITIVE_SYSTEM_CONFIG_FIELDS:
                    continue
                item = [None, None]
                stack.append(("finish_dict_item", output, item))
                stack.append(("visit", item_value, depth + 1, item, 1))
                stack.append(("visit", key, depth + 1, item, 0))
            continue

        if isinstance(value, list):
            output_list = [None] * len(value)
            target[target_key] = output_list
            container_id = id(value)
            active_container_ids.add(container_id)
            stack.append(("leave", container_id))
            for index in range(len(value) - 1, -1, -1):
                stack.append(("visit", value[index], depth + 1, output_list, index))
            continue

        if isinstance(value, tuple):
            output_items = [None] * len(value)
            container_id = id(value)
            active_container_ids.add(container_id)
            stack.append(("leave", container_id))
            stack.append(("finish_tuple", output_items, target, target_key))
            for index in range(len(value) - 1, -1, -1):
                stack.append(("visit", value[index], depth + 1, output_items, index))
            continue

        if isinstance(value, str) and _contains_encoded_secret(value, secrets):
            target[target_key] = _REDACTED
        else:
            target[target_key] = value

    return root[0]


def sanitize_config_for_output(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return an external-safe copy while leaving internal configuration untouched."""
    system_config = config.get("system_config")
    return sanitize_external_payload(
        config, system_config if isinstance(system_config, dict) else None
    )


def prepare_config_export(config: Dict[str, Any], *, desensitize: bool = False) -> Dict[str, Any]:
    """Build a safe full or credential-desensitized configuration export."""
    output = sanitize_config_for_output(config)
    output.pop("profile_id", None)
    if desensitize:
        for subscription in output.get("subscriptions", []):
            if isinstance(subscription, dict) and "url" in subscription:
                subscription["url"] = _DESENSITIZED
        for node in output.get("nodes", []):
            if isinstance(node, dict) and "proxy_string" in node:
                node["proxy_string"] = _DESENSITIZED
    return output
