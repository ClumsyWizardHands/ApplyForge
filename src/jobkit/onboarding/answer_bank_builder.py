"""Phase 2.7 — the answer bank (ONBOARDING.md). MCP: answer_bank_questions,
build_answer_bank.

Every answer is asked out loud and set by the human; nothing is inherited
from data/answer_bank.example.yaml (its identity is a fictional person). The
template supplies only the STRUCTURE: entry ids, aliases, types, the
free-text scaffolds and the novel-question and abort rules.

- Screening answers (work authorization, sponsorship, start date, relocation,
  onsite tolerance, ...) are the human's words. Unanswered stays null: the
  form field is left blank and flagged, never guessed.
- EEO / demographic answers are set only from an explicit choice (hard rule
  4). "I don't wish to answer" is offered and valid; no entry means blank
  and flagged.
- Salary history is never answered (hard rule 5): fixed to leave-blank.
- Salary expectation: the human picks the policy — a number, their own
  wording, or leave it blank every time — and the lowest figure they would
  say.
- Legal acknowledgements (arbitration, AI-use policy, reference consent):
  off unless the human opts in, and even then only labels the human approves
  word for word (`approved_labels`, empty by default) are ticked.

What the human said is kept in data/setup/answer_bank.answers.yaml, and the
bank is regenerated from it each time, so answers can be given over several
sittings."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
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
    template_path,
    today,
    write_text_atomic,
)

DECLINE = "I don't wish to answer"
EEO_KEYS = (
    "gender",
    "race_ethnicity",
    "hispanic_latino",
    "veteran_status",
    "disability_status",
    "sexual_orientation",
    "transgender",
)
IDENTITY_KEYS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "city",
    "state",
    "country",
    "postal_code",
    "linkedin",
    "github",
    "portfolio",
)
COMP_POLICIES = ("number", "text", "leave_blank")
# screening entries the matcher reads that the template does not carry
_EXTRA_SCREENING: dict[str, dict[str, Any]] = {
    "country_of_residence": {
        "aliases": ["What is your current country of residence?", "Country of residence"],
        "type": "text",
    },
    "state_of_residence": {
        "aliases": ["What is your state of residence?", "State of residence"],
        "type": "text",
    },
}
# legal acknowledgement kinds the matcher recognizes (autofill/answer_bank.py)
_ACK_ALIASES = [
    "Please read the arbitration agreement",
    "Agreement to Arbitrate",
    "AI Policy for Application",
    "Do you consent to us contacting your references?",
]


@dataclass(frozen=True)
class Question:
    key: str
    ask: str
    kind: str  # text | boolean | eeo | comp | ack
    required: bool = True


QUESTIONS: tuple[Question, ...] = (
    Question("identity.first_name", "First name, as it should appear on applications", "text"),
    Question("identity.last_name", "Last name", "text"),
    Question("identity.email", "Email for applications", "text"),
    Question("identity.phone", "Phone number", "text"),
    Question(
        "identity.city",
        "City you actually live in (forms feed background checks and payroll: the real one, "
        "not the resume's display location)",
        "text",
    ),
    Question("identity.state", "State or province (e.g. CO)", "text"),
    Question("identity.country", "Country", "text"),
    Question("identity.postal_code", "Postal / ZIP code (optional)", "text", required=False),
    Question("identity.linkedin", "LinkedIn URL (optional)", "text", required=False),
    Question("identity.github", "GitHub URL (optional)", "text", required=False),
    Question("identity.portfolio", "Portfolio or personal site (optional)", "text", required=False),
    Question(
        "screening.work_auth",
        "Are you legally authorized to work in the country you are applying in? (a wrong "
        "answer is misrepresentation, not a typo)",
        "boolean",
    ),
    Question(
        "screening.sponsorship",
        "Will you now or in the future require visa sponsorship?",
        "boolean",
    ),
    Question("screening.notice_period", "Notice period at your current job", "text"),
    Question("screening.start_date", "Earliest start date, as you would write it", "text"),
    Question("screening.relocation", "Are you willing to relocate? In your words", "text"),
    Question(
        "screening.onsite_ok",
        "Can you work from an office? Where, and how many days a week?",
        "text",
    ),
    Question(
        "screening.referral_source",
        "Default answer to 'How did you hear about us?'",
        "text",
        required=False,
    ),
    Question(
        "screening.previously_employed",
        "Default answer to 'Have you previously worked for this company?' (it varies by "
        "company: leave unset to be asked each time)",
        "boolean",
        required=False,
    ),
    Question(
        "screening.non_compete",
        "Are you subject to a non-compete or other restrictive covenant? (ask directly)",
        "boolean",
    ),
    Question("screening.background_check", "Do you consent to a background check?", "boolean"),
    Question("screening.references", "References: what do you want to say?", "text"),
    Question(
        "screening.country_of_residence",
        "Country of residence (residence questions on forms)",
        "text",
        required=False,
    ),
    Question(
        "screening.state_of_residence",
        "State of residence (residence questions on forms)",
        "text",
        required=False,
    ),
    Question(
        "comp_expectation",
        "Salary expectation questions: give a number, your own wording, or leave them blank "
        "every time (policy: number | text | leave_blank) — and the lowest figure you would say",
        "comp",
    ),
    *(
        Question(
            f"eeo.{k}",
            f"EEO '{k.replace('_', ' ')}': your explicit choice, or \"{DECLINE}\", or leave "
            "unset (the field is then left blank and flagged every time)",
            "eeo",
            required=False,
        )
        for k in EEO_KEYS
    ),
    Question(
        "legal_acknowledgement",
        "Should jobkit tick arbitration / AI-use-policy / reference-consent acknowledgements "
        "for you? Off by default; when on, only labels you approve word for word are ticked "
        "(approved_labels, empty until you add them)",
        "ack",
        required=False,
    ),
)
_BY_KEY = {q.key: q for q in QUESTIONS}


def _said(value: Any) -> tuple[Any, str]:
    """(value, what the human said) from either a bare value or
    {value, said}."""
    if isinstance(value, dict) and "value" in value:
        return value["value"], str(value.get("said") or "")
    return value, ""


def _yes_no(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    s = str(value).strip().lower()
    if s in ("yes", "y", "true"):
        return "Yes"
    if s in ("no", "n", "false"):
        return "No"
    raise SetupError(f"{key}: answer yes or no (got {value!r})")


def _clean(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str | int | float):
        raise SetupError(f"{key}: expected text, got {value!r}")
    s = " ".join(str(value).split())
    return s or None


def _normalize(key: str, raw: Any) -> dict[str, Any]:
    """One answer as stored in the record: {value, said, set_on}."""
    q = _BY_KEY.get(key)
    if q is None:
        raise SetupError(f"unknown answer key {key!r}; see answer_bank_questions")
    value, said = _said(raw)
    if q.kind == "boolean":
        out: Any = _yes_no(value, key)
    elif q.kind == "eeo":
        # the human's explicit choice, in their words; a decline in any
        # wording ("Prefer not to say") picks the form's own decline option
        out = _clean(value, key)
    elif q.kind == "comp":
        if not isinstance(value, dict):
            raise SetupError(
                "comp_expectation: give {policy: number|text|leave_blank, amount?, text?, floor?}"
            )
        policy = str(value.get("policy") or "")
        if policy not in COMP_POLICIES:
            raise SetupError(f"comp_expectation.policy must be one of {', '.join(COMP_POLICIES)}")
        floor = value.get("floor")
        if floor is not None and (isinstance(floor, bool) or not isinstance(floor, int)):
            raise SetupError("comp_expectation.floor must be a whole number")
        out = {"policy": policy, "floor": floor}
        if policy == "number":
            amount = value.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise SetupError("comp_expectation.amount must be a whole number for policy number")
            out["amount"] = amount
        elif policy == "text":
            out["text"] = _clean(value.get("text"), "comp_expectation.text")
            if not out["text"]:
                raise SetupError("comp_expectation.text: the words to put in the field")
    elif q.kind == "ack":
        if not isinstance(value, dict) or not isinstance(value.get("opt_in"), bool):
            raise SetupError("legal_acknowledgement: give {opt_in: true|false, approved_labels?}")
        labels = value.get("approved_labels") or []
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            raise SetupError("legal_acknowledgement.approved_labels must be a list of labels")
        out = {
            "opt_in": value["opt_in"],
            "approved_labels": [" ".join(x.split()) for x in labels if x.strip()],
        }
    else:
        out = _clean(value, key)
        if key == "identity.email" and out and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", out):
            raise SetupError(f"identity.email: {out!r} is not an email address")
    return {"value": out, "said": said, "set_on": today()}


def load_record(files: SetupFiles) -> dict[str, dict[str, Any]]:
    data = load_yaml(files.bank_record)
    answers = (data or {}).get("answers") if isinstance(data, dict) else None
    return dict(answers) if isinstance(answers, dict) else {}


def _template(files: SetupFiles) -> dict[str, Any]:
    data = load_yaml(template_path(files, "data/answer_bank.example.yaml"))
    if not isinstance(data, dict):
        raise SetupError("data/answer_bank.example.yaml is not a YAML mapping")
    return data


def build_bank(template: dict[str, Any], record: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The answer bank: the template's structure, the human's answers, and
    nothing else. Every value the template filled in is dropped."""

    def val(key: str) -> Any:
        return (record.get(key) or {}).get("value")

    bank: dict[str, Any] = {
        "bank_version": template.get("bank_version", "1.0"),
        "match_threshold": template.get("match_threshold", 0.82),
        "identity": {k: val(f"identity.{k}") or "" for k in IDENTITY_KEYS},
    }
    screening: list[dict[str, Any]] = []
    for entry in template.get("screening") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        eid = str(entry["id"])
        out = {
            k: copy.deepcopy(v)
            for k, v in entry.items()
            if k in ("id", "aliases", "type", "action", "note")
        }
        if eid == "salary_history":
            # hard rule 5: never answered, whatever anyone says
            out.update({"answer": "", "action": "leave_blank_and_flag"})
        elif eid == "comp_expectation":
            comp = val("comp_expectation") or {}
            policy = comp.get("policy")
            if policy == "number":
                out["answer"] = str(comp["amount"])
            elif policy == "text":
                out["answer"] = comp["text"]
            else:
                out["answer"] = None
            out["floor"] = comp.get("floor")
        else:
            out["answer"] = val(f"screening.{eid}")
        screening.append(out)
    have = {s["id"] for s in screening}
    for eid, shape in _EXTRA_SCREENING.items():
        answer = val(f"screening.{eid}")
        if answer is not None and eid not in have:
            screening.append({"id": eid, **copy.deepcopy(shape), "answer": answer})
    ack = val("legal_acknowledgement") or {}
    if ack.get("opt_in"):
        screening.append(
            {
                "id": "legal_acknowledgement",
                "aliases": list(_ACK_ALIASES),
                "answer": "Yes",
                "type": "boolean",
                # only these exact labels are ever ticked (fail closed)
                "approved_labels": list(ack.get("approved_labels") or []),
            }
        )
    bank["screening"] = screening
    bank["eeo"] = {k: val(f"eeo.{k}") for k in EEO_KEYS}
    for key in ("scaffolds", "novel_questions", "abort_conditions"):
        if key in template:
            bank[key] = copy.deepcopy(template[key])
    return bank


BANK_HEADER = f"""{GENERATED_MARK} — your answer bank.
#
# The questions every application form asks, answered by you in the setup
# interview (what you said is in data/setup/answer_bank.answers.yaml). An
# empty or null answer leaves the form field blank and flagged for you: it is
# never guessed. EEO answers are filled only as you chose them here; salary
# history is never answered. Legal acknowledgements are ticked only for the
# exact labels in legal_acknowledgement.approved_labels.
# Re-run the interview (build_answer_bank) or edit this file directly.
"""


def answer_status(record: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """(answered keys, required keys still unanswered)."""
    answered = [q.key for q in QUESTIONS if q.key in record]
    missing = [q.key for q in QUESTIONS if q.required and q.key not in record]
    return answered, missing


def questions_text(*, root: Path | None = None) -> str:
    """Every answer-bank question, in interview order, with what is set."""
    files = setup_files(root)
    record = load_record(files)
    lines = [
        "Answer-bank questions (ask each out loud; never fill one from the example file).",
        "Keys go to build_answer_bank as {key: value} or {key: {value, said}}.",
    ]
    for q in QUESTIONS:
        state = "unset"
        if q.key in record:
            state = f"set: {record[q.key].get('value')!r}"
        flag = "" if q.required else " (optional)"
        lines.append(f"- {q.key} [{q.kind}]{flag}: {q.ask} — {state}")
    _, missing = answer_status(record)
    lines.append(
        f"{len(missing)} required unanswered" + (f": {', '.join(missing)}" if missing else "")
    )
    return "\n".join(lines)


def build_answer_bank(
    answers: dict[str, Any], *, replace_existing: bool = False, root: Path | None = None
) -> str:
    """Record the human's answers and (re)write data/answer_bank.yaml from the
    template structure plus every answer recorded so far. All or nothing."""
    files = setup_files(root)
    if not isinstance(answers, dict) or not answers:
        raise SetupError("answers must be a non-empty object {key: value}")
    record = load_record(files)
    errors: list[str] = []
    staged: dict[str, dict[str, Any]] = {}
    for key, raw in answers.items():
        try:
            staged[str(key)] = _normalize(str(key), raw)
        except SetupError as e:
            errors.append(str(e))
    if errors:
        raise SetupError("nothing saved:\n" + "\n".join(f"- {e}" for e in errors))
    guard_overwrite(files, files.bank, replace_existing, "your existing answer bank")
    record.update(staged)
    bank = build_bank(_template(files), record)
    kept = backup(files, files.bank)
    write_text_atomic(
        files.bank_record,
        f"{GENERATED_MARK} — what you said, per answer-bank question.\n"
        + dump_yaml({"answers": record}),
    )
    write_text_atomic(files.bank, BANK_HEADER + "\n" + dump_yaml(bank))
    _, missing = answer_status(record)
    lines = [f"saved {len(staged)} answer(s); wrote {files.rel(files.bank)}"]
    if kept:
        lines.append(f"previous answer bank kept at {files.rel(kept)}")
    eeo_unset = [k for k in EEO_KEYS if f"eeo.{k}" not in record]
    if eeo_unset:
        lines.append("EEO left unset (blank and flagged on every form): " + ", ".join(eeo_unset))
    lines.append(
        "still required: " + (", ".join(missing) if missing else "nothing — the bank is complete")
    )
    return "\n".join(lines)
