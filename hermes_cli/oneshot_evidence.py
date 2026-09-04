"""Private oneshot execution receipts, derived at the provider-call boundary.

Request effort is the effort sent by the executor, not a claim about undisclosed
server-side reasoning. Named custom routes require a unique endpoint/config match.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace


def _jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, SimpleNamespace):
        return _jsonable(vars(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"unserialized_type": type(value).__name__}


def _resolved_provider(agent, cfg):
    provider = getattr(agent, "provider", None)
    if provider != "custom":
        return provider if isinstance(provider, str) else None
    from hermes_cli.config_providers import get_compatible_custom_providers, normalize_route_base_url

    client_url = getattr(getattr(agent, "client", None), "base_url", None)
    endpoint = client_url if client_url is not None else getattr(agent, "base_url", None)
    if not endpoint:
        return None
    endpoint = normalize_route_base_url(str(endpoint))
    matches = set()
    for entry in get_compatible_custom_providers(cfg):
        if normalize_route_base_url(entry.get("base_url")) == endpoint:
            name = entry.get("provider_key") or entry.get("name")
            if name:
                matches.add(str(name).removeprefix("custom:"))
    return "custom:" + next(iter(matches)) if len(matches) == 1 else "custom"


def _effort(kwargs):
    extra = kwargs.get("extra_body") or {}
    reasoning = kwargs.get("reasoning") or extra.get("reasoning") or {}
    value = kwargs.get("reasoning_effort") or extra.get("reasoning_effort") or reasoning.get("effort")
    return value if isinstance(value, str) else None


class OneshotEvidence:
    def __init__(self, cfg, trajectory_file=None):
        self.cfg = cfg
        self.calls = []
        self.fd = None
        if trajectory_file:
            path = Path(trajectory_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Unique per execution, private before any bytes exist; refuse collisions/symlinks.
            self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)

    def event(self, kind, data):
        if self.fd is None:
            return
        payload = (json.dumps({"event": kind, "data": _jsonable(data)}, ensure_ascii=True) + "\n").encode()
        while payload:
            written = os.write(self.fd, payload)
            payload = payload[written:]
        os.fsync(self.fd)

    def request(self, agent, kwargs):
        record = {
            "resolved_provider": _resolved_provider(agent, self.cfg),
            "provider": getattr(agent, "provider", None),
            "model": kwargs.get("model") or kwargs.get("modelId"),
            "reasoning_effort": _effort(kwargs),
            "source": "executor_provider_call",
        }
        # Headers, API keys and URL query credentials must never enter receipts.
        request = {key: kwargs[key] for key in (
            "messages", "input", "instructions", "tools", "model", "modelId", "reasoning", "reasoning_effort"
        ) if key in kwargs}
        self.event("request", {"execution": record, "request": request})
        return record

    def response(self, record, response):
        record = dict(record)
        response_model = getattr(response, "model", None)
        if isinstance(response, dict):
            response_model = response.get("model")
        if isinstance(response_model, str) and response_model:
            record["response_model"] = response_model
        self.calls.append(record)
        self.event("response", {"execution": record, "response": response})

    def summary(self):
        last = self.calls[-1] if self.calls else {}
        return {"resolved_provider": last.get("resolved_provider"),
                "reasoning_effort": last.get("reasoning_effort"),
                "execution_evidence": list(self.calls)}

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
