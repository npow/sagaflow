import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from sagaflow.worker import (
    _DIR_TO_LEGACY,
    _pre_register_package_stub,
    _register_legacy_aliases,
    build_registry,
    _is_worker_reachable,
)


def fake_wf_cls():
    class W:  # pragma: no cover
        pass

    return W


# ---------------------------------------------------------------------------
# Existing tests
# ---------------------------------------------------------------------------


async def test_build_registry_includes_hello_world() -> None:
    registry = build_registry()
    assert "hello-world" in set(registry.names())


async def test_is_worker_reachable_true_when_pollers_exist() -> None:
    fake_client = SimpleNamespace(
        service_client=SimpleNamespace(
            workflow_service=SimpleNamespace(
                describe_task_queue=AsyncMock(
                    return_value=SimpleNamespace(
                        pollers=[SimpleNamespace(identity="worker-1")]
                    )
                )
            )
        )
    )
    reachable = await _is_worker_reachable(fake_client)
    assert reachable is True


async def test_is_worker_reachable_false_when_no_pollers() -> None:
    fake_client = SimpleNamespace(
        service_client=SimpleNamespace(
            workflow_service=SimpleNamespace(
                describe_task_queue=AsyncMock(
                    return_value=SimpleNamespace(pollers=[])
                )
            )
        )
    )
    reachable = await _is_worker_reachable(fake_client)
    assert reachable is False


# ---------------------------------------------------------------------------
# Stub pre-registration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def clean_skills_modules():
    """Remove all skills.* entries from sys.modules for test isolation."""
    to_remove = [k for k in sys.modules if k == "skills" or k.startswith("skills.")]
    saved = {k: sys.modules.pop(k) for k in to_remove}
    yield
    # Restore original state.
    for k in list(sys.modules):
        if k == "skills" or k.startswith("skills."):
            del sys.modules[k]
    sys.modules.update(saved)


def test_pre_register_creates_stub_with_marker(tmp_path, clean_skills_modules):
    skill_dir = tmp_path / "my_skill"
    skill_dir.mkdir()
    (skill_dir / "__init__.py").write_text("")

    _pre_register_package_stub(skill_dir, "my_skill")

    mod = sys.modules["skills.my_skill"]
    assert getattr(mod, "_is_stub", False) is True
    assert str(skill_dir) in mod.__path__
    assert mod.__package__ == "skills.my_skill"


def test_pre_register_is_idempotent(tmp_path, clean_skills_modules):
    skill_dir = tmp_path / "my_skill"
    skill_dir.mkdir()
    (skill_dir / "__init__.py").write_text("")

    _pre_register_package_stub(skill_dir, "my_skill")
    first = sys.modules["skills.my_skill"]

    _pre_register_package_stub(skill_dir, "my_skill")
    assert sys.modules["skills.my_skill"] is first


def test_pre_register_does_not_execute_code(tmp_path, clean_skills_modules):
    skill_dir = tmp_path / "my_skill"
    skill_dir.mkdir()
    (skill_dir / "__init__.py").write_text("raise RuntimeError('should not run')")

    _pre_register_package_stub(skill_dir, "my_skill")
    assert getattr(sys.modules["skills.my_skill"], "_is_stub", False) is True


# ---------------------------------------------------------------------------
# Legacy alias registration replaces stubs
# ---------------------------------------------------------------------------


def test_register_legacy_aliases_replaces_stub(tmp_path, clean_skills_modules):
    skill_dir = tmp_path / "my_skill"
    skill_dir.mkdir()
    (skill_dir / "__init__.py").write_text("LOADED = True")

    _pre_register_package_stub(skill_dir, "my_skill")
    assert getattr(sys.modules["skills.my_skill"], "_is_stub", False) is True

    _register_legacy_aliases(skill_dir, "my_skill")
    mod = sys.modules["skills.my_skill"]
    assert not getattr(mod, "_is_stub", False)
    assert getattr(mod, "LOADED", False) is True


# ---------------------------------------------------------------------------
# Cross-skill dependency ordering
# ---------------------------------------------------------------------------


def test_stubs_allow_cross_skill_imports(tmp_path, clean_skills_modules):
    """Simulate autopilot's dependency pattern: skill A imports from skill B."""
    skill_b = tmp_path / "skill_b"
    skill_b.mkdir()
    (skill_b / "__init__.py").write_text("B_VALUE = 42")
    (skill_b / "workflow.py").write_text("WF_VALUE = 'hello'")

    skill_a = tmp_path / "skill_a"
    skill_a.mkdir()
    (skill_a / "__init__.py").write_text(
        "from skills.skill_b.workflow import WF_VALUE\nA_VALUE = WF_VALUE"
    )

    # Phase 1: stubs for both
    _pre_register_package_stub(skill_a, "skill_a")
    _pre_register_package_stub(skill_b, "skill_b")

    # Phase 2: register B first (would happen alphabetically), then A
    _register_legacy_aliases(skill_b, "skill_b")
    _register_legacy_aliases(skill_a, "skill_a")

    assert sys.modules["skills.skill_a"].A_VALUE == "hello"


def test_stubs_allow_reverse_order_imports(tmp_path, clean_skills_modules):
    """Without stubs, loading A before B would fail. With stubs, it works."""
    skill_b = tmp_path / "skill_b"
    skill_b.mkdir()
    (skill_b / "__init__.py").write_text("")
    (skill_b / "workflow.py").write_text("WF_VALUE = 99")

    skill_a = tmp_path / "skill_a"
    skill_a.mkdir()
    (skill_a / "__init__.py").write_text(
        "from skills.skill_b.workflow import WF_VALUE\nA_VALUE = WF_VALUE"
    )

    # Phase 1: stubs
    _pre_register_package_stub(skill_a, "skill_a")
    _pre_register_package_stub(skill_b, "skill_b")

    # Phase 2: load A first (alphabetical order, like the bug scenario)
    _register_legacy_aliases(skill_a, "skill_a")
    _register_legacy_aliases(skill_b, "skill_b")

    assert sys.modules["skills.skill_a"].A_VALUE == 99


# ---------------------------------------------------------------------------
# build_registry loads autopilot
# ---------------------------------------------------------------------------


def test_build_registry_loads_autopilot() -> None:
    registry = build_registry()
    names = set(registry.names())
    assert "autopilot" in names, f"autopilot missing from registry; got: {sorted(names)}"


def test_build_registry_loads_all_legacy_skills() -> None:
    registry = build_registry()
    names = set(registry.names())
    expected = {"hello-world", "deep-qa", "deep-debug", "deep-research",
                "deep-design", "deep-plan", "autopilot", "loop-until-done",
                "team", "proposal-reviewer", "flaky-test-diagnoser"}
    missing = expected - names
    assert not missing, f"missing skills: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Error logging
# ---------------------------------------------------------------------------


def test_build_registry_logs_error_on_failure(tmp_path, caplog, clean_skills_modules):
    """When a skill fails to load, build_registry logs at WARNING + summary ERROR."""
    bad_skill = tmp_path / "bad-skill-temporal"
    bad_skill.mkdir()
    (bad_skill / "__init__.py").write_text("raise RuntimeError('boom')")

    patched_legacy = {**_DIR_TO_LEGACY, "bad-skill-temporal": "bad_skill"}

    with (
        patch("sagaflow.worker._DIR_TO_LEGACY", patched_legacy),
        patch("sagaflow.worker.claude_skills_dir", return_value=tmp_path),
        caplog.at_level(logging.WARNING, logger="sagaflow.worker"),
    ):
        build_registry()

    assert any("bad-skill-temporal" in r.message and r.levelno >= logging.WARNING
               for r in caplog.records), \
        f"expected WARNING for bad-skill-temporal, got: {[r.message for r in caplog.records]}"

    assert any("skill loading failures" in r.message and r.levelno == logging.ERROR
               for r in caplog.records), \
        f"expected ERROR summary, got: {[r.message for r in caplog.records]}"


def test_build_registry_phase3_skips_stubs(tmp_path, clean_skills_modules):
    """Phase 3 should not treat an unresolved stub as a real module."""
    skill_dir = tmp_path / "stub-only-temporal"
    skill_dir.mkdir()
    (skill_dir / "__init__.py").write_text("raise RuntimeError('unreachable')")

    patched_legacy = {"stub-only-temporal": "stub_only"}

    with (
        patch("sagaflow.worker._DIR_TO_LEGACY", patched_legacy),
        patch("sagaflow.worker.claude_skills_dir", return_value=tmp_path),
    ):
        _pre_register_package_stub(skill_dir, "stub_only")
        stub = sys.modules["skills.stub_only"]
        stub._is_stub = True

        # Phase 2 will fail (RuntimeError), leaving the stub in place.
        # Phase 3 should detect the stub and not call register() on it.
        registry = build_registry()
        # No crash means Phase 3 correctly skipped the stub.
