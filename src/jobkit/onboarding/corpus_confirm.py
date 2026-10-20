"""Phase 1.3, 1.5, 1.6 — confirm the corpus draft with the human, then
promote it to data/career_corpus.yaml (ONBOARDING.md). MCP:
corpus_draft_status, confirm_bullets, update_corpus_draft, promote_corpus.

The human decides every entry. For each bullet: keep as-is, edit the wording
(their words), add the number, or drop it — and set `metric_confidence`
(verified | estimate | none) and, for any bullet with a figure, `evidence`.
Those two fields are never defaulted (hard rule 6): confirm_bullets refuses a
kept bullet without them, and promote_corpus refuses while any bullet is
unconfirmed. "A figure" is any number the bullet's final text_long or
text_short states (re-scanned on every decision and again at promotion) or
its `metric` holds: such a bullet cannot be `none` and needs evidence, and
`metric` cannot be cleared while the wording still states one. An edit
without `metric` takes the figures its new wording states.

Company, title and dates of each role, the identity block (display location
and home city are asked, never read off the resume) and education are
confirmed the same way. A role is current (end null: "– Present" on the
resume, tenure counted to today) only when the resume said so or the human
answered current: true; a role with a year-only or missing end is not
confirmed until the human gives the end month or says it is current. The
display location is a city, never empty: every tailored header shows it.

The resume critique stays in its own file. Text reaches the corpus only as
resume text the human kept or words the human gave."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobkit.onboarding.files import (
    GENERATED_MARK,
    SetupError,
    SetupFiles,
    backup,
    dump_yaml,
    guard_overwrite,
    load_yaml,
    setup_files,
    today,
    write_text_atomic,
)
from jobkit.onboarding.ingest import DRAFT_HEADER, metric_of, tag_slug

CONFIDENCES = ("verified", "estimate", "none")
ACTIONS = ("keep", "edit", "drop")
DECISION_FIELDS = frozenset(
    {
        "id",
        "action",
        "text_long",
        "text_short",
        "metric",
        "metric_confidence",
        "evidence",
        "tags",
        "role_id",
    }
)
THIN_BULLETS = 30
THIN_VERIFIED = 0.40
_YM = re.compile(r"^(?:19|20)\d{2}-(?:0[1-9]|1[0-2])$")


def load_draft(files: SetupFiles) -> dict[str, Any]:
    draft = load_yaml(files.corpus_draft)
    if not isinstance(draft, dict):
        raise SetupError(
            f"no corpus draft at {files.rel(files.corpus_draft)}: read the resume first "
            "(ingest_resume, or `jobkit setup ingest <file>`)"
        )
    for key in ("roles", "bullets", "education", "positioning", "voice_samples", "stories"):
        draft.setdefault(key, [])
    draft.setdefault("identity", {})
    draft.setdefault("skill_index", {})
    draft.setdefault("constraints", {})
    return draft


def save_draft(files: SetupFiles, draft: dict[str, Any]) -> None:
    write_text_atomic(files.corpus_draft, DRAFT_HEADER + "\n" + dump_yaml(draft))


def _roles_by_id(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(r.get("id")): r for r in draft["roles"]}


def _role_label(role: dict[str, Any] | None) -> str:
    if not role:
        return "?"
    return f"{role.get('title') or '?'} at {role.get('company') or '?'}"


def text_figures(*texts: Any) -> list[str]:
    """The figures these texts state (ingest.metric_of), in order, each once."""
    out: list[str] = []
    for text in texts:
        found = metric_of(text) if isinstance(text, str) else None
        for fig in (found or "").split("; "):
            if fig and fig not in out:
                out.append(fig)
    return out


def stated_figures(bullet: dict[str, Any]) -> list[str]:
    """Every figure the bullet would put in the corpus: the figures its
    text_long and text_short state, re-scanned from the text every time, plus
    its `metric` field. The `metric` field alone is not trusted: an edit that
    adds a number, or a keep that clears `metric`, leaves the figure in the
    text, and provenance treats a figure in corpus text as sourced."""
    figs = text_figures(bullet.get("text_long"), bullet.get("text_short"))
    for part in str(bullet.get("metric") or "").split(";"):
        part = part.strip()
        if part and part not in figs:
            figs.append(part)
    return figs


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SetupError(f"{what} must be non-empty text")
    return " ".join(value.split())


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def promotion_blockers(draft: dict[str, Any]) -> list[str]:
    """Everything that stops the draft becoming the corpus, in the order to
    fix it. Empty when it is ready."""
    out: list[str] = []
    ident = draft.get("identity") or {}
    if not ident.get("confirmed"):
        out.append("identity not confirmed (name, email, phone, links as you want them shown)")
    if not str(ident.get("name") or "").strip():
        out.append("identity.name is empty")
    if not str(ident.get("display_location") or "").strip():
        out.append(
            "identity.display_location not set: ask what city the resume and cover-letter "
            "header should show (a positioning choice, not necessarily where you live; "
            "required, because every tailored header shows one)"
        )
    if not [p for p in draft.get("positioning") or [] if str(p.get("text") or "").strip()]:
        out.append(
            "no positioning line: the resume summary, or one sentence in the human's own words"
        )
    for p in draft.get("positioning") or []:
        if not p.get("confirmed"):
            out.append(f"positioning {p.get('id')} not confirmed")
    roles = _roles_by_id(draft)
    for r in draft["roles"]:
        if not r.get("confirmed"):
            out.append(f"role {r.get('id')} ({_role_label(r)}) not confirmed")
        else:
            for k in ("company", "title"):
                if not str(r.get(k) or "").strip():
                    out.append(f"role {r.get('id')}: {k} is empty")
            if not _YM.match(str(r.get("start") or "")):
                out.append(f"role {r.get('id')}: start must be YYYY-MM")
            # end null means a current role downstream ("– Present", tenure to
            # today): only the human's explicit current=true may leave it null
            if r.get("current") is True:
                if r.get("end") is not None:
                    out.append(
                        f"role {r.get('id')}: marked current but ends {r.get('end')}: which is it?"
                    )
            elif r.get("end") is None:
                out.append(
                    f"role {r.get('id')} ({_role_label(r)}): end date not set; ask whether it is "
                    "current (end YYYY-MM, or current: true)"
                )
            elif not _YM.match(str(r.get("end"))):
                out.append(f"role {r.get('id')}: end must be YYYY-MM")
    for i, e in enumerate(draft.get("education") or []):
        if not e.get("confirmed"):
            out.append(f"education[{i}] ({e.get('school') or e.get('lines')}) not confirmed")
        elif not str(e.get("school") or "").strip():
            out.append(f"education[{i}]: school is empty")
    pending = [b for b in draft["bullets"] if b.get("status") in (None, "pending")]
    if pending:
        out.append(f"{len(pending)} bullet(s) not confirmed yet ({pending[0]['id']} first)")
    kept = [b for b in draft["bullets"] if b.get("status") in ("kept", "edited")]
    for b in kept:
        conf = b.get("metric_confidence")
        figs = stated_figures(b)
        if conf not in CONFIDENCES:
            out.append(f"bullet {b['id']}: metric_confidence not set")
        elif figs and conf == "none":
            out.append(
                f"bullet {b['id']}: states a figure ({'; '.join(figs)}) but metric_confidence "
                "is none: mark it verified or estimate, edit the figure out, or drop the bullet"
            )
        if not str(b.get("evidence") or "").strip():
            if figs:
                out.append(
                    f"bullet {b['id']}: states a figure ({'; '.join(figs)}) but has no evidence"
                )
            elif conf in ("verified", "estimate"):
                out.append(f"bullet {b['id']}: marked {conf} but has no evidence")
        if str(b.get("role_id")) not in roles:
            out.append(f"bullet {b['id']}: role {b.get('role_id')} does not exist")
    if not kept and not pending:
        out.append("no bullets kept")
    return out


@dataclass
class DraftStatus:
    files: SetupFiles
    draft: dict[str, Any]
    batch: list[dict[str, Any]] = field(default_factory=list)

    def render(self) -> str:
        d = self.draft
        counts = Counter(str(b.get("status") or "pending") for b in d["bullets"])
        roles = _roles_by_id(d)
        lines = [
            f"Corpus draft {self.files.rel(self.files.corpus_draft)} (from "
            f"{(d.get('source') or {}).get('file', '?')}): {len(d['bullets'])} bullets — "
            + ", ".join(
                f"{counts.get(k, 0)} {k}" for k in ("pending", "kept", "edited", "dropped")
            ),
        ]
        ident = d.get("identity") or {}
        if not ident.get("confirmed") or not str(ident.get("display_location") or "").strip():
            lines.append(
                f"Identity to confirm: name={ident.get('name')!r} email={ident.get('email')!r} "
                f"phone={ident.get('phone')!r} links={ident.get('links')!r}; "
                f"location on resume={ident.get('location_on_resume')!r}. Ask: what city "
                "should the resume header show (display_location), and where do you live "
                "(home_city)?"
            )
        todo_roles = [r for r in d["roles"] if not r.get("confirmed")]
        if todo_roles:
            lines.append("Roles to confirm (company and title exactly as they should print):")
            for r in todo_roles:
                note = f" [{'; '.join(r.get('notes') or [])}]" if r.get("notes") else ""
                lines.append(
                    f"  {r['id']}: {_role_label(r)} · {r.get('dates') or 'no dates'} → "
                    f"start={r.get('start')} end={r.get('end')} current={r.get('current')}{note}"
                )
        todo_edu = [
            (i, e) for i, e in enumerate(d.get("education") or []) if not e.get("confirmed")
        ]
        if todo_edu:
            lines.append("Education to confirm:")
            lines += [
                f"  [{i}] school={e.get('school')!r} degree={e.get('degree')!r} "
                f"years={e.get('years')!r}"
                for i, e in todo_edu
            ]
        todo_pos = [p for p in d.get("positioning") or [] if not p.get("confirmed")]
        for p in todo_pos:
            lines.append(
                f"Positioning to confirm: {p.get('id')} {p.get('text')!r} (archetype: "
                f"{p.get('archetype')!r}; ask what kind of role this line is for)"
            )
        if self.batch:
            lines.append(
                f"Next {len(self.batch)} bullets. For each: keep / edit (their words) / add "
                "the number / drop, plus metric_confidence (verified | estimate | none) and "
                "evidence (where it is defensible from; required for any figure):"
            )
            for b in self.batch:
                lines.append(
                    f"  {b['id']} [{_role_label(roles.get(str(b.get('role_id'))))}] "
                    f"{b['text_long']}"
                )
                if b.get("metric"):
                    lines.append(f"      figures: {b['metric']}")
                if b.get("tags"):
                    lines.append(f"      suggested tags: {', '.join(b['tags'])}")
        blockers = promotion_blockers(d)
        if blockers:
            lines.append(f"Before promote_corpus: {len(blockers)} item(s) open.")
        else:
            lines.append("Ready: promote_corpus writes data/career_corpus.yaml.")
        return "\n".join(lines)


def draft_status(batch_size: int = 5, *, root: Path | None = None) -> DraftStatus:
    files = setup_files(root)
    draft = load_draft(files)
    pending = [b for b in draft["bullets"] if b.get("status") in (None, "pending")]
    return DraftStatus(files=files, draft=draft, batch=pending[: max(1, batch_size)])


# ---------------------------------------------------------------------------
# bullets
# ---------------------------------------------------------------------------


def _check_decision(
    d: Any, bullets: dict[str, dict[str, Any]], roles: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """(errors, the changes to apply) for one decision."""
    if not isinstance(d, dict):
        return [f"each decision must be an object, got {d!r}"], {}
    bid = str(d.get("id") or "")
    errs: list[str] = []
    if bid not in bullets:
        return [f"unknown bullet id {bid!r}"], {}
    unknown = sorted(set(d) - DECISION_FIELDS)
    if unknown:
        errs.append(f"{bid}: unknown field(s) {unknown}")
    action = str(d.get("action") or "")
    if action not in ACTIONS:
        return errs + [f"{bid}: action must be one of {', '.join(ACTIONS)}"], {}
    if action == "drop":
        return errs, {"status": "dropped"}
    b = bullets[bid]
    change: dict[str, Any] = {"status": "kept" if action == "keep" else "edited"}
    if action == "keep" and "text_long" in d:
        errs.append(f"{bid}: keep does not change the wording; use action edit with text_long")
    if action == "edit":
        try:
            change["text_long"] = _text(d.get("text_long"), f"{bid}: text_long")
        except SetupError as e:
            errs.append(str(e))
    if "text_short" in d:
        try:
            change["text_short"] = _text(d.get("text_short"), f"{bid}: text_short")
        except SetupError as e:
            errs.append(str(e))
    elif "text_long" in change:
        change["text_short"] = change["text_long"]
    # The figure guard reads the wording this decision leaves, not only the
    # `metric` field: a number added by an edit, or kept in the text while
    # `metric` is cleared, still reaches the corpus (and provenance counts a
    # figure in corpus text as sourced).
    final_long = change.get("text_long", b.get("text_long"))
    final_short = change.get("text_short", b.get("text_short"))
    in_text = text_figures(final_long, final_short)
    if "metric" in d:
        m = d.get("metric")
        if m is not None and (not isinstance(m, str | int | float) or not str(m).strip()):
            errs.append(f"{bid}: metric must be the figure as text, or null to clear it")
        elif m is None and in_text:
            errs.append(
                f"{bid}: metric cannot be cleared while the wording states figures "
                f"({'; '.join(in_text)}): edit them out of text_long (and text_short), or "
                "keep the metric"
            )
        change["metric"] = None if m is None else str(m).strip()
    elif action == "edit":
        # the figures the new wording states, never the replaced wording's
        change["metric"] = "; ".join(in_text) or None
    figures = stated_figures(
        {
            "text_long": final_long,
            "text_short": final_short,
            "metric": change.get("metric", b.get("metric")),
        }
    )
    conf = d.get("metric_confidence")
    if conf not in CONFIDENCES:
        errs.append(
            f"{bid}: metric_confidence must be set by the human: verified (you can point at a "
            "source) | estimate (directionally right) | none (no figure to stand behind)"
        )
    else:
        change["metric_confidence"] = conf
    if figures and conf == "none":
        errs.append(
            f"{bid}: the bullet states a figure ({'; '.join(figures)}); mark it verified or "
            "estimate, edit the figure out, or drop the bullet"
        )
    evidence = d.get("evidence")
    if evidence is not None and not isinstance(evidence, str):
        errs.append(f"{bid}: evidence must be text")
    evidence_text = " ".join(str(evidence or "").split())
    if figures and not evidence_text:
        errs.append(
            f"{bid}: evidence is required for a figure ({'; '.join(figures)}): where is it "
            "defensible from (a dashboard, a document, a person who would confirm it)?"
        )
    elif conf in ("verified", "estimate") and not evidence_text:
        errs.append(
            f"{bid}: evidence is required for {conf}: where is it defensible from (a "
            "dashboard, a document, a person who would confirm it)?"
        )
    change["evidence"] = evidence_text or None
    if "tags" in d:
        tags = d.get("tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            errs.append(f"{bid}: tags must be a list of words")
        else:
            change["tags"] = list(dict.fromkeys(s for s in (tag_slug(t) for t in tags) if s))
    if "role_id" in d:
        if str(d.get("role_id")) not in roles:
            errs.append(f"{bid}: role_id {d.get('role_id')!r} is not a role in the draft")
        else:
            change["role_id"] = str(d.get("role_id"))
    return errs, change


def confirm_bullets(
    decisions: list[dict[str, Any]], *, root: Path | None = None, batch_size: int = 5
) -> str:
    """Apply the human's decisions. All or nothing: one invalid decision and
    none is applied, with every problem listed."""
    files = setup_files(root)
    draft = load_draft(files)
    if not isinstance(decisions, list) or not decisions:
        raise SetupError("decisions must be a non-empty list")
    bullets = {str(b.get("id")): b for b in draft["bullets"]}
    roles = _roles_by_id(draft)
    errors: list[str] = []
    changes: list[tuple[str, dict[str, Any]]] = []
    for d in decisions:
        errs, change = _check_decision(d, bullets, roles)
        errors += errs
        if not errs:
            changes.append((str(d["id"]), change))
    if errors:
        raise SetupError("nothing applied:\n" + "\n".join(f"- {e}" for e in errors))
    for bid, change in changes:
        b = bullets[bid]
        if "text_long" in change and "original_text" not in b:
            b["original_text"] = b.get("text_long")
        b.update(change)
    save_draft(files, draft)
    counts = Counter(c["status"] for _, c in changes)
    head = "applied: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    return head + "\n" + draft_status(batch_size, root=files.root).render()


# ---------------------------------------------------------------------------
# identity, roles, education, positioning, skills, constraints, voice
# ---------------------------------------------------------------------------

UPDATE_KEYS = frozenset(
    {
        "identity",
        "roles",
        "add_roles",
        "education",
        "add_education",
        "positioning",
        "skill_index",
        "constraints",
        "voice_samples",
    }
)
_IDENTITY_FIELDS = (
    "name",
    "email",
    "phone",
    "display_location",
    "home_city",
    "linkedin",
    "github",
    "site",
)
_ROLE_FIELDS = ("company", "title", "location", "start", "end", "current", "drop")
_CONSTRAINT_KEYS = ("never_claim_titles", "never_claim_tools", "never_claim_phrases")


def _ym(value: Any, what: str, allow_none: bool) -> str | None:
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "present")):
        if allow_none:
            return None
        raise SetupError(f"{what} is required (YYYY-MM)")
    s = str(value).strip()
    if not _YM.match(s):
        raise SetupError(f"{what} must be YYYY-MM (e.g. 2021-03), got {value!r}")
    return s


_END_ASK = "the end month (YYYY-MM), or current: true for a role the person still holds"


def _role_end(item: dict[str, Any], what: str) -> tuple[str | None, bool] | None:
    """(end, current) from the human's answer, or None when the answer says
    nothing about the end. A null end makes a role current everywhere
    downstream ("– Present" on the resume, tenure counted to today), so it
    comes only from an explicit current: true (or end "present"), never from
    a missing or null end."""
    raw = item.get("end")
    present = isinstance(raw, str) and raw.strip().lower() == "present"
    has_end = raw is not None and str(raw).strip() != "" and not present
    current = item.get("current")
    if current not in (None, True, False):
        raise SetupError(f"{what}.current must be true or false")
    if current is True or present:
        if has_end:
            raise SetupError(f"{what}: current: true and end {raw!r} contradict; which is it?")
        return None, True
    if has_end:
        return _ym(raw, f"{what}.end", allow_none=False), False
    if "end" in item or current is False:
        raise SetupError(f"{what}: no end date given: ask for {_END_ASK}")
    return None


def _apply_identity(draft: dict[str, Any], data: Any) -> None:
    if not isinstance(data, dict):
        raise SetupError("identity must be an object")
    unknown = sorted(set(data) - set(_IDENTITY_FIELDS) - {"confirm"})
    if unknown:
        raise SetupError(f"identity: unknown field(s) {unknown}")
    ident = draft["identity"]
    links = ident.setdefault("links", {"linkedin": "", "github": "", "site": ""})
    for k in _IDENTITY_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if v is not None and not isinstance(v, str):
            raise SetupError(f"identity.{k} must be text")
        v = " ".join(v.split()) if isinstance(v, str) else v
        if k == "display_location" and not v:
            # every tailored resume and letter header shows it: an empty one
            # promotes cleanly and then fails every tailor run
            raise SetupError(
                "identity.display_location must be a city (e.g. 'Columbus, OH'): the resume "
                "and cover-letter header show it. Ask which city it should be; it can differ "
                "from home_city"
            )
        if k in ("linkedin", "github", "site"):
            links[k] = v or ""
        else:
            ident[k] = v
    if data.get("confirm", True):
        if not str(ident.get("name") or "").strip():
            raise SetupError("identity.name is empty: what name should the resume show?")
        ident["confirmed"] = True


def _apply_roles(draft: dict[str, Any], data: Any) -> None:
    if not isinstance(data, list):
        raise SetupError("roles must be a list of {id, ...}")
    roles = _roles_by_id(draft)
    for item in data:
        if not isinstance(item, dict) or str(item.get("id")) not in roles:
            raise SetupError(f"roles: unknown role {item!r} (ids: {sorted(roles)})")
        unknown = sorted(set(item) - set(_ROLE_FIELDS) - {"id"})
        if unknown:
            raise SetupError(f"roles[{item['id']}]: unknown field(s) {unknown}")
        role = roles[str(item["id"])]
        if item.get("drop"):
            if any(
                b.get("role_id") == role["id"] and b.get("status") in ("kept", "edited")
                for b in draft["bullets"]
            ):
                raise SetupError(f"role {role['id']} has kept bullets; drop or move them first")
            draft["roles"] = [r for r in draft["roles"] if r is not role]
            for b in draft["bullets"]:
                if b.get("role_id") == role["id"]:
                    b["status"] = "dropped"
            continue
        for k in ("company", "title", "location"):
            if k in item:
                role[k] = _text(item[k], f"roles[{role['id']}].{k}") if item[k] else None
        if "start" in item:
            role["start"] = _ym(item["start"], f"roles[{role['id']}].start", allow_none=False)
        end = _role_end(item, f"roles[{role['id']}]")
        if end is not None:
            role["end"], role["current"] = end
        for k in ("company", "title"):
            if not str(role.get(k) or "").strip():
                raise SetupError(f"roles[{role['id']}]: {k} is empty; what should it say?")
        _ym(role.get("start"), f"roles[{role['id']}].start", allow_none=False)
        # confirmed only with an end the resume gave (a month), or the human's
        # answer: a year-only or missing end left null would read as current
        if role.get("current") is not True and not _YM.match(str(role.get("end") or "")):
            raise SetupError(
                f"roles[{role['id']}] ({_role_label(role)}, "
                f"{role.get('dates') or 'no dates on the resume'}): end date not set; ask for "
                f"{_END_ASK}"
            )
        role["confirmed"] = True


def _apply_add_roles(draft: dict[str, Any], data: Any) -> None:
    if not isinstance(data, list):
        raise SetupError("add_roles must be a list")
    ids = set(_roles_by_id(draft))
    for item in data:
        if not isinstance(item, dict):
            raise SetupError("add_roles: each role is an object")
        company = _text(item.get("company"), "add_roles.company")
        title = _text(item.get("title"), "add_roles.title")
        start = _ym(item.get("start"), "add_roles.start", allow_none=False)
        given = _role_end(item, "add_roles")
        if given is None:
            raise SetupError(
                f"add_roles ({title} at {company}): no end date given: ask for {_END_ASK}"
            )
        end, current = given
        base = "role_" + re.sub(r"[^a-z0-9]+", "_", company.lower()).strip("_")[:30]
        rid, n = base, 2
        while rid in ids:
            rid, n = f"{base}_{n}", n + 1
        ids.add(rid)
        draft["roles"].append(
            {
                "id": rid,
                "header_lines": [],
                "company": company,
                "title": title,
                "location": " ".join(str(item.get("location") or "").split()) or None,
                "dates": None,
                "start": start,
                "end": end,
                "current": current,
                "confirmed": True,
                "notes": ["added by the human"],
            }
        )


def _apply_education(draft: dict[str, Any], data: Any) -> None:
    if not isinstance(data, list):
        raise SetupError("education must be a list of {index, ...}")
    edu = draft["education"]
    drop: set[int] = set()
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("index"), int):
            raise SetupError("education: each item needs an integer index")
        i = item["index"]
        if not 0 <= i < len(edu):
            raise SetupError(f"education: no entry [{i}]")
        if item.get("drop"):
            drop.add(i)
            continue
        e = edu[i]
        for k in ("school", "degree", "years"):
            if k in item:
                v = item[k]
                e[k] = " ".join(str(v).split()) if v not in (None, "") else None
        if not str(e.get("school") or "").strip():
            raise SetupError(f"education[{i}]: school is empty; what is the school's name?")
        e["confirmed"] = True
    draft["education"] = [e for i, e in enumerate(edu) if i not in drop]


def _apply_add_education(draft: dict[str, Any], data: Any) -> None:
    if not isinstance(data, list):
        raise SetupError("add_education must be a list")
    for item in data:
        if not isinstance(item, dict):
            raise SetupError("add_education: each entry is an object")
        draft["education"].append(
            {
                "school": _text(item.get("school"), "add_education.school"),
                "degree": " ".join(str(item.get("degree") or "").split()) or None,
                "years": " ".join(str(item.get("years") or "").split()) or None,
                "lines": [],
                "confirmed": True,
            }
        )


def _apply_positioning(draft: dict[str, Any], data: Any) -> None:
    if not isinstance(data, list):
        raise SetupError("positioning must be a list")
    by_id = {str(p.get("id")): p for p in draft["positioning"]}
    for item in data:
        if not isinstance(item, dict):
            raise SetupError("positioning: each entry is an object")
        pid = str(item.get("id") or "")
        if pid in by_id:
            p = by_id[pid]
            if item.get("drop"):
                draft["positioning"] = [x for x in draft["positioning"] if x is not p]
                continue
            if "text" in item:
                p["text"] = _text(item["text"], f"positioning[{pid}].text")
                p["from_resume"] = False
            if "archetype" in item:
                p["archetype"] = " ".join(str(item["archetype"] or "").split()) or None
            p["confirmed"] = True
            continue
        new_id = pid or f"pos_{len(draft['positioning']) + 1:02d}"
        while new_id in by_id:
            new_id += "_x"
        p = {
            "id": new_id,
            "archetype": " ".join(str(item.get("archetype") or "").split()) or None,
            "text": _text(item.get("text"), "positioning.text"),
            "from_resume": False,
            "confirmed": True,
        }
        draft["positioning"].append(p)
        by_id[new_id] = p


def _apply_skill_index(draft: dict[str, Any], data: Any) -> None:
    if not isinstance(data, dict):
        raise SetupError("skill_index must be an object {skill: depth}")
    idx = draft["skill_index"]
    for k, v in data.items():
        key = " ".join(str(k).split())
        if not key:
            continue
        if v in (None, ""):
            idx.pop(key, None)
        else:
            idx[key] = " ".join(str(v).split())


def _apply_constraints(draft: dict[str, Any], data: Any) -> None:
    if not isinstance(data, dict):
        raise SetupError("constraints must be an object")
    unknown = sorted(set(data) - set(_CONSTRAINT_KEYS))
    if unknown:
        raise SetupError(f"constraints: unknown key(s) {unknown}; known: {list(_CONSTRAINT_KEYS)}")
    cons = draft["constraints"]
    for k in _CONSTRAINT_KEYS:
        if k in data:
            v = data[k]
            if not isinstance(v, list):
                raise SetupError(f"constraints.{k} must be a list")
            cons[k] = [" ".join(str(x).split()) for x in v if str(x).strip()]


def _apply_voice(draft: dict[str, Any], data: Any) -> None:
    if not isinstance(data, list):
        raise SetupError("voice_samples must be a list of {source, text}")
    for item in data:
        if not isinstance(item, dict):
            raise SetupError("voice_samples: each sample is an object")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SetupError("voice_samples: text is the human's own prose, pasted as written")
        draft["voice_samples"].append(
            {
                "id": f"v_{len(draft['voice_samples']) + 1:03d}",
                "source": " ".join(str(item.get("source") or "").split()),
                "text": text.strip(),
            }
        )


_APPLY = {
    "identity": _apply_identity,
    "roles": _apply_roles,
    "add_roles": _apply_add_roles,
    "education": _apply_education,
    "add_education": _apply_add_education,
    "positioning": _apply_positioning,
    "skill_index": _apply_skill_index,
    "constraints": _apply_constraints,
    "voice_samples": _apply_voice,
}


def update_draft(changes: dict[str, Any], *, root: Path | None = None) -> str:
    """Apply the human's answers about identity, roles, education,
    positioning, skill_index, constraints and voice samples. All or nothing."""
    files = setup_files(root)
    draft = load_draft(files)
    if not isinstance(changes, dict) or not changes:
        raise SetupError("changes must be a non-empty object")
    unknown = sorted(set(changes) - UPDATE_KEYS)
    if unknown:
        raise SetupError(f"unknown section(s) {unknown}; known: {sorted(UPDATE_KEYS)}")
    for key in (
        "identity",
        "add_roles",
        "roles",
        "add_education",
        "education",
        "positioning",
        "skill_index",
        "constraints",
        "voice_samples",
    ):
        if key in changes:
            _APPLY[key](draft, changes[key])
    save_draft(files, draft)
    return "updated: " + ", ".join(sorted(changes)) + "\n" + draft_status(root=files.root).render()


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------

CORPUS_HEADER = f"""{GENERATED_MARK} — your career corpus.
#
# The single source of truth for every factual claim jobkit may make about you:
# if it is not here, it cannot appear on a generated resume, cover letter or
# form answer (the provenance validator enforces it). Built from your resume
# and confirmed by you entry by entry; metric_confidence and evidence are
# yours. Edit it directly, or add facts through `jobkit questions` / MCP
# get_questions. Personal and gitignored.
"""


def build_corpus(draft: dict[str, Any]) -> dict[str, Any]:
    ident = draft["identity"]
    links = ident.get("links") or {}
    role_order = [str(r["id"]) for r in draft["roles"]]
    kept = [b for b in draft["bullets"] if b.get("status") in ("kept", "edited")]
    kept.sort(key=lambda b: role_order.index(str(b["role_id"])))
    return {
        "corpus_version": "1.0",
        "identity": {
            "name": ident.get("name"),
            "email": ident.get("email") or "",
            "phone": ident.get("phone") or "",
            "display_location": ident.get("display_location") or "",
            "home_city": ident.get("home_city") or "",
            "links": {
                "linkedin": links.get("linkedin") or "",
                "github": links.get("github") or "",
                "site": links.get("site") or "",
            },
        },
        "positioning": [
            {"id": p["id"], "archetype": p.get("archetype") or "", "text": p["text"]}
            for p in draft["positioning"]
            if str(p.get("text") or "").strip()
        ],
        "roles": [
            {
                "id": r["id"],
                "company": r["company"],
                "title": r["title"],
                "start": r["start"],
                "end": r.get("end"),
                "location": r.get("location") or "",
            }
            for r in draft["roles"]
        ],
        "bullets": [
            {
                "id": b["id"],
                "role_id": b["role_id"],
                "text_long": b["text_long"],
                "text_short": b.get("text_short") or b["text_long"],
                "tags": list(b.get("tags") or []),
                "metric": b.get("metric"),
                "metric_confidence": b["metric_confidence"],
                "evidence": b.get("evidence") or "",
            }
            for b in kept
        ],
        "stories": list(draft.get("stories") or []),
        "education": [
            {"school": e["school"], "degree": e.get("degree") or "", "years": e.get("years") or ""}
            for e in draft["education"]
        ],
        "constraints": {
            k: list((draft.get("constraints") or {}).get(k) or []) for k in _CONSTRAINT_KEYS
        },
        "skill_index": dict(draft.get("skill_index") or {}),
        "voice_samples": list(draft.get("voice_samples") or []),
    }


def health(corpus: dict[str, Any]) -> list[str]:
    """Bullet count, metric coverage and tag distribution — and, under 30
    bullets or 40% verified, a plain statement that the corpus is thin."""
    bullets = corpus["bullets"]
    n = len(bullets)
    conf = Counter(str(b.get("metric_confidence")) for b in bullets)
    verified = conf.get("verified", 0) / n if n else 0.0
    tags = Counter(t for b in bullets for t in b.get("tags") or [])
    out = [
        f"bullets: {n} · verified {conf.get('verified', 0)} ({verified:.0%}) · estimate "
        f"{conf.get('estimate', 0)} · none {conf.get('none', 0)}",
        "top tags: " + (", ".join(f"{t} ({c})" for t, c in tags.most_common(10)) or "none"),
        f"positioning: {len(corpus['positioning'])} · stories: {len(corpus['stories'])} · "
        f"voice samples: {len(corpus['voice_samples'])} · skill_index: "
        f"{len(corpus['skill_index'])}",
    ]
    if n < THIN_BULLETS or verified < THIN_VERIFIED:
        out.append(
            f"THIN CORPUS: {n} bullets, {verified:.0%} verified (the bar is {THIN_BULLETS} "
            f"bullets and {THIN_VERIFIED:.0%} verified). Tailored resumes will be mediocre "
            "until it grows: recover the numbers the resume left out (critique, 'Bullets with "
            "no outcome') and add accomplishments it does not mention."
        )
    if not corpus["voice_samples"]:
        out.append(
            "No voice samples: 2-3 pieces of your own writing make cover letters sound like you."
        )
    return out


def promote_corpus(*, replace_existing: bool = False, root: Path | None = None) -> str:
    """Write data/career_corpus.yaml from the confirmed draft. Refuses while
    anything is unconfirmed, and refuses to replace a corpus the interview
    did not write unless replace_existing (the old one is backed up)."""
    files = setup_files(root)
    draft = load_draft(files)
    blockers = promotion_blockers(draft)
    if blockers:
        raise SetupError("not promoted; still open:\n" + "\n".join(f"- {b}" for b in blockers))
    guard_overwrite(files, files.corpus, replace_existing, "your existing career corpus")
    kept_copy = backup(files, files.corpus)
    corpus = build_corpus(draft)
    source = (draft.get("source") or {}).get("file", "your resume")
    header = CORPUS_HEADER + f"#\n# Confirmed {today()} from {source}.\n"
    write_text_atomic(files.corpus, header + "\n" + dump_yaml(corpus))
    lines = [f"wrote {files.rel(files.corpus)}"]
    if kept_copy:
        lines.append(f"previous corpus kept at {files.rel(kept_copy)}")
    return "\n".join(lines + health(corpus))
