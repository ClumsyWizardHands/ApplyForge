"""Stage 7 — form-spec capture (M2b). Fetch application form structure via ATS
adapters, match every field against the Answer Bank, draft novel questions
overnight, compute friction. Runs before any browser opens (SPEC §2.3).

Endpoint ground truth (verified live 2026-08-18):
- Greenhouse: per-job ?questions=true — questions[] + demographic_questions.
- Lever: no public API for apply fields; the hosted apply page is server-rendered
  HTML — parsed with bs4 (counts as api_questions-grade, not a browser scrape).
- Ashby: jobs.ashbyhq.com/api/non-user-graphql ApiJobPosting — the same
  unauthenticated call the public apply page makes; fields carry `path`.
- SmartRecruiters: /postings/{uuid}/configuration returned 404 on the flagship
  public board — UNVERIFIED; SR routes to DOM-scrape fallback at stage time.

Dead postings (review 2026-10-12). A fetcher returns
PostingGone only for a definite not-found of that one posting, checked live
2026-10-12: Greenhouse's per-job API answers 404 {"status":404,"error":"Job
not found"}; Lever's postings API (api.lever.co/v0/postings/{site}/{id})
answers 404 {"ok":false,"error":"Document not found"}, asked only after the
apply page failed. Ashby's form comes from ONE shared GraphQL endpoint
(jobs.ashbyhq.com/api/non-user-graphql, a POST whose body names the
posting), so its HTTP status says nothing about one posting and never counts
(review 2026-10-12: a moved endpoint would have closed every Ashby job).
Only {"data":{"jobPosting":null}} with no errors (not checked live: the
endpoint takes a POST) counts, and only when the public posting API's job
list for the board (api.ashbyhq.com/posting-api/job-board/{token}) answers
and does not list the posting. HTTP 410 counts as gone too for Greenhouse
and Lever. The job is then closed (status 'closed', closed_at, event 'posting_gone'), gets
no form-spec row, and is never tailored or staged. Any other failure (5xx,
403, a timeout, an unparseable page) keeps the old behaviour: a dom_scrape
placeholder, and the job goes on.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import yaml
from bs4 import BeautifulSoup

from jobkit import db, llm
from jobkit.autofill.answer_bank import BankMatcher, comp_expectation_block_for, never_draft
from jobkit.config import CONFIG_DIR, Filters, load_filters, read_config_yaml
from jobkit.contact import user_agent
from jobkit.log import log
from jobkit.pipeline import self_review
from jobkit.validate import claim_judge
from jobkit.validate import style as estimate_styles
from jobkit.validate.claims import coerce_ids, drafting_profile, validate_claims
from jobkit.validate.provenance import ProvenanceResult


def _ua() -> dict[str, str]:
    """The User-Agent header (contact.user_agent: the operator's address)."""
    return {"User-Agent": user_agent()}


ASSESSMENT_PAT = re.compile(r"assessment|timed test|take.?home|coding challenge|hackerrank", re.I)


# ---- per-ATS form fetchers → normalized field dicts ----


@dataclass(frozen=True)
class PostingGone:
    """The ATS said, for this one posting, that it does not exist: HTTP 404 or
    410, or its own not-found body. `signal` says which, for the event."""

    signal: str


# a fetcher's result: the fields, a definite not-found, or None (any other
# failure: the job keeps a dom_scrape placeholder, as before)
FormFetch = list[dict[str, Any]] | PostingGone | None

_GONE_STATUS = frozenset({404, 410})


def _gh_form(client: httpx.Client, token: str, external_id: str) -> FormFetch:
    r = client.get(
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{external_id}?questions=true",
        headers=_ua(),
    )
    if r.status_code in _GONE_STATUS:
        return PostingGone(f"greenhouse-http-{r.status_code}")
    if r.status_code != 200:
        return None
    p = r.json()
    fields: list[dict[str, Any]] = []
    # location_questions: the required "Location" box Greenhouse adds when the
    # employer asks for it. It was never captured, so staging did not know the
    # field existed (2026-10-12). Its latitude/longitude companions
    # are input_hidden: nothing a person fills, so no hidden field is captured.
    for section, compliance in (
        (p.get("questions"), None),
        (p.get("location_questions"), None),
        (p.get("demographic_questions"), "eeo"),
    ):
        qs = section.get("questions") if isinstance(section, dict) else section
        for question in qs or []:
            for f in question.get("fields") or [{}]:
                if str(f.get("type") or "") == "input_hidden":
                    continue
                fields.append(
                    {
                        "identity": f.get("name") or question.get("label", ""),
                        "label": question.get("label", ""),
                        "type": f.get("type", "input_text"),
                        "required": bool(question.get("required")),
                        "options": [v.get("label") for v in (f.get("values") or [])][:20],
                        "compliance_type": compliance,
                    }
                )
    return fields


def _lever_gone(client: httpx.Client, token: str, external_id: str) -> PostingGone | None:
    """Asked only after the apply page failed: Lever's public postings API
    for this posting. Gone only when it says so (404/410, or ok:false with
    a not-found error); anything else is not a definite answer."""
    try:
        r = client.get(f"https://api.lever.co/v0/postings/{token}/{external_id}", headers=_ua())
    except httpx.HTTPError:
        return None
    if r.status_code in _GONE_STATUS:
        return PostingGone(f"lever-api-http-{r.status_code}")
    try:
        body = r.json()
    except ValueError:
        return None
    if (
        isinstance(body, dict)
        and body.get("ok") is False
        and "not found" in str(body.get("error") or "").lower()
    ):
        return PostingGone("lever-api-not-found")
    return None


def _lever_form(client: httpx.Client, token: str, external_id: str) -> FormFetch:
    r = client.get(f"https://jobs.lever.co/{token}/{external_id}/apply", headers=_ua())
    if r.status_code != 200:
        return _lever_gone(client, token, external_id)
    soup = BeautifulSoup(r.text, "html.parser")
    fields: list[dict[str, Any]] = []
    for q in soup.select("li.application-question, div.application-question"):
        label_el = q.select_one(".application-label")
        label = label_el.get_text(" ", strip=True) if label_el else ""
        ctl = q.find(["input", "select", "textarea"])
        if ctl is None and not label:
            continue
        name = str(ctl.get("name", "")) if ctl else ""
        ftype = ctl.name if ctl else "text"
        if ctl and ctl.name == "input":
            ftype = str(ctl.get("type", "text"))
        options = [o.get_text(strip=True) for o in q.find_all("option")][:20]
        required = "✱" in q.get_text() or bool(q.select_one(".required"))
        compliance = "eeo" if name.startswith("eeo") or "eeo[" in name else None
        fields.append(
            {
                "identity": name or label,
                "label": label,
                "type": ftype,
                "required": required,
                "options": [o for o in options if o],
                "compliance_type": compliance,
            }
        )
    return fields or None


_ASHBY_QUERY = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {"
    " jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName,"
    " jobPostingId: $jobPostingId) { id title applicationForm { sections { title"
    " fieldEntries { field isRequired } } } } }"
)


ASHBY_BOARD_LIST = "https://api.ashbyhq.com/posting-api/job-board/{token}"


def _ashby_gone(client: httpx.Client, token: str, external_id: str) -> PostingGone | None:
    """Asked only after Ashby's GraphQL answered jobPosting: null. Gone only
    when the board's public job list answers and does not list the posting;
    a list that fails, cannot be read, or still lists it is no definite
    answer (None: the dom_scrape fallback, as before)."""
    try:
        r = client.get(ASHBY_BOARD_LIST.format(token=token), headers=_ua())
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    jobs = body.get("jobs") if isinstance(body, dict) else None
    if not isinstance(jobs, list):
        return None
    want = external_id.strip().lower()
    for j in jobs:
        if not isinstance(j, dict):
            return None  # a list we cannot read is no answer
        if str(j.get("id") or "").strip().lower() == want:
            return None  # still listed: the null was not about this posting
    return PostingGone("ashby-jobPosting-null-and-not-on-board")


def _ashby_form(client: httpx.Client, token: str, external_id: str) -> FormFetch:
    r = client.post(
        "https://jobs.ashbyhq.com/api/non-user-graphql",
        headers={**_ua(), "Content-Type": "application/json"},
        json={
            "operationName": "ApiJobPosting",
            "variables": {
                "organizationHostedJobsPageName": token,
                "jobPostingId": external_id,
            },
            "query": _ASHBY_QUERY,
        },
    )
    # one shared endpoint for every posting: a 404/410 (or any other
    # status) says the endpoint is unavailable, not that this posting is gone
    if r.status_code != 200:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    raw = body.get("data") if isinstance(body, dict) else None
    if (
        isinstance(raw, dict)
        and "jobPosting" in raw
        and raw["jobPosting"] is None
        and not body.get("errors")
    ):
        return _ashby_gone(client, token, external_id)
    data = (raw or {}).get("jobPosting") or {}
    form = data.get("applicationForm") or {}
    fields: list[dict[str, Any]] = []
    for section in form.get("sections", []):
        section_title = (section.get("title") or "").lower()
        compliance = "eeo" if ("demographic" in section_title or "eeo" in section_title) else None
        for fe in section.get("fieldEntries", []):
            f = fe.get("field") or {}
            title = f.get("title", "")
            fields.append(
                {
                    "identity": f.get("path") or title,  # Ashby identifies fields by `path`
                    "label": title,
                    "type": f.get("type", "String"),
                    "required": bool(fe.get("isRequired")),
                    "options": [v.get("label") for v in (f.get("selectableValues") or [])][:20],
                    "compliance_type": compliance,
                }
            )
    return fields or None


FETCHERS: dict[str, Callable[[httpx.Client, str, str], FormFetch]] = {
    "greenhouse": _gh_form,
    "lever": _lever_form,
    "ashby": _ashby_form,
}


def close_gone_posting(conn: sqlite3.Connection, row: dict[str, Any], gone: PostingGone) -> bool:
    """Close a job whose posting the ATS says is gone, so it is never
    tailored or staged: status 'closed', closed_at, and a 'posting_gone'
    event naming the check. Only a queued or approved open job is closed.
    True when it was."""
    cur = conn.execute(db.CLOSE_GONE_POSTING, {"job_id": row["id"]})
    if not cur.rowcount:
        return False
    detail = {
        "ats": row["ats"],
        "token": row["token"],
        "external_id": row["external_id"],
        "signal": gone.signal,
    }
    conn.execute(
        db.RECORD_EVENT,
        {"job_id": row["id"], "kind": "posting_gone", "detail": json.dumps(detail)},
    )
    conn.commit()
    log("form_spec.posting_gone", job_id=row["id"], **detail)
    return True


# ---- capture + match + draft + friction ----


def _friction(
    field_count: int, required: int, novel: int, assessment: bool, weights: dict[str, int]
) -> int:
    score = (
        field_count * weights.get("field_count", 1)
        + required * weights.get("required_count", 2)
        + novel * weights.get("novel_question_count", 5)
        + (weights.get("has_assessment", 40) if assessment else 0)
    )
    return min(100, score)


def friction_weights() -> dict[str, int]:
    """filters.yaml `friction.weights` (the one owner of the friction score's weights)."""
    raw = read_config_yaml(CONFIG_DIR / "filters.yaml") or {}
    weights: dict[str, int] = (raw.get("friction", {}) or {}).get("weights", {})
    return weights


# Set on a form field (form_specs.fields) whose stored draft the claim judge
# passed: the judge prompt's version. Written at capture, and by `jobkit
# rejudge` (pipeline/rejudge.py), which re-checks a draft without it.
JUDGED_KEY = "claim_judge"


def _open_question(f: dict[str, Any]) -> dict[str, Any]:
    """A novel question as the human sees it (MCP get_novel_questions): why no
    draft exists, and for a draft that failed the fact check, what failed."""
    out: dict[str, Any] = {"identity": f["identity"], "label": f["label"]}
    if f.get("decline_reason"):
        out["flag"] = f["decline_reason"]
    if f.get("provenance_violations"):
        out["fact_check"] = f["provenance_violations"]
    return out


# what the self-review is told it is reading (pipeline/self_review.py)
NOVEL_KIND = "drafted answer to an application-form question"


def draft_novel_answer(
    conn: sqlite3.Connection,
    *,
    prompt: llm.PromptFile,
    corpus: dict[str, Any],
    profile: str,
    job: dict[str, Any],
    label: str,
    question: str,
    max_words: str = "150",
    known_companies: Sequence[str] = (),
    estimate_style: str | None = None,
    review: self_review.Session | None = None,
    field_identity: str = "",
) -> tuple[str | None, str | None, list[dict[str, str]]]:
    """Draft one novel answer and fact-check it against the profile ids it
    cites (validate/claims.py — numbers with units, proper nouns, employer
    attribution, wrong company, never-claim lists). One regeneration with the
    violations fed back; an answer that still fails is NOT returned — the field
    stays open for the human (hard rule 8, fail closed). The prompt and the
    check use one estimate_style (None: the configured one, validate/style.py).
    An answer the rules pass is then read, with its question, by the LLM claim
    judge (validate/claim_judge.py); its "fail" or an unparseable reply is a
    fact-check failure like any rule's, and it never runs on a rule failure.

    With a self-review `review` session (pipeline/self_review.py), each draft
    is reviewed before it is stored, and the review is reported only: an
    answer is never revised. The answer that passed the fact check is what is
    stored (fifth review of the self-review: a rewrite could move a sentence
    to another employer, or build a claim from its cited bullets' words, and
    the fact check sees neither). A failing answer's violations feed a review
    that is reported only; the one regeneration carries the fact-check
    feedback alone, as before the self-review existed. If it fails, no answer
    is returned and the field stays open for the human.

    Returns (answer, decline_reason, violations)."""
    style = estimate_styles.resolve(estimate_style)
    jd_text = re.sub(r"<[^>]+>", " ", str(job.get("jd_html") or ""))
    key = f"novel:{job['id']}:{label[:80]}"
    feedback = ""
    violations: list[dict[str, str]] = []
    rlog = (
        review.log_for(f"novel:{field_identity or label[:80]}", f"Drafted answer: {label}")
        if review is not None
        else None
    )
    scope = (
        "The answer may cite any corpus or education id, must stay within "
        f"{max_words} words, and should decline rather than stretch."
    )

    def render(extra: str) -> str:
        return prompt.render(
            max_words=max_words,
            profile=profile,
            title=str(job["title"]),
            company=str(job["company_name"]),
            question=question + extra,
            estimate_style=style,
        )

    def fact_check(data: dict[str, Any]) -> ProvenanceResult:
        answer_body = str(data.get("answer") or "").strip()
        cited = coerce_ids(data.get("bullets_used"))
        rules = validate_claims(
            answer_body,
            cited,
            corpus,
            target_company=str(job["company_name"]),
            job_title=str(job["title"]),
            jd_text=jd_text,
            known_companies=known_companies,
            estimate_style=style,
            # "Yes." to "Have you previously worked for Fabrikam Payments?" names no
            # employer on its own: the check reads the question too (second
            # review 2026-10-09)
            question=f"{label}\n{question}",
        )
        # the claim judge reads an answer the rules passed, with its question,
        # and can only fail it (validate/claim_judge.py)
        return claim_judge.gate(
            conn,
            rules,
            answer_body,
            cited,
            corpus,
            target_company=str(job["company_name"]),
            job_title=str(job["title"]),
            artifact_kind=NOVEL_KIND,
            question=question if question.startswith(label) else f"{label}\n{question}",
        )

    def answer_text(data: dict[str, Any]) -> str:
        ids = ", ".join(coerce_ids(data.get("bullets_used"))) or "(none)"
        return f"FORM QUESTION: {label}\n\nANSWER:\n{data.get('answer', '')}\n\nBULLETS_USED: {ids}"

    for attempt in range(2):
        text = render(feedback)
        data = llm.call_json(conn, "novel", text, cache_key=key if attempt == 0 else None)
        answer = str(data.get("answer") or "").strip()
        if not answer:
            return None, str(data.get("decline_reason") or "declined"), []
        result = fact_check(data)
        if result.ok:
            if review is not None and rlog is not None:
                # reported only: the answer that passed is the one returned
                self_review.review_only(
                    conn,
                    review.ctx,
                    rlog,
                    review.max_rounds,
                    kind=NOVEL_KIND,
                    scope=scope,
                    draft=answer_text(data),
                    writer_version=prompt.version,
                )
            return answer, None, []
        violations = result.violations
        log(
            "novel.provenance_failed",
            job_id=job["id"],
            attempt=attempt,
            label=label[:80],
            violations=violations,
        )
        if attempt == 0:  # a failing draft must never be served from cache again
            llm.forget(conn, "novel", text, key)
        feedback = (
            "\n\nYOUR PREVIOUS DRAFT FAILED THE FACT CHECK. Violations: "
            f"{json.dumps(violations)}. Use only facts from the cited profile ids, "
            "write each number as the FIGURE STYLE rule says, cite every id you use, and decline "
            "if the profile does not support an answer."
        )
        if review is not None and rlog is not None:
            # the violations feed a self-review that is reported only: the
            # regeneration carries the feedback above and nothing from it
            self_review.review_only(
                conn,
                review.ctx,
                rlog,
                review.max_rounds,
                kind=NOVEL_KIND,
                scope=scope,
                draft=answer_text(data),
                violations=violations,
                writer_version=prompt.version,
            )
    return None, "unsourced-claim", violations


def _review_session(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    corpus: dict[str, Any],
    profile: str,
    filters: Filters | None,
) -> self_review.Session | None:
    """The self-review session for one job's drafted answers: the job's
    requirements, gap report and track; cached per JD hash and profile hash."""
    if filters is None:
        return None
    track = filters.track_for(str(row.get("title") or "")) or "other"
    ctx = self_review.context_for_job(
        conn, int(row["id"]), corpus, self_review.text_hash(profile), track
    )
    return self_review.Session(ctx, filters.self_review_max_rounds) if ctx else None


def _finish_review_session(conn: sqlite3.Connection, session: self_review.Session) -> None:
    """Events for the answers' self-reviews, and their part of the job's
    self_review.md: in the job's newest artifact folder, or a new one named
    the way tailoring names it, so tailoring later adds its artifacts beside."""
    logs = [lg for lg in session.logs if any(r.review.verdict != "error" for r in lg.rounds)]
    if not logs:
        return  # nothing was reviewed (no draft, or every review call failed: logged)
    from jobkit.pipeline import tailor

    ctx = session.ctx
    self_review.record(conn, ctx.job_id, logs)
    out = self_review.newest_job_folder(tailor.OUT_DIR, ctx.job_id) or tailor.out_dir_for(
        ctx.job_id, ctx.company, ctx.title, tailor._today()
    )
    try:
        self_review.write_report(out, ctx, logs, out_root=tailor.OUT_DIR)
    except OSError as e:  # the events above still carry the review
        log("self_review.report_failed", job_id=ctx.job_id, error=str(e)[:150])


def capture_form_specs(
    conn: sqlite3.Connection, limit: int | None = None, draft: bool = True
) -> dict[str, int]:
    weights = friction_weights()
    matcher = BankMatcher()
    from jobkit.pipeline import triage

    corpus: dict[str, Any] = {}
    profile = ""
    known_companies: list[str] = []
    style = estimate_styles.HEDGED
    filters: Filters | None = None
    if draft:
        with open(triage.CORPUS_PATH) as fh:
            corpus = yaml.safe_load(fh) or {}
        profile = drafting_profile(corpus)
        known_companies = [str(r[0]) for r in conn.execute(db.KNOWN_COMPANY_NAMES)]
        style = estimate_styles.configured_estimate_style()  # one style for the run
        filters = load_filters()  # role track + self_review.max_revision_rounds
    novel_prompt = llm.PromptFile("novel_question")
    judge_version = claim_judge.version() if draft else ""

    rows = [
        dict(r)
        for r in conn.execute(
            """SELECT j.id, j.external_id, j.title, j.company_name, j.jd_html, j.comp_max,
                  b.ats, b.token
           FROM jobs j JOIN boards b ON b.id = j.board_id
           LEFT JOIN form_specs fs ON fs.job_id = j.id
           WHERE j.status IN ('queued', 'approved') AND fs.job_id IS NULL
             AND j.closed_at IS NULL
           ORDER BY j.id LIMIT ?""",
            (limit or 10**9,),
        ).fetchall()
    ]
    # feed-discovered jobs have no board row, but their apply_url names the ATS
    from jobkit.discovery.url_resolver import resolve as resolve_url

    for r in conn.execute(
        """SELECT j.id, j.title, j.company_name, j.jd_html, j.comp_max, j.apply_url
           FROM jobs j LEFT JOIN form_specs fs ON fs.job_id = j.id
           WHERE j.status IN ('queued', 'approved') AND fs.job_id IS NULL
             AND j.board_id IS NULL AND j.closed_at IS NULL LIMIT ?""",
        (limit or 10**9,),
    ).fetchall():
        hit = resolve_url(str(r["apply_url"] or ""))
        if hit and hit.supported and hit.token and hit.external_id:
            rows.append(
                {
                    "id": r["id"],
                    "external_id": hit.external_id,
                    "title": r["title"],
                    "company_name": r["company_name"],
                    "jd_html": r["jd_html"],
                    "comp_max": r["comp_max"],
                    "ats": hit.ats,
                    "token": hit.token,
                }
            )
    stats = {
        "captured": 0,
        "dom_fallback": 0,
        "novel_total": 0,
        "drafted": 0,
        "fact_check_failed": 0,
        "posting_gone": 0,
    }
    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        for row in rows:
            fetcher = FETCHERS.get(row["ats"])
            fetched = fetcher(client, row["token"], row["external_id"]) if fetcher else None
            if isinstance(fetched, PostingGone):
                # a definite not-found: close the job, write no form spec, so
                # neither tailoring nor staging ever runs on a dead posting
                if close_gone_posting(conn, row, fetched):
                    stats["posting_gone"] += 1
                continue
            fields = fetched
            if fields is None:
                conn.execute(
                    """INSERT OR REPLACE INTO form_specs
                       (job_id, source, fields, field_count, required_count,
                        novel_questions, novel_question_count, drafted_answers,
                        has_assessment, friction_score, captured_at)
                       VALUES (?, 'dom_scrape', '[]', NULL, NULL, '[]', NULL, '{}',
                               NULL, 50, datetime('now'))""",
                    (row["id"],),
                )
                stats["dom_fallback"] += 1
                continue

            novel: list[dict[str, Any]] = []
            scaffold_by_id = {s["id"]: s for s in matcher.bank.get("scaffolds", [])}
            # salary expectations: the bank's one figure only for tracks it fits
            comp_block = comp_expectation_block_for(
                str(row["title"] or ""), row.get("comp_max"), matcher.bank
            )
            for f in fields:
                # the ATS field id rides along: system fields (Ashby's
                # _systemfield_name labelled plain "Name") match by id
                verdict = matcher.match(
                    f["label"] or f["identity"],
                    f["compliance_type"],
                    identity=f["identity"],
                    comp_expectation_block=comp_block,
                )
                f["match"] = verdict
                if verdict["kind"] == "novel":
                    novel.append(f)
                elif verdict["kind"] == "scaffold":
                    # scaffolds are skeletons, not answers — they go through the
                    # drafting path with the skeleton as explicit guidance
                    sc = scaffold_by_id.get(str(verdict.get("entry_id")), {})
                    f["_scaffold_skeleton"] = str(sc.get("skeleton", "")).strip()
                    f["_scaffold_max_words"] = int(sc.get("max_words", 150))
                    novel.append(f)

            drafted: dict[str, str] = {}
            # each drafted answer is self-reviewed before it is stored
            session = _review_session(conn, row, corpus, profile, filters) if draft else None
            if draft:
                for f in novel:
                    label = f["label"] or f["identity"]
                    blocked = never_draft(label)
                    if blocked:
                        f["match"] = {"kind": "blank_flag", "reason": f"never-draft:{blocked}"}
                        continue
                    if (
                        f["type"].lower()
                        not in (
                            "string",
                            "longtext",
                            "textarea",
                            "input_text",
                            "text",
                            "multi_value_single_select",
                        )
                        and not f["options"]
                    ):
                        continue
                    guidance = (
                        f"\n\nANSWER SHAPE GUIDANCE (from the user's own scaffold):\n"
                        f"{f['_scaffold_skeleton']}"
                        if f.get("_scaffold_skeleton")
                        else ""
                    )
                    try:
                        answer, decline, violations = draft_novel_answer(
                            conn,
                            prompt=novel_prompt,
                            corpus=corpus,
                            profile=profile,
                            job=row,
                            label=label,
                            question=label
                            + (f" (options: {f['options']})" if f["options"] else "")
                            + guidance,
                            max_words=str(f.get("_scaffold_max_words", 150)),
                            known_companies=known_companies,
                            estimate_style=style,
                            review=session,
                            field_identity=str(f["identity"]),
                        )
                        if answer is not None:
                            drafted[f["identity"]] = answer
                            # it passed the claim judge (draft_novel_answer);
                            # `jobkit rejudge` re-checks a draft without this
                            f[JUDGED_KEY] = judge_version
                            stats["drafted"] += 1
                        else:
                            f["decline_reason"] = decline
                        if violations:
                            # fail closed: the draft is NOT stored; the field stays a
                            # novel question and the human sees why
                            f["provenance_violations"] = violations[:10]
                            stats["fact_check_failed"] += 1
                            conn.execute(
                                db.RECORD_EVENT,
                                {
                                    "job_id": row["id"],
                                    "kind": "novel_provenance_failed",
                                    "detail": json.dumps(
                                        {
                                            "field": f["identity"],
                                            "label": label[:120],
                                            "violations": violations[:10],
                                        }
                                    ),
                                },
                            )
                    except Exception as e:  # noqa: BLE001 — a failed draft is a surfaced question
                        log("novel.draft_failed", job_id=row["id"], error=str(e)[:150])
            if session is not None:
                _finish_review_session(conn, session)

            has_assessment = bool(ASSESSMENT_PAT.search(row["jd_html"] or ""))
            required = sum(1 for f in fields if f["required"])
            still_novel = [
                f
                for f in fields
                if f.get("match", {}).get("kind") == "novel" and f["identity"] not in drafted
            ]
            fr = _friction(len(fields), required, len(still_novel), has_assessment, weights)
            conn.execute(
                """INSERT OR REPLACE INTO form_specs
                   (job_id, source, fields, field_count, required_count, novel_questions,
                    novel_question_count, drafted_answers, has_assessment, friction_score,
                    captured_at)
                   VALUES (?, 'api_questions', ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    row["id"],
                    json.dumps(fields),
                    len(fields),
                    required,
                    json.dumps([_open_question(f) for f in still_novel]),
                    len(still_novel),
                    json.dumps(drafted),
                    int(has_assessment),
                    fr,
                ),
            )
            conn.commit()
            stats["captured"] += 1
            stats["novel_total"] += len(still_novel)
            log(
                "form_spec.captured",
                job_id=row["id"],
                ats=row["ats"],
                fields=len(fields),
                novel=len(still_novel),
                friction=fr,
            )
    conn.commit()
    log("form_spec.done", **stats)
    return stats
