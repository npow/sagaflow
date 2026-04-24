"""Tests for ``sagaflow mission launch`` CLI — the integration gap that let B1/B2 ship."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def mission_yaml(tmp_path: Path) -> Path:
    """Minimal valid mission.yaml for CLI testing."""
    p = tmp_path / "mission.yaml"
    p.write_text(json.dumps({
        "mission": "Test mission for CLI verification",
        "workspace": str(tmp_path),
        "success_criteria": [
            {"id": "c1", "description": "always true", "check": "true"}
        ],
    }))
    return p


def test_mission_launch_calls_ensure_session_dirs() -> None:
    """B1 regression: cli.py must call ensure_session_dirs before workflow start."""
    import inspect
    from sagaflow.cli import mission_launch

    src = inspect.getsource(mission_launch.callback)
    assert "ensure_session_dirs(run_id)" in src, (
        "ensure_session_dirs call missing from mission_launch — B1 regression"
    )
    idx_dirs = src.index("ensure_session_dirs(run_id)")
    idx_workflow = src.index("start_workflow")
    assert idx_dirs < idx_workflow, (
        "ensure_session_dirs must be called BEFORE start_workflow"
    )


def test_pyyaml_importable() -> None:
    """B2 regression: yaml must be importable at runtime (not just dev extras)."""
    import yaml
    data = yaml.safe_load("key: value\n")
    assert data == {"key": "value"}


def test_mission_yaml_parsing(mission_yaml: Path) -> None:
    """mission launch must parse YAML without error."""
    import yaml
    from sagaflow.missions.schemas.mission import Mission

    with open(mission_yaml) as f:
        data = yaml.safe_load(f.read())

    m = Mission.model_validate(data)
    assert m.mission == "Test mission for CLI verification"
    assert len(m.success_criteria) == 1


def test_session_id_regex_accepts_mission_format() -> None:
    """The mission run_id format must pass the session_id validator."""
    from sagaflow.missions.lib.paths import validate_session_id
    sid = "mission-20260424-153022"
    assert validate_session_id(sid) == sid


def test_session_id_regex_rejects_traversal() -> None:
    """Session IDs with path traversal must be rejected."""
    from sagaflow.missions.lib.paths import validate_session_id
    with pytest.raises(ValueError):
        validate_session_id("../../../etc/passwd")


def test_ensure_session_dirs_creates_all_dirs(tmp_path: Path) -> None:
    """ensure_session_dirs must create state/, health/, children/, events_detail/, mission/, checks/."""
    with patch("sagaflow.missions.lib.paths._resolve_swarm_root", return_value=tmp_path):
        with patch("sagaflow.missions.lib.paths._resolve_swarm_config", return_value=tmp_path / "config"):
            from sagaflow.missions.lib.paths import _reset_for_tests, ensure_session_dirs
            _reset_for_tests()
            ensure_session_dirs("mission-test-20260424-000000")

            state = tmp_path / "state" / "mission-test-20260424-000000"
            assert state.is_dir()
            assert (state / "health").is_dir()
            assert (state / "children").is_dir()
            assert (state / "events_detail").is_dir()

            mission = tmp_path / "missions" / "mission-test-20260424-000000"
            assert mission.is_dir()
            assert (mission / "checks").is_dir()

            _reset_for_tests()
