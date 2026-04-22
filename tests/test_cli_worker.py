from unittest.mock import patch

from click.testing import CliRunner

from sagaflow.cli import main


def test_worker_run_invokes_run_worker() -> None:
    runner = CliRunner()
    with patch("sagaflow.worker.run_worker") as rw:
        rw.return_value = None
        result = runner.invoke(main, ["worker", "run"])
    assert result.exit_code == 0
