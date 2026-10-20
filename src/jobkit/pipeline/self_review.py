"""Self-review: "ask itself, then adjust" (feature request 2026-10-09).

After the system drafts an artifact a human will see (the resume bullet
selection, the cover letter, a drafted novel form answer) and before any human
sees it, the strong model reviews its own draft against the job's requirements,
the full labeled corpus, the gap report and the job's track, and answers six
questions about it (prompts/self_review.md). Its verdict is `good` or `revise`;
a `revise` comes with a change list, and with `questions_for_human`, facts
ABSENT from the corpus that would close a real gap.

What happens next is decided here, not by the model. Only one thing a review
says can change what ships: a revision of a resume selection that PASSED.

* A resume selection that passed its checks and got `revise` is rewritten
  once by the tailor stage, with the change list appended
  (prompts/self_review_revision.md, improve). Before the writer sees the list,
  each change whose `what` uses a content word that is in none of the corpus,
  the passing draft and the job title is dropped (validate/revision.py
  unsourced_change_terms; the target's name lends no word, and words of tense
  such as "was" and "before" need a source too): "Mention managing SDRs" and
  "Say I was at Fabrikam Payments before" never reach the writer. A `revise` left
  with no change is no change. The
  revision may choose, order and swap corpus bullets but never reword one:
  every bullet it renders must be its named variant's corpus text, word for
  word (tailor.py _reworded), because provenance checks only a bullet's
  numbers, proper nouns and never-claim terms (a revision that added "I also
  managed the sales development team." to b_052's text passed it; sixth
  review of the self-review). It then runs the selection's full check chain
  again (provenance, then authenticity), with an authenticity verdict no
  worse than the selection it revises. A revision that fails anything is
  discarded and the selection it revised ships (Hard Rule 3).
* A cover letter or drafted answer that passed is reviewed and reported only
  (review_only). Its changes are never applied, so no sentence the fact check
  and the critic have not passed can ship. A prose revision used to be allowed,
  gated by comparing it word by word with the passing draft. That could not be
  made safe simply: a sentence moved word for word re-attributed a team to
  another employer, and a sentence built only from its cited bullets' words
  ("I ran the merchant team for ~18 months.") stated what no bullet says (fifth
  review of the self-review, 2026-10-09; Hard Rule 8).
* A draft that FAILED its checks is reviewed with its violations, and the
  review is reported only (review_only). Its regeneration carries the check
  feedback it carried at aca26cc and nothing from the review. A change list
  used to ride on the regeneration; it took its words from the failed draft,
  so a change could endorse a fabricated sentence the fact check could not
  see. A failing artifact is never shipped, and the human gets it, exactly as
  before the self-review existed.
* `self_review.max_revision_rounds` (config/filters.yaml, default 1) caps the
  reviews per artifact per run at max(1, knob) and the resume revisions at the
  knob; 0 means review and report only.

Cost guard: every review goes through llm.call_json with a cache key namespaced
by artifact, JD hash, corpus/profile hash and prompt versions (llm.py hashes the
full rendered prompt into it too), so the same draft is never reviewed twice.
Every loop is bounded by the knob.

Each review is recorded: an events row (kind 'self_review': verdict and
outcome), one 'question_open' event per new question (detail JSON: key,
question, why, fact_needed, job_id, artifact), and out/<job folder>/self_review.md
(rendered from self_review.json beside it, so tailoring and form capture can
each add their artifacts)."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobkit import db as q
from jobkit import llm
from jobkit.autofill.answer_bank import human_answer_barred
from jobkit.log import log
from jobkit.validate.claims import coerce_ids, labeled_sources_for_prompt, never_claim_for_prompt
from jobkit.validate.claims import source_registry as _source_registry
from jobkit.validate.revision import change_vocabulary, unsourced_change_terms

STAGE = "self_review"
PROMPT = "self_review"
REVISION_PROMPT = "self_review_revision"
VERDICTS = ("good", "revise")
REPORT_MD = "self_review.md"
REPORT_JSON = "self_review.json"

# Round outcomes. "accepted"/"discarded" are the two a revision can end in;
# only a passing resume selection is ever revised (improve).
NO_CHANGE = "no_change"  # verdict good (or revise with nothing to change)
ACCEPTED = "accepted"  # the revision passed every check and replaced the draft
DISCARDED = "discarded"  # the revision failed a check; the draft it revised stands
# revise, but nothing was revised: the knob is 0, or the review is reported
# only (a cover letter, a drafted answer, or any draft that failed its checks)
NOT_REVISED = "not_revised"
ERROR = "error"  # the review call failed; the validated draft stands

# why a review_only round with changes revised nothing
REPORT_ONLY_PROSE = (
    "Reported only: a cover letter or drafted answer is never revised, because no "
    "check can confirm that rewritten prose states only what the corpus says"
)
REPORT_ONLY_FAILED = (
    "Reported only: the draft failed its checks, and its regeneration carries the "
    "check feedback alone, never a review's changes"
)

_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewContext:
    """Everything a review of one job's artifacts sees besides the draft."""

    job_id: int
    company: str
    title: str
    track: str
    requirements: dict[str, list[str]]  # must_have / nice_to_have / implied
    gaps: list[str]  # the fit scorer's gap report
    corpus: dict[str, Any]
    cache_ns: str  # "<jd hash>:<corpus or profile hash>" — the cost-guard namespace


def _loads_list(raw: object) -> list[str]:
    try:
        val = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


def context_for_job(
    conn: sqlite3.Connection, job_id: int, corpus: dict[str, Any], corpus_key: str, track: str
) -> ReviewContext | None:
    """The review context for one job, read from the DB (requirements, gaps,
    JD hash). None when the job does not exist."""
    row = conn.execute(q.SELF_REVIEW_JOB_CONTEXT, {"job_id": job_id}).fetchone()
    if row is None:
        return None
    return ReviewContext(
        job_id=job_id,
        company=str(row["company_name"] or ""),
        title=str(row["title"] or ""),
        track=track,
        requirements={k: _loads_list(row[k]) for k in ("must_have", "nice_to_have", "implied")},
        gaps=_loads_list(row["gaps"]),
        corpus=corpus,
        cache_ns=f"{row['jd_hash']}:{corpus_key}",
    )


def text_hash(text: str) -> str:
    """Short content hash for a cache namespace (e.g. the drafting profile)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _fmt_ym(ym: object) -> str:
    if not ym:
        return "Present"
    m = re.match(r"(\d{4})-(\d{1,2})", str(ym))
    if not m or not 1 <= int(m.group(2)) <= 12:
        return str(ym)
    return f"{_MONTHS[int(m.group(2))]} {m.group(1)}"


def corpus_for_review(corpus: dict[str, Any]) -> str:
    """The FULL labeled corpus: roles with dates, every bullet with its id,
    employer · title · dates, metric and metric_confidence, education, the
    positioning variants, the skill index and the never-claim lists."""
    roles = [
        f"- {r.get('id')} | {r.get('company', '')} · {r.get('title', '')} · "
        f"{_fmt_ym(r.get('start'))} – {_fmt_ym(r.get('end'))}"
        for r in corpus.get("roles") or []
    ]
    positioning = [
        f"- {p.get('id')} ({p.get('archetype', '')}): {str(p.get('text') or '').strip()}"
        for p in corpus.get("positioning") or []
    ]
    index = corpus.get("skill_index") or {}
    skills = (
        [f"- {k}: {v}" for k, v in index.items()]
        if isinstance(index, dict)
        else [f"- {k}" for k in index]
    )
    return (
        "ROLES (reverse-chronological):\n"
        + ("\n".join(roles) or "(none)")
        + "\n\nBULLETS AND EDUCATION (citable ids):\n"
        + (labeled_sources_for_prompt(corpus, None, include_education=True) or "(none)")
        + "\n\nPOSITIONING VARIANTS:\n"
        + ("\n".join(positioning) or "(none)")
        + "\n\nSKILL INDEX (skill: evidence) — a skills line may use only these keys:\n"
        + ("\n".join(skills) or "(none)")
        + f"\n\nNEVER CLAIM: {never_claim_for_prompt(corpus)}"
    )


def _violations_text(violations: object) -> str:
    if not violations:
        return "(none — the draft passed every check)"
    if isinstance(violations, list):
        lines = []
        for v in violations[:20]:
            if isinstance(v, dict):
                where = f' — in: "{v["sentence"]}"' if v.get("sentence") else ""
                who = f" [{v['bullet_id']}]" if v.get("bullet_id") else ""
                lines.append(f"- {v.get('kind', 'violation')}{who}: {v.get('detail', '')}{where}")
            else:
                lines.append(f"- {v}")
        return "\n".join(lines)
    return json.dumps(violations, indent=1, default=str)[:4000]


# ---------------------------------------------------------------------------
# the review call
# ---------------------------------------------------------------------------


@dataclass
class Review:
    verdict: str  # good | revise | error
    changes: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, str]] = field(default_factory=list)
    self_check: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    # changes dropped before the writer saw them (screen_changes, a passing
    # resume's revision only): each with "unsourced", the words the corpus, the
    # passing draft and the job's name lack
    dropped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def wants_revision(self) -> bool:
        return self.verdict == "revise" and bool(self.changes)


def _key(raw: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(raw or "").lower()).strip("_")[:60]


def normalize(raw: dict[str, Any], corpus: dict[str, Any]) -> Review:
    """Model output → Review. A reply whose verdict is not "good" or "revise"
    is not a review: it raises, and review() turns that into verdict "error"
    (the checked draft stands, nothing is revised or asked). Unknown bullet ids
    are dropped from changes (a change can only draw on the corpus); a question
    with no key or question text, or one asking for demographic data or
    compensation history (Hard Rules 4, 5), is dropped; keys are snake_case
    and unique."""
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"self-review reply has no valid verdict: {raw.get('verdict')!r}")
    known = set(_source_registry(corpus))
    changes: list[dict[str, Any]] = []
    for c in raw.get("changes") or []:
        if not isinstance(c, dict) or not str(c.get("what") or "").strip():
            continue
        changes.append(
            {
                "what": str(c["what"]).strip()[:600],
                "why": str(c.get("why") or "").strip()[:600],
                "bullet_ids": [i for i in coerce_ids(c.get("bullet_ids")) if i in known],
            }
        )
    questions: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw.get("questions_for_human") or []:
        if not isinstance(item, dict):
            continue
        key = _key(item.get("key"))
        question = str(item.get("question") or "").strip()
        if not key or not question or key in seen:
            continue
        fact = str(item.get("fact_needed") or "").strip()
        barred = human_answer_barred(f"{question} {fact}")
        if barred:
            log("self_review.question_dropped", key=key, reason=barred)
            continue
        seen.add(key)
        questions.append(
            {
                "key": key,
                "question": question[:400],
                "why": str(item.get("why") or "").strip()[:400],
                "fact_needed": fact[:300],
            }
        )
    self_check = [
        {"question": str(s.get("question") or "")[:300], "answer": str(s.get("answer") or "")[:800]}
        for s in raw.get("self_check") or []
        if isinstance(s, dict) and (s.get("question") or s.get("answer"))
    ]
    if verdict == "good":
        changes = []  # "good" means no change; stray changes are not acted on or reported
    return Review(verdict=verdict, changes=changes, questions=questions, self_check=self_check)


def screen_changes(rv: Review, ctx: ReviewContext, *, draft: str) -> None:
    """Drop every change whose `what` uses a content word that none of the
    corpus, the passing draft under review, and the job title contain
    (validate/revision.py; the target's name lends no word, and words of
    tense such as "was" and "before" count). Such a change asks the writer
    for a fact the corpus lacks ("Mention managing SDRs", "Say I was at Fabrikam
    Payments before"); it never reaches the writer. A fact like that belongs in
    questions_for_human. Run only on a change list a writer will see
    (improve): `draft` PASSED its checks."""
    if not rv.changes:
        return
    vocab = change_vocabulary(
        ctx.corpus, draft=draft, target_company=ctx.company, job_title=ctx.title
    )
    kept: list[dict[str, Any]] = []
    for c in rv.changes:
        unsourced = unsourced_change_terms(str(c.get("what") or ""), vocab)
        if unsourced:
            rv.dropped.append({**c, "unsourced": unsourced})
            log("self_review.change_dropped", job_id=ctx.job_id, unsourced=unsourced)
        else:
            kept.append(c)
    rv.changes = kept


def review(
    conn: sqlite3.Connection,
    ctx: ReviewContext,
    *,
    artifact: str,
    kind: str,
    scope: str,
    draft: str,
    violations: object,
    writer_version: str,
) -> Review:
    """One self-review call (stage self_review, strong tier), cached. Never
    raises: a failed call comes back as verdict "error" and the caller keeps
    the draft it already validated. Its changes are not screened here: only
    improve hands a change list to a writer, and it screens it first."""
    prompt = llm.PromptFile(PROMPT)
    text = prompt.render(
        artifact_kind=kind,
        company=ctx.company,
        title=ctx.title,
        track=ctx.track,
        requirements=json.dumps(ctx.requirements, indent=1),
        gaps="\n".join(f"- {g}" for g in ctx.gaps) or "(none identified)",
        corpus=corpus_for_review(ctx.corpus),
        scope=scope,
        violations=_violations_text(violations),
        draft=draft,
    )
    cache_key = f"{STAGE}:{artifact}:{ctx.cache_ns}:{prompt.version}:{writer_version}"
    try:
        raw = llm.call_json(conn, STAGE, text, cache_key=cache_key)
    except Exception as e:  # noqa: BLE001 — a failed review never blocks a validated draft
        log("self_review.failed", job_id=ctx.job_id, artifact=artifact, error=str(e)[:300])
        return Review(verdict="error", error=str(e)[:300])
    try:
        rv = normalize(raw, ctx.corpus)
    except ValueError as e:
        # a reply outside the schema is not a review, and is never served again
        llm.forget(conn, STAGE, text, cache_key)
        log("self_review.failed", job_id=ctx.job_id, artifact=artifact, error=str(e)[:300])
        return Review(verdict="error", error=str(e)[:300])
    log(
        "self_review.reviewed",
        job_id=ctx.job_id,
        artifact=artifact,
        verdict=rv.verdict,
        changes=len(rv.changes),
        questions=len(rv.questions),
    )
    return rv


def revision_block(rv: Review, previous_draft: str) -> str:
    """The text appended to the writer's own prompt for its revision call."""
    changes = "\n".join(
        f"{i}. {c['what']}"
        + (f" (why: {c['why']})" if c.get("why") else "")
        + (f" [bullets: {', '.join(c['bullet_ids'])}]" if c.get("bullet_ids") else "")
        for i, c in enumerate(rv.changes, start=1)
    )
    return llm.PromptFile(REVISION_PROMPT).render(changes=changes, draft=previous_draft)


def versions() -> str:
    """The self-review prompt versions, for keys that must change with them."""
    return f"{llm.PromptFile(PROMPT).version}+{llm.PromptFile(REVISION_PROMPT).version}"


# ---------------------------------------------------------------------------
# per-artifact record
# ---------------------------------------------------------------------------


def _jsonable(x: object) -> Any:
    return json.loads(json.dumps(x, default=str))


@dataclass
class Round:
    review: Review
    violations_in: Any = None  # validator violations the review was shown
    outcome: str = ""
    reason: str = ""
    revision_violations: Any = None  # why a revision was discarded
    added: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    # the draft this round reviewed had FAILED its checks (review_only with
    # violations): the review was reported only, and nothing it said reached
    # the regeneration
    original_failed: bool = False

    def settle(self, ok: bool, failure: object, before: list[str], after: list[str]) -> None:
        """Record how the revision this round asked for (improve) ended."""
        self.added = [i for i in after if i not in before]
        self.dropped = [i for i in before if i not in after]
        if ok:
            self.outcome = ACCEPTED
        else:
            self.outcome = DISCARDED
            self.revision_violations = _jsonable(failure)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.review.verdict,
            "outcome": self.outcome,
            "original_failed": self.original_failed,
            "reason": self.reason,
            "self_check": self.review.self_check,
            "changes": self.review.changes,
            "changes_dropped": self.review.dropped,
            "questions_for_human": self.review.questions,
            "violations_in": _jsonable(self.violations_in) if self.violations_in else [],
            "revision_violations": self.revision_violations or [],
            "added": self.added,
            "dropped": self.dropped,
            "error": self.review.error,
        }


@dataclass
class ArtifactLog:
    artifact: str  # "resume" | "cover_letter" | "novel:<field identity>"
    label: str
    rounds: list[Round] = field(default_factory=list)

    @property
    def reviews(self) -> int:
        return len(self.rounds)

    @property
    def revisions(self) -> int:
        return sum(1 for r in self.rounds if r.outcome in (ACCEPTED, DISCARDED))

    def add(self, rv: Review, violations: object = None) -> Round:
        rnd = Round(review=rv, violations_in=violations)
        if rv.verdict == "error":
            rnd.outcome, rnd.reason = ERROR, "The review call failed; the checked draft stands"
        elif not rv.wants_revision:
            rnd.outcome = NO_CHANGE
        self.rounds.append(rnd)
        return rnd

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact,
            "label": self.label,
            "rounds": [r.to_dict() for r in self.rounds],
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
        }


@dataclass
class Session:
    """The self-reviews of one job's drafted form answers (one log each)."""

    ctx: ReviewContext
    max_rounds: int
    logs: list[ArtifactLog] = field(default_factory=list)

    def log_for(self, artifact: str, label: str) -> ArtifactLog:
        lg = ArtifactLog(artifact[:100], label[:160])
        self.logs.append(lg)
        return lg


def may_review(log_: ArtifactLog, max_rounds: int) -> bool:
    """At most max(1, max_rounds) reviews per artifact per run: one review
    always runs (at max_revision_rounds 0 it only reports and asks), and each
    revise round has at most one review before it."""
    return log_.reviews < max(1, max_rounds)


def may_revise(log_: ArtifactLog, max_rounds: int) -> bool:
    return log_.revisions < max_rounds


def review_only(
    conn: sqlite3.Connection,
    ctx: ReviewContext,
    log_: ArtifactLog,
    max_rounds: int,
    *,
    kind: str,
    scope: str,
    draft: str,
    violations: object = None,
    writer_version: str,
) -> Round | None:
    """A self-review whose changes are never applied: it is recorded for its
    report and its questions_for_human, and nothing it says reaches a writer.
    Used for a cover letter or drafted answer that passed its checks, and,
    with the draft's `violations`, for any draft that FAILED them (the
    regeneration then carries only the check feedback it carried at aca26cc).
    None when the artifact has no review left (max_revision_rounds)."""
    if not may_review(log_, max_rounds):
        return None
    rv = review(
        conn,
        ctx,
        artifact=log_.artifact,
        kind=kind,
        scope=scope,
        draft=draft,
        violations=violations,
        writer_version=writer_version,
    )
    rnd = log_.add(rv, violations)
    rnd.original_failed = violations is not None
    if rv.wants_revision:
        rnd.outcome = NOT_REVISED
        rnd.reason = REPORT_ONLY_FAILED if rnd.original_failed else REPORT_ONLY_PROSE
    return rnd


def improve[T](
    conn: sqlite3.Connection,
    ctx: ReviewContext,
    log_: ArtifactLog,
    max_rounds: int,
    current: T,
    *,
    kind: str,
    scope: str,
    writer_version: str,
    render: Callable[[T], str],
    render_for_writer: Callable[[T], str],
    ids_of: Callable[[T], list[str]],
    revise: Callable[[str], T],
    check: Callable[[T, T], tuple[bool, object]],
    on_discard: Callable[[T], None] | None = None,
) -> T:
    """Self-review a resume bullet selection that PASSED its checks; revise
    while the review asks and rounds are left. Only a selection is ever
    revised, because its revision can be held to corpus text: `check` keeps
    it only when every bullet it renders is its named variant's corpus text,
    word for word (tailor.py _reworded). Provenance alone would not do: it
    checks a bullet's numbers, proper nouns and never-claim terms, not its
    every word, and the drift pass only flags a new word (sixth review of the
    self-review, 2026-10-09). A cover letter or drafted answer is reviewed
    with review_only instead: no check can hold rewritten prose to the corpus.
    The change list is screened (screen_changes) before the writer sees it.
    `revise(block)` is the writer call with the block appended (it may raise:
    the draft stands); `check(candidate, previous)` is the word-for-word rule,
    the selection's full validation chain, and an authenticity verdict no
    worse than the draft it revises, as (ok, failure). A revision that fails
    is discarded (`on_discard` may tidy up) and the draft it revised is
    returned."""
    while may_review(log_, max_rounds):
        draft = render(current)
        rv = review(
            conn,
            ctx,
            artifact=log_.artifact,
            kind=kind,
            scope=scope,
            draft=draft,
            violations=None,
            writer_version=writer_version,
        )
        rnd = log_.add(rv)
        if not rv.wants_revision:
            break
        if not may_revise(log_, max_rounds):
            rnd.outcome = NOT_REVISED
            rnd.reason = "Review only: self_review.max_revision_rounds is 0"
            break
        screen_changes(rv, ctx, draft=draft)
        if not rv.changes:
            rnd.outcome = NO_CHANGE
            rnd.reason = (
                "Every change it asked for used words the corpus lacks, so none "
                "reached the writer; the checked draft stands"
            )
            break
        try:
            candidate = revise(revision_block(rv, render_for_writer(current)))
        except Exception as e:  # noqa: BLE001 — a failed revision call keeps the draft
            rnd.settle(False, [{"kind": "revision_call_failed", "detail": str(e)[:200]}], [], [])
            log("self_review.revision_failed", job_id=ctx.job_id, artifact=log_.artifact)
            break
        try:
            ok, failure = check(candidate, current)
        except Exception as e:  # noqa: BLE001 — a check that cannot run is not a pass
            ok, failure = False, [{"kind": "check_failed_to_run", "detail": str(e)[:200]}]
            log("self_review.check_failed", job_id=ctx.job_id, artifact=log_.artifact)
        rnd.settle(ok, failure, ids_of(current), ids_of(candidate))
        log(
            "self_review.revision",
            job_id=ctx.job_id,
            artifact=log_.artifact,
            outcome=rnd.outcome,
            added=rnd.added,
            dropped=rnd.dropped,
        )
        if not ok:
            rnd.reason = "The revision failed a check; the original stands"
            if on_discard is not None:
                on_discard(candidate)
            break
        current = candidate
    return current


# ---------------------------------------------------------------------------
# events + report
# ---------------------------------------------------------------------------


def record(conn: sqlite3.Connection, job_id: int, logs: list[ArtifactLog]) -> int:
    """One 'self_review' event per review, and one 'question_open' event per
    question this job has not raised before (by key). A review whose call
    failed (verdict "error") is not a review: it is in the log
    (self_review.failed) and in self_review.md, not in events. Returns the
    number of new questions. The caller commits."""
    asked = {
        str(r[0]) for r in conn.execute(q.OPEN_QUESTION_KEYS_FOR_JOB, {"job_id": job_id}) if r[0]
    }
    new_questions = 0
    for lg in logs:
        for i, rnd in enumerate(lg.rounds, start=1):
            if rnd.review.verdict == "error":
                continue
            conn.execute(
                q.RECORD_EVENT,
                {
                    "job_id": job_id,
                    "kind": "self_review",
                    "detail": json.dumps(
                        {
                            "artifact": lg.artifact,
                            "round": i,
                            "verdict": rnd.review.verdict,
                            "outcome": rnd.outcome,
                            "original_failed": rnd.original_failed,
                            "changes": len(rnd.review.changes),
                            "changes_dropped": len(rnd.review.dropped),
                            "questions": len(rnd.review.questions),
                            "added": rnd.added,
                            "dropped": rnd.dropped,
                            "discarded_because": (
                                _jsonable(rnd.revision_violations)
                                if isinstance(rnd.revision_violations, dict)
                                else (rnd.revision_violations or [])[:6]
                            ),
                        }
                    ),
                },
            )
            for qn in rnd.review.questions:
                if qn["key"] in asked:
                    continue
                asked.add(qn["key"])
                new_questions += 1
                conn.execute(
                    q.RECORD_EVENT,
                    {
                        "job_id": job_id,
                        "kind": "question_open",
                        "detail": json.dumps({**qn, "job_id": job_id, "artifact": lg.artifact}),
                    },
                )
    return new_questions


def _first_name(corpus: dict[str, Any]) -> str:
    name = str((corpus.get("identity") or {}).get("name") or "").strip()
    return name.split()[0] if name else "the candidate"


_OUTCOME_TEXT = {
    NO_CHANGE: "no change",
    ACCEPTED: "revision accepted",
    DISCARDED: "revision discarded, the original stands",
    NOT_REVISED: "not revised",
    ERROR: "review failed, the checked draft stands",
}
# Events written before the fifth review of the self-review (2026-10-09), when
# a failing draft's review rode on its regeneration: a discarded revision of a
# draft that had itself FAILED. No original stands. No new round ends this way.
DISCARDED_FAILING_TEXT = "revision discarded; original also failed — sent to human"
DISCARDED_FAILING_LATER_TEXT = (
    "revision discarded; original also failed — a later regeneration passed and ships"
)


def outcome_text(rnd: dict[str, Any]) -> str:
    """How a round ended, in words, from its report entry or its event detail.
    "The original stands" only when the original had passed its checks."""
    outcome = str(rnd.get("outcome"))
    if outcome == DISCARDED and rnd.get("original_failed"):
        return DISCARDED_FAILING_LATER_TEXT if rnd.get("later_passed") else DISCARDED_FAILING_TEXT
    return _OUTCOME_TEXT.get(outcome, outcome)


def _render_failure(x: object) -> list[str]:
    if isinstance(x, list):
        out = []
        for v in x[:12]:
            if isinstance(v, dict):
                where = f' — in: "{v["sentence"]}"' if v.get("sentence") else ""
                out.append(f"- {v.get('kind', 'violation')}: {v.get('detail', '')}{where}")
            else:
                out.append(f"- {v}")
        return out
    return [f"- {json.dumps(x, default=str)[:600]}"] if x else []


def render_report(doc: dict[str, Any], corpus: dict[str, Any] | None = None) -> str:
    """self_review.md from the self_review.json document."""
    who = _first_name(corpus or {}) if corpus else str(doc.get("candidate") or "the candidate")
    lines = [
        f"# Self-review — {doc.get('company', '')} · {doc.get('title', '')} "
        f"(job {doc.get('job_id')})",
        "",
        "Each artifact below was reviewed by the model against the job's requirements, "
        "the full corpus, the gap report and the role track, before any human saw it. "
        "Only a passing resume bullet selection may be revised: the revision is kept "
        "only when every bullet it renders is its corpus text word for word, it passes "
        "every check the original had to pass and its authenticity verdict is no worse; "
        "otherwise the passing original ships. A cover letter or "
        "drafted answer is never revised: its review is reported here only, and what "
        "ships is the draft that passed the checks. A failing original's review is "
        "reported only; the original is regenerated within the usual limit, with the "
        "usual check feedback and nothing from the review, under the usual checks; if "
        "no regeneration passes, it goes to you.",
    ]
    questions: list[tuple[str, dict[str, str]]] = []
    for name, art in (doc.get("artifacts") or {}).items():
        lines += ["", f"## {art.get('label') or name}", ""]
        for i, rnd in enumerate(art.get("rounds") or [], start=1):
            lines.append(f"### Round {i}: verdict {rnd.get('verdict')}, {outcome_text(rnd)}")
            if rnd.get("reason"):
                lines.append(f"\n{rnd['reason']}.")
            if rnd.get("error"):
                lines.append(f"\nError: {rnd['error']}")
            if rnd.get("violations_in"):
                lines += ["", "**Checks the draft had failed (fed to the review):**"]
                lines += _render_failure(rnd["violations_in"])
            if rnd.get("self_check"):
                lines += ["", "**What it asked itself:**"]
                for s in rnd["self_check"]:
                    lines.append(f"- *{s.get('question', '')}* {s.get('answer', '')}")
            if rnd.get("changes"):
                applied = rnd.get("outcome") in (ACCEPTED, DISCARDED)
                lines += [
                    "",
                    "**Changes it asked for:**"
                    if applied
                    else "**Changes it suggested (not applied):**",
                ]
                for c in rnd["changes"]:
                    ids = f" (bullets: {', '.join(c['bullet_ids'])})" if c.get("bullet_ids") else ""
                    why = f" Why: {c['why']}" if c.get("why") else ""
                    lines.append(f"- {c.get('what', '')}{ids}.{why}")
            if rnd.get("changes_dropped"):
                lines += [
                    "",
                    "**Changes dropped before the writer saw them (words the corpus lacks):**",
                ]
                for c in rnd["changes_dropped"]:
                    lines.append(
                        f"- {c.get('what', '')} (unsourced: {', '.join(c.get('unsourced') or [])})"
                    )
            if rnd.get("outcome") in (ACCEPTED, DISCARDED):
                verb = "" if rnd.get("outcome") == ACCEPTED else " (proposed, discarded)"
                lines += [
                    "",
                    f"**Bullets added{verb}:** {', '.join(rnd.get('added') or []) or 'none'}  ",
                    f"**Bullets dropped{verb}:** {', '.join(rnd.get('dropped') or []) or 'none'}",
                ]
            if rnd.get("revision_violations"):
                lines += ["", "**Why the revision was discarded:**"]
                lines += _render_failure(rnd["revision_violations"])
            lines.append("")
            questions += [(name, qn) for qn in rnd.get("questions_for_human") or []]
    lines += ["", f"## Questions for {who}", ""]
    if not questions:
        lines.append("None: the corpus covers what these artifacts need.")
    seen: set[str] = set()
    for name, qn in questions:
        if qn.get("key") in seen:
            continue
        seen.add(str(qn.get("key")))
        lines.append(
            f"- **{qn.get('key')}** ({name}): {qn.get('question', '')}"
            + (f" Why: {qn['why']}" if qn.get("why") else "")
            + (f" Fact needed: {qn['fact_needed']}" if qn.get("fact_needed") else "")
        )
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).rstrip() + "\n"


def _job_folders(out_root: Path, job_id: int) -> list[Path]:
    """The job's artifact folders (<company>-<title>-<job_id>-<date>), newest first."""
    pat = re.compile(rf"-{job_id}-\d{{4}}-\d{{2}}-\d{{2}}$")
    try:
        found = [p for p in out_root.iterdir() if p.is_dir() and pat.search(p.name)]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.name[-10:], reverse=True)


def newest_job_folder(out_root: Path, job_id: int) -> Path | None:
    folders = _job_folders(out_root, job_id)
    return folders[0] if folders else None


def write_report(
    out: Path, ctx: ReviewContext, logs: list[ArtifactLog], *, out_root: Path | None = None
) -> Path:
    """Merge these artifacts into out/self_review.json (an artifact reviewed
    again replaces its old entry; other artifacts are kept, including those an
    earlier folder of the same job holds) and re-render out/self_review.md."""
    out.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {}
    sources = [out / REPORT_JSON]
    if out_root is not None:
        sources += [p / REPORT_JSON for p in _job_folders(out_root, ctx.job_id) if p != out]
    for src in sources:
        try:
            old = json.loads(src.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(old, dict):
            for name, art in (old.get("artifacts") or {}).items():
                doc.setdefault("artifacts", {}).setdefault(name, art)
    doc.update(
        {
            "job_id": ctx.job_id,
            "company": ctx.company,
            "title": ctx.title,
            "track": ctx.track,
            "candidate": _first_name(ctx.corpus),
            "prompt_versions": versions(),
        }
    )
    arts = doc.setdefault("artifacts", {})
    for lg in logs:
        if lg.rounds:
            arts[lg.artifact] = lg.to_dict()
    (out / REPORT_JSON).write_text(json.dumps(doc, indent=1))
    path = out / REPORT_MD
    path.write_text(render_report(doc, ctx.corpus))
    return path


# ---------------------------------------------------------------------------
# read side: CLI `jobkit self-review` / MCP get_self_review
# ---------------------------------------------------------------------------


def summary(conn: sqlite3.Connection, job_id: int, out_root: Path, *, full: bool = False) -> str:
    """The latest self-review outcomes for a job, the questions it raised, and
    where self_review.md is (with `full`, its text too). Read-only: no LLM call."""
    job = conn.execute(q.JOB_BRIEF, {"job_id": job_id}).fetchone()
    if job is None:
        return f"no job with id {job_id}"
    lines = [f"job {job_id}: {job['company_name']} · {job['title']}"]
    report = next(
        (p / REPORT_MD for p in _job_folders(out_root, job_id) if (p / REPORT_MD).exists()),
        None,
    )
    lines.append(f"report: {report}" if report else "report: none written yet")
    rows = conn.execute(q.SELF_REVIEW_EVENTS_FOR_JOB, {"job_id": job_id}).fetchall()
    if not rows:
        lines.append("no self-review has run for this job yet (it runs when artifacts are drafted)")
    # newest first: each artifact's latest run is the rounds read back to round 1
    latest: dict[str, list[str]] = {}
    complete: set[str] = set()
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            continue
        art = str(d.get("artifact") or "?")
        if art in complete:
            continue
        latest.setdefault(art, []).insert(
            0,
            f"  {art} round {d.get('round')} [{r['at']}]: verdict {d.get('verdict')}, "
            f"{outcome_text(d)}"
            + (f"; added {d['added']}" if d.get("added") else "")
            + (f"; dropped {d['dropped']}" if d.get("dropped") else ""),
        )
        if int(d.get("round") or 1) <= 1:
            complete.add(art)
    for entries in latest.values():
        lines += entries
    questions = conn.execute(q.OPEN_QUESTIONS_FOR_JOB, {"job_id": job_id}).fetchall()
    lines.append(f"questions raised for the candidate: {len(questions)}")
    for r in questions:
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            continue
        lines.append(
            f"  - {d.get('key')}: {d.get('question')}"
            + (f" (why: {d['why']})" if d.get("why") else "")
        )
    if full and report is not None:
        try:
            lines += ["", report.read_text()]
        except OSError as e:
            lines.append(f"(could not read {report}: {e})")
    return "\n".join(lines)
