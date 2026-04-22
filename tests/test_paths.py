from pathlib import Path

from sagaflow.paths import Paths


def test_paths_default_home_is_dot_sagaflow(tmp_path: Path) -> None:
    p = Paths(root=tmp_path)
    assert p.root == tmp_path
    assert p.inbox == tmp_path / "INBOX.md"
    assert p.runs_dir == tmp_path / "runs"
    assert p.worker_log_dir == tmp_path / "logs"


def test_run_dir_for(tmp_path: Path) -> None:
    p = Paths(root=tmp_path)
    run_dir = p.run_dir_for("hello-world-abc123")
    assert run_dir == tmp_path / "runs" / "hello-world-abc123"


def test_ensure_creates_directories(tmp_path: Path) -> None:
    p = Paths(root=tmp_path)
    p.ensure()
    assert p.runs_dir.is_dir()
    assert p.worker_log_dir.is_dir()


def test_from_env_respects_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SAGAFLOW_ROOT", str(tmp_path / "custom"))
    p = Paths.from_env()
    assert p.root == tmp_path / "custom"


def test_from_env_defaults_to_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SAGAFLOW_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    p = Paths.from_env()
    assert p.root == tmp_path / ".sagaflow"
