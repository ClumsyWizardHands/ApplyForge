"""Re-judge (claim-judge review 2026-10-10): the claim judge
(validate/claim_judge.py) reads the cover letters and drafted answers written
from the day it shipped. Letters and answers stored before then were never
judged, and staging still fills them. `jobkit rejudge` (MCP rejudge, which runs
it in the background) checks those stored texts now with the check a new draft
gets: the rule-based fact check (validate/claims.py) against the current
corpus, then the judge on what the rules pass (claim_judge.gate, which never
overrides a rule failure). Whatever fails is moved to the fail-closed state a
new draft that failed is left in. A false rejection costs a redraft or a
question for the human; a false acceptance sends a fabrication.

What it checks (selection, read-only):

* letters: each job's newest application at stage 'tailored' with a cover
  letter, never submitted, on a job not applied to (db.REJUDGE_LETTERS). The
  letter is its folder's cover.txt; its citations and the bullets it may cite
  are provenance.json's `cover_bullets_used` and `bullets`. Skipped: a letter
  whose provenance.json names the current judge prompt version (judged when it
  was drafted), and one an earlier rejudge passed under that version;
* answers: every stored answer in form_specs.drafted_answers of a job never
  submitted or applied to (db.REJUDGE_FORM_SPECS). Skipped: an answer the
  human gave (db.HUMAN_ANSWERED_FIELDS: the human's own word is not judged),
  and one whose form field carries the current judge version
  (form_spec.JUDGED_KEY: stamped at capture when the judge passed the draft,
  and here when a re-check passes it). A stored answer keeps no citations, so
  it is checked against none: the role list, education and skill index can
  still support it, a fact only a bullet states fails it.

Rejected and closed jobs are included: `jobkit stage` with no arguments and
MCP stage_forms(job_ids=…) can still stage them.

Fail-closed states (the ones tailoring and form capture leave a failed draft in):

* a letter: cover.pdf, cover.txt and cover_critique.md are removed and
  cover_rejected.md holds the violations and the letter, NOT FOR SENDING; the
  application goes back to 'approved' with no cover_path, so it is never
  staged; a `cover_provenance_failed` event (rejudge: true). It carries no
  attempt key: the letter was not written by the current corpus and prompts,
  so the next tailor run re-tailors an approved job once, with the judge in
  the loop, and parks it as usual if that fails;
* an answer: removed from drafted_answers and listed in novel_questions with
  flag unsourced-claim and its fact_check list (MCP get_novel_questions); a
  `novel_provenance_failed` event (rejudge: true); friction recomputed.

A judge call that fails outright (llm.call's retries exhausted) leaves that
text as it was and unjudged; the report names it, nothing is recorded, and the
next run tries it again (the CLI exits 1). A text that changed while the judge
was reading it (re-tailored, recaptured, answered by the human) is left alone.
One `claims_rejudged` event per job checked. A dry run lists, makes no LLM
call and writes nothing. The judge's calls are cached in llm_cache on the full
prompt, so checking an unchanged text again costs nothing."""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobkit import db as q
from jobkit.autofill import form_spec
from jobkit.log import log
from jobkit.pipeline import tailor
from jobkit.validate import claim_judge
from jobkit.validate import style as estimate_styles
from jobkit.validate.claims import coerce_ids, validate_claims
from jobkit.validate.provenance import ProvenanceResult

EVENT = q.CLAIMS_REJUDGED_EVENT
# a stored letter that cannot be read cannot be checked: it fails closed
UNREADABLE = "rejudge_unreadable"
UNSOURCED = "unsourced-claim"  # the flag form capture gives a failed draft


@dataclass
class Letter:
    job_id: int
    app_id: int
    company: str
    title: str
    cover_path: str
    text: str
    cited: list[str]
    allowed: list[str]
    jd_text: str
    problem: str = ""  # why it cannot be read ("" = readable)


@dataclass
class Answer:
    job_id: int
    identity: str
    label: str
    question: str
    text: str
    company: str
    title: str
    jd_text: str


@dataclass
class JobItems:
    job_id: int
    company: str
    title: str
    letter: Letter | None = None
    answers: list[Answer] = field(default_factory=list)


@dataclass
class Selection:
    jobs: dict[int, JobItems]
    skipped: list[tuple[int, str]]


@dataclass
class Outcome:
    job_id: int
    company: str
    title: str
    what: str  # "letter" or "answer <identity>"
    ok: bool
    violations: list[dict[str, str]] = field(default_factory=list)
    note: str = ""

    def render(self) -> str:
        head = f"job {self.job_id} {self.company} — {self.title}: {self.what}"
        if self.ok:
            return f"{head} passed"
        reasons = "; ".join(
            f"{v.get('kind')}: {str(v.get('detail') or '')[:140]}" for v in self.violations[:3]
        )
        more = f" (+{len(self.violations) - 3} more)" if len(self.violations) > 3 else ""
        return f"{head} FAILED — {self.note} [{reasons}{more}]"


@dataclass
class RejudgeReport:
    dry_run: bool
    judge: str
    explicit: bool
    selection: Selection
    outcomes: list[Outcome] = field(default_factory=list)
    unjudged: list[tuple[int, str, str]] = field(default_factory=list)  # job, what, error
    changed: list[tuple[int, str]] = field(default_factory=list)  # left alone: changed meanwhile

    @property
    def letters(self) -> int:
        return sum(1 for j in self.selection.jobs.values() if j.letter is not None)

    @property
    def answers(self) -> int:
        return sum(len(j.answers) for j in self.selection.jobs.values())

    def render(self) -> str:
        which = "the jobs given" if self.explicit else "every job not submitted or applied to"
        lines: list[str] = []
        if self.dry_run:
            lines.append("DRY RUN — no LLM call, nothing changed.")
            lines.append(
                f"would check {self.letters} cover letter(s) and {self.answers} drafted "
                f"answer(s) on {len(self.selection.jobs)} job(s) ({which}): the rule-based "
                f"fact check, then the claim judge (prompt {self.judge}):"
            )
            for j in self.selection.jobs.values():
                parts = []
                if j.letter is not None:
                    parts.append(
                        f"letter {j.letter.cover_path}"
                        + (f" ({j.letter.problem}: fails closed)" if j.letter.problem else "")
                    )
                if j.answers:
                    parts.append(
                        "answers "
                        + ", ".join(f'{a.identity} ("{a.label[:60]}")' for a in j.answers)
                    )
                lines.append(f"  job {j.job_id} {j.company} — {j.title}: " + "; ".join(parts))
        else:
            failed = [o for o in self.outcomes if not o.ok]
            lines.append(
                f"checked {len(self.outcomes)} of {self.letters + self.answers} stored text(s) "
                f"({which}) with the rule-based fact check and the claim judge (prompt "
                f"{self.judge}): {len(self.outcomes) - len(failed)} passed, {len(failed)} failed"
            )
            lines += [f"  {o.render()}" for o in failed]
            lines += [f"  {o.render()}" for o in self.outcomes if o.ok]
            if self.unjudged:
                lines.append("NOT CHECKED — the judge could not be reached; run again:")
                lines += [f"  job {j}: {what} ({err[:160]})" for j, what, err in self.unjudged]
            if self.changed:
                lines.append("left alone — changed while it was being checked:")
                lines += [f"  job {j}: {what}" for j, what in self.changed]
        if self.selection.skipped:
            lines.append("skipped:")
            lines += [f"  job {j}: {why}" for j, why in self.selection.skipped]
        lines.append(
            "A failed letter is moved to cover_rejected.md and its application goes back to "
            "approved (never staged; an approved job is re-tailored once by the next tailor "
            "run, with the judge). A failed answer goes back to your open questions "
            "(get_novel_questions) with the reasons. Answers you gave are never judged."
        )
        return "\n".join(lines)


# ---- selection (read-only) ----


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _letter(conn: sqlite3.Connection, r: sqlite3.Row, judge: str) -> Letter | str:
    """The stored letter to check, or why it is skipped."""
    cover = Path(str(r["cover_path"]))
    folder = cover.parent
    prov_path = (
        Path(str(r["provenance_path"])) if r["provenance_path"] else folder / "provenance.json"
    )
    prov = _read_json(prov_path)
    check = (prov or {}).get("cover_fact_check")
    if (
        prov is not None
        and str(prov.get("claim_judge_prompt") or "") == judge
        and isinstance(check, dict)
        and check.get("ok") is True
    ):
        return f"letter judged when it was drafted (judge {judge})"
    passed = conn.execute(
        q.LETTER_PASSED_REJUDGE,
        {
            "job_id": r["job_id"],
            "judge": judge,
            "letter": str(r["cover_path"]),
            "since": r["updated_at"],
        },
    ).fetchone()
    if passed is not None:
        return f"letter passed an earlier rejudge (judge {judge})"
    problem = ""
    try:
        text = (folder / "cover.txt").read_text()
    except OSError:
        text, problem = "", f"{folder / 'cover.txt'} is missing or unreadable"
    if not problem and not text.strip():
        problem = f"{folder / 'cover.txt'} is empty"
    if not problem and prov is None:
        problem = f"{prov_path} is missing or unreadable, so the letter's citations are unknown"
    prov = prov or {}
    return Letter(
        job_id=int(r["job_id"]),
        app_id=int(r["app_id"]),
        company=str(r["company_name"] or ""),
        title=str(r["title"] or ""),
        cover_path=str(r["cover_path"]),
        text=text,
        cited=coerce_ids(prov.get("cover_bullets_used")),
        allowed=[
            str(b.get("bullet_id"))
            for b in prov.get("bullets") or []
            if isinstance(b, dict) and b.get("bullet_id")
        ],
        jd_text=tailor._jd_plain(r),
        problem=problem,
    )


def _question(f: dict[str, Any], label: str) -> str:
    """The question as form capture showed it to the writer and the judge."""
    options = f.get("options")
    return label + (f" (options: {options})" if options else "")


def select(
    conn: sqlite3.Connection, job_ids: list[int] | None = None, judge: str | None = None
) -> Selection:
    """The stored letters and answers to check, and what is skipped with the
    reason (module docstring). Read-only."""
    judge = judge or claim_judge.version()
    wanted = set(job_ids or [])
    jobs: dict[int, JobItems] = {}
    skipped: list[tuple[int, str]] = []
    seen: set[int] = set()

    def items(r: sqlite3.Row) -> JobItems:
        jid = int(r["job_id"])
        if jid not in jobs:
            jobs[jid] = JobItems(jid, str(r["company_name"] or ""), str(r["title"] or ""))
        return jobs[jid]

    for r in conn.execute(q.REJUDGE_LETTERS).fetchall():
        jid = int(r["job_id"])
        if wanted and jid not in wanted:
            continue
        seen.add(jid)
        got = _letter(conn, r, judge)
        if isinstance(got, str):
            skipped.append((jid, got))
        else:
            items(r).letter = got

    for r in conn.execute(q.REJUDGE_FORM_SPECS).fetchall():
        jid = int(r["job_id"])
        if wanted and jid not in wanted:
            continue
        seen.add(jid)
        try:
            drafted = json.loads(r["drafted_answers"] or "{}")
            fields = json.loads(r["fields"] or "[]")
        except ValueError:
            skipped.append((jid, "form spec is not readable JSON"))
            continue
        if not isinstance(drafted, dict):
            continue
        by_id = {
            str(f.get("identity")): f for f in fields if isinstance(f, dict) and f.get("identity")
        }
        human = {str(x[0]) for x in conn.execute(q.HUMAN_ANSWERED_FIELDS, {"job_id": jid})}
        mine = judged = 0
        jd_text = re.sub(r"<[^>]+>", " ", str(r["jd_html"] or ""))
        for identity, value in drafted.items():
            ident = str(identity)
            if ident in human:
                mine += 1
                continue
            f = by_id.get(ident, {})
            if str(f.get(form_spec.JUDGED_KEY) or "") == judge:
                judged += 1
                continue
            label = str(f.get("label") or ident)
            items(r).answers.append(
                Answer(
                    job_id=jid,
                    identity=ident,
                    label=label,
                    question=_question(f, label),
                    text=str(value),
                    company=str(r["company_name"] or ""),
                    title=str(r["title"] or ""),
                    jd_text=jd_text,
                )
            )
        if mine:
            skipped.append((jid, f"{mine} answer(s) you gave (never judged)"))
        if judged:
            skipped.append((jid, f"{judged} answer(s) judged when drafted (judge {judge})"))

    for jid in sorted(wanted - seen):
        skipped.append((jid, _skip_reason(conn, jid)))
    return Selection(jobs=dict(sorted(jobs.items())), skipped=skipped)


def _skip_reason(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute(q.JOB_STATUS, {"job_id": job_id}).fetchone()
    if row is None:
        return "no such job"
    app = conn.execute(q.APPLICATION_FOR_JOB, {"job_id": job_id}).fetchone()
    if row["status"] == "applied" or (app is not None and app["submitted_at"]):
        return "already submitted: nothing left to stop"
    return "no stored letter or drafted answer to check"


# ---- checking ----


def _check_letter(
    conn: sqlite3.Connection,
    letter: Letter,
    corpus: dict[str, Any],
    known: list[str],
    style: str,
) -> ProvenanceResult:
    """The check tailoring gives a new letter (pipeline/tailor.py
    fact_check): the rules, then the judge on what they pass."""
    if letter.problem:
        return ProvenanceResult(
            ok=False,
            violations=[
                {
                    "kind": UNREADABLE,
                    "detail": f"{letter.problem}: the letter cannot be re-checked; failed closed",
                    "sentence": "",
                }
            ],
        )
    rules = validate_claims(
        letter.text,
        letter.cited,
        corpus,
        target_company=letter.company,
        job_title=letter.title,
        jd_text=letter.jd_text,
        allowed_ids=letter.allowed,
        known_companies=known,
        estimate_style=style,
    )
    return claim_judge.gate(
        conn,
        rules,
        letter.text,
        letter.cited,
        corpus,
        target_company=letter.company,
        job_title=letter.title,
        artifact_kind=claim_judge.LETTER_KIND,
    )


def _check_answer(
    conn: sqlite3.Connection,
    answer: Answer,
    corpus: dict[str, Any],
    known: list[str],
    style: str,
) -> ProvenanceResult:
    """The check form capture gives a new draft (autofill/form_spec.py
    draft_novel_answer), with no citations: a stored answer keeps none."""
    rules = validate_claims(
        answer.text,
        [],
        corpus,
        target_company=answer.company,
        job_title=answer.title,
        jd_text=answer.jd_text,
        known_companies=known,
        estimate_style=style,
        question=f"{answer.label}\n{answer.question}",
    )
    return claim_judge.gate(
        conn,
        rules,
        answer.text,
        [],
        corpus,
        target_company=answer.company,
        job_title=answer.title,
        artifact_kind=form_spec.NOVEL_KIND,
        question=(
            answer.question
            if answer.question.startswith(answer.label)
            else f"{answer.label}\n{answer.question}"
        ),
    )


# ---- fail-closed moves ----


def _rejected_md(letter: Letter, violations: list[dict[str, str]], judge: str) -> str:
    return (
        "# Cover letter REJECTED on re-check — do not send\n\n"
        "This letter was written before the claim judge read letters (or under an older "
        f"judge prompt). `jobkit rejudge` checked it on {datetime.date.today():%Y-%m-%d} with "
        f"the rule-based fact check and the claim judge (prompt {judge}), and it failed. "
        "It is no longer attached at staging.\n\n"
        + (
            "Violations of kind `claim_judge_…` are the claim judge's reasons: it reads a "
            "letter only after the rule-based check passed it.\n\n"
            if any(v.get("kind") in claim_judge.KINDS for v in violations)
            else ""
        )
        + "## Violations\n\n"
        + "\n".join(
            f"- **{v.get('kind')}**: {v.get('detail')}"
            + (f' — in: "{v["sentence"]}"' if v.get("sentence") else "")
            for v in violations
        )
        + "\n\n## The letter (NOT FOR SENDING)\n\n"
        + letter.text
        + "\n"
    )


def _fail_letter(
    conn: sqlite3.Connection, letter: Letter, violations: list[dict[str, str]], judge: str
) -> bool:
    """Move a stored letter to the fail-closed state. False when the
    application no longer holds this letter (left alone)."""
    cur = conn.execute(
        q.REJECT_STORED_COVER, {"id": letter.app_id, "cover_path": letter.cover_path}
    )
    if cur.rowcount != 1:
        return False  # re-tailored, submitted or changed since it was read
    # the application no longer points at the letter, so staging never
    # attaches it; the files below are for the human, and a failure to write
    # them is logged but never puts the letter back
    folder = Path(letter.cover_path).parent
    rejected = folder / "cover_rejected.md"
    written = ""
    try:
        if letter.text.strip():
            rejected.write_text(_rejected_md(letter, violations, judge))
            written = str(rejected)
        for stale in {Path(letter.cover_path), *(folder / n for n in tailor.FINAL_COVER_FILES)}:
            stale.unlink(missing_ok=True)
    except OSError as e:
        log("rejudge.letter_files_failed", job_id=letter.job_id, error=str(e)[:200])
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": letter.job_id,
            "kind": "cover_provenance_failed",
            "detail": json.dumps(
                {
                    "file": written,
                    # no attempt key: the letter was not written by the current
                    # inputs, so tailor_approved re-tailors an approved job once
                    "attempt": "",
                    "rejudge": True,
                    "judge": judge,
                    "violations": violations[:12],
                }
            ),
        },
    )
    conn.commit()
    log("rejudge.letter_failed", job_id=letter.job_id, violations=violations[:12])
    return True


def _apply_answers(
    conn: sqlite3.Connection,
    job_id: int,
    passed: list[Answer],
    failed: list[tuple[Answer, list[dict[str, str]]]],
    judge: str,
) -> tuple[list[str], list[str], list[str]]:
    """Stamp the passed answers, move the failed ones back to the human.
    Re-reads the form spec first: an answer that changed since it was read
    (recaptured, or answered by the human) is left alone. Returns the
    identities that passed and are still stored, that failed and were moved,
    and that were left alone."""
    row = conn.execute(q.REJUDGE_FORM_SPEC, {"job_id": job_id}).fetchone()
    if row is None:  # the form spec is gone: nothing left to stage
        return [], [], [a.identity for a in passed] + [a.identity for a, _ in failed]
    fields = json.loads(row["fields"] or "[]")
    drafted = json.loads(row["drafted_answers"] or "{}")
    novel = json.loads(row["novel_questions"] or "[]")
    human = {str(x[0]) for x in conn.execute(q.HUMAN_ANSWERED_FIELDS, {"job_id": job_id})}
    by_id = {str(f.get("identity")): f for f in fields if isinstance(f, dict)}

    def unchanged(a: Answer) -> bool:
        return (
            a.identity not in human and a.identity in drafted and str(drafted[a.identity]) == a.text
        )

    kept: list[str] = []
    moved: list[str] = []
    left: list[str] = []
    for a in passed:
        if not unchanged(a):
            left.append(a.identity)
            continue
        kept.append(a.identity)
        if a.identity in by_id:  # a field the form no longer lists cannot be stamped
            by_id[a.identity][form_spec.JUDGED_KEY] = judge
    for a, violations in failed:
        if not unchanged(a):
            left.append(a.identity)
            continue
        del drafted[a.identity]
        f = by_id.get(a.identity)
        if f is not None:  # as form capture leaves a draft that failed
            f["decline_reason"] = UNSOURCED
            f["provenance_violations"] = violations[:10]
            f.pop(form_spec.JUDGED_KEY, None)
        entry = {
            "identity": a.identity,
            "label": (f or {}).get("label") or a.label,
            "flag": UNSOURCED,
            "fact_check": violations[:10],
        }
        novel = [
            n for n in novel if str(n.get("identity") if isinstance(n, dict) else n) != a.identity
        ] + [entry]
        conn.execute(
            q.RECORD_EVENT,
            {
                "job_id": job_id,
                "kind": "novel_provenance_failed",
                "detail": json.dumps(
                    {
                        "field": a.identity,
                        "label": a.label[:120],
                        "rejudge": True,
                        "judge": judge,
                        "violations": violations[:10],
                    }
                ),
            },
        )
        log("rejudge.answer_failed", job_id=job_id, field=a.identity, violations=violations[:10])
        moved.append(a.identity)
    if kept or moved:
        friction = form_spec._friction(
            int(row["field_count"] or len(fields)),
            int(row["required_count"] or 0),
            len(novel),
            bool(row["has_assessment"]),
            form_spec.friction_weights(),
        )
        conn.execute(
            q.UPDATE_REJUDGED_FORM_SPEC,
            {
                "job_id": job_id,
                "fields": json.dumps(fields),
                "drafted_answers": json.dumps(drafted),
                "novel_questions": json.dumps(novel),
                "novel_question_count": len(novel),
                "friction_score": friction,
            },
        )
    return kept, moved, left


# ---- the run ----


def rejudge(
    conn: sqlite3.Connection, job_ids: list[int] | None = None, *, dry_run: bool = False
) -> RejudgeReport:
    """Check the stored letters and answers the selection picks (module
    docstring), or those of the given job_ids, and move every one that fails
    to its fail-closed state. dry_run lists them, makes no LLM call and
    writes nothing."""
    judge = claim_judge.version()
    sel = select(conn, job_ids, judge)
    report = RejudgeReport(dry_run=dry_run, judge=judge, explicit=bool(job_ids), selection=sel)
    if dry_run:
        log("rejudge.dry_run", letters=report.letters, answers=report.answers)
        return report
    if not sel.jobs:
        log("rejudge.done", checked=0)
        return report
    corpus = yaml.safe_load(tailor.CORPUS_PATH.read_text()) or {}  # missing: fail loudly
    known = [str(r[0]) for r in conn.execute(q.KNOWN_COMPANY_NAMES)]
    style = estimate_styles.configured_estimate_style()

    for job in sel.jobs.values():
        detail: dict[str, Any] = {"judge": judge, "letter": None, "letter_ok": None}
        if job.letter is not None:
            letter = job.letter
            try:
                result = _check_letter(conn, letter, corpus, known, style)
            except RuntimeError as e:  # the judge could not be reached: nothing recorded
                report.unjudged.append((job.job_id, "letter", str(e)))
                log("rejudge.unjudged", job_id=job.job_id, what="letter", error=str(e)[:300])
            else:
                detail["letter"] = letter.cover_path
                detail["letter_ok"] = result.ok
                if result.ok:
                    report.outcomes.append(
                        Outcome(job.job_id, job.company, job.title, "letter", True)
                    )
                elif _fail_letter(conn, letter, result.violations, judge):
                    report.outcomes.append(
                        Outcome(
                            job.job_id,
                            job.company,
                            job.title,
                            "letter",
                            False,
                            result.violations,
                            "moved to cover_rejected.md; the application is back at approved",
                        )
                    )
                else:
                    detail["letter"] = detail["letter_ok"] = None
                    report.changed.append((job.job_id, "letter"))
        passed: list[Answer] = []
        failed: list[tuple[Answer, list[dict[str, str]]]] = []
        for a in job.answers:
            try:
                result = _check_answer(conn, a, corpus, known, style)
            except RuntimeError as e:
                report.unjudged.append((job.job_id, f"answer {a.identity}", str(e)))
                log("rejudge.unjudged", job_id=job.job_id, what=a.identity, error=str(e)[:300])
                continue
            if result.ok:
                passed.append(a)
            else:
                failed.append((a, result.violations))
        kept, moved, left = (
            _apply_answers(conn, job.job_id, passed, failed, judge)
            if passed or failed
            else ([], [], [])
        )
        for a in passed:
            if a.identity in kept:
                report.outcomes.append(
                    Outcome(job.job_id, job.company, job.title, f"answer {a.identity}", True)
                )
        for a, violations in failed:
            if a.identity in moved:
                report.outcomes.append(
                    Outcome(
                        job.job_id,
                        job.company,
                        job.title,
                        f'answer {a.identity} ("{a.label[:60]}")',
                        False,
                        violations,
                        "back in your open questions (get_novel_questions)",
                    )
                )
        report.changed += [(job.job_id, f"answer {i}") for i in left]
        detail["answers_ok"] = kept
        detail["answers_failed"] = moved
        if detail["letter"] is not None or kept or moved:
            conn.execute(
                q.RECORD_EVENT, {"job_id": job.job_id, "kind": EVENT, "detail": json.dumps(detail)}
            )
        conn.commit()
    log(
        "rejudge.done",
        checked=len(report.outcomes),
        failed=sum(1 for o in report.outcomes if not o.ok),
        unjudged=len(report.unjudged),
        changed=len(report.changed),
    )
    return report
