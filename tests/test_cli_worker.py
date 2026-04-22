from unittest.mock import patch

from click.testing import CliRunner

from skillflow.cli import main


def test_worker_run_invokes_run_worker() -> None:
    runner = CliRunner()
    with patch("skillflow.worker.run_worker") as rw:
        rw.return_value = None
        result = runner.invoke(main, ["worker", "run"])
    assert result.exit_code == 0
