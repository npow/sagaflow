"""Tests for sagaflow.catalog — skill capability discovery."""

import json
from pathlib import Path

import pytest

from sagaflow.catalog import (
    SkillCatalog,
    _compute_source_hash,
    _parse_frontmatter,
    parse_enums,
)


ENUM_REGISTRY = """\
# Skill Metadata Enum Registry

## Category

```
debug      — root cause analysis, hypothesis testing
design     — architecture, spec generation
qa         — defect detection, auditing
research   — exploration, synthesis
plan       — implementation planning
execution  — running workflows
report     — generating documents
tool       — utility commands
meta       — skills about skills
```

## Capability Tags

```
adversarial-critique       parallel-agents            defect-detection
hypothesis-testing         root-cause-analysis        loop-based
```

## Input Types

```
artifact-file   git-diff   concept   task   question   topic   code-path
```

## Output Types

```
defect-registry   design-spec   report   plan   code   diagnosis
```

## Scalar Enums

| Field | Values |
|-------|--------|
| `complexity` | `simple` | `moderate` | `complex` |
| `model_tier` | `haiku` | `sonnet` | `opus` |
| `cost_profile` | `low` | `medium` | `high` |
| `maturity` | `experimental` | `beta` | `stable` | `deprecated` |
"""


def _make_skill(skills_dir: Path, name: str, frontmatter: str) -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(f"---\n{frontmatter}\n---\n\n# {name}\n\nDescription body.\n")
    return md


@pytest.fixture
def enum_path(tmp_path: Path) -> Path:
    p = tmp_path / "_shared" / "skill-metadata-enums.md"
    p.parent.mkdir(parents=True)
    p.write_text(ENUM_REGISTRY)
    return p


@pytest.fixture
def skills_dir(tmp_path: Path, enum_path: Path) -> Path:
    sd = tmp_path
    _make_skill(sd, "deep-qa", """\
name: deep-qa
description: Adversarial defect detection for artifacts
category: qa
capabilities:
  - adversarial-critique
  - defect-detection
best_for:
  - "reviewing existing artifacts for defects"
not_for:
  - "fixing defects"
input_types:
  - artifact-file
  - git-diff
output_types:
  - defect-registry
complexity: moderate
cost_profile: medium
maturity: stable
related_skills:
  - name: deep-design
    relation: alternative""")

    _make_skill(sd, "deep-design", """\
name: deep-design
description: Adversarial design stress-testing
category: design
capabilities:
  - adversarial-critique
input_types:
  - concept
output_types:
  - design-spec
complexity: complex
cost_profile: high
maturity: stable""")

    _make_skill(sd, "sprint-retro", """\
name: sprint-retro
description: Generate a sprint retrospective report
category: report
input_types:
  - task
output_types:
  - report
complexity: moderate
cost_profile: low
maturity: beta
metadata_source: inferred""")

    _make_skill(sd, "bad-cap", """\
name: bad-cap
description: A skill with invalid capability
category: qa
capabilities:
  - nonexistent-cap""")

    _make_skill(sd, "no-frontmatter", "")
    (sd / "no-frontmatter" / "SKILL.md").write_text("# Just body\n\nNo frontmatter here.\n")

    _make_skill(sd, "_shared", "name: shared\ndescription: internal")

    _make_skill(sd, "deep-qa-temporal", """\
name: deep-qa-temporal
description: Temporal shim for deep-qa
category: qa""")

    return sd


@pytest.fixture
def catalog(skills_dir: Path, enum_path: Path) -> SkillCatalog:
    return SkillCatalog.from_skills_dir(skills_dir, enum_path)


# ── parse_enums ────────────────────────────────────────────────────────

class TestParseEnums:
    def test_categories(self, enum_path: Path):
        enums = parse_enums(enum_path)
        assert "category" in enums
        assert "debug" in enums["category"]
        assert "qa" in enums["category"]
        assert len(enums["category"]) == 9

    def test_capabilities(self, enum_path: Path):
        enums = parse_enums(enum_path)
        assert "adversarial-critique" in enums["capabilities"]
        assert "defect-detection" in enums["capabilities"]

    def test_input_types(self, enum_path: Path):
        enums = parse_enums(enum_path)
        assert "artifact-file" in enums["input_types"]
        assert "git-diff" in enums["input_types"]

    def test_output_types(self, enum_path: Path):
        enums = parse_enums(enum_path)
        assert "defect-registry" in enums["output_types"]

    def test_scalar_enums(self, enum_path: Path):
        enums = parse_enums(enum_path)
        assert "complexity" in enums
        assert "simple" in enums["complexity"]
        assert "moderate" in enums["complexity"]

    def test_missing_file(self, tmp_path: Path):
        missing = tmp_path / "nope.md"
        assert not missing.exists()
        enums = parse_enums(missing) if missing.exists() else {}
        assert enums == {}


# ── _parse_frontmatter ─────────────────────────────────────────────────

class TestParseFrontmatter:
    def test_valid(self, tmp_path: Path):
        f = tmp_path / "test.md"
        f.write_text("---\nname: foo\ndescription: bar\n---\n\n# Body\n")
        fm = _parse_frontmatter(f)
        assert fm["name"] == "foo"
        assert fm["description"] == "bar"

    def test_no_frontmatter(self, tmp_path: Path):
        f = tmp_path / "test.md"
        f.write_text("# Just a heading\n\nNo frontmatter.\n")
        assert _parse_frontmatter(f) == {}

    def test_missing_file(self, tmp_path: Path):
        assert _parse_frontmatter(tmp_path / "gone.md") == {}

    def test_yaml_list(self, tmp_path: Path):
        f = tmp_path / "test.md"
        f.write_text("---\nname: foo\ncapabilities:\n  - a\n  - b\n---\n")
        fm = _parse_frontmatter(f)
        assert fm["capabilities"] == ["a", "b"]


# ── SkillCatalog.from_skills_dir ───────────────────────────────────────

class TestFromSkillsDir:
    def test_loads_valid_skills(self, catalog: SkillCatalog):
        assert catalog.show("deep-qa") is not None
        assert catalog.show("deep-design") is not None
        assert catalog.show("sprint-retro") is not None

    def test_skips_shared(self, catalog: SkillCatalog):
        assert catalog.show("_shared") is None
        assert catalog.show("shared") is None

    def test_skips_no_frontmatter(self, catalog: SkillCatalog):
        assert catalog.show("no-frontmatter") is None

    def test_includes_temporal_shim(self, catalog: SkillCatalog):
        assert catalog.show("deep-qa-temporal") is not None

    def test_validation_warnings(self, catalog: SkillCatalog):
        bad = catalog.show("bad-cap")
        assert bad is not None
        assert any("nonexistent-cap" in w for w in bad.validation_warnings)

    def test_metadata_source_preserved(self, catalog: SkillCatalog):
        retro = catalog.show("sprint-retro")
        assert retro is not None
        assert retro.metadata_source == "inferred"

    def test_related_skills_parsed(self, catalog: SkillCatalog):
        qa = catalog.show("deep-qa")
        assert qa is not None
        assert len(qa.related_skills) == 1
        assert qa.related_skills[0]["name"] == "deep-design"


# ── list_all ───────────────────────────────────────────────────────────

class TestListAll:
    def test_all(self, catalog: SkillCatalog):
        all_skills = catalog.list_all()
        assert len(all_skills) >= 4

    def test_filter_category(self, catalog: SkillCatalog):
        qa = catalog.list_all(category="qa")
        names = [s.name for s in qa]
        assert "deep-qa" in names
        assert "deep-design" not in names

    def test_filter_maturity(self, catalog: SkillCatalog):
        stable = catalog.list_all(maturity="stable")
        names = [s.name for s in stable]
        assert "deep-qa" in names
        assert "sprint-retro" not in names

    def test_filter_capability(self, catalog: SkillCatalog):
        adv = catalog.list_all(capability="adversarial-critique")
        names = [s.name for s in adv]
        assert "deep-qa" in names
        assert "deep-design" in names
        assert "sprint-retro" not in names

    def test_sorted_by_name(self, catalog: SkillCatalog):
        all_skills = catalog.list_all()
        names = [s.name for s in all_skills]
        assert names == sorted(names)


# ── search ─────────────────────────────────────────────────────────────

class TestSearch:
    def test_basic(self, catalog: SkillCatalog):
        results = catalog.search("defect")
        assert "deep-qa" in [r[0].name for r in results]

    def test_no_match(self, catalog: SkillCatalog):
        results = catalog.search("zzz_nonexistent_zzz")
        assert len(results) == 0

    def test_scores_descending(self, catalog: SkillCatalog):
        results = catalog.search("adversarial critique")
        scores = [r[1] for r in results]
        assert scores == sorted(scores, reverse=True)


# ── match ──────────────────────────────────────────────────────────────

class TestMatch:
    def test_qa_intent(self, catalog: SkillCatalog):
        results = catalog.match("audit code quality and find defects")
        assert len(results) > 0
        top = results[0]
        assert top[0].name == "deep-qa"
        assert top[1] > 0

    def test_design_intent(self, catalog: SkillCatalog):
        results = catalog.match("design a new architecture")
        names = [r[0].name for r in results[:3]]
        assert "deep-design" in names

    def test_with_input_type(self, catalog: SkillCatalog):
        results = catalog.match("review", input_type="artifact-file")
        scored = {r[0].name: r[2] for r in results}
        if "deep-qa" in scored:
            assert scored["deep-qa"]["input_type"] == 1.0

    def test_with_output_type(self, catalog: SkillCatalog):
        results = catalog.match("find bugs", output_type="defect-registry")
        scored = {r[0].name: r[2] for r in results}
        if "deep-qa" in scored:
            assert scored["deep-qa"]["output_type"] == 1.0

    def test_scores_between_0_and_1(self, catalog: SkillCatalog):
        results = catalog.match("anything")
        for _, score, _ in results:
            assert 0.0 <= score <= 1.0

    def test_scores_descending(self, catalog: SkillCatalog):
        results = catalog.match("review code for defects")
        scores = [r[1] for r in results]
        assert scores == sorted(scores, reverse=True)


# ── show ───────────────────────────────────────────────────────────────

class TestShow:
    def test_found(self, catalog: SkillCatalog):
        s = catalog.show("deep-qa")
        assert s is not None
        assert s.category == "qa"
        assert "adversarial-critique" in s.capabilities

    def test_not_found(self, catalog: SkillCatalog):
        assert catalog.show("nonexistent") is None


# ── related ────────────────────────────────────────────────────────────

class TestRelated:
    def test_valid_ref(self, catalog: SkillCatalog):
        related = catalog.related("deep-qa")
        assert len(related) == 1
        assert related[0]["name"] == "deep-design"

    def test_no_related(self, catalog: SkillCatalog):
        assert catalog.related("sprint-retro") == []

    def test_nonexistent_skill(self, catalog: SkillCatalog):
        assert catalog.related("nope") == []


# ── lint ───────────────────────────────────────────────────────────────

class TestLint:
    def test_catches_unknown_capability(self, catalog: SkillCatalog):
        issues = catalog.lint()
        cap_issues = [i for i in issues if "nonexistent-cap" in i]
        assert len(cap_issues) >= 1

    def test_strict_mode(self, catalog: SkillCatalog):
        issues = catalog.lint(strict=True)
        error_issues = [i for i in issues if i.startswith("ERROR")]
        assert len(error_issues) >= 1

    def test_warns_missing_fields(self, catalog: SkillCatalog):
        issues = catalog.lint()
        missing = [i for i in issues if "missing recommended" in i]
        assert len(missing) >= 1


# ── stats ──────────────────────────────────────────────────────────────

class TestStats:
    def test_total(self, catalog: SkillCatalog):
        s = catalog.stats()
        assert s["total"] >= 4

    def test_with_metadata(self, catalog: SkillCatalog):
        s = catalog.stats()
        assert s["with_metadata"] >= 3

    def test_by_category(self, catalog: SkillCatalog):
        s = catalog.stats()
        assert "qa" in s["by_category"]
        assert "design" in s["by_category"]

    def test_by_maturity(self, catalog: SkillCatalog):
        s = catalog.stats()
        assert "stable" in s["by_maturity"]


# ── save / from_cache round-trip ───────────────────────────────────────

class TestCacheRoundTrip:
    def test_save_and_load(self, catalog: SkillCatalog, tmp_path: Path, enum_path: Path):
        cache = tmp_path / "catalog.json"
        lock = tmp_path / "catalog.lock"
        catalog.save(cache, lock, source_hash="abc123")

        assert cache.exists()
        assert lock.exists()

        loaded = SkillCatalog.from_cache(cache, enum_path)
        qa = loaded.show("deep-qa")
        assert qa is not None
        assert qa.category == "qa"
        assert loaded.show("deep-design") is not None

    def test_lock_contents(self, catalog: SkillCatalog, tmp_path: Path):
        cache = tmp_path / "catalog.json"
        lock = tmp_path / "catalog.lock"
        catalog.save(cache, lock, source_hash="xyz")

        data = json.loads(lock.read_text())
        assert data["source_hash"] == "xyz"
        assert data["skill_count"] >= 4

    def test_cache_includes_stats(self, catalog: SkillCatalog, tmp_path: Path):
        cache = tmp_path / "catalog.json"
        lock = tmp_path / "catalog.lock"
        catalog.save(cache, lock)

        data = json.loads(cache.read_text())
        assert "stats" in data
        assert data["stats"]["total"] >= 4


# ── _compute_source_hash ──────────────────────────────────────────────

class TestSourceHash:
    def test_deterministic(self, skills_dir: Path, enum_path: Path):
        h1 = _compute_source_hash(skills_dir, enum_path)
        h2 = _compute_source_hash(skills_dir, enum_path)
        assert h1 == h2

    def test_changes_on_edit(self, skills_dir: Path, enum_path: Path):
        h1 = _compute_source_hash(skills_dir, enum_path)
        (skills_dir / "deep-qa" / "SKILL.md").write_text("---\nname: deep-qa\n---\nchanged")
        h2 = _compute_source_hash(skills_dir, enum_path)
        assert h1 != h2
