"""MCP tools for the setup interview (ONBOARDING.md "MCP tools added by this
phase"). Registered on the jobkit MCP server by mcp_server.py; each one is a
thin wrapper over the onboarding module that the CLI (`jobkit setup ...`)
also calls, so a Claude skill can drive the whole interview.

Structured inputs (decisions, changes, answers, companies) are JSON: an
array or object, or the same as a JSON string. A refused step raises with the
reason (a ValueError), which the client shows as a tool error."""

from __future__ import annotations

import json
from typing import Any

import yaml

from jobkit.onboarding import answer_bank_builder, corpus_confirm, ingest, intake, status, targets
from jobkit.onboarding.files import SetupError


def parse_structured(value: Any, what: str) -> Any:
    """A JSON (or YAML) string parsed, or the structure as given."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            try:
                return yaml.safe_load(value)
            except yaml.YAMLError as e:
                raise SetupError(f"{what}: not valid JSON ({e})") from e
    return value


def setup_status() -> str:
    """Which setup steps are done, in order (resume, career corpus,
    preferences, target companies, answer bank, database, MCP registration,
    daily runs), with the next action for the first one that is not.
    Read-only. Call it first when a person asks to set up jobkit, and after
    each step."""
    return status.setup_status().render()


def ingest_resume(path: str = "", text: str = "", replace_draft: bool = False) -> str:
    """Read the person's resume (absolute path to a PDF, DOCX, HTML or text
    file; or the resume pasted as `text`) and write the corpus DRAFT
    (data/career_corpus.draft.yaml), the critique (data/resume_critique.md)
    and the extracted text. The draft holds only text copied from the resume
    (hard rule 6): never add, rewrite or improve anything in it — suggestions
    live in the critique. metric_confidence and evidence stay unset until the
    person gives them. When a file does not extract cleanly this refuses and
    asks for the text: ask the person to paste it, never guess.
    replace_draft=true starts over a draft that already holds confirmations."""
    result = ingest.ingest_resume(path or None, text=text or None, replace_draft=replace_draft)
    return result.render()


def corpus_draft_status(batch_size: int = 5) -> str:
    """The corpus draft's state: identity, roles and education still to
    confirm, the next `batch_size` unconfirmed bullets (walk them five at a
    time), and what still blocks promote_corpus."""
    return corpus_confirm.draft_status(batch_size).render()


def confirm_bullets(decisions: list[dict[str, Any]] | str) -> str:
    """Record the person's decision for each bullet. `decisions` is a list of
    {id, action: keep|edit|drop, metric_confidence: verified|estimate|none,
    evidence, text_long?, text_short?, metric?, tags?, role_id?}.

    Ask, never assume: metric_confidence and evidence come from the person
    (verified = they can point at a source; estimate = directionally right;
    none = no figure to stand behind). A bullet whose final wording states a
    figure (re-read from text_long and text_short, whatever `metric` says), or
    whose `metric` holds one, cannot be `none` and needs evidence; `metric`
    cannot be cleared to null while the wording still states a figure. `edit`
    takes text_long in the person's own words — never your rewrite; without
    `metric`, an edit takes the figures its new wording states. `metric` is a
    figure the person states. text_short is a shorter variant they approved
    word for word. All or nothing: any invalid decision and nothing is
    applied."""
    parsed = parse_structured(decisions, "decisions")
    if not isinstance(parsed, list):
        raise SetupError("decisions must be a list")
    return corpus_confirm.confirm_bullets(parsed)


def update_corpus_draft(changes: dict[str, Any] | str) -> str:
    """Record the person's answers about the rest of the corpus. `changes` is
    an object with any of:
    identity {name, email, phone, display_location, home_city, linkedin,
      github, site} — display_location (the city the resume and letter header
      show; required, never empty) and home_city are asked, never read off
      the resume; confirms the identity;
    roles [{id, company?, title?, location?, start: YYYY-MM, end?: YYYY-MM,
      current?: true|false, drop?}] — confirms each listed role (company and
      title exactly as they should print). A role needs an end month or
      current: true; ask which when the resume gives only a year or no end
      (never assume current: it prints "Present"). add_roles [{company, title,
      start, end | current: true, location?}];
    education [{index, school?, degree?, years?, drop?}] — confirms each;
      add_education [{school, degree?, years?}];
    positioning [{id?, text?, archetype?, drop?}] — the person's own words;
    skill_index {skill: depth}; constraints {never_claim_titles,
      never_claim_tools, never_claim_phrases}; voice_samples [{source, text}]
      — prose the person wrote, pasted as written.
    All or nothing."""
    parsed = parse_structured(changes, "changes")
    if not isinstance(parsed, dict):
        raise SetupError("changes must be an object")
    return corpus_confirm.update_draft(parsed)


def promote_corpus(replace_existing: bool = False) -> str:
    """Write data/career_corpus.yaml from the confirmed draft, then report
    bullet count, verified share and tags — and say plainly when the corpus
    is thin. Refuses while anything is unconfirmed or any figure lacks
    metric_confidence/evidence. An existing corpus the interview did not
    write is replaced only with replace_existing=true (backed up first)."""
    return corpus_confirm.promote_corpus(replace_existing=replace_existing)


def answer_bank_questions() -> str:
    """Every answer-bank question in interview order (identity, work
    authorization, sponsorship, start date, relocation, onsite, comp
    expectation policy, EEO, legal acknowledgements), with what is already
    set. Ask each one out loud; never take an answer from the example file."""
    return answer_bank_builder.questions_text()


def build_answer_bank(answers: dict[str, Any] | str, replace_existing: bool = False) -> str:
    """Save the person's answers and (re)write data/answer_bank.yaml. `answers`
    maps question keys (answer_bank_questions) to a value or {value, said}.
    Booleans take yes/no. EEO keys take only the person's explicit choice or
    "I don't wish to answer" (hard rule 4) — leave a key out to keep the field
    blank and flagged. comp_expectation takes {policy: number|text|leave_blank,
    amount?, text?, floor?}. legal_acknowledgement takes {opt_in, approved_labels}
    (approved_labels: exact labels the person approved; empty by default).
    Salary history is never answered. Answers can come over several calls."""
    parsed = parse_structured(answers, "answers")
    if not isinstance(parsed, dict):
        raise SetupError("answers must be an object")
    return answer_bank_builder.build_answer_bank(parsed, replace_existing=replace_existing)


def run_intake(answers: dict[str, Any] | str, replace_existing: bool = False) -> str:
    """Turn the intake interview into config: writes config/filters.yaml and
    config/commute_table.yaml (each setting commented with the answer that
    produced it), plus config/autoapprove.yaml (no rules) and
    config/banned_phrases.yaml when missing. `answers`: {tracks: [{name,
    titles?, comp_floor?, comp_basis: base|ote, said}], include_titles {items,
    said}, exclude_titles {items, said}, seniority_floor {level:
    any|no_internships|experienced|senior, said}, locations {work_mode:
    remote_only|hybrid|onsite_ok, places, accept_remote, relocation_cities,
    max_office_days_per_week, max_commute_minutes, commute_minutes {place:
    minutes}, said}, comp {default_floor (required: the floor for titles
    outside the tracks, and the line the answer bank's salary-expectation
    figure is measured against), currency, ote_base_share, said},
    industries {wanted, refused, said}, blocklist_companies {items, said},
    max_age_days?, nightly_at?, writing_style? {estimate_style:
    hedged|confident, said}}. `said` is the person's own words. Ask against
    their corpus. A config the interview did not write is replaced only with
    replace_existing=true (backed up first)."""
    parsed = parse_structured(answers, "answers")
    if not isinstance(parsed, dict):
        raise SetupError("answers must be an object")
    return intake.run_intake(parsed, replace_existing=replace_existing)


def seed_targets(
    companies: list[Any] | str, probe: bool = True, include_not_found: bool = True
) -> str:
    """Find and verify the public Greenhouse / Lever / Ashby board of each
    company (a name, or {name, domain?, url?, tier: 1|2|3, said?}), through
    the documented public board endpoints only, and append them to
    config/targets.yaml. Reports boards found, unverified, and not found
    with what was tried (paste a careers or board URL to retry one). A
    company with no board is recorded without one unless
    include_not_found=false. Existing entries are kept as they are."""
    parsed = parse_structured(companies, "companies")
    if not isinstance(parsed, list):
        raise SetupError("companies must be a list")
    return targets.seed_targets(parsed, probe=probe, include_not_found=include_not_found)


TOOLS = (
    setup_status,
    ingest_resume,
    corpus_draft_status,
    confirm_bullets,
    update_corpus_draft,
    promote_corpus,
    answer_bank_questions,
    build_answer_bank,
    run_intake,
    seed_targets,
)


def register(server: Any) -> None:
    """Add every setup tool to the MCP server."""
    for fn in TOOLS:
        server.add_tool(fn)
