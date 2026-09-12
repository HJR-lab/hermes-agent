"""A profile pinned with ``model.allow_fallback: false`` stops on a primary outage and never falls back.

Incident: the deus profile (claude-code-cli / claude-opus-5) hit a Claude Code session limit. The
harness reports it as bare text — "Claude Code CLI exited 1: You've hit your session limit · resets
12am" — which classified as a generic failure, so after the retries the loop walked
``fallback_providers`` onto openai-codex and served kanban work from the wrong provider.

These drive the real conversation loop against a primary that fails exactly that way, with the
fallback chain handed straight to the agent (as the gateway and delegation do) so the refusal is
proven at the runtime choke point, not only where config is read.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE
from hermes_cli.kanban_worker_exit import worker_exit_code

SESSION_LIMIT = "Claude Code CLI exited 1: You've hit your session limit · resets 12am (Asia/Singapore)"
CODEX = {"provider": "openai-codex", "model": "gpt-5.6-sol", "base_url": "https://chatgpt.com/backend-api/codex"}


def test_pinned_profile_refuses_init_time_fallback_when_primary_is_unavailable():
    """The pin is loaded before client construction, where a missing primary used to switch routes."""
    from run_agent import AIAgent

    Path(os.environ["HERMES_HOME"], "config.yaml").write_text(
        yaml.safe_dump({"model": {"allow_fallback": False}}), encoding="utf-8",
    )
    resolved_providers = []

    def resolve(provider, **_kwargs):
        resolved_providers.append(provider)
        if provider == CODEX["provider"]:
            return MagicMock(api_key="fallback-key", base_url=CODEX["base_url"]), CODEX["model"]
        return None, None

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        pytest.raises(RuntimeError, match="no API key was found"),
    ):
        AIAgent(
            provider="claude-code-cli",
            model="claude-opus-5",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[dict(CODEX)],
        )

    assert resolved_providers == ["claude-code-cli"]


def _run_turn_against_session_limit(*, pinned: bool):
    """One real turn whose primary always fails with the Claude Code session-limit text.

    Returns the turn result, the agent, and a spy on the fallback walk (every chain entry the loop
    considers passes through ``_should_skip_fallback_candidate``; the spy skips them all so the
    unpinned control never makes a network call).
    """
    from run_agent import AIAgent

    model_cfg = {"allow_fallback": False} if pinned else {}
    Path(os.environ["HERMES_HOME"], "config.yaml").write_text(
        yaml.safe_dump({"model": model_cfg, "agent": {"api_max_retries": 1}, "compression": {"enabled": False}}),
        encoding="utf-8",
    )
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[dict(CODEX)],
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = RuntimeError(SESSION_LIMIT)
    # Keep the transport mocked but pin the runtime identity to the exact Deus route under test.
    agent.provider = agent.requested_provider = "claude-code-cli"
    agent.model = "claude-opus-5"
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    primary = (agent.provider, agent.model)

    with (
        patch("agent.chat_completion_helpers._should_skip_fallback_candidate", return_value=True) as walked,
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)) as resolved,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("work kanban task t_pinned")
    return result, agent, primary, walked, resolved


def test_pinned_profile_never_walks_the_fallback_chain_and_exits_rate_limited():
    result, agent, primary, walked, resolved = _run_turn_against_session_limit(pinned=True)

    walked.assert_not_called()
    resolved.assert_not_called()
    assert (agent.provider, agent.model) == primary

    # Truthful outcome: a quota wall on the pinned route, not a task failure or a provider switch.
    assert result["failed"] is True
    assert result["failure_reason"] == "rate_limit"
    assert worker_exit_code(result, kanban_task="t_pinned") == KANBAN_RATE_LIMIT_EXIT_CODE
    assert "hit your session limit" in result["final_response"]
    assert f"pinned to {primary[0]}/{primary[1]}" in result["final_response"]


def test_unpinned_profile_still_walks_its_fallback_chain():
    """Control: the same failure without the pin reaches the chain, so the test above is not vacuous
    and profiles that allow fallback keep it."""
    result, _agent, _primary, walked, _resolved = _run_turn_against_session_limit(pinned=False)

    assert [c.args[3] for c in walked.call_args_list] == ["openai-codex"]
    assert result["failure_reason"] == "rate_limit"
    assert "pinned to" not in result["final_response"]
