from unittest.mock import patch

from click.testing import CliRunner

from sagaflow.cli import main


def test_doctor_all_green() -> None:
    runner = CliRunner()
    with (
        patch("sagaflow.cli._probe_temporal", return_value=("OK", None)),
        patch("sagaflow.cli._probe_transport", return_value=("OK", None)),
        patch("sagaflow.cli._probe_worker", return_value=("OK", None)),
        patch("sagaflow.cli._probe_hook", return_value=("OK", None)),
    ):
        result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert result.output.count("OK") >= 4


def test_doctor_reports_failures() -> None:
    runner = CliRunner()
    with (
        patch("sagaflow.cli._probe_temporal", return_value=("FAIL", "not reachable")),
        patch("sagaflow.cli._probe_transport", return_value=("OK", None)),
        patch("sagaflow.cli._probe_worker", return_value=("OK", None)),
        patch("sagaflow.cli._probe_hook", return_value=("OK", None)),
    ):
        result = runner.invoke(main, ["doctor"])
    assert result.exit_code != 0
    assert "FAIL" in result.output
    assert "not reachable" in result.output
