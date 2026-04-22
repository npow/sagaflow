import pytest

from skillflow.durable.state import ActivityOutcome, WorkflowState


def test_increment_generation_is_monotonic() -> None:
    s = WorkflowState(run_id="abc", skill="hello-world")
    assert s.generation == 0
    s.increment_generation()
    assert s.generation == 1
    s.increment_generation()
    assert s.generation == 2


def test_record_activity_outcome() -> None:
    s = WorkflowState(run_id="abc", skill="hello-world")
    s.record_outcome(ActivityOutcome(activity_id="act-1", role="greeter", status="completed"))
    assert len(s.activity_outcomes) == 1
    assert s.activity_outcomes[0].status == "completed"


def test_add_cost_accumulates() -> None:
    s = WorkflowState(run_id="abc", skill="hello-world")
    s.add_cost(input_tokens=100, output_tokens=50, haiku=True)
    s.add_cost(input_tokens=200, output_tokens=100, haiku=True)
    # Haiku pricing: $0.80/$4 per million, so 300 input + 150 output
    expected = (300 / 1_000_000) * 0.80 + (150 / 1_000_000) * 4.0
    assert s.cost_running_total == pytest.approx(expected)


def test_terminal_label_is_set_once() -> None:
    s = WorkflowState(run_id="abc", skill="hello-world")
    s.set_terminal("completed")
    with pytest.raises(RuntimeError, match="already terminated"):
        s.set_terminal("failed")
