"""Skill capability discovery catalog.

Scans SKILL.md frontmatter across all skills, validates against the canonical
enum registry, and provides query/match APIs for CLI and Aimee integration.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class SkillMetadata:
    name: str
    description: str
    category: str | None = None
    capabilities: list[str] = field(default_factory=list)
    best_for: list[str] = field(default_factory=list)
    not_for: list[str] = field(default_factory=list)
    input_types: list[str] = field(default_factory=list)
    output_types: list[str] = field(default_factory=list)
    output_signals: list[str] = field(default_factory=list)
    complexity: str | None = None
    model_tier: str | None = None
    cost_profile: str | None = None
    execution: dict[str, str] = field(default_factory=dict)
    related_skills: list[dict[str, str]] = field(default_factory=list)
    maturity: str | None = None
    user_invocable: bool = False
    has_temporal: bool = False
    skill_md_path: str = ""
    metadata_source: str | None = None
    validation_warnings: list[str] = field(default_factory=list)


class CatalogStaleError(Exception):
    pass


_WEIGHTS = {
    "capability": 0.45,
    "best_for": 0.25,
    "category": 0.15,
    "input_type": 0.10,
    "output_type": 0.05,
}


def parse_enums(enums_path: Path) -> dict[str, set[str]]:
    """Parse the enum registry markdown into {section: set_of_values}."""
    text = enums_path.read_text()
    enums: dict[str, set[str]] = {}

    section_map = {
        "## Category": "category",
        "## Capability Tags": "capabilities",
        "## Input Types": "input_types",
        "## Output Types": "output_types",
    }

    current: str | None = None
    in_code = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in section_map:
            current = section_map[stripped]
            enums.setdefault(current, set())
            continue
        if stripped == "## Scalar Enums":
            current = "_scalars"
            continue
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if current and in_code and current != "_scalars":
            if current == "category":
                # "debug      — root cause analysis..." → take only first token
                first = stripped.split()[0] if stripped else ""
                if first and first[0].isalpha() and "—" not in first:
                    enums[current].add(first)
            else:
                for token in stripped.split():
                    clean = token.strip("—,")
                    if clean and "—" not in clean and clean[0].isalpha():
                        enums[current].add(clean)

    # Parse scalar enums from the markdown table rows
    for row_match in re.finditer(r"^\|\s*`(\w+)`\s*\|(.+)\|", text, re.MULTILINE):
        field_name = row_match.group(1)
        raw_vals = row_match.group(2)
        vals: set[str] = set()
        for m in re.finditer(r"`([^`]+)`", raw_vals):
            val = m.group(1).strip()
            if val and not val.startswith("<") and not val.startswith("("):
                vals.add(val)
        if vals:
            enums[field_name] = vals

    return enums


def _parse_frontmatter(path: Path) -> dict[str, Any]:
    """Extract YAML frontmatter from a SKILL.md file."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = -1
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = i
            break
    if end < 0:
        return {}
    import yaml
    try:
        return yaml.safe_load("\n".join(lines[1:end])) or {}
    except Exception:
        return {}


def _compute_source_hash(skills_dir: Path, enums_path: Path) -> str:
    """SHA-256 of sorted concatenation of all SKILL.md + enum registry content."""
    parts: list[str] = []
    if enums_path.exists():
        parts.append(enums_path.read_text())
    for md in sorted(skills_dir.rglob("SKILL.md")):
        try:
            parts.append(md.read_text())
        except OSError:
            continue
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


class SkillCatalog:
    """In-memory skill catalog built from SKILL.md frontmatter."""

    def __init__(self, skills: dict[str, SkillMetadata], enums: dict[str, set[str]]):
        self._skills = skills
        self._enums = enums
        self._indexes = self._build_indexes()

    def _build_indexes(self) -> dict[str, dict[str, list[str]]]:
        by_category: dict[str, list[str]] = {}
        by_capability: dict[str, list[str]] = {}
        by_input: dict[str, list[str]] = {}
        by_output: dict[str, list[str]] = {}
        for name, s in self._skills.items():
            if s.category:
                by_category.setdefault(s.category, []).append(name)
            for c in s.capabilities:
                by_capability.setdefault(c, []).append(name)
            for t in s.input_types:
                by_input.setdefault(t, []).append(name)
            for t in s.output_types:
                by_output.setdefault(t, []).append(name)
        return {
            "by_category": by_category,
            "by_capability": by_capability,
            "by_input_type": by_input,
            "by_output_type": by_output,
        }

    @classmethod
    def from_skills_dir(cls, skills_dir: Path, enums_path: Path) -> SkillCatalog:
        enums = parse_enums(enums_path) if enums_path.exists() else {}
        skills: dict[str, SkillMetadata] = {}
        for skill_md in sorted(skills_dir.rglob("SKILL.md")):
            if skill_md.parent.name == "_shared":
                continue
            fm = _parse_frontmatter(skill_md)
            if not fm:
                continue
            name = fm.get("name", skill_md.parent.name)
            warnings: list[str] = []

            category = fm.get("category")
            if category and "category" in enums and category not in enums["category"]:
                warnings.append(f"unknown category: {category!r}")

            caps = fm.get("capabilities", []) or []
            if isinstance(caps, str):
                caps = [caps]
            for c in caps:
                if "capabilities" in enums and c not in enums["capabilities"]:
                    warnings.append(f"unknown capability: {c!r}")

            in_types = fm.get("input_types", []) or []
            if isinstance(in_types, str):
                in_types = [in_types]
            for t in in_types:
                if "input_types" in enums and t not in enums["input_types"]:
                    warnings.append(f"unknown input_type: {t!r}")

            out_types = fm.get("output_types", []) or []
            if isinstance(out_types, str):
                out_types = [out_types]
            for t in out_types:
                if "output_types" in enums and t not in enums["output_types"]:
                    warnings.append(f"unknown output_type: {t!r}")

            exec_block = fm.get("execution", {}) or {}
            has_temporal = bool(exec_block.get("temporal_skill"))
            if not has_temporal:
                temporal_dir = skills_dir / f"{name}-temporal"
                has_temporal = temporal_dir.is_dir()

            related = fm.get("related_skills", []) or []

            skills[name] = SkillMetadata(
                name=name,
                description=fm.get("description", ""),
                category=category,
                capabilities=caps,
                best_for=fm.get("best_for", []) or [],
                not_for=fm.get("not_for", []) or [],
                input_types=in_types,
                output_types=out_types,
                output_signals=fm.get("output_signals", []) or [],
                complexity=fm.get("complexity"),
                model_tier=fm.get("model_tier"),
                cost_profile=fm.get("cost_profile"),
                execution=exec_block,
                related_skills=related,
                maturity=fm.get("maturity"),
                user_invocable=bool(fm.get("user_invocable", False)),
                has_temporal=has_temporal,
                skill_md_path=str(skill_md),
                metadata_source=fm.get("metadata_source"),
                validation_warnings=warnings,
            )

        return cls(skills, enums)

    @classmethod
    def from_cache(cls, cache_path: Path, enums_path: Path) -> SkillCatalog:
        if not cache_path.exists():
            raise CatalogStaleError("catalog.json missing")
        data = json.loads(cache_path.read_text())
        enums = parse_enums(enums_path) if enums_path.exists() else {}
        skills: dict[str, SkillMetadata] = {}
        for name, raw in data.get("skills", {}).items():
            raw.pop("validation_warnings", None)
            skills[name] = SkillMetadata(
                name=raw.get("name", name),
                description=raw.get("description", ""),
                category=raw.get("category"),
                capabilities=raw.get("capabilities", []),
                best_for=raw.get("best_for", []),
                not_for=raw.get("not_for", []),
                input_types=raw.get("input_types", []),
                output_types=raw.get("output_types", []),
                output_signals=raw.get("output_signals", []),
                complexity=raw.get("complexity"),
                model_tier=raw.get("model_tier"),
                cost_profile=raw.get("cost_profile"),
                execution=raw.get("execution", {}),
                related_skills=raw.get("related_skills", []),
                maturity=raw.get("maturity"),
                user_invocable=raw.get("user_invocable", False),
                has_temporal=raw.get("has_temporal", False),
                skill_md_path=raw.get("skill_md_path", ""),
                metadata_source=raw.get("metadata_source"),
            )
        return cls(skills, enums)

    def save(self, cache_path: Path, lock_path: Path,
             source_hash: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        data = {
            "version": 1,
            "built_at": now,
            "source_hash": source_hash,
            "skills": {n: asdict(s) for n, s in self._skills.items()},
            "indexes": self._indexes,
            "stats": self.stats(),
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data, indent=2))
        lock_data = {
            "built_at": now,
            "source_hash": source_hash,
            "skill_count": len(self._skills),
        }
        lock_path.write_text(json.dumps(lock_data, indent=2))

    def list_all(
        self,
        category: str | None = None,
        maturity: str | None = None,
        capability: str | None = None,
    ) -> list[SkillMetadata]:
        results = list(self._skills.values())
        if category:
            results = [s for s in results if s.category == category]
        if maturity:
            results = [s for s in results if s.maturity == maturity]
        if capability:
            results = [s for s in results if capability in s.capabilities]
        return sorted(results, key=lambda s: s.name)

    def search(self, query: str) -> list[tuple[SkillMetadata, float]]:
        tokens = query.lower().split()
        scored: list[tuple[SkillMetadata, float]] = []
        for s in self._skills.values():
            haystack = " ".join([
                s.name, s.description,
                s.category or "",
                " ".join(s.capabilities),
                " ".join(s.best_for),
            ]).lower()
            hits = sum(1 for t in tokens if t in haystack)
            if hits:
                scored.append((s, hits / len(tokens)))
        return sorted(scored, key=lambda x: -x[1])

    def show(self, name: str) -> SkillMetadata | None:
        return self._skills.get(name)

    def match(
        self,
        intent: str,
        input_type: str | None = None,
        output_type: str | None = None,
    ) -> list[tuple[SkillMetadata, float, dict[str, float]]]:
        intent_lower = intent.lower()
        intent_tokens = set(intent_lower.split())
        cap_enums = self._enums.get("capabilities", set())
        intent_tags = intent_tokens & cap_enums
        if not intent_tags:
            for tag in cap_enums:
                parts = tag.split("-")
                if any(p in intent_tokens for p in parts):
                    intent_tags.add(tag)

        results: list[tuple[SkillMetadata, float, dict[str, float]]] = []
        for s in self._skills.values():
            signals: dict[str, float] = {}

            if intent_tags and s.capabilities:
                overlap = len(intent_tags & set(s.capabilities))
                signals["capability"] = overlap / len(intent_tags)
            else:
                bf_haystack = " ".join(s.best_for).lower()
                cap_haystack = " ".join(s.capabilities).lower()
                combined = bf_haystack + " " + cap_haystack
                cap_hits = sum(1 for t in intent_tokens if t in combined)
                signals["capability"] = min(cap_hits / max(len(intent_tokens), 1), 1.0)

            bf_text = " ".join(s.best_for).lower()
            if bf_text:
                bf_hits = sum(1 for t in intent_tokens if t in bf_text)
                signals["best_for"] = bf_hits / len(intent_tokens) if intent_tokens else 0
            else:
                signals["best_for"] = 0

            cat_tokens = {"debug", "design", "qa", "research", "plan",
                          "execution", "report", "tool", "meta"}
            inferred_cat = intent_tokens & cat_tokens
            if not inferred_cat:
                cat_map = {
                    "review": "qa", "audit": "qa", "defect": "qa", "bug": "debug",
                    "fix": "debug", "diagnose": "debug", "spec": "design",
                    "architect": "design", "explore": "research", "find": "research",
                    "implement": "execution", "build": "execution", "run": "execution",
                }
                for t in intent_tokens:
                    if t in cat_map:
                        inferred_cat.add(cat_map[t])
            signals["category"] = 1.0 if s.category and s.category in inferred_cat else 0.0

            if input_type:
                signals["input_type"] = 1.0 if input_type in s.input_types else 0.0
            else:
                signals["input_type"] = 0.0

            if output_type:
                signals["output_type"] = 1.0 if output_type in s.output_types else 0.0
            else:
                signals["output_type"] = 0.0

            score = sum(
                _WEIGHTS[k] * signals.get(k, 0) for k in _WEIGHTS
            )
            score = max(0.0, min(1.0, score))

            if score > 0:
                results.append((s, score, signals))

        return sorted(results, key=lambda x: -x[1])

    def related(self, name: str) -> list[dict[str, str]]:
        s = self._skills.get(name)
        if not s:
            return []
        valid = []
        for r in s.related_skills:
            ref = r.get("name", "")
            if ref in self._skills:
                valid.append(r)
        return valid

    def lint(self, strict: bool = False) -> list[str]:
        issues: list[str] = []
        recommended = ["category", "capabilities", "input_types", "output_types", "complexity"]
        for name, s in sorted(self._skills.items()):
            for w in s.validation_warnings:
                issues.append(f"{'ERROR' if strict else 'WARN'} {name}: {w}")
            missing = [f for f in recommended
                       if not getattr(s, f)]
            if missing:
                issues.append(f"WARN {name}: missing recommended fields: {', '.join(missing)}")
            for r in s.related_skills:
                ref = r.get("name", "")
                if ref and ref not in self._skills:
                    issues.append(f"WARN {name}: related skill {ref!r} not found in catalog")
        return issues

    def stats(self) -> dict[str, Any]:
        by_cat: dict[str, int] = {}
        by_mat: dict[str, int] = {}
        with_meta = 0
        warn_count = 0
        for s in self._skills.values():
            if s.category:
                by_cat[s.category] = by_cat.get(s.category, 0) + 1
            mat = s.maturity or "unset"
            by_mat[mat] = by_mat.get(mat, 0) + 1
            if s.category or s.capabilities:
                with_meta += 1
            warn_count += len(s.validation_warnings)
        return {
            "total": len(self._skills),
            "with_metadata": with_meta,
            "by_category": by_cat,
            "by_maturity": by_mat,
            "validation_warnings": warn_count,
        }


def default_skills_dir() -> Path:
    from sagaflow.prompts import claude_skills_dir
    return claude_skills_dir()


def default_enums_path() -> Path:
    return default_skills_dir() / "_shared" / "skill-metadata-enums.md"


def default_cache_path() -> Path:
    from sagaflow.paths import Paths
    return Paths.from_env().root / "catalog.json"


def default_lock_path() -> Path:
    from sagaflow.paths import Paths
    return Paths.from_env().root / "catalog.lock"


def build_catalog(force: bool = False) -> SkillCatalog:
    """Build or load the catalog, rebuilding if stale."""
    cache = default_cache_path()
    lock = default_lock_path()
    enums = default_enums_path()
    skills_dir = default_skills_dir()

    if not force and cache.exists() and lock.exists():
        try:
            return SkillCatalog.from_cache(cache, enums)
        except Exception:
            pass

    catalog = SkillCatalog.from_skills_dir(skills_dir, enums)
    source_hash = _compute_source_hash(skills_dir, enums)
    catalog.save(cache, lock, source_hash)
    return catalog
