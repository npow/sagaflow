import pytest

from sagaflow.registry import SkillRegistry, SkillSpec


def fake_workflow_cls():
    class W:  # pragma: no cover - test double
        pass

    return W


def fake_activity(_inp):  # pragma: no cover - test double
    return None


def test_register_and_lookup() -> None:
    registry = SkillRegistry()
    wf = fake_workflow_cls()
    spec = SkillSpec(name="hello-world", workflow_cls=wf, activities=[fake_activity])
    registry.register(spec)
    assert registry.get("hello-world") is spec
    assert list(registry.names()) == ["hello-world"]


def test_duplicate_registration_raises() -> None:
    registry = SkillRegistry()
    wf = fake_workflow_cls()
    spec = SkillSpec(name="hello-world", workflow_cls=wf, activities=[])
    registry.register(spec)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(spec)


def test_get_unknown_raises() -> None:
    registry = SkillRegistry()
    with pytest.raises(KeyError, match="unknown skill"):
        registry.get("missing")


def test_all_activities_returns_union() -> None:
    registry = SkillRegistry()

    def a1(inp):  # pragma: no cover
        return None

    def a2(inp):  # pragma: no cover
        return None

    registry.register(SkillSpec(name="s1", workflow_cls=fake_workflow_cls(), activities=[a1]))
    registry.register(SkillSpec(name="s2", workflow_cls=fake_workflow_cls(), activities=[a2]))
    assert set(registry.all_activities()) == {a1, a2}


def test_all_workflows_returns_union() -> None:
    registry = SkillRegistry()
    w1 = fake_workflow_cls()
    w2 = fake_workflow_cls()
    registry.register(SkillSpec(name="s1", workflow_cls=w1, activities=[]))
    registry.register(SkillSpec(name="s2", workflow_cls=w2, activities=[]))
    assert set(registry.all_workflows()) == {w1, w2}
