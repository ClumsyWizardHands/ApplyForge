"""LLM claim-support judge (package 4, 2026-10-10): an ADDITIONAL gate on the
free prose an employer reads, after the rule-based fact check
(validate/claims.py) has passed it. Two callers: cover letters
(pipeline/tailor.py) and drafted answers to novel form questions
(autofill/form_spec.py). pipeline/rejudge.py (`jobkit rejudge`) runs the same
gate on letters and answers stored before the judge existed.

The rule-based check sees numbers, proper nouns, employer attribution and
lists of experience markers. It cannot read meaning, so a claim with none of
those passes it: "I also led the sales development team.", "I know
trade licensing inside and out.", "Fabrikam Payments was a great place to work."
(tests/test_claims.py BLIND_SPOT). The judge (prompts/claim_judge.md, stage
"claim_judge", strong tier) reads the text against the sources it cites, the
corpus's roles, education and skill index, and the full labeled corpus, and
lists every claim about the candidate they do not state. Forward-looking intent
about the target and opinions about the target company are not claims. A
denial is one: "No." to "Have you ever been terminated?" or "...convicted of a
felony?" states a past the corpus never records, and the rules cannot see it
(tests/test_claim_denials.py). The only denial with a source is not having
worked at a company the role list lacks (prompt 1.1; 1.0 exempted every plain
denial, review 2026-10-10). A fact
that only an uncited bullet states is unsupported: the writer must cite what it
uses (an accepted false rejection: it costs a redraft).

It can only reject (gate()):
- it runs only when the rule-based check passed. A rule-based failure is
  returned unchanged, so no judge reply can rescue it, and the judge is not
  even asked;
- a "fail" verdict, a "pass" that still lists an unsupported claim, a "fail"
  that names none, and a reply that is not the JSON the prompt asks for all
  fail the text. The violations have the rule-based check's shape (kind,
  detail, sentence), so the callers' existing regeneration and fail-closed
  paths handle them unchanged: cover_rejected.md, provenance.json and a
  drafted answer's fact_check list name the judge's reasons;
- a call that fails outright (llm.call's retries exhausted) raises, like the
  writer's own call failing: nothing is written as final, the cover letter's
  job stays approved and is tried again on the next run, and a drafted
  answer stays open for the human.

Cached through llm.py like every other stage: the effective key hashes the
full rendered prompt (the text, its sources, the corpus, the target), so a
changed draft, corpus or prompt version misses it. A reply that does not
parse is forgotten, never served again."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from jobkit import llm
from jobkit.log import log
from jobkit.validate.claims import labeled_sources_for_prompt, source_registry
from jobkit.validate.provenance import ProvenanceResult

STAGE = "claim_judge"
PROMPT_NAME = "claim_judge"

# violation kinds, in the rule-based check's shape
UNSUPPORTED = "claim_judge_unsupported"
UNPARSEABLE = "claim_judge_unparseable"
KINDS = frozenset({UNSUPPORTED, UNPARSEABLE})

LETTER_KIND = "cover letter"
NO_QUESTION = "(none: this is not a form answer)"
NO_CITED = "(none: the text cites no source)"

_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def version() -> str:
    """The judge prompt's version (part of tailor.py's cover attempt key)."""
    return llm.PromptFile(PROMPT_NAME).version


def _fmt_ym(ym: object) -> str:
    if not ym:
        return "Present"
    parts = str(ym).split("-")
    try:
        return f"{_MONTHS[int(parts[1])]} {parts[0]}"
    except (IndexError, ValueError):
        return str(ym)


def roles_for_prompt(corpus: dict[str, Any]) -> str:
    """Every role: employer · title · dates, in corpus order."""
    lines = [
        f"- {r.get('company') or ''} · {r.get('title') or ''} · "
        f"{_fmt_ym(r.get('start'))} – {_fmt_ym(r.get('end'))}"
        for r in corpus.get("roles") or []
        if isinstance(r, dict)
    ]
    return "\n".join(lines) or "(none)"


def education_for_prompt(corpus: dict[str, Any]) -> str:
    edu = [k for k in source_registry(corpus) if k.startswith("edu_")]
    return labeled_sources_for_prompt(corpus, edu) or "(none)"


def skills_for_prompt(corpus: dict[str, Any]) -> str:
    """The skill index with each key's note ("hands-on (confirmed …)")."""
    index = corpus.get("skill_index") or {}
    if isinstance(index, dict):
        lines = [f"- {k}: {v}" if v else f"- {k}" for k, v in index.items()]
    else:
        lines = [f"- {k}" for k in index]
    return "\n".join(lines) or "(none)"


def render_prompt(
    text: str,
    cited_ids: Iterable[object],
    corpus: dict[str, Any],
    *,
    target_company: str,
    job_title: str = "",
    artifact_kind: str = LETTER_KIND,
    question: str = "",
) -> tuple[llm.PromptFile, str]:
    """The judge prompt for one text. The text is rendered last, so nothing
    it contains can stand in for another placeholder."""
    pf = llm.PromptFile(PROMPT_NAME)
    ids = list(dict.fromkeys(str(i).strip() for i in cited_ids if str(i).strip()))
    prompt = pf.render(
        artifact_kind=artifact_kind,
        company=target_company.strip() or "(unnamed company)",
        title=job_title.strip() or "(untitled)",
        question=question.strip() or NO_QUESTION,
        cited=labeled_sources_for_prompt(corpus, ids) or NO_CITED,
        roles=roles_for_prompt(corpus),
        education=education_for_prompt(corpus),
        skills=skills_for_prompt(corpus),
        corpus=labeled_sources_for_prompt(corpus, None) or "(none)",
        figures=figures_rule(),
        text=text.strip(),
    )
    return pf, prompt


def _unparseable(detail: str) -> dict[str, str]:
    return {
        "kind": UNPARSEABLE,
        "detail": f"the claim judge's reply is not a verdict ({detail[:200]}); failed closed",
        "sentence": "",
    }


def parse_verdict(data: object) -> list[dict[str, str]]:
    """The violations one judge reply amounts to. [] only for a well-formed
    "pass" that lists nothing unsupported; everything else fails the text."""
    if not isinstance(data, dict):
        return [_unparseable("not a JSON object")]
    verdict = data.get("verdict")
    items = data.get("unsupported")
    if not isinstance(verdict, str) or verdict.strip().lower() not in ("pass", "fail"):
        return [_unparseable(f"verdict {str(verdict)[:40]!r} is neither pass nor fail")]
    if not isinstance(items, list):
        return [_unparseable("'unsupported' is missing or not a list")]
    out: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            sentence = str(item.get("sentence") or "").strip()
            claim = str(item.get("claim") or "").strip()
            why = str(item.get("why") or "").strip()
        else:
            sentence, claim, why = "", str(item).strip(), ""
        # any entry fails the text, a "pass" that lists one included
        detail = claim or sentence or "an unsupported claim (no detail given)"
        out.append(
            {
                "kind": UNSUPPORTED,
                "detail": (f"{detail} — {why}" if why else detail)[:400],
                "sentence": sentence[:160],
            }
        )
    if verdict.strip().lower() == "fail" and not out:
        out.append(
            {
                "kind": UNSUPPORTED,
                "detail": "the claim judge failed the text without naming a claim",
                "sentence": "",
            }
        )
    return out


def judge(
    conn: sqlite3.Connection,
    text: str,
    cited_ids: Iterable[object],
    corpus: dict[str, Any],
    *,
    target_company: str,
    job_title: str = "",
    artifact_kind: str = LETTER_KIND,
    question: str = "",
) -> list[dict[str, str]]:
    """Ask the judge about one text. Returns its violations ([] = pass). A
    reply that does not parse is a violation and is dropped from the cache; a
    call that fails outright raises (see the module docstring)."""
    pf, prompt = render_prompt(
        text,
        cited_ids,
        corpus,
        target_company=target_company,
        job_title=job_title,
        artifact_kind=artifact_kind,
        question=question,
    )
    key = f"{STAGE}:{pf.version}"  # namespace; llm.py hashes it with the full prompt
    try:
        data: object = llm.call_json(conn, STAGE, prompt, cache_key=key)
    except ValueError as e:  # json.JSONDecodeError included: no JSON object at all
        violations = [_unparseable(str(e))]
    else:
        violations = parse_verdict(data)
    if any(v["kind"] == UNPARSEABLE for v in violations):
        llm.forget(conn, STAGE, prompt, key)  # a bad reply is never served again
    log(
        "claim_judge.verdict",
        artifact=artifact_kind,
        target=target_company,
        ok=not violations,
        violations=violations[:12],
    )
    return violations


def gate(
    conn: sqlite3.Connection,
    rules: ProvenanceResult,
    text: str,
    cited_ids: Iterable[object],
    corpus: dict[str, Any],
    *,
    target_company: str,
    job_title: str = "",
    artifact_kind: str = LETTER_KIND,
    question: str = "",
) -> ProvenanceResult:
    """The rule-based result `rules`, failed by the judge when it finds an
    unsupported claim. The judge runs only when `rules` passed: a rule-based
    failure comes back unchanged, so the judge can never override one."""
    if not rules.ok:
        return rules
    violations = judge(
        conn,
        text,
        cited_ids,
        corpus,
        target_company=target_company,
        job_title=job_title,
        artifact_kind=artifact_kind,
        question=question,
    )
    if not violations:
        return rules
    return ProvenanceResult(ok=False, violations=[*rules.violations, *violations])


FIGURES_CONFIDENT = (
    "The candidate chose to state approximate figures confidently "
    "(estimate_style: confident). A figure whose source is approximate (a '~', "
    "'about', or metric_confidence 'estimate') MAY be written without the "
    "approximation mark: do NOT flag a dropped '~' or 'about' on its own. DO "
    "flag a changed value or unit, added precision (~19% written as 19.4%), a "
    "range collapsed to one end (6-10 written as 10), or a dropped 'up to' or "
    "'+'."
)
FIGURES_HEDGED = (
    "Approximate figures must stay approximate (estimate_style: hedged): flag "
    "an estimate-confidence figure written as a precise result, as well as any "
    "changed value, unit, precision or range."
)


def figures_rule() -> str:
    """How the judge treats approximation marks: the same estimate_style the
    rule-based check applies (config/banned_phrases.yaml drift.estimate_style),
    so the two gates never disagree about a dropped '~'."""
    from jobkit.validate.style import CONFIDENT, configured_estimate_style

    return FIGURES_CONFIDENT if configured_estimate_style() == CONFIDENT else FIGURES_HEDGED
