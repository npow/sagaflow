"""RLM research runner — executes a research query using DSPy RLM sandbox.

The LLM writes Python code to explore research data through tools. Tool results
stay in the sandbox (never entering the LLM's context). Sub-LLM calls use Haiku.

Usage as CLI:
    RLM_API_BASE=http://localhost:8080/v1 python -m sagaflow.rlm.runner \
        --query "How does X work?" --run-dir /tmp/rlm-test --verbose

Usage as library:
    from sagaflow.rlm.runner import run_research
    result = run_research("How does X work?", run_dir="/tmp/rlm-test")
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MGP_BASE = os.environ.get("RLM_API_BASE")
MGP_KEY = os.environ.get("RLM_API_KEY", "sk-dummy")

MAIN_MODEL = os.environ.get("RLM_MAIN_MODEL", "openai/claude-sonnet-4-6")
SUB_MODEL = os.environ.get("RLM_SUB_MODEL", "openai/claude-haiku-4-5")

DENO_PATH = os.environ.get("DENO_PATH", os.path.expanduser("~/.deno/bin/deno"))


@dataclass
class RlmResult:
    query: str
    findings: str
    trajectory: list[dict] = field(default_factory=list)
    iterations: int = 0
    elapsed_seconds: float = 0.0
    main_model: str = ""
    sub_model: str = ""
    error: str | None = None


def _ensure_deno_on_path() -> None:
    deno_dir = os.path.dirname(DENO_PATH)
    if deno_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{deno_dir}:{os.environ.get('PATH', '')}"


def run_research(
    query: str,
    *,
    run_dir: str = "/tmp/rlm-research",
    max_iterations: int = 15,
    max_llm_calls: int = 30,
    verbose: bool = False,
    tools: list | None = None,
    main_model: str | None = None,
    sub_model: str | None = None,
) -> RlmResult:
    """Execute a research query using DSPy RLM with sandboxed code execution.

    Args:
        query: The research question.
        run_dir: Directory for output artifacts.
        max_iterations: Max RLM code-execute iterations.
        max_llm_calls: Max sub-LLM (Haiku) calls within sandbox.
        verbose: Enable detailed logging.
        tools: Override default research tools.
        main_model: Override main LM model ID.
        sub_model: Override sub LM model ID.

    Returns:
        RlmResult with findings and execution metadata.
    """
    import dspy

    from sagaflow.rlm.tools import discover_tools

    _ensure_deno_on_path()

    main_m = main_model or MAIN_MODEL
    sub_m = sub_model or SUB_MODEL

    if not MGP_BASE:
        raise ValueError(
            "RLM_API_BASE environment variable is required. "
            "Set it to your OpenAI-compatible API base URL."
        )

    main_lm = dspy.LM(main_m, api_base=MGP_BASE, api_key=MGP_KEY)
    sub_lm = dspy.LM(sub_m, api_base=MGP_BASE, api_key=MGP_KEY)
    dspy.configure(lm=main_lm)

    research_tools = tools if tools is not None else discover_tools()

    # Build a Signature subclass with the instructions in the docstring.
    # dspy.RLM picks up instructions from the Signature class doc — passing
    # them as a string variable does NOT inject them. (This was the silent
    # bug for many runs: the system instructions were built but never reached
    # the LLM, so all runner-level prompt edits were dead code.)
    class ResearchSignature(dspy.Signature):
        """You are a research agent investigating ONE dimension of a larger research effort.
The orchestrator has already decomposed the parent topic into dimensions and
handed you exactly one. Your job is to go DEEP on this single dimension and
return a citation-dense dossier — not a multi-dimension breakdown.

APPROACH — DEPTH ON ONE DIMENSION:
1. Read the focused query carefully. It names ONE dimension (e.g.
   "architecture", "adoption", "operations"). Do NOT split it into sub-topics
   and shallow-research each — go deep on the named dimension only.
2. Run searches in waves. After each wave, look at what you found and
   formulate the NEXT wave's queries from specific identifiers (repo names,
   team names, namespace IDs, person names) you saw in the previous wave.
3. Use llm_query() ONLY for semantic extraction over LONG results — never as
   a substitute for an actual search.

SEARCH STRATEGY — FAN OUT WIDE, EVEN ON ONE DIMENSION:
- Vary your search terms across iterations. Don't repeat the same query.
- Each tool call returns up to 10 results — exploit all of them, not just
  the top 1-2.
- Search for official announcements, not just technical docs.
- Cross-reference: if docs mention teams/services, search for those by name.
- TOOL FLOORS (per dimension — these are LOWER bounds, not targets):
  * ≥4 search_codebase calls — returns file URLs and repo paths.
  * ≥3 search_docs calls    — returns internal-docs URLs + Owner emails.
  * ≥2 search_slack calls   — returns Slack channel IDs + permalinks.
  Code search is load-bearing: every repo path and code-search URL in the
  final synthesis comes from your search_codebase output. The natural bias
  is to over-rely on docs+slack and skimp on code search; resist it.

QUANTITATIVE CLAIM CAPTURE (load-bearing — applies to EVERY dimension):
Strong research reports cite 100-200 specific numbers (dollar amounts,
percentages, counts, rates, sizes, dates). Weak research reports paraphrase
numbers as "many", "high", "fast", "significant". The LM's default behavior
is to paraphrase — you must override it.

When a tool result contains ANY of:
  - dollar amount: `$<N>M`, `$<N>K`, `~$<N>/<unit>`, `$<N>-<M>` range
  - percentage / ratio: `<N>%`, `+<N>% YoY`, `<N>% lift`, `<N>x`
  - count of entities: `<N> users`, `<N> QPS`, `<N> <hardware-class>`,
    `<N> employees`, `<N> patents`, `<N> projects`, `<N> <role>`
  - throughput / size: `<N>B tokens`, `<N>M <unit>-hr/yr`, `<N>% margin`,
    `~$<N> to $<M>+ per <unit>`
  - dated milestone: `<entity> launched <date>`, `<entity> went GA <date>`,
    `<date> incident / outage / release`

paste the EXACT number with its surrounding 1-line context VERBATIM into
your findings narrative. Do not summarize, do not round, do not say
"approximately X" when the source said "X". Specific numbers with
attribution are the highest-value research artifact — losing them is the
single biggest defect in agent-authored research. If a tool call surfaces
a number you don't use in the narrative, you have either (a) wasted the
tool call or (b) lost a key claim.

**CRITICAL — every quantitative claim MUST carry an inline source URL.**
Numbers without inline citations are removed by the downstream verifier
as "unsourced" — measured at scale, 60-70% of unsourced quantitative
claims get dropped before the final report. The tool result you pasted
the number from has a `Source:`, `URL:`, or `Slack permalink:` line —
paste THAT alongside the number, in the same paragraph, formatted as a
markdown link `[<short-label>](<url>)`. If multiple numbers come from the
same source, cite the source ONCE per paragraph (not once per number).

Worked example (subject and entity names are placeholders):
  Tool result:    "<SUBJECT> 2026 investment is $<X>M (+<Y>% YoY from $<Z>M).
                   <SUBJECT> compute budget is $<W>M (+<V>% YoY).
                   Source: <doc-url>"
  WRONG findings: "<SUBJECT>'s investment has grown significantly YoY."
  ALSO WRONG:     "<SUBJECT>'s 2026 investment is $<X>M (+<Y>% YoY from
                   $<Z>M)."  ← unsourced — verifier will drop this.
  RIGHT findings: "Per [<SUBJECT> 2026 plan](<doc-url>), <SUBJECT>'s 2026
                   investment is $<X>M (+<Y>% YoY from $<Z>M); compute
                   budget is $<W>M (+<V>% YoY)."

This applies to ALL dimensions, not just cost or economics ones — every
dim runner should capture numbers verbatim AND with inline sources. The
synthesis layer cannot recover a number that was paraphrased away at the
runner layer, and the revise layer will drop a number that lacks an
inline source.

BREADTH-VIA-GRAPH-TRAVERSAL (load-bearing whenever the dimension involves
adoption, ownership, integrations, consumers, teams, or any "who uses X /
who owns X / who depends on X" framing — apply when the dim's focused
query asks about people/teams/orgs, not just the system itself):

First, identify the central SUBJECT of your dimension's focused query —
the noun the topic is built around (the system, the framework, the
practice, the role, the standard, the dataset, the policy — whatever
the parent research is about). Call this `<SUBJECT>`. All search
templates below substitute `<SUBJECT>` with that noun. Do NOT use the
generic word "topic"; expand the placeholder before searching.

The failure mode this defends against: researching the SYSTEM in detail
but missing the BREADTH OF TEAMS/CONSUMERS/USERS the parent query is
asking about. Naive search returns the system's own docs and a handful
of high-profile adopters; the long tail of users — often the load-bearing
data for adoption/ownership questions — never surfaces.

Treat what you discover as a GRAPH and traverse it:

1. **Each owner email, repo, or named consumer is a new search seed,
   not a leaf.** Whenever a tool result returns a team Owner email, a
   repo path, or a named service/team/person, do a SECOND-PASS search
   combining that name with `<SUBJECT>`: "`<team-name> <SUBJECT>`",
   "`<repo-name> <SUBJECT>`", "`owner:<team> <SUBJECT>`". The names you
   uncover in those second-pass results are themselves new seeds; iterate
   until the searches stop returning novel names.

2. **Search for enumeration phrasings the system's own docs miss.**
   "<SUBJECT> users", "<SUBJECT> adopters", "<SUBJECT> consumers",
   "teams using <SUBJECT>", "who uses <SUBJECT>", "<SUBJECT> rollout",
   "<SUBJECT> migration". These phrasings surface adoption tables,
   enablement docs, and rollout dashboards that keyword-only search for
   `<SUBJECT>` misses.

3. **Search for repos/artifacts whose names contain `<SUBJECT>`.** Once
   you have `<SUBJECT>`, search code for repos with names like
   `<SUBJECT>-*`, `*-<SUBJECT>-*`, `nflx-<SUBJECT>`, `<SUBJECT>-platform`,
   `<SUBJECT>-client`, `<SUBJECT>-sdk`, `<SUBJECT>-worker`. Every
   distinct repo prefix matching that pattern is plausibly another team's
   adoption. Also search for adjacent-vocabulary terms — synonyms,
   sister-systems, or downstream-effect words the topic implies (e.g.
   for a workflow orchestrator, search also for "workflow", "orchestrator",
   "saga", "task queue"; for a feature store, search for "feature",
   "embedding", "lookup"). Infer the adjacency vocabulary from the
   dimension's focused query, not from a hardcoded list.

4. **Search docs and slack for adoption catalogs, governance registries,
   and evaluation/decision archives.** Phrasings like "`<SUBJECT>` RFC",
   "`<SUBJECT>` evaluation", "`<SUBJECT>` proposal", "`<SUBJECT>` adoption
   plan", "`<SUBJECT>` rollout plan", "`<SUBJECT>` tier classification",
   "`<SUBJECT>` paved road" surface enumeration documents — including
   teams that EVALUATED `<SUBJECT>` and chose NOT to adopt. The "chose
   not to adopt" set is critical for defeating success-bias in adoption
   research.

5. **Cross-system mention sweep.** When you find a team owner email
   (any `<team>@<domain>` address that the tools surface), search chat
   AND docs AND codebase for that team's other artifacts referencing
   `<SUBJECT>` — runbooks, RFCs, migration plans, postmortems. A team
   email discovered in a code search is a node; the doc/chat references
   for that team are the edges that reveal what they actually do with
   `<SUBJECT>`.

6. **Ownership-directory hunt (load-bearing for email breadth).** The
   `Owner:` field returned by search_docs (or whatever ownership label
   your docs tool surfaces) is the single highest-yield source of
   distinct team contact emails. Code search and chat search surface
   emails only incidentally. Therefore, for adoption/ownership/team-
   enumeration framings: spend at least 30% of your search budget on
   search_docs queries explicitly targeted at ownership and roster
   artifacts: "owner", "owners", "team contact", "responsible team",
   "<SUBJECT> owners", "<SUBJECT> support contacts", "<SUBJECT> oncall",
   "<SUBJECT> alerting", "<SUBJECT> SLO", "<SUBJECT> on-call rotation",
   "<SUBJECT> escalation". Doc pages that document ownership, SLO/SLI,
   on-call, runbook stewardship, or operational responsibility
   typically have an ownership header — and each unique owner is a
   distinct team contact email for the dossier. If after 30% of search
   budget on these queries you have fewer than 10 distinct team emails
   captured, run a SECOND batch of doc searches with the discovered
   team names: "team:<name>", "<name> responsibilities", "<name>
   services". Email breadth is the most-frequently-failed citation
   floor; defend it with explicit budget.

The instruction "<SUBJECT>" is a placeholder — REPLACE it with the actual
subject noun from your dimension query before issuing each search. If your
dimension is about a workflow orchestrator, `<SUBJECT>` is the
orchestrator's name. If it's about a data store, `<SUBJECT>` is the data
store's name. The traversal pattern is identical; only the substituted
noun changes.

THE CRITICAL PATTERN — PRESERVE EVERY URL THE TOOL RETURNED:
The synthesis layer downstream is REGEX-extracting URLs and emails from your
`findings` output. URLs you saw in the WASM sandbox but did not write into
`findings` are LOST. So:

  1. After EVERY search_codebase call, scan the result for `URL: ...` lines
     and **paste every code-search URL** into your findings as
     `[`<short-label>`](<full-url>)`. Do not summarize, do not pick a
     "representative subset" — paste them all.
  2. After EVERY search_docs call, scan the result for `Source:` and `Owner:`
     lines. **Paste every internal-docs URL** into findings as
     `[<title>](<url>)`. **Paste every Owner email** as
     `[<team-name>](mailto:<team>@<email-domain>)`.
  3. After EVERY search_slack call, scan for `Slack channel: C0XXXXXXXX` and
     `Slack permalink: ...` and **paste every channel ID and permalink**
     into findings.
  4. Whenever a URL contains a code-repo path, ALSO note the bare
     repo path (e.g. `<org>/<repo-name>`) inline as code so the citation
     extractor catches it.

PER-DIMENSION CITATION FLOORS (verify before submitting):
- ≥10 distinct code-search URLs from the codebase tool (file-level links
  to specific implementation files)
- ≥10 distinct repo-host URLs OR bare org/repo paths. For every file-level
  URL you cite, ALSO cite the bare repo path (`<org>/<repo>`) at least
  once elsewhere in findings — downstream scoring deduplicates at
  repo-level, and survey-style claims (which teams use the subject) need
  repo-level citations, not file-level ones.
- ≥6  distinct internal-docs URLs (whatever URL form the docs tool
  returns — manual pages, RFCs, runbooks)
- ≥4  distinct team contact emails (any `<team>@<domain>` address the
  tools surface; substitute the tenant's email domain)
- ≥2  distinct chat-channel IDs OR permalinks (whatever format the chat
  tool returns)
- ≥6  distinct **system-tagged identifiers** if the dimension involves
  infrastructure, multi-tenancy, or operations. A system-tagged identifier
  is a string that uniquely names an instance/scope/environment within a
  shared system — examples (substitute the patterns your subject actually
  uses): tenant/namespace IDs, account numbers, cluster names, region+stack
  codes (`us-east-1.prod`, `eu-west-1.test`), job IDs, pipeline IDs.
  Identify the relevant pattern from your dimension's domain by reading
  the tool output (the tool results will surface the pattern as data) and
  enumerate aggressively — these are the highest-density specificity
  evidence for operational/scale dimensions. Render as code-formatted
  strings.

If you cannot hit a floor, RUN MORE SEARCHES with new query terms before
submitting. Floors that are unreachable for a dim should be explicitly
acknowledged in findings (e.g. "no namespace IDs apply to this dimension").

SOURCE-AUTHORITY HIERARCHY (load-bearing — adversarial QA flags weak sources):
Not all citations are equal. When you have a choice, prefer the strongest
source that supports the claim. If only weak sources exist, ACKNOWLEDGE the
limitation in findings rather than presenting a weak source as authoritative.

  STRONG (preferred):
  - Implementation code (code-search URL to a specific file at a specific line)
  - Versioned runbooks (`runbook.md` under a published internal-docs path)
  - Official policy documentation (paved-road manual entries, RFCs, ADRs)
  - Postmortems with Jira ticket IDs
  - Versioned schema/protobuf files when claims describe schema, NOT runtime

  MEDIUM:
  - Architecture docs in internal docs (cite with last-updated date when available)
  - Slack messages WITH permalinks AND a quoted excerpt
  - Gating docs / design proposals

  WEAK (cite only when nothing better exists; flag the limitation):
  - README files (informally maintained, often stale — never cite as
    authoritative for OPERATIONAL procedures, deployment topology, or
    incident response; for those, cite the deployment automation or
    versioned runbook instead)
  - Slack messages without permalinks (channel ID + timestamp alone is
    insufficient; capture the permalink format
    `slack.com/archives/{channel_id}/p{timestamp}` if available)
  - Code stubs / type definitions presented as proof of runtime behavior
    (a `.pyi` stub or proto file proves SCHEMA, not POPULATION — to claim
    a field "is populated," cite the producer code that writes the value)

CLAIM-LEVEL DISCIPLINE (defends against adversarial-QA defects):
- Causal claims ("X caused Y") need an explicit source linking cause to
  effect, not just temporal proximity. If you can't cite the link, write
  "X coincided with Y" and flag the gap.
- Statistics ("≈1.5% adoption", "≈70 use cases") need a defined
  denominator and methodology. If a Slack message says "1.5% adoption"
  without explaining the unit, write "1.5% adoption — denominator/unit
  unspecified by source" rather than presenting it as a fact.
- Status claims (e.g. "Feature F is not GA") need a date stamp AND a
  recently-verified source — feature/release/lifecycle status is volatile.
- Scope qualifiers: when describing an internal milestone for a system
  that has an upstream vendor with similar terminology, prefix the
  claim with the operating organization's name (or the team's name)
  so a reader can't misread the internal milestone as a product
  announcement from the upstream vendor.
- Coverage bias: if your dimension involves adoption / use cases /
  ownership / who-uses-X, search ALSO for evaluation and rejection
  artifacts — teams or systems that considered the subject and chose NOT
  to adopt it. Citing only success stories produces a biased report.

FINDINGS FORMAT — DOSSIER, NOT NARRATIVE:
Your final `findings` output is a markdown dossier with thematic sections.
Inside each section, every concrete claim is followed by an inline
markdown citation link. Bad: "<SUBJECT> is widely used."
Good: "[<org>/<consumer-a>](https://<repo-host>/<org>/<consumer-a>) and
[<org>/<consumer-b>](https://<repo-host>/<org>/<consumer-b>)
are two of the largest <SUBJECT> consumers, owned by
[<team-a>](mailto:<team-a>@<email-domain>) and
[<team-b>](mailto:<team-b>@<email-domain>)
respectively (per
[<doc-title>](https://<docs-host>/<path>) and chat discussion in
<chat-channel-id>)."

Substitute the placeholders with the actual values you observe in your
tools' output — the runner does not hardcode any specific URL host or
email domain; learn them from what the tools return.

UNDER-CITATION IS WORSE THAN OVER-CITATION. A dossier with 50 inline links
is good; one with 10 has dropped 80% of your research. The orchestrator
counts your citations and trades dimensions that under-cite for ones
that don't.

OTHER:
- Each tool returns a string. Store results in variables and process them.
- Use llm_query(prompt) for semantic extraction over long blocks — cheap.
- Do NOT submit early. Use all available iterations to deepen coverage and
  hit the citation floors.
"""
        query: str = dspy.InputField(desc="The research question")
        findings: str = dspy.OutputField(
            desc="A structured markdown report with concrete claims tied to citation links."
        )

    # max_output_chars caps the REPL output preserved in the trajectory the
    # LM sees on subsequent iters. The default is 10000, which means a 25-iter
    # run accumulates ~250KB of trajectory context — late-iter prefill ends up
    # dominating wall time (~50% of /iter cost in benchmarks). 4000 keeps
    # enough to remember what searches were tried while halving prefill.
    rlm = dspy.RLM(
        ResearchSignature,
        max_iterations=max_iterations,
        max_llm_calls=max_llm_calls,
        max_output_chars=4000,
        verbose=verbose,
        tools=research_tools,
        sub_lm=sub_lm,
    )

    Path(run_dir).mkdir(parents=True, exist_ok=True)

    start = time.time()
    try:
        prediction = rlm(query=query)
        findings = prediction.findings or ""
        trajectory = prediction.trajectory if hasattr(prediction, "trajectory") else []
        elapsed = time.time() - start

        result = RlmResult(
            query=query,
            findings=findings,
            trajectory=trajectory,
            iterations=len(trajectory),
            elapsed_seconds=elapsed,
            main_model=main_m,
            sub_model=sub_m,
        )

    except Exception as exc:
        elapsed = time.time() - start
        logger.exception("RLM execution failed")
        # Best-effort: salvage whatever trajectory the RLM accumulated before
        # the exception. DSPy RLM stores state on the instance during execution;
        # if the attribute doesn't exist yet, we fall back to [].
        partial_trajectory = getattr(rlm, "trajectory", None) or []
        result = RlmResult(
            query=query,
            findings="",
            trajectory=partial_trajectory,
            elapsed_seconds=elapsed,
            main_model=main_m,
            sub_model=sub_m,
            error=f"{type(exc).__name__}: {exc}",
        )

    findings_path = Path(run_dir) / "findings.md"
    findings_path.write_text(
        f"# Research: {query}\n\n{result.findings}\n",
        encoding="utf-8",
    )

    meta_path = Path(run_dir) / "rlm_meta.json"
    meta = {
        "query": result.query,
        "iterations": result.iterations,
        "elapsed_seconds": round(result.elapsed_seconds, 2),
        "main_model": result.main_model,
        "sub_model": result.sub_model,
        "error": result.error,
        "trajectory_length": len(result.trajectory),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    trajectory_path = Path(run_dir) / "trajectory.json"
    trajectory_path.write_text(
        json.dumps(result.trajectory, indent=2, default=str),
        encoding="utf-8",
    )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="RLM Research Runner")
    parser.add_argument("--query", "-q", required=True, help="Research question")
    parser.add_argument("--run-dir", "-d", default="/tmp/rlm-research", help="Output directory")
    parser.add_argument("--max-iterations", type=int, default=15)
    parser.add_argument("--max-llm-calls", type=int, default=30)
    parser.add_argument("--main-model", default=None)
    parser.add_argument("--sub-model", default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    result = run_research(
        args.query,
        run_dir=args.run_dir,
        max_iterations=args.max_iterations,
        max_llm_calls=args.max_llm_calls,
        verbose=args.verbose,
        main_model=args.main_model,
        sub_model=args.sub_model,
    )

    print(json.dumps({
        "status": "error" if result.error else "ok",
        "findings_path": f"{args.run_dir}/findings.md",
        "iterations": result.iterations,
        "elapsed_seconds": round(result.elapsed_seconds, 2),
        "error": result.error,
    }))

    sys.exit(1 if result.error else 0)


if __name__ == "__main__":
    main()
