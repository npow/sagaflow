"""Tests for sagaflow.budget — economic control subsystem."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sagaflow.budget.policy import BudgetPolicy, PolicyLoader
from sagaflow.budget.ledger import BudgetDecision, BudgetLedger, BudgetStatus
from sagaflow.budget.enforcer import BudgetEnforcer
from sagaflow.budget.router import TierRouter
from sagaflow.budget.alerts import fire_threshold_alert
from sagaflow.budget.registry import get_enforcer, register, unregister
from sagaflow.cost import _infer_tier, estimate_cost_from_result


# ── BudgetPolicy ──────────────────────────────────────────────────────────


def test_policy_defaults() -> None:
    p = BudgetPolicy()
    assert p.max_cost_usd is None
    assert p.downgrade_threshold == 0.85
    assert p.hard_stop is True
    assert p.alert_thresholds == [0.5, 0.8, 1.0]
    assert p.downgrade_ladder == ["OPUS", "SONNET", "HAIKU"]


def test_policy_custom() -> None:
    p = BudgetPolicy(max_cost_usd=5.0, downgrade_threshold=0.7, hard_stop=False)
    assert p.max_cost_usd == 5.0
    assert p.downgrade_threshold == 0.7
    assert p.hard_stop is False


# ── BudgetLedger ─────────────────────────────────────────────────────────


def test_ledger_no_budget() -> None:
    ledger = BudgetLedger(policy=BudgetPolicy())
    status = ledger.check()
    assert status.decision == BudgetDecision.ALLOW
    assert status.budget_usd is None
    assert status.fraction is None


def test_ledger_within_budget() -> None:
    ledger = BudgetLedger(policy=BudgetPolicy(max_cost_usd=1.0))
    ledger.record_step(0.3)
    status = ledger.check()
    assert status.decision == BudgetDecision.ALLOW
    assert status.fraction == pytest.approx(0.3)


def test_ledger_downgrade_threshold() -> None:
    ledger = BudgetLedger(policy=BudgetPolicy(max_cost_usd=1.0, downgrade_threshold=0.85))
    ledger.record_step(0.9)
    status = ledger.check()
    assert status.decision == BudgetDecision.DOWNGRADE
    assert status.fraction == pytest.approx(0.9)


def test_ledger_abort_on_exceed() -> None:
    ledger = BudgetLedger(policy=BudgetPolicy(max_cost_usd=1.0, hard_stop=True))
    ledger.record_step(1.5)
    status = ledger.check()
    assert status.decision == BudgetDecision.ABORT


def test_ledger_no_abort_when_soft_stop() -> None:
    ledger = BudgetLedger(policy=BudgetPolicy(max_cost_usd=1.0, hard_stop=False))
    ledger.record_step(1.5)
    status = ledger.check()
    assert status.decision == BudgetDecision.DOWNGRADE


def test_ledger_budget_fraction_zero_budget() -> None:
    ledger = BudgetLedger(policy=BudgetPolicy(max_cost_usd=0.0))
    assert ledger.budget_fraction == 0.0
    ledger.record_step(0.01)
    assert ledger.budget_fraction == float("inf")


def test_ledger_step_count() -> None:
    ledger = BudgetLedger(policy=BudgetPolicy(max_cost_usd=10.0))
    ledger.record_step(0.1)
    ledger.record_step(0.2)
    ledger.record_step(0.3)
    assert ledger.step_count == 3
    assert ledger.accumulated_cost_usd == pytest.approx(0.6)


def test_ledger_from_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "run_manifest.json"
    manifest.write_text(json.dumps({
        "budget_result": {
            "final_cost_usd": 0.42,
            "step_count": 7,
            "alerts_fired": [0.5],
        }
    }))
    policy = BudgetPolicy(max_cost_usd=1.0)
    ledger = BudgetLedger.from_manifest_or_fresh(policy, manifest)
    assert ledger.accumulated_cost_usd == pytest.approx(0.42)
    assert ledger.step_count == 7
    assert 0.5 in ledger.alerts_fired


def test_ledger_from_manifest_missing_file() -> None:
    policy = BudgetPolicy(max_cost_usd=1.0)
    ledger = BudgetLedger.from_manifest_or_fresh(policy, Path("/nonexistent"))
    assert ledger.accumulated_cost_usd == 0.0
    assert ledger.step_count == 0


def test_ledger_from_manifest_none_path() -> None:
    policy = BudgetPolicy(max_cost_usd=1.0)
    ledger = BudgetLedger.from_manifest_or_fresh(policy, None)
    assert ledger.accumulated_cost_usd == 0.0


# ── TierRouter ───────────────────────────────────────────────────────────


def test_router_no_downgrade() -> None:
    policy = BudgetPolicy(max_cost_usd=10.0)
    router = TierRouter(policy)
    status = BudgetStatus(
        decision=BudgetDecision.ALLOW,
        current_cost_usd=1.0, budget_usd=10.0,
        fraction=0.1, recommended_tier=None, message="ok",
    )
    assert router.resolve("critic", "SONNET", status) == "SONNET"


def test_router_downgrade_opus_to_sonnet() -> None:
    policy = BudgetPolicy(max_cost_usd=10.0)
    router = TierRouter(policy)
    status = BudgetStatus(
        decision=BudgetDecision.DOWNGRADE,
        current_cost_usd=9.0, budget_usd=10.0,
        fraction=0.9, recommended_tier=None, message="downgrade",
    )
    assert router.resolve("critic", "OPUS", status) == "SONNET"


def test_router_downgrade_sonnet_to_haiku() -> None:
    policy = BudgetPolicy(max_cost_usd=10.0)
    router = TierRouter(policy)
    status = BudgetStatus(
        decision=BudgetDecision.DOWNGRADE,
        current_cost_usd=9.0, budget_usd=10.0,
        fraction=0.9, recommended_tier=None, message="downgrade",
    )
    assert router.resolve("critic", "SONNET", status) == "HAIKU"


def test_router_downgrade_haiku_stays_haiku() -> None:
    policy = BudgetPolicy(max_cost_usd=10.0)
    router = TierRouter(policy)
    status = BudgetStatus(
        decision=BudgetDecision.DOWNGRADE,
        current_cost_usd=9.0, budget_usd=10.0,
        fraction=0.9, recommended_tier=None, message="downgrade",
    )
    assert router.resolve("critic", "HAIKU", status) == "HAIKU"


def test_router_tier_profile_override() -> None:
    policy = BudgetPolicy(max_cost_usd=10.0, tier_profile={"judge": "HAIKU"})
    router = TierRouter(policy)
    status = BudgetStatus(
        decision=BudgetDecision.ALLOW,
        current_cost_usd=1.0, budget_usd=10.0,
        fraction=0.1, recommended_tier=None, message="ok",
    )
    assert router.resolve("judge", "OPUS", status) == "HAIKU"


def test_router_unknown_tier_no_downgrade() -> None:
    policy = BudgetPolicy(max_cost_usd=10.0)
    router = TierRouter(policy)
    status = BudgetStatus(
        decision=BudgetDecision.DOWNGRADE,
        current_cost_usd=9.0, budget_usd=10.0,
        fraction=0.9, recommended_tier=None, message="downgrade",
    )
    assert router.resolve("critic", "UNKNOWN_TIER", status) == "UNKNOWN_TIER"


# ── BudgetEnforcer ───────────────────────────────────────────────────────


def test_enforcer_pre_dispatch_allow() -> None:
    policy = BudgetPolicy(max_cost_usd=10.0)
    ledger = BudgetLedger(policy=policy)
    router = TierRouter(policy)
    enforcer = BudgetEnforcer(ledger, router)

    tier, status = enforcer.pre_dispatch("critic", "SONNET")
    assert tier == "SONNET"
    assert status.decision == BudgetDecision.ALLOW


def test_enforcer_pre_dispatch_downgrade() -> None:
    policy = BudgetPolicy(max_cost_usd=1.0, downgrade_threshold=0.85)
    ledger = BudgetLedger(policy=policy, accumulated_cost_usd=0.9)
    router = TierRouter(policy)
    enforcer = BudgetEnforcer(ledger, router)

    tier, status = enforcer.pre_dispatch("critic", "OPUS")
    assert tier == "SONNET"
    assert status.decision == BudgetDecision.DOWNGRADE


def test_enforcer_pre_dispatch_abort() -> None:
    policy = BudgetPolicy(max_cost_usd=1.0, hard_stop=True)
    ledger = BudgetLedger(policy=policy, accumulated_cost_usd=1.5)
    router = TierRouter(policy)
    enforcer = BudgetEnforcer(ledger, router)

    tier, status = enforcer.pre_dispatch("critic", "SONNET")
    assert status.decision == BudgetDecision.ABORT


def test_enforcer_record_cost_and_alerts() -> None:
    policy = BudgetPolicy(max_cost_usd=1.0, alert_thresholds=[0.5, 0.8, 1.0])
    ledger = BudgetLedger(policy=policy)
    router = TierRouter(policy)
    enforcer = BudgetEnforcer(ledger, router)

    crossed = enforcer.record_cost(0.55)
    assert 0.5 in crossed
    assert 0.8 not in crossed

    crossed = enforcer.record_cost(0.30)
    assert 0.8 in crossed
    assert 0.5 not in crossed  # already fired


def test_enforcer_no_duplicate_alerts() -> None:
    policy = BudgetPolicy(max_cost_usd=1.0, alert_thresholds=[0.5])
    ledger = BudgetLedger(policy=policy)
    router = TierRouter(policy)
    enforcer = BudgetEnforcer(ledger, router)

    crossed1 = enforcer.record_cost(0.6)
    assert 0.5 in crossed1

    crossed2 = enforcer.record_cost(0.1)
    assert crossed2 == []


def test_enforcer_no_alerts_without_budget() -> None:
    policy = BudgetPolicy()  # no max_cost_usd
    ledger = BudgetLedger(policy=policy)
    router = TierRouter(policy)
    enforcer = BudgetEnforcer(ledger, router)

    crossed = enforcer.record_cost(100.0)
    assert crossed == []


# ── Registry ─────────────────────────────────────────────────────────────


def test_registry_round_trip() -> None:
    policy = BudgetPolicy(max_cost_usd=5.0)
    ledger = BudgetLedger(policy=policy)
    router = TierRouter(policy)
    enforcer = BudgetEnforcer(ledger, router)

    register("wf-123", enforcer)
    assert get_enforcer("wf-123") is enforcer

    unregister("wf-123")
    assert get_enforcer("wf-123") is None


def test_registry_missing_returns_none() -> None:
    assert get_enforcer("nonexistent-wf") is None


def test_registry_unregister_missing_is_noop() -> None:
    unregister("nonexistent-wf")  # should not raise


# ── Cost helpers ─────────────────────────────────────────────────────────


def test_infer_tier_haiku() -> None:
    assert _infer_tier("claude-haiku-4-5-20251001") == "HAIKU"


def test_infer_tier_opus() -> None:
    assert _infer_tier("claude-opus-4-7") == "OPUS"


def test_infer_tier_sonnet() -> None:
    assert _infer_tier("claude-sonnet-4-6") == "SONNET"


def test_infer_tier_unknown_defaults_sonnet() -> None:
    assert _infer_tier("gpt-4o") == "SONNET"


def test_estimate_cost_from_result() -> None:
    result = {
        "_input_tokens": "1000",
        "_output_tokens": "500",
        "_model": "claude-sonnet-4-6",
    }
    cost = estimate_cost_from_result(result)
    assert cost > 0
    assert cost == pytest.approx((1000 * 3.0 + 500 * 15.0) / 1_000_000)


def test_estimate_cost_from_result_missing_tokens() -> None:
    result = {"_model": "claude-sonnet-4-6"}
    cost = estimate_cost_from_result(result)
    assert cost == 0.0


def test_estimate_cost_from_result_bad_tokens() -> None:
    result = {"_input_tokens": "not-a-number", "_output_tokens": "500", "_model": "claude-sonnet-4-6"}
    cost = estimate_cost_from_result(result)
    assert cost == 0.0


# ── Alerts ───────────────────────────────────────────────────────────────


def test_alert_no_channel_logs_only() -> None:
    fire_threshold_alert(
        threshold=0.5,
        accumulated_cost_usd=0.5,
        max_cost_usd=1.0,
        run_id="test-run",
        slack_channel=None,
        slack_thread_ts=None,
    )


@patch("sagaflow.slack_progress._slack_post")
def test_alert_posts_to_slack(mock_post: object) -> None:
    fire_threshold_alert(
        threshold=0.8,
        accumulated_cost_usd=0.8,
        max_cost_usd=1.0,
        run_id="test-run",
        slack_channel="C123",
        slack_thread_ts="1234.5678",
    )
    mock_post.assert_called_once()  # type: ignore[attr-defined]


# ── Manifest budget_result ───────────────────────────────────────────────


def test_write_budget_result(tmp_path: Path) -> None:
    from sagaflow.manifest import write_budget_result, _read_manifest

    manifest_file = tmp_path / "run_manifest.json"
    manifest_file.write_text(json.dumps({"run_id": "test", "status": "RUNNING"}))
    write_budget_result(
        run_dir=tmp_path,
        accumulated_cost_usd=0.42,
        max_cost_usd=1.0,
        step_count=7,
        alerts_fired=[0.5],
        final_decision="allow",
    )

    data = _read_manifest(tmp_path)
    br = data["budget_result"]
    assert br["final_cost_usd"] == pytest.approx(0.42)
    assert br["max_cost_usd"] == 1.0
    assert br["step_count"] == 7
    assert br["alerts_fired"] == [0.5]
    assert br["final_decision"] == "allow"


# ── PolicyLoader ─────────────────────────────────────────────────────────


def test_policy_loader_from_frontmatter(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: my-skill\n"
        "budget:\n"
        "  max_cost_usd: 2.5\n"
        "  hard_stop: false\n"
        "---\n"
        "# My Skill\n"
    )
    policy = PolicyLoader.load(skill_dir)
    assert policy is not None
    assert policy.max_cost_usd == 2.5
    assert policy.hard_stop is False


def test_policy_loader_from_yaml(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    budget_yaml = skill_dir / "SKILL.budget.yaml"
    budget_yaml.write_text(
        "max_cost_usd: 3.0\n"
        "downgrade_threshold: 0.7\n"
    )
    policy = PolicyLoader.load(skill_dir)
    assert policy is not None
    assert policy.max_cost_usd == 3.0
    assert policy.downgrade_threshold == 0.7


def test_policy_loader_yaml_overrides_frontmatter(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: x\nbudget:\n  max_cost_usd: 1.0\n---\n"
    )
    (skill_dir / "SKILL.budget.yaml").write_text("max_cost_usd: 5.0\n")
    policy = PolicyLoader.load(skill_dir)
    assert policy is not None
    assert policy.max_cost_usd == 5.0


def test_policy_loader_cli_overrides(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.budget.yaml").write_text("max_cost_usd: 3.0\n")
    policy = PolicyLoader.load(skill_dir, overrides={"max_cost_usd": 10.0})
    assert policy is not None
    assert policy.max_cost_usd == 10.0


def test_policy_loader_no_config(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    policy = PolicyLoader.load(skill_dir)
    assert policy is None


def test_policy_loader_overrides_only(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    policy = PolicyLoader.load(skill_dir, overrides={"max_cost_usd": 2.0})
    assert policy is not None
    assert policy.max_cost_usd == 2.0
