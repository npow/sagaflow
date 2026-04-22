"""Smoke tests for HelloWorldState — verifies it extends WorkflowState cleanly."""

from __future__ import annotations

from sagaflow.durable.state import WorkflowState
from skills.hello_world.state import HelloWorldState


def test_hello_world_state_inherits_workflow_state_fields() -> None:
    s = HelloWorldState(run_id="r1", skill="hello-world")
    assert isinstance(s, WorkflowState)
    # Base fields default cleanly.
    assert s.run_id == "r1"
    assert s.skill == "hello-world"
    assert s.generation == 0
    assert s.activity_outcomes == []
    assert s.cost_running_total == 0.0
    assert s.terminal_label is None


def test_hello_world_state_adds_greeting_fields_with_defaults() -> None:
    s = HelloWorldState(run_id="r1", skill="hello-world")
    assert s.greeting_name == ""
    assert s.greeting_text == ""


def test_hello_world_state_accepts_greeting_values() -> None:
    s = HelloWorldState(
        run_id="r1",
        skill="hello-world",
        greeting_name="alice",
        greeting_text="hello, alice",
    )
    assert s.greeting_name == "alice"
    assert s.greeting_text == "hello, alice"


def test_hello_world_state_participates_in_base_workflow_lifecycle() -> None:
    s = HelloWorldState(run_id="r1", skill="hello-world")
    s.increment_generation()
    s.set_terminal("completed")
    assert s.generation == 1
    assert s.terminal_label == "completed"
