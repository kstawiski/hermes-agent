"""Executor receipts cannot inherit a requested identity or invent missing effort."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.oneshot_evidence import OneshotEvidence


@pytest.mark.parametrize("effort", ["high", "xhigh"])
def test_oneshot_cli_passes_reasoning_to_agent(effort, monkeypatch, tmp_path):
    import hermes_cli.main as main
    import hermes_cli.oneshot as oneshot
    import hermes_cli.config as config
    import hermes_cli.mcp_startup as mcp
    import run_agent
    from hermes_cli._parser import build_top_level_parser

    # Real config/runtime provider resolution with an isolated named custom endpoint.
    cfg = {"model": {"default": "example-model", "provider": "custom:local"},
           "providers": {"local": {"base_url": "http://127.0.0.1:54321/v1", "api_key": "test-key"}},
           "agent": {"reasoning_effort": "low"}}
    monkeypatch.setattr(config, "load_config", lambda **kw: cfg)
    monkeypatch.setattr(mcp, "ensure_mcp_discovery_before_agent_build", lambda **kw: None)
    monkeypatch.setattr(oneshot, "_create_session_db_for_oneshot", lambda: None)
    monkeypatch.setattr(oneshot, "_close_agent", lambda *a: None)
    captured = {}

    class Agent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def run_conversation(self, prompt):
            return {"final_response": "finished", "messages": [{"role": "user", "content": prompt}]}

    monkeypatch.setattr(run_agent, "AIAgent", Agent)
    monkeypatch.setattr(main, "_confirm_startup_expensive_model_override", lambda args: None)
    monkeypatch.setattr(main, "_cleanup_oneshot_runtime", lambda: None)
    exit_codes = []
    monkeypatch.setattr(main, "_exit_after_oneshot", exit_codes.append)
    parser, _, _ = build_top_level_parser()
    env_path = tmp_path / "env-transcript.jsonl"
    monkeypatch.setenv("HERMES_TRANSCRIPT_FILE", str(env_path))
    explicit_path = tmp_path / "explicit-transcript.jsonl"
    trajectory_args = ["--trajectory-file", str(explicit_path)] if effort == "xhigh" else []
    args = parser.parse_args(["-z", "work", "--model", "example-model", "--provider", "custom:local", "--reasoning", effort, *trajectory_args])
    main._run_oneshot_from_args(args)
    assert exit_codes == [0]
    assert captured["reasoning_config"] == {"enabled": True, "effort": effort}
    transcript = explicit_path if effort == "xhigh" else env_path
    events = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert events[0] == {"event": "prompt", "data": {"content": "work"}}
    assert events[-1]["data"]["final_response"] == "finished"
    if effort == "xhigh":
        assert not env_path.exists()


def test_receipts_bind_endpoint_and_record_fallback_without_requested_values(tmp_path):
    cfg = {"providers": {"local": {"base_url": "http://127.0.0.1:54321/v1", "api_key": "secret"}}}
    path = tmp_path / "trajectory.jsonl"
    evidence = OneshotEvidence(cfg, path)
    agent = SimpleNamespace(provider="custom", base_url="http://wrong/v1", client=SimpleNamespace(base_url="http://127.0.0.1:54321/v1/"), requested_provider="custom:local")
    kwargs = {"model": "model-a", "extra_body": {"reasoning": {"effort": "high"}}, "extra_headers": {"Authorization": "secret"}, "messages": [{"role": "tool", "content": "full output"}]}
    record = evidence.request(agent, kwargs)
    evidence.response(record, SimpleNamespace(model="model-a", choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))]))
    assert evidence.summary()["resolved_provider"] == "custom:local"
    assert evidence.summary()["reasoning_effort"] == "high"
    # A fallback client endpoint overrides stale requested/base_url values.
    agent.client.base_url = "http://elsewhere/v1"
    record = evidence.request(agent, {"model": "fallback"})
    evidence.response(record, {"model": "fallback"})
    assert evidence.summary()["resolved_provider"] == "custom"
    assert evidence.summary()["reasoning_effort"] is None
    assert len(evidence.summary()["execution_evidence"]) == 2
    evidence.close()
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert events[0]["data"]["request"]["messages"][0]["content"] == "full output"
    assert events[1]["data"]["response"]["choices"][0]["message"]["content"] == "answer"
    assert "secret" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        OneshotEvidence(cfg, path)
    absent = OneshotEvidence(cfg)
    assert absent.summary()["resolved_provider"] is None
    assert absent.summary()["reasoning_effort"] is None


@pytest.mark.parametrize("streaming", [False, True])
def test_call_boundary_records_effective_middleware_request(streaming, monkeypatch, tmp_path):
    from agent.turn_api_call import perform_api_call
    import hermes_cli.middleware as middleware
    import agent.relay_llm as relay

    evidence = OneshotEvidence({})
    agent = SimpleNamespace(
        _oneshot_evidence=evidence, _disable_streaming=not streaming,
        api_mode="chat_completions", base_url="https://example.invalid/v1", provider="openai",
        model="requested-model", session_id="test", platform="cli", _has_stream_consumers=lambda: True,
        _has_pending_redirect=lambda: False, _interruptible_api_call=lambda kw: {"model": kw["model"]},
        _interruptible_streaming_api_call=lambda kw, **other: {"model": kw["model"]},
    )
    def effective_request(kwargs, execute, **other):
        # Middleware may alter what is executed. The receipt must capture this value.
        return execute({**kwargs, "model": "effective-model", "reasoning_effort": "xhigh"})
    monkeypatch.setattr(middleware, "run_llm_execution_middleware", effective_request)
    monkeypatch.setattr(relay, "execute", lambda kw, fn, **other: fn(kw))
    perform_api_call(agent, api_kwargs={"model": "requested-model", "reasoning_effort": "high"},
        _original_api_kwargs={}, _llm_middleware_trace=[], _moa_prepared_request=None,
        _retry=SimpleNamespace(), thinking_spinner=None, retry_count=0, api_call_count=0,
        api_request_id="r", effective_task_id="t", turn_id="turn", interrupted=False)
    assert evidence.summary()["reasoning_effort"] == "xhigh"
    assert evidence.summary()["execution_evidence"][0]["model"] == "effective-model"
