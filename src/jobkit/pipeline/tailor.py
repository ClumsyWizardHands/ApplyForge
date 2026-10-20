"""Stages 9-10 — tailor + cover letter (M4). Strong model SELECTS from the
corpus; the provenance validator gates every artifact; Typst renders one page.
A failing resume fails loudly — no PDF, violations logged (hard rule 3).

Pointed at the target roles (audit 2026-10-08 §2 item 6): the job's role track
(config.Filters.track_for) steers which bullets lead; a bullet variant that
drops a number its other variant carries is never used; the current role and
every role in the last few years (filters.yaml `resume.recent_role_years`) get
at least one bullet, topped up from the corpus in code, and older roles left
empty fold into one "Earlier experience" line. All of that is selection and
order: the code adds only corpus text. What the model renders is checked by
provenance (its numbers, proper nouns and never-claim terms) and by the drift
pass, which reverts a bullet that drifted far from its source; a first draft
may trim a bullet lightly, as at aca26cc.

Self-review (feature request 2026-10-09, pipeline/self_review.py): the bullet
selection and the cover letter are each reviewed by the model before anything
renders. A passing selection may be revised once (filters.yaml
`self_review.max_revision_rounds`); the revision must render every bullet as
its named variant's corpus text, word for word (_reworded), then runs the
same full check chain and is kept only if it passes, with an authenticity
verdict no worse than the selection it revises, else the original stands. The
cover letter is never revised: its review is reported only, and the letter
that passed the fact check and the critic ships (fifth review of the
self-review: rewritten prose cannot be checked against the corpus simply). A
failing draft's violations feed a review that is reported only; its
regeneration carries the same check feedback as at aca26cc and nothing from
the review. out/<job>/self_review.md records it.

Claim judge (package 4, 2026-10-10, validate/claim_judge.py): a letter the
rule-based fact check passed is read by an LLM judge for claims the rules
cannot see. Its "fail", or a reply that does not parse, is a fact-check
failure like any rule's: the same regeneration, the same cover_rejected.md.
It never runs on, and so never overrides, a rule-based rejection."""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobkit import db as q
from jobkit import llm
from jobkit.config import Filters, load_filters
from jobkit.log import log
from jobkit.normalize import MIN_JD_CHARS, jd_is_missing
from jobkit.paths import HOME as PROJECT_ROOT
from jobkit.pipeline import self_review
from jobkit.pipeline.triage import month_index, record_jd_missing
from jobkit.render.pdf import page_count, render_pdf, rendered_pages
from jobkit.validate import claim_judge
from jobkit.validate import style as estimate_styles
from jobkit.validate.authenticity import (
    AuthenticityReport,
    record_report,
    review_cover,
    review_resume,
)
from jobkit.validate.authenticity import load_config as load_authenticity_config
from jobkit.validate.claims import (
    coerce_ids,
    has_hard,
    labeled_sources_for_prompt,
    never_claim_for_prompt,
    validate_claims,
)
from jobkit.validate.provenance import (
    ProvenanceResult,
    canonical_skills,
    claim_keys,
    corpus_hash,
    figures_report,
    skill_index_keys,
    validate_tailored,
)

CORPUS_PATH = PROJECT_ROOT / "data" / "career_corpus.yaml"  # project root (jobkit/paths.py)
OUT_DIR = PROJECT_ROOT / "out"
# the files of a sendable cover letter in its job folder; none of them may
# remain once the letter fails the fact check (here, and pipeline/rejudge.py)
FINAL_COVER_FILES = ("cover.pdf", "cover.txt", "cover_critique.md")

# filters.yaml `resume.recent_role_years` when the key is absent
DEFAULT_RECENT_ROLE_YEARS = 5

# a fit-scoring gap that is overqualification never reaches the cover letter
# (prompts/score_fit.md lists it last, prefixed "overqualified:")
_OVERQUALIFIED = re.compile(r"(?i)^\s*over-?qualifi")

_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# US states for location_strategy `job_state` rules: full names (longest first,
# so "west virginia" is not read as "virginia") and postal codes.
_US_STATES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
}  # fmt: skip
_STATE_CODES = frozenset(_US_STATES.values())
_DC = re.compile(r"(?i)\bwashington,?\s*d\.?\s?c\b|\bdistrict of columbia\b")


def _fmt_date(ym: str | None) -> str:
    if not ym:
        return "Present"
    y, m = ym.split("-")
    return f"{_MONTHS[int(m)]} {y}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def out_dir_for(job_id: int, company: str, title: str, day: datetime.date) -> Path:
    """One artifact folder per job: the job id is in the name, so duplicate
    postings (same company, title and day) no longer overwrite each other."""
    return OUT_DIR / f"{_slug(company)}-{_slug(title)}-{job_id}-{day.isoformat()}"


def _load_corpus() -> tuple[dict[str, Any], str]:
    text = CORPUS_PATH.read_text()
    return yaml.safe_load(text), corpus_hash(text)


def figures(bullet_id: str = "", rendered: str = "") -> str:
    """CLI `jobkit figures` / MCP `figures`: the configured estimate_style
    (config/banned_phrases.yaml drift.estimate_style), what it allows, and the
    provenance check under it — of every corpus bullet's own text, of one
    bullet's text (`bullet_id`), or of one rendering of it (`bullet_id` +
    `rendered`). Read-only: no LLM call, no DB."""
    try:
        corpus, _chash = _load_corpus()
    except OSError as e:
        return f"no corpus at {CORPUS_PATH}: {e}"
    return figures_report(corpus, bullet_id=bullet_id.strip(), rendered=rendered.strip())


def _today() -> datetime.date:
    """Today's date; one seam so tests are not time bombs."""
    return datetime.date.today()


def _recent_role_years(filters: Filters) -> int:
    resume = filters.raw.get("resume") or {}
    return int(resume.get("recent_role_years", DEFAULT_RECENT_ROLE_YEARS))


def required_role_ids(corpus: dict[str, Any], today: datetime.date, years: int) -> set[str]:
    """The roles a resume must show at least one bullet for: the current role
    (no end date) and every role that ended within the last `years` years."""
    cutoff = today.year * 12 + today.month - 1 - years * 12
    out: set[str] = set()
    for role in corpus.get("roles") or []:
        end = role.get("end")
        idx = month_index(end) if end else None
        if not end or (idx is not None and idx >= cutoff):
            out.add(str(role["id"]))
    return out


def _variant_text(bullet: dict[str, Any], variant: str) -> str:
    return str(bullet.get(f"text_{variant}") or "").strip()


def metric_safe_variant(bullet: dict[str, Any], preferred: str) -> str:
    """`preferred`, unless it drops a number (value + unit) that the bullet's
    other variant carries while carrying nothing the other lacks — then the
    other variant (audit 2026-10-08: b_058's long variant lost "124% and then 131%
    of annual quota"). When each variant has a number the other lacks, neither
    can be preferred and `preferred` stands."""
    preferred = preferred if preferred in ("long", "short") else "short"
    other = "short" if preferred == "long" else "long"
    mine = claim_keys(_variant_text(bullet, preferred))
    theirs = claim_keys(_variant_text(bullet, other))
    if _variant_text(bullet, other) and theirs - mine and not mine - theirs:
        return other
    return preferred


def _claims_absent_from(rendered: str, replacement: str) -> list[str]:
    """The numbers (value + unit) `rendered` asserts that `replacement` lacks."""
    return sorted(
        f"{kind}:{value:g}" for kind, value in claim_keys(rendered) - claim_keys(replacement)
    )


def keep_metrics(
    selected: list[dict[str, Any]], corpus: dict[str, Any], *, estimate_style: str | None = None
) -> list[dict[str, str]]:
    """Mutates `selected`: swaps any variant that drops a number its other
    variant has (metric_safe_variant), and restores the source text of a
    rendered bullet that trimmed away one of its own variant's numbers. Each
    change is made only when the replacement source text passes provenance on
    its own. Returns the changes, for provenance.json.

    Never a laundering step (hard rule 3): a rendering that fails provenance
    itself, or that asserts a number its replacement does not carry, is left
    exactly as the model wrote it — overwriting it with corpus text would erase
    a fabrication with no violation recorded. tailor_job validates the model's
    selection before calling this; the guard holds for any other caller too.
    `estimate_style` None means the configured one (validate/style.py)."""
    style = estimate_styles.resolve(estimate_style)
    bullets = {b["id"]: b for b in corpus.get("bullets") or []}
    changes: list[dict[str, str]] = []
    for item in selected:
        b = bullets.get(item.get("bullet_id"))
        if b is None:
            continue  # provenance reports the unknown id
        rendered = str(item.get("rendered") or "")
        chosen = str(item.get("variant") or "short")
        safe = metric_safe_variant(b, chosen)
        source = _variant_text(b, chosen)
        wants_swap = safe != chosen
        wants_restore = bool(claim_keys(source) - claim_keys(rendered))
        if not (wants_swap or wants_restore):
            continue
        if not validate_tailored([item], corpus, estimate_style=style).ok:
            log("tailor.metric_keep_skipped", bullet_id=b["id"], reason="fails provenance")
            continue  # left for the validator to reject
        if wants_swap:
            swap_text = _variant_text(b, safe)
            extra = _claims_absent_from(rendered, swap_text)
            cand = {**item, "variant": safe, "rendered": swap_text}
            if not extra and validate_tailored([cand], corpus, estimate_style=style).ok:
                item.update(cand)
                changes.append({"bullet_id": str(b["id"]), "action": f"variant {chosen}->{safe}"})
                continue
            if extra:
                log("tailor.metric_keep_skipped", bullet_id=b["id"], reason="swap", claims=extra)
            else:
                log("tailor.metric_variant_unsafe", bullet_id=b["id"], variant=safe)
        if wants_restore:
            extra = _claims_absent_from(rendered, source)
            if extra:
                log("tailor.metric_keep_skipped", bullet_id=b["id"], reason="restore", claims=extra)
                continue
            if validate_tailored([{**item, "rendered": source}], corpus, estimate_style=style).ok:
                item["rendered"] = source
                changes.append({"bullet_id": str(b["id"]), "action": "metric restored"})
    return changes


def top_up_required_roles(
    selected: list[dict[str, Any]],
    corpus: dict[str, Any],
    required: set[str],
    *,
    estimate_style: str | None = None,
) -> list[str]:
    """Append one corpus bullet for each required role the selection left
    empty: the role's first bullet (corpus order is the human's own order)
    whose metric-safe variant passes provenance as written. Returns the ids
    added. A required role with no usable bullet is logged and stays empty.
    `estimate_style` None means the configured one (validate/style.py)."""
    style = estimate_styles.resolve(estimate_style)
    bullets = {b["id"]: b for b in corpus.get("bullets") or []}
    covered = {
        bullets[i["bullet_id"]]["role_id"] for i in selected if i.get("bullet_id") in bullets
    }
    added: list[str] = []
    for role in corpus.get("roles") or []:
        rid = str(role["id"])
        if rid not in required or rid in covered:
            continue
        for b in (b for b in corpus.get("bullets") or [] if b.get("role_id") == rid):
            variant = metric_safe_variant(b, "short")
            item = {"bullet_id": b["id"], "variant": variant, "rendered": _variant_text(b, variant)}
            if validate_tailored([item], corpus, estimate_style=style).ok:
                selected.append(item)
                added.append(str(b["id"]))
                break
        else:
            log("tailor.required_role_uncovered", role_id=rid)
    return added


def _post_process(
    selected: list[dict[str, Any]], corpus: dict[str, Any], required: set[str], style: str
) -> dict[str, Any]:
    changes = keep_metrics(selected, corpus, estimate_style=style)
    added = top_up_required_roles(selected, corpus, required, estimate_style=style)
    return {"metric_changes": changes, "topped_up": added}


def _corpus_for_prompt(corpus: dict[str, Any], required: set[str] | None = None) -> str:
    required = required or set()
    role_lines = []
    for r in corpus.get("roles") or []:
        role_lines.append(
            f"- {r['id']} | {r.get('company', '')} · {r.get('title', '')} · "
            f"{_fmt_date(r.get('start'))} – {_fmt_date(r.get('end'))}"
            + (" | REQUIRED: at least one bullet" if str(r["id"]) in required else "")
        )
    lines = []
    for b in corpus["bullets"]:
        lines.append(
            f"- id:{b['id']} role:{b['role_id']} conf:{b['metric_confidence']}\n"
            f"  long: {b['text_long'].strip()}\n  short: {b['text_short'].strip()}"
        )
    pos = [f"- {p['id']} ({p['archetype']}): {p['text'].strip()}" for p in corpus["positioning"]]
    return (
        "ROLES (reverse-chronological):\n"
        + "\n".join(role_lines)
        + "\n\nBULLETS:\n"
        + "\n".join(lines)
        + "\n\nPOSITIONING VARIANTS:\n"
        + "\n".join(pos)
        + f"\n\nNEVER CLAIM: {never_claim_for_prompt(corpus)}"
        + f"\nSKILL INDEX (skills_line may use ONLY these exact strings): "
        f"{skill_index_keys(corpus)}"
    )


def _jd_plain(job: sqlite3.Row) -> str:
    """The job description as plain text — the only source a letter may draw
    company facts (numbers, names) from."""
    if job["jd_markdown"]:
        return str(job["jd_markdown"])
    return re.sub(r"<[^>]+>", " ", str(job["jd_html"] or ""))


def job_state(location_raw: str | None) -> str | None:
    """The US state a job location names, as a postal code, or None. A full
    state name anywhere ("Jersey City, New Jersey"), else an upper-case postal
    code token ("Newark, NJ", "Remote (NJ)")."""
    raw = location_raw or ""
    if _DC.search(raw):
        return "DC"
    low = raw.lower()
    for name in sorted(_US_STATES, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
            return _US_STATES[name]
    for m in re.finditer(r"(?<![A-Za-z])[A-Z]{2}(?![A-Za-z])", raw):
        if m.group(0) in _STATE_CODES:
            return m.group(0)
    return None


def _is_remote(job: sqlite3.Row | dict[str, Any]) -> bool:
    return job["remote"] == 1 or str(job["location_bucket"] or "").startswith("remote")


def _rule_matches(when: dict[str, Any], job: sqlite3.Row | dict[str, Any]) -> bool:
    """Every condition in a location_strategy rule's `when` must hold. An
    unknown condition never matches (logged), so a typo cannot fire a rule."""
    loc = str(job["location_raw"] or "")
    for key, want in when.items():
        if key == "job_state":
            states = {str(s).upper() for s in (want if isinstance(want, list) else [want])}
            if job_state(loc) not in states:
                return False
        elif key == "job_city_in":
            cities = [str(c) for c in (want if isinstance(want, list) else [want])]
            if not any(
                re.search(rf"(?<![a-z]){re.escape(c.lower())}(?![a-z])", loc.lower())
                for c in cities
            ):
                return False
        elif key == "remote":
            if _is_remote(job) != bool(want):
                return False
        else:
            log("tailor.location_rule_unknown_condition", condition=key)
            return False
    return True


def _display_location(
    job: sqlite3.Row | dict[str, Any], filters: Filters, corpus: dict[str, Any]
) -> str:
    """The resume/letter header location, driven only by config: the first
    filters.yaml `location_strategy.rules` entry whose `when` matches the job,
    else `location_strategy.default`, else the corpus identity.display_location.
    No city is hardcoded here."""
    strat = filters.raw.get("location_strategy") or {}
    for rule in strat.get("rules") or []:
        when, use = (rule or {}).get("when"), (rule or {}).get("use")
        if isinstance(when, dict) and when and use and _rule_matches(when, job):
            return str(use)
    fallback = strat.get("default") or (corpus.get("identity") or {}).get("display_location")
    if not fallback:
        raise ValueError(
            "no display location: set location_strategy.default in config/filters.yaml "
            "or identity.display_location in the corpus"
        )
    return str(fallback)


def _resume_data(
    corpus: dict[str, Any],
    selection: dict[str, Any],
    display_location: str,
    required: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    ident = corpus["identity"]
    pos_text = next(
        (
            p["text"].strip()
            for p in corpus["positioning"]
            if p["id"] == selection.get("positioning_id")
        ),
        corpus["positioning"][0]["text"].strip(),
    )
    by_role: dict[str, list[str]] = {}
    bullets = {b["id"]: b for b in corpus["bullets"]}
    for item in selection["selected"]:
        b = bullets[item["bullet_id"]]
        by_role.setdefault(b["role_id"], []).append(str(item["rendered"]).strip())
    roles = []
    earlier = []  # roles shown on one "Earlier experience" line, never as empty headers
    for role in corpus["roles"]:
        rb = by_role.get(role["id"], [])
        entry = {
            "company": role["company"],
            "title": role["title"],
            "dates": f"{_fmt_date(role['start'])} – {_fmt_date(role.get('end'))}",
        }
        if rb or role["id"] in required:
            roles.append({**entry, "bullets": rb})
        else:
            earlier.append(entry)
    return {
        "identity": {
            "name": ident["name"],
            "display_location": display_location,
            "phone": ident["phone"],
            "email": ident["email"],
            "linkedin": ident["links"]["linkedin"],
        },
        "positioning": pos_text,
        "skills": list(selection.get("skills_line") or [])[:12],
        "roles": roles,
        "earlier": earlier,
        "education": [
            {"school": e["school"], "degree": e.get("degree") or "", "years": e["years"]}
            for e in corpus.get("education", [])
        ],
    }


def _shrink(
    selection: dict[str, Any],
    corpus: dict[str, Any],
    required: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """One-page budget enforcement: first switch to short every bullet whose
    short variant keeps all its numbers, then drop the last bullet whose role
    keeps another bullet or is not required. Returns False when nothing is
    left to shrink."""
    bullets = {b["id"]: b for b in corpus["bullets"]}
    selected = selection["selected"]
    changed = False
    for item in selected:
        b = bullets[item["bullet_id"]]
        if item.get("variant") != "short" and metric_safe_variant(b, "short") == "short":
            item["variant"] = "short"
            item["rendered"] = _variant_text(b, "short")
            changed = True
    if changed:
        return True
    if len(selected) > 6:
        per_role = Counter(bullets[i["bullet_id"]]["role_id"] for i in selected)
        for idx in range(len(selected) - 1, -1, -1):
            rid = bullets[selected[idx]["bullet_id"]]["role_id"]
            if rid not in required or per_role[rid] > 1:
                selected.pop(idx)
                return True
    return False


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _prior_letters(job_id: int) -> list[str]:
    """The candidate's earlier final letters (out/*/cover.txt), newest first,
    up to banned_phrases.yaml `cross_letter.lookback_letters`. This job's own
    folders (any date) are left out: a re-tailor is not a repeat. Rejected
    drafts (cover_rejected.md) are not letters."""
    cfg = load_authenticity_config().get("cross_letter") or {}
    if not cfg.get("enabled", True):
        return []
    limit = int(cfg.get("lookback_letters", 50))
    own = re.compile(rf"-{job_id}-\d{{4}}-\d{{2}}-\d{{2}}$")
    try:
        paths = [p for p in OUT_DIR.glob("*/cover.txt") if not own.search(p.parent.name)]
    except OSError:
        return []
    paths.sort(key=_mtime, reverse=True)
    letters: list[str] = []
    for p in paths[:limit]:
        try:
            letters.append(p.read_text())
        except OSError:
            continue
    return letters


class JDMissingError(RuntimeError):
    """The job's description is too short to tailor against (normalize.MIN_JD_CHARS)."""


def _attempt_key(
    jd_hash: object,
    chash: str,
    tailor_version: str,
    cover_version: str,
    style: str,
    review: str = "",
    judge: str = "",
) -> str:
    """Everything a tailoring run's letter depends on, the estimate_style the
    fact check ran under, the self-review (its prompt versions and the
    max_revision_rounds knob, as `review`) and the claim judge's prompt
    version (`judge`) included. A job whose cover failed is not re-tailored
    automatically until one of these changes. With no `judge` this is the key
    recorded before the claim judge existed (_unchanged_since_cover_failure)."""
    judged = f"cj{judge}:" if judge else ""
    return f"{jd_hash}:{chash}:{tailor_version}:{cover_version}:{review}:{judged}{style}"


def _review_tag(max_rounds: int) -> str:
    """The self-review part of the attempt key."""
    return f"sr{self_review.versions()}x{max_rounds}"


# ---- what the self-review sees of each artifact (pipeline/self_review.py) ----

RESUME_KIND = "resume bullet selection"
RESUME_SCOPE = (
    "The resume may draw on any bullet in the corpus; the current role and recent roles "
    "keep at least one bullet each."
)
COVER_KIND = "cover letter"


def _cover_scope(selected_ids: list[str]) -> str:
    return (
        "The letter may cite ONLY the bullets on this resume: "
        f"{', '.join(selected_ids) or '(none)'}. Never ask it to use any other bullet."
    )


def _bullet_ids(selection: dict[str, Any]) -> list[str]:
    return [str(s.get("bullet_id")) for s in selection.get("selected") or [] if isinstance(s, dict)]


def _selection_text(selection: dict[str, Any], corpus: dict[str, Any]) -> str:
    """A selection as the self-review reads it: each bullet with its employer."""
    bullets = {b["id"]: b for b in corpus.get("bullets") or []}
    roles = {r["id"]: r for r in corpus.get("roles") or []}
    lines = [f"POSITIONING: {selection.get('positioning_id')}", "SELECTED BULLETS (in order):"]
    for s in selection.get("selected") or []:
        if not isinstance(s, dict):
            continue
        b = bullets.get(s.get("bullet_id"))
        role = roles.get(b.get("role_id")) if b else None
        label = f"{role.get('company')} · {role.get('title')}" if role else "unknown bullet"
        lines.append(f"- {s.get('bullet_id')} [{label}] ({s.get('variant')}): {s.get('rendered')}")
    skills = selection.get("skills_line") or []
    lines.append(f"SKILLS LINE: {', '.join(str(x) for x in skills) or '(none)'}")
    return "\n".join(lines)


def _selection_json(selection: dict[str, Any]) -> str:
    keys = ("positioning_id", "selected", "ordering_note", "skills_line")
    return json.dumps({k: selection[k] for k in keys if k in selection}, indent=1)


def _letter_text(cover: dict[str, Any]) -> str:
    ids = ", ".join(coerce_ids(cover.get("bullets_used"))) or "(none)"
    return f"{cover.get('letter', '')}\n\nBULLETS_USED: {ids}"


def _authenticity_failure(rep: AuthenticityReport) -> list[dict[str, str]]:
    """An authenticity verdict as a violation list (for the self-review)."""
    out = [
        {"kind": "authenticity", "detail": str(f)} for f in rep.lexical_flags + rep.structural_flags
    ]
    out += [
        {"kind": "authenticity_figures", "detail": "; ".join(f["figures"])}
        for f in rep.drift_flags
        if f.get("figures")
    ]
    if rep.critic_notes:
        out.append({"kind": "critic", "detail": rep.critic_notes[:400]})
    return out or [{"kind": "authenticity", "detail": f"verdict {rep.verdict}"}]


def _reworded(selected: object, corpus: dict[str, Any]) -> list[dict[str, str]]:
    """Each bullet in `selected` that does not render its named variant's
    corpus text word for word, as violations. A self-review revision must have
    none (sixth review of the self-review, 2026-10-09; Hard Rules 3, 8).
    Provenance checks a bullet's numbers, proper nouns and never-claim terms,
    not its every word, and the drift pass only flags a new word: a revision
    that rendered b_052's own text plus "I also managed the sales development
    team." passed both. A first draft may still trim a bullet lightly
    (prompts/select_bullets.md), as at aca26cc."""
    items = selected if isinstance(selected, list) else [selected]
    bullets = {b["id"]: b for b in corpus.get("bullets") or []}
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            detail = f"not a bullet: {str(item)[:80]}"
            out.append({"bullet_id": "", "kind": "revision_reworded", "detail": detail})
            continue
        bid = str(item.get("bullet_id") or "")
        variant = str(item.get("variant") or "")
        b = bullets.get(bid)
        source = _variant_text(b, variant) if b and variant in ("long", "short") else ""
        if not source or str(item.get("rendered") or "").strip() != source:
            out.append(
                {
                    "bullet_id": bid,
                    "kind": "revision_reworded",
                    "detail": f"{bid or '(no id)'} is not its {variant or '(no)'} variant's "
                    "corpus text word for word",
                }
            )
    return out


_VERDICT_RANK = {"pass": 0, "soft_flag": 1, "fail": 2}


def _no_worse(revised: str, original: str) -> bool:
    """A revision may replace a draft only with a verdict no worse than its own."""
    return _VERDICT_RANK.get(revised, 2) <= _VERDICT_RANK.get(original, 2)


@dataclass
class _SelectionCheck:
    ok: bool
    kind: str = ""  # "" | "provenance" | "authenticity" | "post_revert"
    failure: Any = None
    adjustments: dict[str, Any] = field(default_factory=dict)
    rep: AuthenticityReport | None = None


def _check_selection(
    conn: sqlite3.Connection,
    job_id: int,
    raw: dict[str, Any],
    corpus: dict[str, Any],
    required: set[str],
    style: str,
    skills_indexed: bool,
) -> _SelectionCheck:
    """The resume's full check chain, the same for a first draft, a
    regeneration and a self-review revision. Mutates `raw`.

    Provenance gates the model's own output first (hard rule 3): a fabricated
    number fails here before the metric-keeping step could overwrite it with
    corpus text. Then the deterministic adjustments (metric-safe variants, one
    corpus bullet per empty required role), each corpus text checked on its
    own, and the whole selection again; authenticity 10b, whose drift pass may
    revert bullets to corpus text; and provenance once more on what will
    actually render (corpus text that fails, e.g. under estimate_style
    "hedged" an unhedged estimate in the source itself, fails loudly)."""
    skills = list(raw.get("skills_line") or []) if skills_indexed else None
    raw["selected"] = list(raw.get("selected") or [])
    result = validate_tailored(raw["selected"], corpus, skills_line=skills, estimate_style=style)
    if not raw["selected"]:  # an empty resume is not a passing one
        result.ok = False
        result.violations.append(
            {"bullet_id": "", "kind": "empty_selection", "detail": "no bullets"}
        )
    adjustments: dict[str, Any] = {}
    if result.ok:
        adjustments = _post_process(raw["selected"], corpus, required, style)
        if any(adjustments.values()):
            log("tailor.selection_adjusted", job_id=job_id, **adjustments)
            result = validate_tailored(
                raw["selected"], corpus, skills_line=skills, estimate_style=style
            )
    if not result.ok:
        return _SelectionCheck(False, "provenance", result.violations, adjustments)
    rep = review_resume(conn, job_id, raw["selected"], corpus)
    if rep.verdict == "fail":
        failure = {
            "authenticity": rep.lexical_flags,
            "figures": [f["figures"] for f in rep.drift_flags if f.get("figures")],
            "critic": rep.critic_notes,
        }
        return _SelectionCheck(False, "authenticity", failure, adjustments, rep)
    raw["skills_line"] = canonical_skills(skills or [], corpus)
    recheck = validate_tailored(raw["selected"], corpus, skills_line=None, estimate_style=style)
    if not recheck.ok:
        return _SelectionCheck(False, "post_revert", recheck.violations, adjustments, rep)
    return _SelectionCheck(True, "", None, adjustments, rep)


def _record_reviews(
    conn: sqlite3.Connection, job_id: int, logs: list[self_review.ArtifactLog]
) -> None:
    """Events for these self-reviews, committed now (the caller may raise)."""
    self_review.record(conn, job_id, logs)
    conn.commit()


def tailor_job(conn: sqlite3.Connection, job_id: int) -> dict[str, str]:
    job = conn.execute(
        """SELECT j.*, r.must_have, r.nice_to_have, r.implied, r.jd_markdown, sc.gaps
           FROM jobs j
           LEFT JOIN job_requirements r ON r.job_id = j.id
           LEFT JOIN scores sc ON sc.job_id = j.id WHERE j.id = ?""",
        (job_id,),
    ).fetchone()
    if job is None:
        raise ValueError(f"no job {job_id}")
    if jd_is_missing(job["jd_html"]):
        # Refuse before any LLM call: with no JD the letter is written from the
        # title alone and can describe some other company's job (audit §2.2). The
        # job keeps its human-approved status; the rejection row and the raised
        # error say why no artifacts exist.
        record_jd_missing(conn, job_id, "tailor", job["jd_html"], reject_job=False)
        conn.commit()
        raise JDMissingError(
            f"job {job_id}: description under {MIN_JD_CHARS} characters — refusing to "
            "tailor (rejection logged: layer=sanity, reason=jd_missing)"
        )
    corpus, chash = _load_corpus()
    filters = load_filters()
    # one estimate_style for the whole run: the prompts and every check agree
    style = estimate_styles.configured_estimate_style()
    track = filters.track_for(str(job["title"] or "")) or "other"
    required = required_role_ids(corpus, _today(), _recent_role_years(filters))
    reqs = {k: json.loads(job[k] or "[]") for k in ("must_have", "nice_to_have", "implied")}
    sel_prompt = llm.PromptFile("select_bullets")
    # caller key = namespace; llm.call hashes it with the model and full prompt
    tailor_key = f"tailor:{job['jd_hash']}:{chash}:{sel_prompt.version}"
    # the skills line may only repeat skill_index keys; with no index there is
    # nothing to check it against, so it is left off the resume (fail closed)
    skills_indexed = bool(skill_index_keys(corpus))
    if not skills_indexed:
        log("tailor.skills_omitted", job_id=job_id, reason="corpus skill_index is empty")

    # ---- self-review context: the same for both artifacts of this job ----
    gaps = json.loads(job["gaps"] or "[]")
    max_rounds = filters.self_review_max_rounds
    review_ctx = self_review.ReviewContext(
        job_id=job_id,
        company=str(job["company_name"]),
        title=str(job["title"]),
        track=track,
        requirements={k: [str(x) for x in v] for k, v in reqs.items()},
        gaps=[str(g) for g in gaps],
        corpus=corpus,
        cache_ns=f"{job['jd_hash']}:{chash}",
    )
    resume_log = self_review.ArtifactLog("resume", "Resume bullet selection")

    def render_selection(extra: str) -> str:
        return sel_prompt.render(
            title=job["title"],
            company=job["company_name"],
            track=track,
            requirements=json.dumps(reqs, indent=1) + extra,
            corpus=_corpus_for_prompt(corpus, required),
            estimate_style=style,
        )

    selection: dict[str, Any] | None = None
    kept = _SelectionCheck(False)
    feedback = ""
    failure: object = None
    for attempt in range(2):
        sel_text = render_selection(feedback)
        raw = llm.call_json(
            conn, "tailor", sel_text, cache_key=tailor_key if attempt == 0 else None
        )
        chk = _check_selection(conn, job_id, raw, corpus, required, style, skills_indexed)
        if chk.ok:
            selection, kept = raw, chk
            break
        failure = chk.failure
        if chk.kind == "post_revert":
            # the drift check reverted bullets to corpus text that itself fails
            _record_reviews(conn, job_id, [resume_log])
            raise RuntimeError(f"post-revert provenance failure: {failure}")
        if chk.kind == "authenticity":
            log("resume.authenticity_failed", job_id=job_id, attempt=attempt, detail=failure)
            feedback = (
                "\n\nYOUR PREVIOUS SELECTION FAILED AUTHENTICITY REVIEW: "
                f"{json.dumps(failure)}. Choose and render bullets closer to the source."
            )
        else:
            log("provenance.failed", job_id=job_id, attempt=attempt, violations=failure)
            feedback = (
                "\n\nYOUR PREVIOUS ATTEMPT FAILED PROVENANCE VALIDATION. "
                f"Violations: {json.dumps(failure)}. Use ONLY source words, "
                "write every number as the FIGURE STYLE rule says, and take skills_line "
                "entries ONLY from the SKILL INDEX, spelled exactly."
            )
        if attempt == 0:  # a failing selection must never be served from cache again
            llm.forget(conn, "tailor", sel_text, tailor_key)
        # the violations feed a self-review that is reported only: nothing it
        # says reaches the next attempt, which carries the feedback above alone
        self_review.review_only(
            conn,
            review_ctx,
            resume_log,
            max_rounds,
            kind=RESUME_KIND,
            scope=RESUME_SCOPE,
            draft=_selection_text(raw, corpus),
            violations=failure,
            writer_version=sel_prompt.version,
        )
    if selection is None:
        _record_reviews(conn, job_id, [resume_log])
        raise RuntimeError(f"resume failed twice for job {job_id}: {failure}")

    # ---- self-review of the passing selection: revise, keep only if it passes ----
    revision_key = f"tailor-revision:{job['jd_hash']}:{chash}:{sel_prompt.version}"
    revision_text = {"text": ""}
    last: dict[str, _SelectionCheck] = {}

    def revise_selection(block: str) -> dict[str, Any]:
        revision_text["text"] = render_selection(block)
        return llm.call_json(conn, "tailor", revision_text["text"], cache_key=revision_key)

    def check_selection(
        candidate: dict[str, Any], _previous: dict[str, Any]
    ) -> tuple[bool, object]:
        last.pop("chk", None)
        # A revision may choose, order and swap corpus bullets, never reword
        # one: every bullet the writer returned is its named variant's corpus
        # text, word for word, before anything adjusts it (_reworded)
        reworded = _reworded(candidate.get("selected") or [], corpus)
        if reworded:
            return False, reworded
        last["chk"] = chk = _check_selection(
            conn, job_id, candidate, corpus, required, style, skills_indexed
        )
        if not chk.ok:
            return False, chk.failure
        # …and so is what the chain left to render, with no new word flagged
        after = _reworded(candidate["selected"], corpus) + [
            {
                "bullet_id": str(f.get("bullet_id")),
                "kind": "drift_new_words",
                "detail": f"{f.get('bullet_id')} has words its source lacks: "
                + ", ".join(str(w) for w in f["new_words"]),
            }
            for f in (chk.rep.drift_flags if chk.rep else [])
            if f.get("new_words")
        ]
        if after:
            return False, after
        # a revision never ships a worse authenticity verdict than the
        # selection it would replace
        before = last.get("kept", kept).rep
        if chk.rep is None or before is None or not _no_worse(chk.rep.verdict, before.verdict):
            got = chk.rep.verdict if chk.rep else "none"
            was = before.verdict if before else "none"
            return False, (_authenticity_failure(chk.rep) if chk.rep else []) + [
                {"kind": "authenticity_verdict", "detail": f"{got} (the original was {was})"}
            ]
        last["kept"] = chk
        return True, None

    def discard_selection(_candidate: dict[str, Any]) -> None:
        llm.forget(conn, "tailor", revision_text["text"], revision_key)
        checked = last.get("chk")
        shipped = last.get("kept", kept).rep
        if checked is not None and checked.rep is not None and shipped is not None:
            # the revision's review was recorded: the newest row must be what ships
            record_report(conn, job_id, "resume", shipped)

    selection = self_review.improve(
        conn,
        review_ctx,
        resume_log,
        max_rounds,
        selection,
        kind=RESUME_KIND,
        scope=RESUME_SCOPE,
        writer_version=sel_prompt.version,
        render=lambda s: _selection_text(s, corpus),
        render_for_writer=_selection_json,
        ids_of=_bullet_ids,
        revise=revise_selection,
        check=check_selection,
        on_discard=discard_selection,
    )
    kept = last.get("kept", kept)
    adjustments = kept.adjustments
    _record_reviews(conn, job_id, [resume_log])

    # every path above ran the post-revert recheck; this one guards the
    # selection that will actually be rendered, whatever produced it
    recheck = validate_tailored(
        selection["selected"], corpus, skills_line=None, estimate_style=style
    )
    if not recheck.ok:
        raise RuntimeError(f"post-revert provenance failure: {recheck.violations}")

    out = out_dir_for(job_id, str(job["company_name"]), str(job["title"]), _today())
    out.mkdir(parents=True, exist_ok=True)

    # ---- resume: render, enforce one page ----
    display_loc = _display_location(job, filters, corpus)
    resume_path = out / "resume.pdf"
    while True:
        resume_data = _resume_data(corpus, selection, display_loc, required)
        render_pdf("resume.typ", resume_data, resume_path)
        try:
            pages = page_count(resume_path)
        except ValueError:  # a PDF we cannot read: typst's own layout decides
            pages = rendered_pages("resume.typ", resume_data)
        if pages <= 1:
            break
        if not _shrink(selection, corpus, required):
            raise RuntimeError(f"cannot fit resume on one page for job {job_id}")
        # shrinking swaps in short variants: what renders must pass provenance too
        shrunk = validate_tailored(selection["selected"], corpus, estimate_style=style)
        if not shrunk.ok:
            resume_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"one-page shrink produced bullets that fail provenance for job {job_id}: "
                f"{shrunk.violations}"
            )

    # ---- cover letter: only the selected bullets, labeled; every claim checked ----
    cover_prompt = llm.PromptFile("cover_letter")
    # P3 closes on a win; a gap reaches the letter only as context, and only
    # when it is a stated must-have — overqualification never does
    letter_gaps = [str(g) for g in gaps if not _OVERQUALIFIED.match(str(g))]
    must_haves = [str(m) for m in reqs.get("must_have") or []]
    selected_ids = [str(s["bullet_id"]) for s in selection["selected"]]
    bullets_ctx = labeled_sources_for_prompt(corpus, selected_ids)
    voice = "\n\n---\n\n".join(v["text"].strip() for v in corpus.get("voice_samples", []))
    jd_text = _jd_plain(job)
    known_companies = [str(r[0]) for r in conn.execute(q.KNOWN_COMPANY_NAMES)]
    prior_letters = _prior_letters(job_id)
    cover_key = f"cover:{job['jd_hash']}:{chash}:{cover_prompt.version}"
    run_key = _attempt_key(
        job["jd_hash"],
        chash,
        sel_prompt.version,
        cover_prompt.version,
        style,
        _review_tag(max_rounds),
        judge=claim_judge.version(),
    )
    cover_log = self_review.ArtifactLog("cover_letter", "Cover letter")
    cover_scope = _cover_scope(selected_ids)

    def render_cover(extra: str) -> str:
        return cover_prompt.render(
            title=job["title"],
            company=job["company_name"],
            must_haves="; ".join(must_haves) or "(none extracted)",
            gaps="; ".join(letter_gaps) or "none identified",
            bullets=bullets_ctx,
            skills=", ".join(skill_index_keys(corpus)) or "(none)",
            never_claim=never_claim_for_prompt(corpus),
            voice=voice,
            jd=(job["jd_markdown"] or "")[:8000],
            feedback=extra,
            estimate_style=style,
        )

    def fact_check(draft: dict[str, Any]) -> ProvenanceResult:
        letter_text = str(draft.get("letter", ""))
        cited = coerce_ids(draft.get("bullets_used"))
        rules = validate_claims(
            letter_text,
            cited,
            corpus,
            target_company=job["company_name"],
            job_title=job["title"],
            jd_text=jd_text,
            allowed_ids=selected_ids,
            known_companies=known_companies,
            estimate_style=style,
        )
        # the claim judge reads a letter the rules passed, and can only fail
        # it: its verdict is a fact-check failure like any rule's
        # (validate/claim_judge.py)
        return claim_judge.gate(
            conn,
            rules,
            letter_text,
            cited,
            corpus,
            target_company=str(job["company_name"]),
            job_title=str(job["title"]),
            artifact_kind=COVER_KIND,
        )

    def authenticity(draft: dict[str, Any]) -> AuthenticityReport:
        return review_cover(
            conn,
            job_id,
            str(draft.get("letter", "")),
            job["company_name"],
            prior_letters=prior_letters,
            corpus=corpus,
            jd=jd_text,
            cited=coerce_ids(draft.get("bullets_used")),
        )

    critique = ""  # the feedback for the next attempt (as at aca26cc)
    cover: dict[str, Any] = {}
    cover_rep = None
    cover_prov = None
    for attempt in range(3):  # initial + up to 2 regenerations (SPEC 10b)
        cover_text = render_cover(critique)
        cover = llm.call_json(
            conn, "cover", cover_text, cache_key=cover_key if attempt == 0 else None
        )
        # fact check first (free, deterministic); a letter that fails it never
        # reaches the critic and is never written as final
        cover_prov = fact_check(cover)
        cover_rep = authenticity(cover) if cover_prov.ok else None
        if cover_rep is not None and cover_rep.verdict != "fail":
            break
        if cover_rep is not None:
            violations: list[dict[str, str]] = _authenticity_failure(cover_rep)
            critique = (
                "\n\nYOUR PREVIOUS DRAFT FAILED AUTHENTICITY REVIEW. Specific "
                f"problems: {cover_rep.lexical_flags + cover_rep.structural_flags} "
                f"{cover_rep.critic_notes}. Rewrite avoiding these exactly."
            )
        else:
            violations = cover_prov.violations
            log(
                "cover.provenance_failed",
                job_id=job_id,
                attempt=attempt,
                hard=has_hard(cover_prov),
                violations=cover_prov.violations,
            )
            critique = (
                "\n\nYOUR PREVIOUS DRAFT FAILED THE FACT CHECK. Violations: "
                f"{json.dumps(cover_prov.violations)}. State only facts from the candidate "
                "facts above, with each number written as the FIGURE STYLE rule says, list "
                "every bullet id you drew on in bullets_used, attach each fact only to the "
                "employer its bullet is labeled with, and name no company other than "
                f"{job['company_name']} and the candidate's own employers."
            )
        if attempt == 0:  # only the first draft is cached; never serve a failed one again
            llm.forget(conn, "cover", cover_text, cover_key)
        log("cover.regenerating", job_id=job_id, attempt=attempt)
        # the violations feed a self-review that is reported only: its changes
        # never reach a regeneration (fifth review of the self-review: a change
        # list could endorse a fabricated sentence the fact check cannot see)
        self_review.review_only(
            conn,
            review_ctx,
            cover_log,
            max_rounds,
            kind=COVER_KIND,
            scope=cover_scope,
            draft=_letter_text(cover),
            violations=violations,
            writer_version=cover_prompt.version,
        )
    assert cover_prov is not None

    # ---- self-review of a passing letter: reported only, never revised ----
    # The letter that passed the fact check and the critic is what ships. A
    # rewrite could move a sentence to another employer, or build a claim from
    # its cited bullets' words, and no check sees either (fifth review of the
    # self-review, 2026-10-09; Hard Rules 3, 8).
    if cover_rep is not None and cover_rep.verdict != "fail":
        self_review.review_only(
            conn,
            review_ctx,
            cover_log,
            max_rounds,
            kind=COVER_KIND,
            scope=cover_scope,
            draft=_letter_text(cover),
            writer_version=cover_prompt.version,
        )
    _record_reviews(conn, job_id, [cover_log])
    assert cover_prov is not None
    letter = str(cover.get("letter", ""))
    cover_path: Path | None = out / "cover.pdf"
    rejected_path: Path | None = None
    if not cover_prov.ok:
        # fail closed: an unsupported claim is never rendered as a sendable letter.
        # The last draft is kept only inside a clearly-labeled review file.
        cover_path = None
        rejected_path = out / "cover_rejected.md"
        for stale in FINAL_COVER_FILES:
            (out / stale).unlink(missing_ok=True)
        rejected_path.write_text(
            "# Cover letter REJECTED by the fact check — do not send\n\n"
            "Every draft stated something the cited corpus bullets do not support.\n\n"
            + (
                "Violations of kind `claim_judge_…` are the claim judge's reasons: it "
                "reads a letter only after the rule-based check passed it.\n\n"
                if any(v.get("kind") in claim_judge.KINDS for v in cover_prov.violations)
                else ""
            )
            + "## Violations\n\n"
            + "\n".join(
                f"- **{v['kind']}**: {v['detail']}"
                + (f' — in: "{v["sentence"]}"' if v.get("sentence") else "")
                for v in cover_prov.violations
            )
            + "\n\n## Last draft (NOT FOR SENDING)\n\n"
            + letter
            + "\n"
        )
        conn.execute(
            q.RECORD_EVENT,
            {
                "job_id": job_id,
                "kind": "cover_provenance_failed",
                "detail": json.dumps(
                    {
                        "file": str(rejected_path),
                        "attempt": run_key,
                        "violations": cover_prov.violations[:12],
                    }
                ),
            },
        )
        log("cover.rejected_final", job_id=job_id, hard=has_hard(cover_prov))
    else:
        assert cover_rep is not None
        if cover_rep.verdict == "fail":
            critique_path = out / "cover_critique.md"
            critique_path.write_text(
                "# Cover letter failed authenticity after 2 regenerations\n\n"
                f"flags: {cover_rep.lexical_flags + cover_rep.structural_flags}\n\n"
                f"critic: {cover_rep.critic_notes}\n\n## Last draft\n\n" + letter
            )
            conn.execute(
                q.RECORD_EVENT,
                {
                    "job_id": job_id,
                    "kind": "cover_authenticity_failed",
                    "detail": json.dumps({"file": str(critique_path), "attempt": run_key}),
                },
            )
            log("cover.failed_final", job_id=job_id)
        assert cover_path is not None
        render_pdf(
            "cover_letter.typ",
            {
                "identity": {
                    "name": corpus["identity"]["name"],
                    "display_location": display_loc,
                    "phone": corpus["identity"]["phone"],
                    "email": corpus["identity"]["email"],
                },
                "company": job["company_name"],
                "title": job["title"],
                "date": f"{_today():%B} {_today().day}, {_today().year}",  # no zero-pad
                "body": letter,
            },
            cover_path,
        )
        (out / "cover.txt").write_text(letter)

    # ---- self-review report: what it asked itself, what changed, questions ----
    review_path = self_review.write_report(
        out, review_ctx, [resume_log, cover_log], out_root=OUT_DIR
    )

    # ---- gap report + provenance ----
    gap_path = out / "gap_report.md"
    gap_path.write_text(
        f"# Gap report — {job['company_name']} · {job['title']}\n\n"
        + "\n".join(f"- {g}" for g in gaps or ["none identified"])
        + "\n"
    )
    prov_path = out / "provenance.json"
    prov_path.write_text(
        json.dumps(
            {
                "job_id": job_id,
                "corpus_hash": chash,
                "tailor_prompt": sel_prompt.version,
                "cover_prompt": cover_prompt.version,
                "claim_judge_prompt": claim_judge.version(),
                "positioning_id": selection.get("positioning_id"),
                "display_location": display_loc,
                "track": track,
                "required_roles": sorted(required),
                "selection_adjustments": adjustments,
                "earlier_experience": [
                    f"{e['title']}, {e['company']}" for e in resume_data["earlier"]
                ],
                "bullets": [
                    {
                        "bullet_id": s["bullet_id"],
                        "variant": s.get("variant"),
                        "rendered": s["rendered"],
                    }
                    for s in selection["selected"]
                ],
                "skills_line": list(selection.get("skills_line") or []),
                "skills_line_source": (
                    "corpus skill_index keys"
                    if skills_indexed
                    else "omitted: corpus skill_index is empty"
                ),
                "cover_bullets_used": coerce_ids(cover.get("bullets_used")),
                "cover_fact_check": {
                    "ok": cover_prov.ok,
                    "violations": cover_prov.violations,
                },
                "self_review": {
                    "prompts": self_review.versions(),
                    "max_revision_rounds": max_rounds,
                    "outcomes": {
                        lg.artifact: [r.outcome for r in lg.rounds]
                        for lg in (resume_log, cover_log)
                    },
                    "report": str(review_path),
                },
                "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            },
            indent=1,
        )
    )

    if not conn.execute("SELECT 1 FROM applications WHERE job_id=?", (job_id,)).fetchone():
        conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'approved')", (job_id,))
    # a cover that failed authenticity twice is rendered for HUMAN review, and one
    # that failed the fact check is not rendered at all; in both cases the
    # application does NOT advance to 'tailored' — staging only picks up 'tailored'
    cover_failed = not cover_prov.ok or (cover_rep is not None and cover_rep.verdict == "fail")
    final_stage = "approved" if cover_failed else "tailored"
    # a submitted application keeps its stage and the paths of what was sent:
    # re-tailoring it (tailor --job-id, MCP retailor) used to set it back to
    # 'tailored' (track/applications.py, 2026-10-15)
    updated = conn.execute(
        """UPDATE applications SET stage=?, resume_path=?, cover_path=?,
           gap_report_path=?, provenance_path=?, updated_at=datetime('now')
           WHERE job_id=? AND id=(SELECT MAX(id) FROM applications WHERE job_id=?)
             AND submitted_at IS NULL""",
        (
            final_stage,
            str(resume_path),
            str(cover_path) if cover_path else None,
            str(gap_path),
            str(prov_path),
            job_id,
            job_id,
        ),
    ).rowcount
    if not updated:
        log("tailor.application_already_submitted", job_id=job_id, out=str(out))
        conn.execute(
            "INSERT INTO events (job_id, kind, detail) VALUES (?, 'retailored_after_submit', ?)",
            (job_id, str(out)),
        )
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'tailored', ?)", (job_id, str(out))
    )
    conn.commit()
    log("tailor.done", job_id=job_id, out=str(out), bullets=len(selection["selected"]))
    paths = {
        "resume": str(resume_path),
        "cover": str(cover_path) if cover_path else "",
        "gap_report": str(gap_path),
        "provenance": str(prov_path),
        "self_review": str(review_path),
    }
    if rejected_path is not None:
        paths["cover_rejected"] = str(rejected_path)
    return paths


def _unchanged_since_cover_failure(
    conn: sqlite3.Connection, job_id: int, jd_hash: object, current: dict[str, str]
) -> bool:
    """True when the job's last tailoring ended in a cover failure (fact check,
    or authenticity twice) and nothing it depends on has changed since. Without
    this, every `jobkit run` re-tailored every such job from scratch: three
    uncached strong-model cover calls plus a critic call, each time (review of
    wp2). `current` is filled lazily with the corpus hash and prompt versions."""
    ev = conn.execute(q.LAST_COVER_FAILURE_FOR_JOB, {"job_id": job_id}).fetchone()
    if ev is None:
        return False
    try:
        recorded = json.loads(ev["detail"] or "{}").get("attempt")
    except (ValueError, AttributeError):
        return False
    if not recorded:
        return False  # recorded before attempt keys existed: retry once, then it has one
    if not current:
        try:
            current["chash"] = corpus_hash(CORPUS_PATH.read_text())
            current["style"] = estimate_styles.configured_estimate_style()
            current["review"] = _review_tag(load_filters().self_review_max_rounds)
        except (OSError, ValueError):
            return False  # no corpus or a bad config: let tailor_job fail loudly as before
        current["tailor"] = llm.PromptFile("select_bullets").version
        current["cover"] = llm.PromptFile("cover_letter").version
        current["judge"] = claim_judge.version()
    inputs = (
        jd_hash,
        current["chash"],
        current["tailor"],
        current["cover"],
        current["style"],
        current["review"],
    )
    # A failure recorded before the claim judge existed carries no judge
    # version. It still stands: the judge can only add a rejection to the
    # checks that failed it, so the same inputs would fail again. A failure
    # recorded with a judge version is retried when that version changes.
    return recorded in (_attempt_key(*inputs, judge=current["judge"]), _attempt_key(*inputs))


def tailor_approved(conn: sqlite3.Connection, limit: int | None = None) -> int:
    """Tailor every approved job that has no artifacts yet, or whose last cover
    failure is out of date. A job whose cover letter failed with the current
    JD, corpus and prompts is skipped (logged as tailor.skipped_cover_failed):
    edit the corpus, or retry it by hand with MCP `retailor(n)` /
    `jobkit tailor --job-id N`."""
    rows = conn.execute(
        """SELECT DISTINCT j.id, j.jd_hash FROM jobs j JOIN applications a ON a.job_id = j.id
           WHERE j.status = 'approved' AND a.stage = 'approved'
           ORDER BY j.id LIMIT ?""",
        (limit or 10**9,),
    ).fetchall()
    done = 0
    current: dict[str, str] = {}
    for r in rows:
        if _unchanged_since_cover_failure(conn, r["id"], r["jd_hash"], current):
            log("tailor.skipped_cover_failed", job_id=r["id"])
            continue
        try:
            tailor_job(conn, r["id"])
            done += 1
        except JDMissingError:
            continue  # skipped, already logged as jd_missing.refused + rejection row
        except Exception as e:  # noqa: BLE001 — one failure must not block the batch
            log("tailor.job_failed", job_id=r["id"], error=str(e)[:300])
    return done
