"""Questions for the human (package 3, feature request 2026-10-09): ask only for
facts the system cannot know, then adjust everything they affect.

Where questions come from (aggregate):

* the self-review's questions (events kind 'question_open', package 2) of open
  queued or approved jobs;
* recurring gaps in the fit scorer's gap reports (scores.gaps) of the same open
  jobs. Gap text is free prose, so it is reduced to topics with a fixed
  vocabulary (TOPICS below): sales tools ("Clari proficiency (required tool not
  documented)" -> tool-clari), sales methodologies (method-meddpicc) and
  experience types (exp-healthcare, exp-sdr-bdr-team-management). An overqualification
  gap is never a question, and neither is an experience gap about tenure or
  degree ("3+ years closing", "limited enterprise exposure"): the corpus dates
  already state those facts. A gap with no known topic is not asked; the
  self-review asks the open-ended ones.

A topic the corpus already answers is never asked. A tool or method only by a
skill_index key that is it (in_skill_index: "Clari", not "Clari dashboards")
or by the never-claim lists; a bullet, role or skill_index value that names it
in passing ("the team used Clari") never answers it (review 2026-10-09,
skill-answers). An experience topic by its pattern anywhere in the skill_index
(keys and values), the bullets (text, tags, metric), the roles, education,
stories or the never-claim lists. A self-review question whose key
is only a topic plus generic words ("clari_experience") merges into that topic;
any other self-review key is normalized (generic words dropped, tokens sorted)
so the same fact raised by two jobs is one question.

Questions are deduplicated by that key and ranked by how many open jobs they
would help, then by those jobs' summed fit scores. A key the human answered or
declined is not asked again (undecline reopens a declined one).

The daily cap (filters.yaml `questions.max_per_day`, default 3) is counted per
local day with 'question_asked' events: the digest, `jobkit questions`, MCP
get_questions and get_queue show the same day's questions, and answering one
does not reveal another until the next day. The full ranked list is shown on
request (`jobkit questions --all`, get_questions(show_all=true)).

Answers (answer) are judged on the whole answer (classify_answer). An answer
that is only a "no" ("no", "never used it", "I have never used Clari", "not
that I recall"), or an explicit decline (`--decline`, decline=true), is a
'question_declined' event: the question is not asked again and the gap stays
a gap, until the human takes it back (undecline: a 'question_undeclined'
event). An answer that may be a no or a fact — it opens with a negation
("Never below 115% of quota") or denies the subject ("never used Clari") — is
refused and nothing is recorded. So is an answer that can put a name into
skill_index (a tool or method question, or an answer filed as a skill) unless
it is, as a whole, one fixed sentence of the human's own use (skill_doubt):
"[<tool>:] [yes,] I used|I've used|I have used|I use|I ran <tool>|it", then
nothing but how often, how long, at which of the human's own corpus companies
and for what ("I used Clari weekly at Haulmark for two years"). Anything else is
refused: "Definitely not" is a decline; "My manager used Clari", "Clari? Only
heard of it", "Yes, daily", "Yes, my AE", "Yes, I used Clarity" and "Only
HubSpot and Salesforce" are refused. Only a tool or method question's answer
can become a skill_index item, and an answer to any other question that names
a tool or method (other_tools: "Yes, sold to 12 lab couriers; the team used Clari")
is refused too, so no bullet ever names one. Any other answer
becomes a corpus PROPOSAL in data/corpus_proposals.yaml (never in
data/career_corpus.yaml) plus 'corpus_proposal' and 'question_answered'
events. The proposal is the human's
own text: a tool or method answer becomes a skill_index item, anything else a
bullet whose text is the answer verbatim (a leading "yes" dropped). That text
is held to the answer's rules too (text_reads_as_no): with the "yes" dropped
it may open with a negation only as a threshold and a figure ("Never below
115% of quota"), so "Yes, not in healthcare." is refused. Nothing is
rewritten, improved or inferred (Hard Rule 6).

Confirmation (confirm) is the human gate. It shows a skill item's name next
to the human's whole answer, verbatim. It refuses a proposal whose answer
reads as a no under the same rules (one recorded before they existed) and
a skill proposal for anything but a tool or method question, and
writes the entry into data/career_corpus.yaml only when the human has set
`evidence` and `metric_confidence` (verified | estimate | none) and, for a
bullet, the role it belongs to: none of them is ever defaulted (Hard Rules 6,
8). The entry is
inserted as text at the end of its block, so the file's comments and layout
survive; the result is re-parsed and must equal the old corpus plus exactly
the new entry, or nothing is written. Then a background `jobkit rescore
--job-ids …` re-scores the jobs the question listed (pipeline/rescore.py).

A corpus, proposals file or questions config that does not parse never takes
down the digest or get_queue: their questions section becomes one
"Questions unavailable" line (answer and confirm refuse with the same reason).

No LLM call anywhere in this module."""

from __future__ import annotations

import copy
import datetime
import json
import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from jobkit import background, paths
from jobkit import db as q
from jobkit.autofill.answer_bank import human_answer_barred
from jobkit.config import load_filters
from jobkit.log import log
from jobkit.validate.provenance import (
    constraints,
    numeric_claims,
    phrase_pattern,
    skill_index_keys,
)

CORPUS_PATH = paths.HOME / "data" / "career_corpus.yaml"
PROPOSALS_PATH = paths.HOME / "data" / "corpus_proposals.yaml"

ASKED = "question_asked"
ANSWERED = "question_answered"
DECLINED = "question_declined"
UNDECLINED = "question_undeclined"
PROPOSED = "corpus_proposal"
CONFIRMED = "proposal_confirmed"

CONFIDENCES = ("verified", "estimate", "none")
KINDS = ("skill", "bullet")
# the question kinds whose answer is a skill_index item by default
SKILL_KINDS = ("tool", "method")

PENDING = "pending"

# ---------------------------------------------------------------------------
# topics: the fixed vocabulary gap prose is reduced to
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Topic:
    kind: str  # tool | method | experience
    label: str  # canonical name: the skill_index key a confirmed answer writes
    pattern: re.Pattern[str]  # on gap text and corpus text (case-sensitive where needed)
    loose: re.Pattern[str]  # case-insensitive, on self-review keys
    ask: str = ""  # an experience topic's question stem

    @property
    def key(self) -> str:
        prefix = {"tool": "tool", "method": "method", "experience": "exp"}[self.kind]
        return f"{prefix}-{_slug(self.label)}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _topic(kind: str, label: str, alias: str, *, case: bool = False, ask: str = "") -> Topic:
    body = rf"(?<![A-Za-z0-9])(?:{alias})(?![A-Za-z0-9])"
    return Topic(
        kind=kind,
        label=label,
        pattern=re.compile(body, 0 if case else re.I),
        loose=re.compile(body, re.I),
        ask=ask,
    )


# (label, alias regex, case-sensitive). Case-sensitive only where the name is
# also an everyday word ("cold outreach", "a chorus of").
_TOOLS: tuple[tuple[str, str, bool], ...] = (
    ("Salesforce", r"salesforce|sfdc", False),
    ("HubSpot", r"hub\s?spot", False),
    ("Gong", r"gong(?:\.io)?", False),
    ("Chorus", r"Chorus(?:\.ai)?", True),
    ("Outreach", r"Outreach(?:\.io)?", True),
    ("Salesloft", r"sales\s?loft", False),
    ("Clari", r"clari", False),
    ("LinkedIn Sales Navigator", r"(?:linkedin\s+)?sales\s?nav(?:igator)?", False),
    ("ZoomInfo", r"zoom\s?info", False),
    ("Cognism", r"cognism", False),
    ("Apollo", r"apollo(?:\.io)?", False),
    ("6sense", r"6\s?sense", False),
    ("Lusha", r"lusha", False),
    ("Seamless.AI", r"seamless\.ai", False),
    ("Highspot", r"highspot", False),
    ("Seismic", r"Seismic", True),
    ("Marketo", r"marketo", False),
    ("Pardot", r"pardot", False),
    ("Tableau", r"tableau", False),
    ("Looker", r"looker", False),
    ("Power BI", r"power\s?bi", False),
    ("Zoho", r"zoho", False),
    ("Pipedrive", r"pipedrive", False),
    ("Microsoft Dynamics", r"(?:microsoft\s+|ms\s+)?dynamics\s?(?:365|crm)", False),
    ("Aircall", r"aircall", False),
    ("Dialpad", r"dialpad", False),
    ("Orum", r"orum", False),
    ("Nooks", r"Nooks", True),
    ("Demandbase", r"demandbase", False),
    ("Bombora", r"bombora", False),
    ("Gainsight", r"gainsight", False),
    ("Vidyard", r"vidyard", False),
    ("Clay", r"Clay", True),
)

_METHODS: tuple[tuple[str, str, bool], ...] = (
    ("MEDDPICC", r"meddpicc|meddicc|medpicc|meddpic", False),
    ("MEDDIC", r"meddic", False),
    ("Challenger", r"challenger(?:\s+sale)?", False),
    ("SPIN Selling", r"spin\s+selling", False),
    ("Sandler", r"sandler", False),
    ("Command of the Message", r"command\s+of\s+the\s+message|force\s+management", False),
    ("Value Selling", r"value\s+selling", False),
    ("Solution Selling", r"solution\s+selling", False),
    ("Gap Selling", r"gap\s+selling", False),
    ("BANT", r"BANT", True),
)

# experience types: a domain, segment or motion a recruiter screens for, with
# the question it becomes: (label, alias regex, case-sensitive, question stem)
_EXPERIENCE: tuple[tuple[str, str, bool, str], ...] = (
    (
        "SDR/BDR team management",
        r"(?:manag|lead|coach|ran|run)\w*\s+(?:an?\s+|the\s+)?"
        r"(?:sdrs?|bdrs?|sales development|business development rep\w*)"
        r"|(?:sdr|bdr|sales development)\s+(?:team\s+)?(?:management|leadership|manager)",
        False,
        "Have you managed SDRs or BDRs (a sales development team)?",
    ),
    (
        "healthcare",
        r"health\s?care|health[-\s]?tech|medical|clinical|hospitals?|life sciences|pharma\w*",
        False,
        "Have you sold in healthcare (providers, payers, health tech)?",
    ),
    (
        "financial services",
        r"fin[-\s]?tech|financial (?:services|institutions?)|banking|banks",
        False,
        "Have you sold to banks, fintechs or other financial services firms?",
    ),
    (
        "legal industry",
        r"legal (?:industry|profession|tech|market|domain|sector)|legaltech|law firms?"
        r"|attorneys|lawyers",
        False,
        "Have you sold to law firms or the legal industry?",
    ),
    (
        "public sector",
        r"public sector|government|govtech|federal agenc\w*|state and local|sled",
        False,
        "Have you sold to government or the public sector?",
    ),
    (
        "cybersecurity",
        r"cyber\s?security|security (?:products?|software|sales|buyers|market)|infosec"
        r"|endpoint|edr|siem|zero trust",
        False,
        "Have you sold cybersecurity products?",
    ),
    (
        "developer tools",
        r"developer[-\s]tools|dev\s?tools|infrastructure (?:software|buyers)"
        r"|developer (?:platforms?|audiences?|buyers)|selling to developers",
        False,
        "Have you sold developer tools or infrastructure software?",
    ),
    (
        "technical buyers",
        r"technical (?:buyers|stakeholders|audiences?|selling|decision[-\s]makers)"
        r"|engineering (?:buyers|leaders)",
        False,
        "Have you sold to technical buyers (engineers, CTOs, IT leaders)?",
    ),
    (
        "partnerships",
        r"partnerships?\s+(?:experience|management|development|sales|structuring|team|role)"
        r"|partner (?:management|sales|ecosystem|relationships?)|channel (?:sales|partners?)"
        r"|alliances|resellers?",
        False,
        "Have you run partnerships, alliances or channel sales?",
    ),
    (
        "territory planning",
        r"territory (?:planning|design|management|strategy|carving)",
        False,
        "Have you planned or carved sales territories?",
    ),
    (
        "board-level exposure",
        r"board[-\s]level|board of directors|board (?:meetings?|reporting|presentations?|decks?)",
        False,
        "Have you presented or reported to a board of directors?",
    ),
    (
        "product-led growth",
        r"product[-\s]led|plg",
        False,
        "Have you sold in a product-led growth (PLG) motion?",
    ),
    (
        "cold calling",
        r"cold[-\s]call(?:ing|s)?|outbound prospecting",
        False,
        "Have you done cold calling or outbound prospecting yourself?",
    ),
    (
        "hiring sales reps",
        r"(?:hiring|recruiting) (?:reps|sdrs|bdrs|aes|sales\w*)",
        False,
        "Have you hired sales reps?",
    ),
    (
        "usage-based pricing",
        r"usage[-\s]based|consumption[-\s]based",
        False,
        "Have you sold usage-based or consumption-priced products?",
    ),
    (
        "international sales",
        r"emea|apac|latam|international (?:sales|markets?)",
        False,
        "Have you sold internationally (EMEA, APAC, LATAM)?",
    ),
    (
        "marketing automation",
        r"marketing automation",
        False,
        "Have you worked hands-on with marketing automation?",
    ),
)

TOPICS: tuple[Topic, ...] = (
    *(_topic("tool", label, alias, case=case) for label, alias, case in _TOOLS),
    *(_topic("method", label, alias, case=case) for label, alias, case in _METHODS),
    *(
        _topic("experience", label, alias, case=case, ask=ask)
        for label, alias, case, ask in _EXPERIENCE
    ),
)
TOPIC_BY_KEY: dict[str, Topic] = {t.key: t for t in TOPICS}

_OVERQUALIFIED = re.compile(r"(?i)^\s*over-?qualifi")
# tenure and degree: judgments about facts the corpus dates already state
_SHORTFALL = re.compile(
    r"(?i)\b\d+(?:\.\d+)?\+?\s*(?:years?|yrs?|months?|mos?)\b|-year\b"
    r"|\b(?:tenure|limited|shortfall|short of|less than|fewer than|insufficient"
    r"|not enough|lacks? depth|years? since|absence)\b"
)

# words that say nothing about WHICH fact a self-review question asks for
_GENERIC = frozenset(
    {
        "a", "an", "and", "any", "at", "background", "candidate", "depth", "direct", "documented",
        "evidence", "experience", "experiences", "exposure", "fact", "familiar", "familiarity",
        "for", "hands", "has", "have", "in", "industry", "knowledge", "level", "market", "of", "on",
        "or", "platform", "previous", "prior", "proficiency", "proficient", "sales", "sector",
        "selling", "skill", "skills", "the", "tool", "tools", "usage", "use", "used", "using",
        "vertical", "with", "years", "yrs",
    }
)  # fmt: skip


def _tokens(s: object) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(s or "").lower()) if t]


def topics_in_gap(gap: str) -> list[Topic]:
    """The topics one gap-report line names. None for an overqualification
    gap; no experience topic when the gap is about tenure or degree."""
    text = str(gap or "").strip()
    if not text or _OVERQUALIFIED.match(text):
        return []
    shortfall = _SHORTFALL.search(text) is not None
    return [
        t for t in TOPICS if not (t.kind == "experience" and shortfall) and t.pattern.search(text)
    ]


def topic_for_key(raw_key: object) -> Topic | None:
    """The topic a self-review question key is about, when the key is exactly
    one topic plus generic words ("clari_experience" -> tool-clari). A key with
    any other content word ("commissions_in_salesforce_at_ledgerline") is its own fact."""
    phrase = " ".join(_tokens(raw_key))
    hits = [(t, m) for t in TOPICS if (m := t.loose.search(phrase))]
    if len(hits) != 1:
        return None
    t, m = hits[0]
    rest = _tokens(phrase[: m.start()] + " " + phrase[m.end() :])
    return t if all(tok in _GENERIC for tok in rest) else None


def normalize_key(raw_key: object) -> str:
    """The dedupe key of a self-review question: its topic's key, or its own
    content words (generic words dropped), sorted and joined with "_"."""
    t = topic_for_key(raw_key)
    if t is not None:
        return t.key
    toks = sorted({tok for tok in _tokens(raw_key) if tok not in _GENERIC})
    return "_".join(toks)[:60] or "_".join(_tokens(raw_key))[:60]


# ---------------------------------------------------------------------------
# the corpus side
# ---------------------------------------------------------------------------


class SourceError(ValueError):
    """A file the questions read (the corpus, the proposals file, the
    questions config) does not parse. answer/confirm refuse with it; the
    digest, get_queue and get_questions show one line instead of failing."""


def _parse_error(path: Path, e: yaml.YAMLError) -> SourceError:
    return SourceError(
        f"{path.parent.name}/{path.name} does not parse ({' '.join(str(e).split())[:200]})"
    )


def _parse_yaml(path: Path) -> Any:
    """yaml.safe_load of `path`; None when there is no file. A parse error
    is a SourceError naming the file."""
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise _parse_error(path, e) from e


def load_corpus(path: Path | None = None) -> dict[str, Any]:
    """The corpus as a dict; {} when there is none. SourceError when it does
    not parse."""
    data = _parse_yaml(path or CORPUS_PATH)
    return data if isinstance(data, dict) else {}


def _strings(x: object) -> Iterable[str]:
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(x, list | tuple):
        for v in x:
            yield from _strings(v)
    elif x is not None:
        yield str(x)


def corpus_fact_text(corpus: dict[str, Any]) -> str:
    """Every place the corpus can already answer a question from: the skill
    index, bullets, roles, education, stories and the never-claim lists (a
    tool the human said never to claim is answered too). Not identity or
    voice samples."""
    parts: list[str] = []
    for key in ("skill_index", "bullets", "roles", "education", "stories", "constraints"):
        parts.extend(_strings(corpus.get(key)))
    return "\n".join(parts)


def in_skill_index(topic: Topic, corpus: dict[str, Any]) -> bool:
    """A skill_index key IS the tool or method: the topic's own pattern
    matches the whole key ("Salesforce", "SFDC"; not "Salesforce CPQ"). The
    only corpus fact that says the human used it: a skill_index key is
    written only by the human or from the one accepted sentence of their own
    use (skill_doubt). The claims validator holds letters and answers to the
    same test (validate/claims.py, unsourced_tool)."""
    return any(topic.pattern.fullmatch(k.strip()) for k in skill_index_keys(corpus))


def topic_answered(topic: Topic, corpus: dict[str, Any], facts: str | None = None) -> bool:
    """The corpus already answers `topic`, so it is never asked. A tool or
    method only by a skill_index key that is it (in_skill_index), or by the
    never-claim lists (the human said never to claim it): a bullet, role or
    story that names it in passing ("the team used Clari for the forecast")
    never closes its question (review 2026-10-09, skill-answers). An
    experience topic by anything in corpus_fact_text (`facts`, when the
    caller has it)."""
    if topic.kind in SKILL_KINDS:
        never = "\n".join(_strings(corpus.get("constraints")))
        return in_skill_index(topic, corpus) or topic.pattern.search(never) is not None
    text = corpus_fact_text(corpus) if facts is None else facts
    return topic.pattern.search(text) is not None


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobRef:
    job_id: int
    company: str
    title: str
    status: str
    fit: int | None

    def brief(self) -> str:
        fit = f"fit {self.fit}" if self.fit is not None else "unscored"
        return f"job {self.job_id} {self.company} — {self.title} ({fit}, {self.status})"


@dataclass
class Question:
    key: str
    kind: str  # tool | method | experience | fact
    label: str
    question: str
    why: list[str] = field(default_factory=list)
    gap_examples: list[str] = field(default_factory=list)
    jobs: dict[int, JobRef] = field(default_factory=dict)
    fact_needed: str = ""

    @property
    def n_jobs(self) -> int:
        return len(self.jobs)

    @property
    def fit_total(self) -> int:
        return sum(j.fit or 0 for j in self.jobs.values())

    @property
    def default_entry(self) -> str:
        """What a confirmed answer writes: a skill_index item for a tool or a
        method, a bullet for anything else."""
        return "skill" if self.kind in SKILL_KINDS else "bullet"

    def why_text(self) -> str:
        parts = list(dict.fromkeys(w for w in self.why if w))[:2]
        if self.gap_examples:
            parts.append(
                f"The fit scorer lists it as a gap for {self.n_jobs} open job(s), e.g. "
                f'"{self.gap_examples[0][:140]}".'
            )
        return " ".join(parts) or "It would close a gap in the jobs below."


def _topic_question(t: Topic) -> Question:
    if t.kind == "tool":
        text = (
            f"Have you used {t.label} hands-on? If yes: at which job, for how long, and "
            "what you did with it."
        )
    elif t.kind == "method":
        text = (
            f"Have you run deals or coached a team on {t.label}? If yes: at which job, "
            "and how you used it."
        )
    else:
        text = (
            f"{t.ask} Your corpus does not record it. If yes: at which job, what you did, "
            "and one concrete result (with a number if you have one)."
        )
    return Question(key=t.key, kind=t.kind, label=t.label, question=text)


def _loads_list(raw: object) -> list[str]:
    try:
        val = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


def _job(r: sqlite3.Row, id_col: str) -> JobRef:
    fit = r["fit_score"]
    return JobRef(
        job_id=int(r[id_col]),
        company=str(r["company_name"] or ""),
        title=str(r["title"] or ""),
        status=str(r["status"] or ""),
        fit=int(fit) if fit is not None else None,
    )


def settled_keys(conn: sqlite3.Connection) -> dict[str, str]:
    """{key: 'question_answered' | 'question_declined'} for every key the human
    has settled, read in event order. A declined key stays declined whatever
    came before it, until the human undeclines it (undecline): then it is
    open again."""
    out: dict[str, str] = {}
    for r in conn.execute(q.SETTLED_QUESTION_KEYS):
        key, kind = str(r["key"] or ""), str(r["kind"])
        if not key:
            continue
        if kind == UNDECLINED:
            if out.get(key) == DECLINED:
                del out[key]
        elif kind == DECLINED or out.get(key) != DECLINED:
            out[key] = kind
    return out


def aggregate(conn: sqlite3.Connection, corpus: dict[str, Any] | None = None) -> list[Question]:
    """Every open question, deduplicated by key and ranked: most open jobs
    helped first, then the highest summed fit score of those jobs, then key.
    Read-only."""
    corpus = load_corpus() if corpus is None else corpus
    facts = corpus_fact_text(corpus)
    settled = settled_keys(conn)
    answered: dict[str, bool] = {}

    def known(t: Topic) -> bool:
        if t.key not in answered:
            answered[t.key] = topic_answered(t, corpus, facts)
        return answered[t.key]

    found: dict[str, Question] = {}
    for r in conn.execute(q.OPEN_JOBS_WITH_GAPS):
        job = _job(r, "id")
        for gap in _loads_list(r["gaps"]):
            for t in topics_in_gap(gap):
                if t.key in settled or known(t):
                    continue
                qn = found.setdefault(t.key, _topic_question(t))
                qn.jobs[job.job_id] = job
                if gap not in qn.gap_examples:
                    qn.gap_examples.append(gap)
    for r in conn.execute(q.OPEN_SELF_REVIEW_QUESTIONS):
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            continue
        if not isinstance(d, dict) or not d.get("key"):
            continue
        job = _job(r, "job_id")
        topic = topic_for_key(d["key"])
        if topic is not None:
            if topic.key in settled or known(topic):
                continue
            qn = found.setdefault(topic.key, _topic_question(topic))
        else:
            key = normalize_key(d["key"])
            if not key or key in settled:
                continue
            qn = found.setdefault(
                key,
                Question(
                    key=key,
                    kind="fact",
                    label=key,
                    question=str(d.get("question") or "").strip(),
                    fact_needed=str(d.get("fact_needed") or "").strip(),
                ),
            )
        qn.jobs[job.job_id] = job
        if d.get("why"):
            qn.why.append(f"{d['why']} (self-review, job {job.job_id})")
    return sorted(found.values(), key=lambda x: (-x.n_jobs, -x.fit_total, x.key))


# ---------------------------------------------------------------------------
# the daily cap
# ---------------------------------------------------------------------------


@dataclass
class Daily:
    day: str
    cap: int
    shown: list[Question]
    settled_today: int  # asked today and no longer open (answered, declined, jobs closed)
    open_total: int  # every open question, shown or not


def todays(
    conn: sqlite3.Connection,
    *,
    cap: int | None = None,
    today: datetime.date | None = None,
    corpus: dict[str, Any] | None = None,
    record: bool = True,
) -> Daily:
    """Today's questions: the ones already put in front of the human today that
    are still open, topped up from the ranked list to `cap` (filters.yaml
    `questions.max_per_day`). A question answered or declined today still uses
    its slot. With `record`, each newly shown key is recorded as a
    'question_asked' event for the day (caller's connection, committed)."""
    if cap is None:
        try:
            cap = load_filters().questions_per_day
        except yaml.YAMLError as e:
            raise _parse_error(Path("config/filters.yaml"), e) from e
    day = (today or datetime.date.today()).isoformat()
    ranked = aggregate(conn, corpus)
    by_key = {qn.key: qn for qn in ranked}
    asked = list(
        dict.fromkeys(
            str(r["key"]) for r in conn.execute(q.QUESTIONS_ASKED_ON_DAY, {"day": day}) if r["key"]
        )
    )
    shown = [by_key[k] for k in asked if k in by_key]
    slots = cap - len(asked)
    for qn in ranked:
        if slots <= 0:
            break
        if qn.key in asked:
            continue
        shown.append(qn)
        slots -= 1
        if record:
            conn.execute(
                q.RECORD_EVENT_ONCE,
                {
                    "job_id": None,
                    "kind": ASKED,
                    "detail": json.dumps({"day": day, "key": qn.key}, sort_keys=True),
                },
            )
    if record:
        conn.commit()
    return Daily(
        day=day,
        cap=cap,
        shown=shown,
        settled_today=len([k for k in asked if k not in by_key]),
        open_total=len(ranked),
    )


# ---------------------------------------------------------------------------
# proposals file (data/corpus_proposals.yaml)
# ---------------------------------------------------------------------------

_PROPOSALS_HEADER = """\
# corpus_proposals.yaml — facts you gave in answer to jobkit's questions
# (`jobkit answer`, MCP answer_question). NOTHING here is in
# data/career_corpus.yaml until you confirm it, one proposal at a time:
#   jobkit confirm-proposal <id>     (MCP confirm_proposal)
# Confirming needs `evidence` and `metric_confidence` set by you (and, for a
# bullet, its role_id); none of them is ever defaulted. Written by jobkit.
"""


def load_proposals(path: Path | None = None) -> list[dict[str, Any]]:
    """The proposals file's entries; [] when there is none. SourceError when
    it does not parse."""
    data = _parse_yaml(path or PROPOSALS_PATH) or {}
    items = data.get("proposals") if isinstance(data, dict) else None
    return [x for x in items or [] if isinstance(x, dict)]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def save_proposals(items: list[dict[str, Any]], path: Path | None = None) -> None:
    body = yaml.safe_dump({"proposals": items}, sort_keys=False, allow_unicode=True, width=100)
    _atomic_write(path or PROPOSALS_PATH, _PROPOSALS_HEADER + body)


def pending_proposals(path: Path | None = None) -> list[dict[str, Any]]:
    return [p for p in load_proposals(path) if p.get("status") == PENDING]


# ---------------------------------------------------------------------------
# answering
# ---------------------------------------------------------------------------

# What counts as a "no" (review 2026-10-09). A decline throws the question
# away and a proposal can put a tool's name into skill_index, so both are
# decided on the WHOLE answer, never on how it starts:
#
# * a decline is an answer that is nothing but a no. Lowercased, punctuation
#   dropped and the question's own subject ("Clari", "healthcare") read as
#   "it", it matches _DECLINE_EXACT: "No.", "Never used it", "I have never
#   used Clari", "Haven't", "I've not used it", "Unfortunately not", "Not that
#   I recall", "No, I have never used it at any job";
# * an answer that is not one but opens with a negation ("Never below 115% of
#   quota", "None of my AEs churned", "No, but I used HubSpot"), or denies the
#   subject anywhere ("Used Gong daily; never used Clari", "limited exposure
#   to Clari"), is ambiguous: refused, nothing recorded. The human answers
#   just "no" (or declines explicitly), or gives the fact so that it does not
#   open with a negation. Only a threshold may, after "yes" ("Yes, never
#   below 115% of quota"): the text written drops the "yes", and any other
#   opening negation ("Yes, not in healthcare." would write "Not in
#   healthcare.") is refused (text_reads_as_no);
# * an answer that can put a name into skill_index must also plainly state
#   use (states_use below); one that does not is ambiguous too;
# * anything else is a fact and becomes a proposal. "No-code Clay workflows
#   at ledgerline" is one: a hyphenated word is one word, and "no-code" is not "no".
#
# Skill proposals fail closed (review 2026-10-09). A skill_index key is a name
# a tailored resume may list whatever its value says, and Hard Rule 1 now
# allows unattended submission. "Definitely not", "Clari? no", "My manager
# used Clari; I did not" and "Only HubSpot and Salesforce" were facts, and one
# confirm put Clari into skill_index. Then (package S, review 2026-10-09)
# naming the tool was enough: "Clari? Only heard of it", "Clari was on my
# wishlist", "My manager used Clari" and "Clari was rolled out after I left"
# were skill proposals. Then (review 2026-10-09, skill-answers) the word
# lists still let through what they did not list: "Yes, I used Clarity",
# "Yes, BoostUp", "Yes, my AE", "Yes, I tried it", "Yes, if hired", "Daily
# emails from Clari", "Yes, my manager's Clari dashboards", "Yes, jk". A
# denylist cannot be made complete, so the tail of a "yes" is no longer
# judged by one. An answer to a tool or method question, or one filed as a
# skill, is a fact only when the WHOLE answer is one sentence of this form
# (_skill_sentence):
#
#   [<topic> :|,|-|.]  [yes|yeah|yep|yup ,]  I used|I've used|I have used|
#   I use|I ran  <topic>|it  [<topic's purpose compound>]  then only, in any
#   order: how often ("daily", "weekly", "twice a week", "every day"), how
#   long ("for 2 years", "for 18 months", "for the past two years"), where
#   ("at <a company of the human's own corpus roles>") and for what ("for
#   forecasting", "on every deal", "in pipeline reviews"); one final "." or
#   "!" at most.
#
# <topic> is matched with the topic's own `pattern` (case-sensitive for
# everyday-word names: "outreach", "clay"), never followed by a letter, a
# digit or a hyphen, apostrophe, slash or dot joined to one: "Clarity",
# "Clari-like", "Clari's" and "Clari.ai" are not Clari. Package S also
# accepted a bare "yes" and a bare "daily" as openers ("Yes, daily at
# Haulmark"); both left the rest of the answer open ("Daily emails from
# Clari", "Yes, the SDR next to me"), so they are removed here. That deviates
# from the package S spec, which lists them: raised with the owner, not
# reconciled silently.
#
# A tool or method named in the answer to ANY other question (a self-review
# fact such as cadence_clari_forecast, an experience such as exp-healthcare)
# is refused as well (other_tools; review 2026-10-09, skill-answers). Those
# answers went through the plain decline check only, so "Yes, my manager ran
# Clari forecast calls at Haulmark" and "Yes, sold to 12 lab couriers at Haulmark;
# the team used Clari for the forecast" became bullets; a bullet naming Clari
# closed tool-clari's own question and sourced "At Haulmark I ran Clari
# forecast calls every week." in a letter. Who used the tool is not judged
# there either: a tool enters the corpus only through its own question, in
# the one accepted sentence, as a skill_index item. The claims validator
# holds letters and answers to skill_index keys too (unsourced_tool).
#
# The word lists below stay as a first check, so a refusal names what failed
# ("it contains "manager's"") before the generic "not of the form" reason:
# no negation, third-party subject, non-use signal (_SKILL_DOUBT_WORDS, any
# "n't", "'d" or "'ll", _SKILL_DOUBT_PHRASE, a "?"), and no other tool or
# method. A possessive or hyphenated word is also read in its parts there
# ("manager's", "team-wide", "demo-only").
#
# Everything else is 'ambiguous': nothing is recorded and the human is asked
# to answer in the fixed form. A false refusal costs one re-answer; a false
# proposal can put a tool the human never used on a resume. A proposal still
# needs the human's confirm, which re-applies this rule to the answer as
# recorded.

_NO = r"no|nope|nah|nay|negative"
_SOFT = r"sorry|unfortunately|sadly|honestly|afraid|alas|well|um|uh|definitely|absolutely|certainly"
_VERB = (
    r"used|use|done|do|did|sold|sell|run|ran|managed|manage|touched|tried|had|been|seen"
    r"|worked with|worked in|worked on"
)
_OBJ = r"it|that|this|them|those|one|any|either"
_PREP = r"in|into|with|on|to|for"
# "used it", "sold into it"; or a bare "it" where the subject was a verb phrase
# ("I have never managed SDRs" reads "i have never it")
_VP = rf"(?: (?:{_VERB})(?: (?:{_PREP}))?(?: (?:{_OBJ}))?| it)?"
_NEG_AUX = (
    r"have not|haven't|havent|have never|never have|never|did not|didn't|didnt"
    r"|do not|don't|dont|had not|hadn't|hadnt|not"
)
_DECLINE_CORE = "|".join(
    (
        _NO,
        r"none|nothing|zero|n/a|na|nada|not really|not at all|not yet|not much",
        r"not that i (?:recall|remember|know of|can recall|can remember)",
        r"(?:i )?don't think so|(?:i )?dont think so|(?:i'm |im )?afraid not",
        rf"(?:(?:i|we) )?(?:{_NEG_AUX}){_VP}",
        rf"(?:i've|ive|we've) (?:not|never){_VP}",
        rf"(?:no|zero) (?:(?:hands-on|real|direct|professional) )?(?:experience|exposure)"
        rf"(?: (?:{_PREP}) (?:{_OBJ}))?",
    )
)
_DECLINE_TAIL = (
    r"at any (?:job|role|company)|anywhere|before|professionally|yet|at all|either|really"
    r"|personally|ever|sorry|unfortunately|i'm afraid|im afraid|" + _NO + r"|never"
)
# the subject may come first: "Clari? No." reads "it no"
_DECLINE_EXACT = re.compile(
    rf"(?:it )?(?:(?:{_NO}|{_SOFT}) )*(?:{_DECLINE_CORE})(?: (?:{_DECLINE_TAIL}))*"
)
# words an answer may open with before its negation ("I have never", "Sorry, I
# haven't", "Unfortunately not", "Definitely not", "Clari? Never" as "it never")
_LEAD_WORDS = frozenset(
    {
        "i", "i've", "ive", "i'm", "im", "we", "we've", "weve", "have", "has", "had", "do",
        "did", "does", "am", "was", "were", "unfortunately", "sadly", "honestly", "actually",
        "afraid", "sorry", "well", "um", "uh", "hmm", "so", "oh", "alas", "personally",
        "really", "frankly", "truthfully", "definitely", "absolutely", "certainly", "it",
    }
)  # fmt: skip
_NEGATORS = frozenset(
    {
        "no", "nope", "nah", "nay", "never", "none", "not", "nothing", "neither", "nor", "n/a",
        "na", "nada", "cannot", "havent", "hasnt", "hadnt", "didnt", "dont", "doesnt", "cant",
        "wasnt", "werent", "wont", "isnt", "arent", "couldnt", "negative",
    }
)  # fmt: skip
# a skill answer with any of these anywhere does not plainly state use. "na"
# is left out: mid-answer it is North America ("ran Clari for the NA team");
# a bare "na" is a decline and a leading one is caught by starts_negated.
_SKILL_NEGATORS = (_NEGATORS - {"na"}) | frozenset(
    {
        "nobody", "nowhere", "hardly", "barely", "scarcely", "seldom", "rarely", "without",
        "except", "excluding", "instead", "rather", "limited", "minimal",
    }
)  # fmt: skip
# someone other than the human: the use may have been theirs, not the human's
_THIRD_PARTY = frozenset(
    {
        "manager", "managers", "boss", "bosses", "director", "directors", "vp", "vps", "cro",
        "ceo", "cto", "coo", "cfo", "founder", "founders", "leadership", "leaders", "management",
        "execs", "executives", "team", "teams", "teammate", "teammates", "colleague",
        "colleagues", "coworker", "coworkers", "co-worker", "co-workers", "peer", "peers",
        "predecessor", "successor", "reps", "aes", "sdrs", "bdrs", "sellers", "salespeople",
        "staff", "employees", "admins", "revops", "company", "companies", "org", "orgs",
        # singulars and other people (review 2026-10-09, skill-answers)
        "rep", "ae", "sdr", "bdr", "seller", "exec", "executive", "admin", "employee", "se",
        "ses", "csm", "csms", "analyst", "analysts", "ops", "intern", "interns", "contractor",
        "contractors", "consultant", "consultants", "agency", "vendor", "vendors", "partner",
        "partners", "customer", "customers", "client", "clients", "prospect", "prospects",
        "buyer", "buyers", "friend", "friends", "mentor", "coach", "spouse", "wife",
        "husband", "mom", "dad", "mother", "father", "brother", "sister", "reports",
        "directs", "member", "members",
        "organization", "organisation", "organizations", "department", "dept", "employer",
        "firm", "we", "we've", "weve", "we'd", "we're", "we'll", "us", "our", "ours", "they",
        "they've", "theyve", "they'd", "they're", "theyre", "they'll", "them", "their",
        "theirs", "he", "he's", "him", "his", "she", "she's", "her", "hers", "someone",
        "somebody", "others", "everyone", "everybody", "people", "folks", "whose",
    }
)  # fmt: skip
# knowing of, training on, evaluating, wanting or planning a tool, an
# organization's events around it, access without use, hedges, and
# exclusivity or contrast: none of them says the human used it
_NON_USE = frozenset(
    {
        # awareness
        "heard", "hear", "aware", "familiar", "familiarity", "know", "knew", "known",
        "knowledge", "read", "seen", "saw", "watched", "observed", "shadowed", "shadowing",
        "exposure", "exposed", "understand", "understanding", "learned", "learning", "studied",
        "studying", "researched", "explored", "looked", "glanced", "noticed",
        # training
        "trained", "training", "course", "courses", "certified", "certification",
        "certificate", "webinar", "webinars", "tutorial", "tutorials", "class", "classes",
        "workshop", "workshops",
        # trial and evaluation
        "demo", "demos", "demoed", "demoing", "trial", "trials", "trialed", "trialled", "pilot",
        "pilots", "piloted", "poc", "sandbox", "evaluate", "evaluated", "evaluating",
        "evaluation", "considered", "considering", "shortlisted", "compared", "comparing",
        "comparison", "versus", "vs", "competitor", "competitors", "competing", "alternative",
        "alternatives", "against",
        # wishes, plans, the future, the hypothetical
        "wishlist", "wish-list", "wish", "wished", "want", "wanted", "wanting", "plan", "plans",
        "planned", "planning", "intend", "intended", "intending", "hope", "hoped", "hoping",
        "would", "will", "could", "should", "might", "gonna", "soon", "someday", "eventually",
        "future", "upcoming",
        # hedges and minimal or indirect use
        "maybe", "perhaps", "probably", "possibly", "likely", "guess", "think", "believe",
        "suppose", "assume", "kinda", "sorta", "somewhat", "slightly", "partially", "partly",
        "basic", "basics", "briefly", "brief", "little", "bit", "occasionally", "sporadically",
        "indirectly", "indirect", "secondhand", "second-hand", "passively", "passive",
        "vicariously", "adjacent", "around",
        # an organization's events, and timing outside the human's own use
        "rolled", "rollout", "roll-out", "adopted", "adoption", "introduced", "launched",
        "implemented", "implementation", "deployed", "deployment", "bought", "purchased",
        "purchase", "licensed", "procured", "procurement", "selected", "chose", "chosen",
        "migrated", "migrating", "migration", "switched", "switching", "replaced", "replacing",
        "replacement", "sunset", "sunsetted", "deprecated", "cancelled", "canceled",
        "discontinued", "after", "before", "left", "leaving", "joined", "joining", "departed",
        # access without use
        "access", "login", "seat", "read-only", "view-only", "viewer",
        # exclusivity and contrast
        "only", "just", "merely", "solely", "but", "though", "although", "however", "whereas",
        "otherwise",
        # close variants the lists above missed (review 2026-10-09, skill-answers)
        "zero", "0", "tried", "trying", "tested", "testing", "can", "learn", "if", "when",
        "next", "tomorrow", "starting", "start", "sold", "sell", "selling", "resold", "resell",
        "invested", "investor", "shares", "stock", "worked", "interviewed", "applied", "taught",
        "shown", "showed", "competed", "hypothetically", "hypothetical", "theoretically",
        "technically", "nominally", "roleplay", "role-play", "simulation", "simulated", "mock",
        "kind-of", "sort-of", "ish", "paper", "listened", "emailed", "mentioned", "newsletter",
        "inbox", "subscribed", "installed", "paid", "clone", "like", "style",
        # retractions and jokes
        "jk", "kidding", "joking", "joke", "nvm", "nevermind", "scratch", "lol", "lmao",
        "lied", "fake", "fib", "sarcasm", "sarcastic",
    }
)  # fmt: skip
_SKILL_DOUBT_WORDS = _SKILL_NEGATORS | _THIRD_PARTY | _NON_USE
# the same, as phrases, on the answer with the topic read as "it"
_SKILL_DOUBT_PHRASE = re.compile(
    r"\b(?:"
    r"once(?! (?:a|an|per|every|each)\b)"  # "once" is not "once a week"
    r"|kind of|sort of|a few times|a couple(?: of)? times|at times|now and then"
    r"|going to|about to|in the works"
    # passive or someone else's use: "used by", "it was used at Haulmark"
    r"|(?:used|run|ran|owned|managed|bought|chosen|picked|set up|rolled out|introduced"
    r"|adopted) by"
    r"|(?:was|were|is|are|been|being|got|gets|get)(?: \S+)? (?:used|run|ran|utilized"
    r"|leveraged|in use|rolled|adopted|introduced|deployed|implemented|bought|purchased"
    r"|licensed)"
    r"|(?:that|which|who|where) (?:\S+ )?(?:used|use|uses|ran|run|runs|had|has|adopted"
    r"|bought)"
    r"|(?:had|has|have|got) it"  # "Haulmark had Clari"
    r")\b"
)
_YES_WORDS = frozenset({"yes", "yeah", "yep", "yup"})
# a verb of use or possession and the words that may come right before it: any
# other word is a subject other than the human ("Yes, Haulmark used it", "Yes,
# Sarah ran it"); "I used it" and "I've used it" pass
_USE_VERBS = frozenset({"used", "uses", "ran", "runs", "had", "has", "owned", "owns"})
_OWN_LEAD = _YES_WORDS | frozenset({"i", "i've", "ive", "have", "also", "and"})
# retraction or joke markers the word lists cannot see ("/s")
_SKILL_DOUBT_RAW = re.compile(r"\?|(?<![A-Za-z0-9])/s\b")

# --- the one sentence a skill answer may be (_skill_sentence) ---------------
# not followed by a letter or digit, or by a hyphen, apostrophe, slash, dot or
# underscore joined to one: "Clari-like", "Clari's", "Clari.ai", "Clarity"
_NAME_END = r"(?![A-Za-z0-9_]|[-'./][A-Za-z0-9_])"
_NAME_START = r"(?<![A-Za-z0-9_'./-])"
# atomic, so a long run of spaces cannot make the match backtrack quadratically
_SEP = r"(?>\s*[,.:;!—–-]\s*|\s+)"
_SK_YES = r"(?i:yes|yeah|yep|yup)"
_SK_USE = r"(?i:i\s+(?:used|use|ran)|i'?ve\s+used|i\s+have\s+used)"
_SK_COUNT = (
    r"(?:[1-9][0-9]*(?:\.[0-9]+)?\+?|a|an|one|two|three|four|five|six|seven|eight|nine|ten"
    r"|eleven|twelve)"
)
_SK_PERIOD = r"(?:day|week|month|quarter)"
_SK_FREQ = (
    rf"(?:daily|weekly|monthly|quarterly|(?:every|each)\s+(?:business\s+)?{_SK_PERIOD}"
    rf"|(?:once|twice|(?:two|three|four|five|[1-9][0-9]*)\s+times)\s+(?:a|per|each|every)"
    rf"\s+{_SK_PERIOD}|on\s+a\s+(?:daily|weekly|monthly)\s+basis)"
)
_SK_DURATION = (
    rf"(?:for\s+(?:the\s+(?:last|past)\s+)?(?:{_SK_COUNT}\s+)?(?:years?|months?)"
    rf"|for\s+(?:over|more\s+than)\s+{_SK_COUNT}\s+(?:years?|months?))"
)
_SK_PURPOSE_NOUN = (
    r"(?:(?:forecast|pipeline|deal|call)\s+(?:calls?|reviews?|meetings?|inspection"
    r"|qualification|coaching|recording)|forecasting|forecasts?|pipeline|deals?|calls?"
    r"|prospecting|outbound|sequences|sequencing|discovery|qualification)"
)
_SK_PURPOSE = (
    rf"(?:(?:for|in|on|during)\s+(?:(?:every|each|all(?:\s+of)?|daily|weekly|monthly"
    rf"|quarterly)\s+)?(?:my\s+)?(?:own\s+)?{_SK_PURPOSE_NOUN})"
)
# right after the object: "I ran Clari forecast calls"
_SK_COMPOUND = r"(?:(?:forecast|pipeline|deal)\s+(?:calls?|reviews?|meetings?))"


def _topic_name_re(topic: Topic) -> str:
    """The topic's own `pattern` (case-sensitive where it is), as a name that
    stands alone (_NAME_START, _NAME_END)."""
    body = topic.pattern.pattern
    return f"{_NAME_START}(?{'i' if topic.pattern.flags & re.I else '-i'}:{body}){_NAME_END}"


def _skill_sentence(topic: Topic, places: Iterable[str]) -> re.Pattern[str]:
    """The only shape a skill answer may have (see the rules above _NO):
    `places` are the companies of the human's own corpus roles; with none, an
    "at ..." part is never accepted."""
    name = _topic_name_re(topic)
    obj = rf"(?:{name}|{_NAME_START}(?i:it){_NAME_END})"
    at = "|".join(
        rf"{_NAME_START}(?i:{re.escape(p)}){_NAME_END}"
        for p in sorted({str(p).strip() for p in places if str(p).strip()}, key=len, reverse=True)
    )
    parts = [_SK_FREQ, _SK_DURATION, _SK_PURPOSE] + ([rf"at\s+(?:{at})"] if at else [])
    tail = rf"(?:{_SEP}(?i:(?:and\s+)?(?:{'|'.join(parts)}))){{0,6}}"
    return re.compile(
        rf"(?>\s*)(?:{name}{_SEP})?(?:{_SK_YES}{_SEP})?{_SK_USE}\s+{obj}"
        rf"(?:\s+(?i:{_SK_COMPOUND}))?{tail}(?>\s*)(?:[.!]\s*)?"
    )


def corpus_places(corpus: dict[str, Any]) -> list[str]:
    """The companies of the human's own corpus roles: the only places a
    skill answer may name ("at Haulmark")."""
    out: list[str] = []
    for r in corpus.get("roles") or []:
        c = str(r.get("company") or "").strip() if isinstance(r, dict) else ""
        if c and c not in out:
            out.append(c)
    return out


# the subject (as "it") denied anywhere in the answer
_DENIES_IT = re.compile(
    r"(?:\b(?:never|not|no|cannot)\b|n't\b|\b(?:havent|hasnt|hadnt|didnt|dont|doesnt|cant)\b)"
    r"(?: \S+){0,2}? (?:used|use|using|done|did|sold|sell|selling|run|ran|managed|touched|tried"
    r"|worked with|worked in|worked on)(?: (?:in|into|with|on|to|for))? (?:it|that|them)\b"
    r"|\b(?:no|zero|little|limited|minimal)(?: (?:hands-on|real|direct|professional))?"
    r" (?:experience|exposure) (?:with|in|on|to) (?:it|that|them)\b"
)
_AFFIRMATIVE = re.compile(r"(?i)^\s*(?:yes|yeah|yep|yup)\b[\s,.;:!—–-]*")


def _words(text: str) -> list[str]:
    """Lowercased words, curly apostrophes straightened. A hyphenated or
    slashed word stays one word: "no-code" is not "no", "n/a" is one word."""
    t = str(text or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    return re.findall(r"[a-z0-9]+(?:['/-][a-z0-9]+)*", t)


def subject_pattern(key: str) -> re.Pattern[str] | None:
    """What question `key` asks about, as a pattern (the topic's names), so
    "I have never used Clari" reads as "I have never used it"."""
    topic = TOPIC_BY_KEY.get((key or "").strip())
    return topic.loose if topic is not None else None


def _normalized(answer: str, subject: re.Pattern[str] | None) -> str:
    text = subject.sub(" it ", answer or "") if subject is not None else answer or ""
    return " ".join(_words(text))


def is_decline(answer: str, subject: re.Pattern[str] | None = None) -> bool:
    """A "no": the whole answer is a decline and nothing else (see the
    grammar above). `subject` (subject_pattern(key)) lets a decline name the
    thing asked about: "I've never used Clari"."""
    norm = _normalized(answer, subject)
    return bool(norm) and _DECLINE_EXACT.fullmatch(norm) is not None


def starts_negated(answer: str) -> bool:
    """The answer opens with a negation, after at most a few lead words ("I
    have never", "Sorry, I haven't", "Unfortunately not", "None of"). An
    answer that opens with "yes" never does."""
    for w in _words(answer)[:4]:
        if w in _NEGATORS or w.endswith("n't"):
            return True
        if w not in _LEAD_WORDS:
            return False
    return False


def is_ambiguous(answer: str, subject: re.Pattern[str] | None = None) -> bool:
    """Not a plain decline, but it opens with a negation ("Never below 115% of
    quota", "No, but I used HubSpot daily") or denies the subject somewhere
    ("Used Gong daily; never used Clari"): maybe a no, maybe a fact. Refused;
    the human answers one or the other."""
    if is_decline(answer, subject):
        return False
    norm = _normalized(answer, subject)
    if starts_negated(norm):
        return True
    return _DENIES_IT.search(norm) is not None


def skill_bound(key: str, *, kind: str = "", question_kind: str = "") -> bool:
    """The answer to question `key` can put a name into skill_index: it is
    filed as a skill (`kind`), or the question asks about a tool or a method
    (`question_kind`, or the key's own topic: a `question_kind` read back
    from the proposals file never lifts the rule from a tool key). Filing
    such an answer as a bullet does not lift the stricter rule (states_use)."""
    if (kind or "").strip().lower() == "skill" or skill_topic(key):
        return True
    return question_kind in SKILL_KINDS


def other_tools(answer: str, key: str) -> list[Topic]:
    """The tools and methods (TOPICS of SKILL_KINDS) `answer` names, other
    than question `key`'s own topic, each with its own `pattern`
    (case-sensitive for everyday-word names). An answer to ANY question that
    names one is refused (classify_answer): a tool or method enters the corpus
    only as a skill_index item, from its own question, in the one accepted
    sentence of the human's own use (review 2026-10-09, skill-answers: "Yes,
    my manager ran Clari forecast calls at Haulmark" to a fact question, and
    "Yes, sold to 12 lab couriers at Haulmark; the team used Clari for the
    forecast" to exp-healthcare, became bullets that sourced "I ran Clari
    forecast calls" in a letter)."""
    own = TOPIC_BY_KEY.get((key or "").strip())
    if own is not None and own.kind not in SKILL_KINDS:
        own = None
    rest = own.pattern.sub(" ", answer or "") if own is not None else answer or ""
    return [t for t in TOPICS if t.kind in SKILL_KINDS and t is not own and t.pattern.search(rest)]


def _word_parts(word: str) -> list[str]:
    """A word's parts as the third-party and non-use lists read them: a
    trailing possessive dropped ("manager's", "managers'"), then split at
    hyphens and slashes ("team-wide", "demo-only"). Not for the negators:
    "no-code" is not a no."""
    base = re.sub(r"'s?$", "", word)
    return [p for p in re.split(r"[-/]", base) if p]


_PART_DOUBT_WORDS = _THIRD_PARTY | _NON_USE


def _sentence_text(answer: str) -> str:
    """The answer as _skill_sentence reads it: curly quotes straightened."""
    return (
        str(answer or "")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def skill_doubt(answer: str, key: str, *, places: Iterable[str] = ()) -> str:
    """Why an answer that can put the topic of `key` into skill_index does not
    plainly state the human's own use, or "" when it does (see the rules
    above _NO). It must be a tool or method question, have no "?",
    negation, third-party subject or non-use signal anywhere, name no other
    tool or method, and be, as a whole, the one sentence _skill_sentence
    accepts ("I used Clari weekly at Haulmark for two years"), where `places`
    (corpus_places) are the only companies it may name. Fail closed: an
    answer this rejects is refused, never proposed."""
    topic = TOPIC_BY_KEY.get((key or "").strip())
    if topic is None or topic.kind not in SKILL_KINDS:
        return SKILL_KIND_REFUSED
    name = topic.label
    words = _normalized(answer, subject_pattern(key)).split()
    hit = next(
        (
            w
            for w in words
            if w in _SKILL_DOUBT_WORDS
            or w.endswith(("n't", "'d", "'ll"))
            or any(part in _PART_DOUBT_WORDS for part in _word_parts(w))
        ),
        None,
    )
    if hit is not None:
        return f'it contains "{hit}"'
    phrase = _SKILL_DOUBT_PHRASE.search(" ".join(words))
    if phrase is not None:
        return f'it contains "{phrase.group(0)}"'
    other_subject = next(
        (f"{a} {b}" for a, b in pairwise(words) if b in _USE_VERBS and a not in _OWN_LEAD),
        None,
    )
    if other_subject is not None:
        return f'it contains "{other_subject}"'
    others = [t.label for t in other_tools(answer, key)]
    if others:
        return f"it names {', '.join(others)}; answer about {name} alone"
    raw = _SKILL_DOUBT_RAW.search(answer or "")
    if raw is not None:
        return f'it contains "{raw.group(0)}"'
    if _skill_sentence(topic, places).fullmatch(_sentence_text(answer)) is None:
        return f'it is not of the form "I used {name} ..."'
    return ""


def skill_form(name: str, places: Iterable[str] = ()) -> str:
    """The accepted form of a skill answer, in words."""
    where = [str(p).strip() for p in places if str(p).strip()]
    at = f", where (at {' / at '.join(where)}: your own corpus roles)" if where else ""
    return (
        f'"I used {name}" (or "I\'ve used", "I have used", "I use", "I ran"; "it" may stand '
        f'for {name}; "yes," or "{name}:" may come first), then only how often (daily, '
        f"weekly, twice a week), how long (for 2 years, for 18 months){at} and for what "
        "(for forecasting, on every deal, in pipeline reviews)"
    )


def states_use(answer: str, key: str, *, places: Iterable[str] = ()) -> bool:
    """See skill_doubt."""
    return not skill_doubt(answer, key, places=places)


def classify_answer(
    answer: str, key: str = "", *, skill: bool | None = None, places: Iterable[str] = ()
) -> str:
    """'decline', 'ambiguous' or 'fact', for the answer to question `key`.
    An answer that names a tool or method other than the question's own
    (other_tools) is 'ambiguous' whatever the question, and whatever it is
    filed as. `skill` (default: skill_bound(key)) says whether the answer can
    put a name into skill_index; such an answer is a fact only when it also
    states the human's own use in the one accepted form (skill_doubt, with
    `places`: corpus_places); otherwise it is 'ambiguous'."""
    subject = subject_pattern(key)
    if is_decline(answer, subject):
        return "decline"
    if is_ambiguous(answer, subject) or other_tools(answer, key):
        return "ambiguous"
    if (skill_bound(key) if skill is None else skill) and not states_use(
        answer, key, places=places
    ):
        return "ambiguous"
    return "fact"


def answer_text(answer: str) -> str:
    """The text a proposal records: the answer as given, with a leading "yes",
    "yeah", "yep" or "yup" (and its punctuation) dropped and the first letter
    capitalized. Nothing else changes."""
    text = _AFFIRMATIVE.sub("", str(answer or "").strip(), count=1).strip()
    return text[:1].upper() + text[1:] if text else ""


def _figures(text: str) -> str:
    """The figures in the human's text, as written ("the number if present")."""
    seen: list[str] = []
    for c in numeric_claims(text):
        fig = str(getattr(c, "figure", "") or c.text).strip()
        if fig and fig not in seen:
            seen.append(fig)
    return "; ".join(seen)


def _role_ids(corpus: dict[str, Any]) -> list[str]:
    return [str(r.get("id")) for r in corpus.get("roles") or [] if isinstance(r, dict)]


def _today() -> datetime.date:
    """Today's date; one seam so tests are not time bombs."""
    return datetime.date.today()


@dataclass
class AnswerResult:
    ok: bool
    text: str
    proposal_id: int | None = None
    declined: bool = False


def _find_question(
    conn: sqlite3.Connection, key: str, corpus: dict[str, Any]
) -> tuple[Question | None, dict[str, Any] | None]:
    """(open question, None), or (None, pending proposal) for a key answered
    but not yet confirmed (a new answer replaces it), or (None, None)."""
    for qn in aggregate(conn, corpus):
        if qn.key == key:
            return qn, None
    for p in pending_proposals():
        if p.get("key") == key:
            return None, p
    return None, None


def _choose_kind(kind: str, question_kind: str, key: str) -> str:
    k = (kind or "").strip().lower()
    if not k:
        return "skill" if question_kind in SKILL_KINDS else "bullet"
    if k not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)} (or empty), got {kind!r}")
    if k == "skill" and not skill_topic(key):
        raise ValueError(SKILL_KIND_REFUSED)
    return k


SKILL_KIND_REFUSED = (
    "a skill_index item needs a tool or method question (its name is the skill); this "
    "answer can only become a bullet, so answer without kind=skill"
)


def skill_topic(key: str) -> bool:
    """`key` is a tool or method question: the only answers that may become a
    skill_index item (review 2026-10-09, skill-answers: an experience answer
    filed as a skill wrote the experience's label, "healthcare", as a
    skill)."""
    topic = TOPIC_BY_KEY.get((key or "").strip())
    return topic is not None and topic.kind in SKILL_KINDS


def _check_confidence(value: str) -> str:
    v = (value or "").strip().lower()
    if v and v not in CONFIDENCES:
        raise ValueError(
            f"metric_confidence must be one of {', '.join(CONFIDENCES)}, got {value!r}"
        )
    return v


def _check_role(role_id: str, corpus: dict[str, Any]) -> str:
    rid = (role_id or "").strip()
    if rid and rid not in _role_ids(corpus):
        raise ValueError(f"no role '{rid}' in the corpus; roles: {', '.join(_role_ids(corpus))}")
    return rid


AMBIGUOUS_HELP = (
    "nothing recorded: that may be a no or a fact. A decline is kept, so to decline "
    'answer just "no" (or "I haven\'t used it"), or decline explicitly (`jobkit answer '
    "{key} --decline`, MCP answer_question(key, decline=true)). To propose a fact, give just "
    "the fact (where, how long, what you did), not opening with a negation; only a threshold "
    'may, after "yes" ("Yes, never below 115% of quota at <company>").'
)
# a skill answer (skill_bound): "yes, never below quota" is refused there too
SKILL_AMBIGUOUS_HELP = (
    "nothing recorded: that may be a no or a fact{doubt}. This answer can add {name} to "
    "your skill_index, a name a tailored resume may list, so it must be one plain sentence "
    "about your own use: {form}. Nothing else is accepted: no other person, other tool, "
    "negation, hedge, plan or question mark. A decline is kept, so to decline answer just "
    '"no" (or "I haven\'t used it"), or decline explicitly (`jobkit answer {key} --decline`, '
    'MCP answer_question(key, decline=true)). To propose it, e.g.: "{example}".'
)


def skill_example(name: str, places: Iterable[str] = ()) -> str:
    """An answer the accepted form takes, for the refusal messages."""
    where = [str(p).strip() for p in places if str(p).strip()]
    return f"Yes, I used {name} weekly" + (f" at {where[0]}" if where else "") + " for two years"


def tool_help(key: str, others: list[Topic], places: Iterable[str] = ()) -> str:
    """Why an answer to `key` that names `others` (other_tools) is refused,
    and what the human can do instead."""
    places = list(places)
    names = ", ".join(t.label for t in others)
    first = others[0]
    return (
        f"A tool or method enters your corpus only as a skill_index item, from its own "
        f"question ({', '.join(t.key for t in others)}), as one plain sentence about your own "
        f"use: {skill_form(first.label, places)}. Named in any other answer it is refused, "
        f'whoever used it: a bullet that says "the team used {first.label}" would let a '
        f"letter claim it as yours. Answer '{key}' again without naming {names}, or decline "
        f"it (`jobkit answer {key} --decline`, MCP answer_question(key, decline=true)). For "
        f'{first.label} itself: `jobkit answer {first.key} "{skill_example(first.label, places)}"`'
        " while that question is open (`jobkit questions --all` lists them)."
    )


def ambiguous_help(key: str, answer: str, *, skill: bool, places: Iterable[str] = ()) -> str:
    """The refusal for an ambiguous answer to `key`, naming what made a skill
    answer doubtful, or the other tool or method it names. `places`:
    corpus_places."""
    if not skill:
        others = other_tools(answer, key)
        if others:
            names = ", ".join(t.label for t in others)
            return f"nothing recorded: it names {names}. {tool_help(key, others, places)}"
        return AMBIGUOUS_HELP.format(key=key)
    places = list(places)
    topic = TOPIC_BY_KEY.get(key)
    name = topic.label if topic is not None else "it"
    doubt = skill_doubt(answer, key, places=places)
    return SKILL_AMBIGUOUS_HELP.format(
        key=key,
        name=name,
        doubt=f" ({doubt})" if doubt else "",
        form=skill_form(name, places),
        example=skill_example(name, places),
    )


def written_text(p: dict[str, Any]) -> str:
    """The exact text confirming proposal `p` writes (its `text`): a bullet's
    text_long and text_short, or a skill_index value's text before its
    confirmation note. The proposals file is plain YAML, so it may differ from
    the answer beside it; confirm holds THIS text to the rules the answer is
    held to (reads_as_no) and shows it (review 2026-10-09)."""
    return str(p.get("text") or "")


def verbatim_skill(p: dict[str, Any]) -> str:
    """A skill proposal as the human must check it: the skill_index name next
    to the exact text the entry writes (written_text), never shortened (the
    name is the claim a resume may list, whatever the value says; a cut could
    hide "...; I did not"), and their whole answer, verbatim, when it differs
    from that text. Empty for a bullet (entry_preview)."""
    if p.get("kind") != "skill":
        return ""
    text = written_text(p)
    said = str(p.get("answer") or "")
    out = f'skill_index {p.get("name")}, writing exactly: "{text}"'
    if said and said != text:
        out += f' (your answer, verbatim: "{said}")'
    return out


def entry_preview(p: dict[str, Any], written: str = "") -> str:
    """What confirming `p` writes, with the exact text, never shortened: the
    skill item (verbatim_skill), or the bullet (`written`, its id once
    written)."""
    skill = verbatim_skill(p)
    if skill:
        return skill
    text = written_text(p)
    said = str(p.get("answer") or "")
    out = f'{written or "a new bullet"}, writing exactly: "{text}"'
    if said and said != text:
        out += f' (your answer, verbatim: "{said}")'
    return out


def answer(
    conn: sqlite3.Connection,
    key: str,
    answer_: str,
    *,
    evidence: str = "",
    metric_confidence: str = "",
    role_id: str = "",
    kind: str = "",
    decline: bool = False,
) -> AnswerResult:
    """Record the human's answer to question `key`. A "no" — the whole
    answer is a decline, or `decline` is set — is declined (undecline takes
    it back); an answer that may be a no or a fact is refused; anything else
    becomes a pending corpus proposal (a new answer to a key with a pending
    proposal replaces it). Writes the proposals file and events; never the
    corpus."""
    try:
        return _answer(
            conn,
            key,
            answer_,
            evidence=evidence,
            metric_confidence=metric_confidence,
            role_id=role_id,
            kind=kind,
            decline=decline,
        )
    except SourceError as e:
        return AnswerResult(False, f"nothing recorded: {e}. Fix that file, then answer again.")


def _answer(
    conn: sqlite3.Connection,
    key: str,
    answer_: str,
    *,
    evidence: str,
    metric_confidence: str,
    role_id: str,
    kind: str,
    decline: bool,
) -> AnswerResult:
    key = (key or "").strip()
    text_in = (answer_ or "").strip()
    if not key or not (text_in or decline):
        return AnswerResult(False, "nothing recorded: give a question key and an answer")
    corpus = load_corpus()
    if settled_keys(conn).get(key) == DECLINED:
        return AnswerResult(
            False,
            f"nothing recorded: you declined '{key}' already; it stays a gap. To answer it "
            f"after all: `jobkit undecline {key}` (MCP undecline_question), then answer.",
        )
    question, previous = _find_question(conn, key, corpus)
    if question is not None:
        q_text, q_kind, jobs = question.question, question.kind, list(question.jobs)
    elif previous is not None:
        q_text = str(previous.get("question") or "")
        q_kind = str(previous.get("question_kind") or "fact")
        jobs = [int(j) for j in previous.get("jobs") or []]
    else:
        return AnswerResult(
            False,
            f"nothing recorded: no open question '{key}'. `jobkit questions --all` "
            "(MCP get_questions(show_all=true)) lists them.",
        )
    barred = human_answer_barred(q_text)
    if barred:
        return AnswerResult(
            False,
            f"nothing recorded: '{key}' asks for demographic data or compensation history "
            f"({barred}); those never enter the corpus (hard rules 4, 5).",
        )
    if (kind or "").strip().lower() == "skill" and not skill_topic(key):
        return AnswerResult(False, f"nothing recorded: {SKILL_KIND_REFUSED}")
    skill = skill_bound(key, kind=kind, question_kind=q_kind)
    places = corpus_places(corpus)
    verdict = classify_answer(text_in, key, skill=skill, places=places) if text_in else "decline"
    if decline and verdict == "fact":
        return AnswerResult(
            False,
            f"nothing recorded: decline was asked for, but the answer reads as a fact "
            f'("{text_in[:80]}"). Decline with no answer (or just "no"), or answer without '
            "decline to propose the fact.",
        )
    if verdict == "ambiguous" and not decline:
        return AnswerResult(False, ambiguous_help(key, text_in, skill=skill, places=places))
    proposals = load_proposals()
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
    if decline or verdict == "decline":
        closed = [p for p in proposals if p.get("key") == key and p.get("status") == PENDING]
        for p in closed:
            p["status"] = "declined"
        if closed:
            save_proposals(proposals)
        conn.execute(
            q.RECORD_EVENT,
            {
                "job_id": None,
                "kind": DECLINED,
                "detail": json.dumps(
                    {"key": key, "answer": text_in, "jobs": jobs, "explicit": decline}
                ),
            },
        )
        conn.commit()
        log("questions.declined", key=key, jobs=jobs, explicit=decline)
        return AnswerResult(
            True,
            f"recorded: you declined '{key}'. It will not be asked again, and the gap stays "
            "a gap: nothing is added to the corpus. If that was a mistake: "
            f"`jobkit undecline {key}` (MCP undecline_question).",
            declined=True,
        )
    try:
        entry_kind = _choose_kind(kind, q_kind, key)
        confidence = _check_confidence(metric_confidence)
        rid = _check_role(role_id, corpus)
    except ValueError as e:
        return AnswerResult(False, f"nothing recorded: {e}")
    text = answer_text(text_in)
    if not text:
        return AnswerResult(
            False,
            "nothing recorded: a yes needs the fact itself (where, how long, what you did). "
            "That text, in your words, is what the corpus entry will say.",
        )
    # the text the entry would write is held to the answer's rules too
    # (text_reads_as_no): "Yes, no." is a fact as given, but would write "No."
    if text_reads_as_no(
        {"key": key, "kind": entry_kind, "question_kind": q_kind, "answer": text_in, "text": text},
        places=places,
    ):
        return AnswerResult(
            False,
            f"{ambiguous_help(key, text, skill=skill, places=places)} (Your answer would be "
            f'written as exactly "{text}".)',
        )
    for p in proposals:
        if p.get("key") == key and p.get("status") == PENDING:
            p["status"] = "superseded"
    new_id = max([int(p.get("id") or 0) for p in proposals] or [0]) + 1
    topic = TOPIC_BY_KEY.get(key)
    proposal: dict[str, Any] = {
        "id": new_id,
        "key": key,
        "question_kind": q_kind,
        "question": q_text,
        "answer": text_in,
        "kind": entry_kind,
        "name": topic.label if topic else None,
        "text": text,
        "evidence": evidence.strip() or None,
        "metric_confidence": confidence or None,
        "role_id": rid or None,
        "jobs": jobs,
        "status": PENDING,
        "answered_at": now,
    }
    proposals.append(proposal)
    save_proposals(proposals)
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": None,
            "kind": PROPOSED,
            "detail": json.dumps({"id": new_id, "key": key, "kind": entry_kind, "jobs": jobs}),
        },
    )
    conn.execute(
        q.RECORD_EVENT,
        {"job_id": None, "kind": ANSWERED, "detail": json.dumps({"key": key, "proposal": new_id})},
    )
    conn.commit()
    log("questions.proposed", key=key, proposal=new_id, kind=entry_kind, jobs=jobs)
    lines = [
        f"proposal {new_id} recorded for '{key}' (data/corpus_proposals.yaml). Nothing is in "
        "your corpus yet.",
        "It would add:",
        *(f"  {ln}" for ln in render_entry(proposal, corpus).splitlines()),
    ]
    if entry_kind == "skill":
        lines.append(
            f"Check it: {verbatim_skill(proposal)}. Once confirmed, a tailored resume may "
            f"list {proposal['name']} as a skill."
        )
    missing = missing_fields(proposal)
    if missing:
        lines.append(
            f"Before it can be confirmed you must set: {', '.join(missing)} (never defaulted)."
        )
    lines.append(confirm_hint(proposal))
    return AnswerResult(True, "\n".join(lines), proposal_id=new_id)


def missing_fields(p: dict[str, Any]) -> list[str]:
    out = []
    if not str(p.get("evidence") or "").strip():
        out.append("evidence")
    if str(p.get("metric_confidence") or "") not in CONFIDENCES:
        out.append("metric_confidence (verified | estimate | none)")
    if p.get("kind") == "bullet" and not str(p.get("role_id") or "").strip():
        out.append("role_id")
    return out


def confirm_hint(p: dict[str, Any]) -> str:
    args = []
    for m in missing_fields(p):
        if m.startswith("evidence"):
            args.append('--evidence "<where this is defensible from>"')
        elif m.startswith("metric_confidence"):
            args.append("--metric-confidence verified|estimate|none")
        elif m == "role_id":
            args.append("--role-id <role id>")
    cli = " ".join([f"jobkit confirm-proposal {p.get('id')}", *args])
    return f"Confirm: `{cli}` (MCP confirm_proposal({p.get('id')}, ...))."


def undecline(conn: sqlite3.Connection, key: str) -> AnswerResult:
    """Take back a decline: the key is open again (a 'question_undeclined'
    event), so it can be answered and returns to the questions while an open
    job still has the gap. Nothing else changes: proposals the decline
    closed stay closed."""
    key = (key or "").strip()
    if not key:
        return AnswerResult(False, "nothing changed: give the question key")
    if settled_keys(conn).get(key) != DECLINED:
        return AnswerResult(False, f"nothing changed: '{key}' is not declined")
    conn.execute(
        q.RECORD_EVENT,
        {"job_id": None, "kind": UNDECLINED, "detail": json.dumps({"key": key})},
    )
    conn.commit()
    log("questions.undeclined", key=key)
    return AnswerResult(
        True,
        f"'{key}' is open again: answer it with `jobkit answer {key} \"<your answer>\"` (MCP "
        "answer_question). It returns to the daily questions while an open job still lists it.",
    )


def _skill_value(p: dict[str, Any], day: datetime.date) -> str:
    return (
        f"{p.get('text')} (confirmed {day.isoformat()}; evidence: {p.get('evidence') or '?'}; "
        f"metric_confidence: {p.get('metric_confidence') or '?'})"
    )


def _next_bullet_id(corpus: dict[str, Any]) -> str:
    nums = [
        int(m.group(1))
        for b in corpus.get("bullets") or []
        if isinstance(b, dict) and (m := re.fullmatch(r"b_(\d+)", str(b.get("id") or "")))
    ]
    return f"b_{(max(nums) if nums else 0) + 1:03d}"


def _bullet_entry(p: dict[str, Any], corpus: dict[str, Any]) -> dict[str, Any]:
    text = str(p.get("text") or "")
    key = str(p.get("key") or "")
    topic = TOPIC_BY_KEY.get(key)
    entry: dict[str, Any] = {
        "id": _next_bullet_id(corpus),
        "role_id": p.get("role_id"),
        "text_long": text,
        "text_short": text,
        "tags": [_slug(topic.label) if topic else key.replace("_", "-")],
    }
    figures = _figures(text)
    if figures:
        entry["metric"] = figures
    entry["metric_confidence"] = p.get("metric_confidence")
    entry["evidence"] = p.get("evidence")
    return entry


def render_entry(
    p: dict[str, Any], corpus: dict[str, Any], day: datetime.date | None = None
) -> str:
    """The exact entry confirming `p` would write (unset fields shown as ?)."""
    day = day or _today()
    if p.get("kind") == "skill":
        return f"skill_index → {p.get('name')}: {_skill_value(p, day)}"
    entry = _bullet_entry(p, corpus)
    shown = {k: (v if v not in (None, "") else "?") for k, v in entry.items()}
    return "bullets →\n" + yaml.safe_dump([shown], sort_keys=False, allow_unicode=True).rstrip()


# ---------------------------------------------------------------------------
# confirming: the only write to data/career_corpus.yaml
# ---------------------------------------------------------------------------


class CorpusWriteError(RuntimeError):
    """The corpus could not be updated safely; nothing was written."""


# a top-level key or a document marker; a list item at column 0 ("- id: b_001",
# PyYAML's default layout) is inside the block above it
_TOP_KEY = re.compile(r"^(?:[^\s#-]|---)")


def _block_bounds(lines: list[str], key: str) -> tuple[int, int] | None:
    """(index of the `key:` line, index one past the block's last line)."""
    head = re.compile(rf"^{re.escape(key)}:")
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _TOP_KEY.match(lines[i])), len(lines))
    # comments and blank lines at column 0 before the next key belong to it
    while end - 1 > start and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    return start, end


def insert_into_block(text: str, key: str, snippet: str, *, list_block: bool) -> str:
    """Insert `snippet` (unindented YAML for one list item or one mapping
    entry) at the end of the top-level `key:` block, indented like the block's
    existing items. Comments and everything else stay as they are. A missing
    block is appended; an empty inline one ("key: []", "key: {}") is opened."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    bounds = _block_bounds(lines, key)
    indent = "  "
    if bounds is None:
        body = "".join(indent + ln if ln.strip() else ln for ln in snippet.splitlines(True))
        return "".join(lines) + f"\n{key}:\n" + body
    start, end = bounds
    inline = lines[start][len(key) + 1 :].split("#", 1)[0].strip()
    if inline in ("[]", "{}", "~", "null"):
        lines[start] = f"{key}:\n"
    elif inline:
        raise CorpusWriteError(f"the corpus '{key}' block is written inline; make it a block first")
    item = re.compile(r"^(\s*)-\s") if list_block else re.compile(r"^(\s+)[^\s#-]")
    for ln in lines[start + 1 : end]:
        m = item.match(ln)
        if m:
            indent = m.group(1)
            break
    body = "".join(indent + ln if ln.strip() else ln for ln in snippet.splitlines(True))
    return "".join(lines[:end]) + body + "".join(lines[end:])


def _never_claim_hit(text: str, corpus: dict[str, Any]) -> str | None:
    cons = constraints(corpus)
    for item in [*cons["never_claim_tools"], *cons["never_claim_phrases"]]:
        if phrase_pattern(item).search(text):
            return item
    return None


@dataclass
class ConfirmResult:
    ok: bool
    text: str
    written: str = ""
    jobs: list[int] = field(default_factory=list)


def _launch_rescore(conn: sqlite3.Connection, job_ids: list[int]) -> str:
    launch = background.spawn(conn, "rescore", "--job-ids", ",".join(str(j) for j in job_ids))
    return launch.describe()


def confirm(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    evidence: str = "",
    metric_confidence: str = "",
    role_id: str = "",
    launch: Callable[[sqlite3.Connection, list[int]], str] | None = None,
) -> ConfirmResult:
    """The human's confirmation of one proposal: refuse one whose answer reads
    as a no, fill in what they set now, refuse while evidence,
    metric_confidence (or a bullet's role) is unset, then write the entry into
    the corpus, mark the proposal confirmed, record a 'proposal_confirmed'
    event and start a background rescore of the jobs the question listed."""
    try:
        return _confirm(
            conn,
            proposal_id,
            evidence=evidence,
            metric_confidence=metric_confidence,
            role_id=role_id,
            launch=launch,
        )
    except SourceError as e:
        return ConfirmResult(False, f"not confirmed: {e}. The corpus is unchanged.")


def reads_as_no(p: dict[str, Any], *, places: Iterable[str] = ()) -> bool:
    """The proposal's answer, as the human gave it, is a decline or may be one
    ("I have never used Clari", "Never below 115% of quota"), or names a tool
    or method other than its question's own (other_tools: "the team used
    Clari for the forecast"). Such an answer never reaches the corpus: a
    skill_index key names a tool the resume may list, whatever its value
    says, so a skill proposal must also be the one accepted sentence of the
    human's own use (skill_doubt, with `places`: corpus_places): "Definitely
    not", "My manager used Clari", "Clari? Only heard of it", "Yes, daily"
    never reach skill_index, and no bullet names a tool, even when recorded
    under an older, looser rule. The exact text the entry would write
    (written_text) is held to the same rules (text_reads_as_no): the
    proposals file is plain YAML, and confirm used to check the answer but
    write the text, so a text edited to "I have never used Clari" beside the
    answer "I used Clari weekly at Haulmark" was written (review 2026-10-09)."""
    said = str(p.get("answer") or p.get("text") or "")
    key = str(p.get("key") or "")
    if classify_answer(said, key, skill=_proposal_skill_bound(p), places=places) != "fact":
        return True
    return text_reads_as_no(p, places=places)


# The one negation a bullet's text may open with: a threshold the human never
# fell below, then a figure ("Never below 115% of quota at Haulmark", "Not
# under 95% attainment", "Never fewer than 12 meetings a week"). Any other
# opening negation is a denial, whatever "yes" came before it: "Yes, not in
# healthcare." wrote the bullet "Not in healthcare." and "Yes, no healthcare
# at all." wrote "No healthcare at all.", each tagged with the question's
# topic (review 2026-10-09, third pass).
_THRESHOLD_OPENING = re.compile(
    r"(?i)^\s*(?:never|not)\s+(?:below|under|less\s+than|fewer\s+than|lower\s+than)"
    r"\s+(?=[~≈$£€]?\s*\d)"
)


def text_reads_as_no(p: dict[str, Any], *, places: Iterable[str] = ()) -> bool:
    """The exact text confirming `p` would write (written_text) is empty, is a
    decline or may be one, names a tool or method other than its question's
    own, or, for a skill proposal, is not the one accepted sentence of the
    human's own use (classify_answer, skill_doubt): the rules `p`'s answer is
    held to, applied to what is written. One case is excused for a bullet: a
    fact the human opened with "yes" because it opens with a threshold ("Yes —
    never below 115% of quota" is recorded as "Never below 115% of quota"),
    when the text is exactly that answer less its "yes" (answer_text), opens
    with "never"/"not" + below/under/less than/fewer than/lower than + a
    figure (_THRESHOLD_OPENING), and has no other negation and denies nothing.
    "Yes, not in healthcare." (recorded as "Not in healthcare.") and "Yes, no."
    (recorded as "No.") are never excused."""
    text = written_text(p)
    key = str(p.get("key") or "")
    if not text.strip() or other_tools(text, key):
        return True
    skill = _proposal_skill_bound(p)
    verdict = classify_answer(text, key, skill=skill, places=places)
    if verdict == "fact":
        return False
    if verdict == "decline" or skill:
        return True
    said = str(p.get("answer") or "")
    if _AFFIRMATIVE.match(said) is None or answer_text(said) != text:
        return True
    threshold = _THRESHOLD_OPENING.match(text)
    if threshold is None:
        return True
    rest = _words(text[threshold.end() :])
    if any(w in _SKILL_NEGATORS or w.endswith("n't") for w in rest):
        return True  # "Never below 115% of quota; not in healthcare"
    return _DENIES_IT.search(_normalized(text, subject_pattern(key))) is not None


def _proposal_skill_bound(p: dict[str, Any]) -> bool:
    return skill_bound(
        str(p.get("key") or ""),
        kind=str(p.get("kind") or ""),
        question_kind=str(p.get("question_kind") or ""),
    )


def _confirm(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    evidence: str,
    metric_confidence: str,
    role_id: str,
    launch: Callable[[sqlite3.Connection, list[int]], str] | None,
) -> ConfirmResult:
    proposals = load_proposals()
    p = next((x for x in proposals if int(x.get("id") or 0) == proposal_id), None)
    if p is None:
        return ConfirmResult(False, f"not confirmed: no proposal {proposal_id}")
    if p.get("status") != PENDING:
        return ConfirmResult(False, f"not confirmed: proposal {proposal_id} is {p.get('status')}")
    corpus = load_corpus()
    if not corpus:
        return ConfirmResult(False, f"not confirmed: no corpus at {CORPUS_PATH}")
    places = corpus_places(corpus)
    key = str(p.get("key") or "")
    if p.get("kind") == "skill":
        # the name written is the question's own topic, never another one
        # (the proposals file is plain YAML)
        topic = TOPIC_BY_KEY.get(key)
        if not skill_topic(key) or topic is None or p.get("name") != topic.label:
            log("questions.confirm_refused_skill", proposal=proposal_id, key=key)
            return ConfirmResult(
                False,
                f"not confirmed: proposal {proposal_id} would write skill_index "
                f"'{p.get('name')}' for question '{key}'. {SKILL_KIND_REFUSED}; a skill item's "
                "name is always the tool or method the question asked about. Answer again "
                f"(`jobkit answer {key} ...`) or decline it.",
            )
    if reads_as_no(p, places=places):
        # the exact text confirming would write, whole, and the answer beside
        # it when they differ: the human sees what was checked
        said = str(p.get("answer") or p.get("text") or "")
        text = written_text(p)
        exact = f'confirming would write exactly "{text}"' + (
            f' (your answer, verbatim: "{said}")' if said != text else ""
        )
        skill = _proposal_skill_bound(p)
        others = [] if skill else other_tools(f"{said}\n{text}", key)
        if others:
            # a bullet naming a tool or method (recorded before this rule):
            # it would source a first-person claim of that tool in a letter
            log("questions.confirm_refused_tool", proposal=proposal_id, key=key)
            return ConfirmResult(
                False,
                f"not confirmed: your answer to '{key}' names "
                f"{', '.join(t.label for t in others)}; {exact}. " + tool_help(key, others, places),
            )
        log("questions.confirm_refused_negated", proposal=proposal_id, key=key)
        if skill:
            doubt = skill_doubt(said, key, places=places) or (
                skill_doubt(text, key, places=places) if text.strip() else "it writes no text"
            )
            topic = TOPIC_BY_KEY.get(key)
            name = topic.label if topic is not None else str(p.get("name") or "it")
            how = (
                f"A skill_index item needs an answer that plainly says you yourself used {name}"
                + (f" ({doubt})" if doubt else "")
                + f": answer again as one plain sentence about your own use, "
                f'{skill_form(name, places)}; e.g. "{skill_example(name, places)}".'
            )
        else:
            how = (
                "If it is a fact, answer again with just the fact, not opening with a "
                'negation; only a threshold may, after "yes" ("Yes, never below 115% of '
                'quota at <company>").'
            )
        return ConfirmResult(
            False,
            f"not confirmed: your answer to '{key}' reads as a no, or may be one; {exact}. "
            f"A no never enters the corpus. To decline: `jobkit answer {key} "
            f"--decline` (MCP answer_question(key, decline=true)). {how}",
        )
    try:
        confidence = _check_confidence(metric_confidence)
        rid = _check_role(role_id, corpus)
    except ValueError as e:
        return ConfirmResult(False, f"not confirmed: {e}")
    changed = False
    for field_, value in (("evidence", evidence.strip()), ("metric_confidence", confidence)):
        if value:
            p[field_], changed = value, True
    if rid:
        p["role_id"], changed = rid, True
    if p.get("kind") == "bullet" and p.get("role_id") not in _role_ids(corpus):
        p["role_id"] = None
    if changed:
        save_proposals(proposals)
    missing = missing_fields(p)
    if missing:
        return ConfirmResult(
            False,
            f"not confirmed: set {', '.join(missing)} first; jobkit never defaults them "
            f"(hard rules 6, 8). {confirm_hint(p)}\nIt would write {entry_preview(p)}.",
        )
    text = written_text(p)
    name = str(p.get("name") or "")
    hit = _never_claim_hit(f"{name}\n{text}", corpus)
    if hit:
        return ConfirmResult(
            False,
            f"not confirmed: your corpus constraints say never to claim '{hit}'. If that has "
            "changed, edit constraints in data/career_corpus.yaml first.",
        )
    day = _today()
    try:
        written = _write_entry(p, corpus, day)
    except CorpusWriteError as e:
        log("questions.confirm_failed", proposal=proposal_id, error=str(e)[:200])
        return ConfirmResult(False, f"not confirmed: {e}. The corpus is unchanged.")
    p["status"] = "confirmed"
    p["confirmed_at"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
    p["written"] = written
    save_proposals(proposals)
    jobs = [int(j) for j in p.get("jobs") or []]
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": None,
            "kind": CONFIRMED,
            "detail": json.dumps(
                {"id": proposal_id, "key": p.get("key"), "written": written, "jobs": jobs}
            ),
        },
    )
    conn.commit()
    log("questions.confirmed", proposal=proposal_id, key=p.get("key"), written=written)
    lines = [
        f"confirmed proposal {proposal_id}: wrote {entry_preview(p, written)} to {CORPUS_PATH}."
    ]
    if jobs:
        try:
            started = (launch or _launch_rescore)(conn, jobs)
            lines.append(f"Re-scoring the {len(jobs)} job(s) it helps: {started}.")
        except RuntimeError as e:
            ids = ",".join(str(j) for j in jobs)
            lines.append(
                f"The rescore could not start ({e}); run `jobkit rescore --job-ids {ids}`."
            )
    return ConfirmResult(True, "\n".join(lines), written=written, jobs=jobs)


def _write_entry(p: dict[str, Any], corpus: dict[str, Any], day: datetime.date) -> str:
    """Insert the proposal's entry into data/career_corpus.yaml as text, verify
    the result parses to the old corpus plus exactly that entry, then replace
    the file atomically (the previous version is kept as career_corpus.yaml.bak).
    Returns what was written ("bullet b_025" / "skill_index Clari")."""
    path = CORPUS_PATH
    old_text = path.read_text()
    expected = copy.deepcopy(corpus)
    comment = f"# from your answer to question {p.get('key')} (proposal {p.get('id')}, {day})\n"
    if p.get("kind") == "skill":
        name = str(p.get("name") or "").strip()
        index = corpus.get("skill_index")
        if index is not None and not isinstance(index, dict):
            raise CorpusWriteError("skill_index is not a mapping")
        existing = {str(k).strip().lower() for k in (index or {})}
        if not name or name.lower() in existing:
            raise CorpusWriteError(f"skill_index already has '{name}'")
        value = _skill_value(p, day)
        snippet = comment + yaml.safe_dump(
            {name: value}, sort_keys=False, allow_unicode=True, width=4096
        )
        new_text = insert_into_block(old_text, "skill_index", snippet, list_block=False)
        expected["skill_index"] = {**(index or {}), name: value}
        written = f"skill_index {name}"
    else:
        entry = _bullet_entry(p, corpus)
        if any(
            str(b.get("text_long") or "").strip() == entry["text_long"]
            for b in corpus.get("bullets") or []
            if isinstance(b, dict)
        ):
            raise CorpusWriteError("a bullet with this exact text is already in the corpus")
        snippet = comment + yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=4096)
        new_text = insert_into_block(old_text, "bullets", snippet, list_block=True)
        expected["bullets"] = [*(corpus.get("bullets") or []), entry]
        written = f"bullet {entry['id']}"
    try:
        parsed = yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        raise CorpusWriteError(f"the edited corpus would not parse ({e})") from e
    if parsed != expected:
        raise CorpusWriteError("the edited corpus did not equal the old corpus plus the new entry")
    shutil.copyfile(path, path.with_name(path.name + ".bak"))
    _atomic_write(path, new_text)
    return written


# ---------------------------------------------------------------------------
# rendering: CLI `jobkit questions`, MCP get_questions, digest, get_queue
# ---------------------------------------------------------------------------


def _question_lines(n: int, qn: Question, *, jobs_shown: int = 4) -> list[str]:
    jobs = sorted(qn.jobs.values(), key=lambda j: -(j.fit or 0))
    more = f"; and {len(jobs) - jobs_shown} more" if len(jobs) > jobs_shown else ""
    yes = (
        f'A yes written as "I used {qn.label} ..." (then only how often, how long, at which '
        "of your companies, for what) becomes a proposal for a skill_index item"
        if qn.default_entry == "skill"
        else "A yes that names no tool or method becomes a proposal for a bullet"
    )
    return [
        f"{n}. [{qn.key}] {qn.question}",
        f"   Why: {qn.why_text()}",
        f"   Helps {qn.n_jobs} open job(s): "
        + "; ".join(j.brief() for j in jobs[:jobs_shown])
        + more,
        f'   Answer: `jobkit answer {qn.key} "<your answer>"` (MCP answer_question). {yes}; '
        "a plain no (or --decline) is recorded and not asked again.",
    ]


def _proposal_lines(pending: list[dict[str, Any]]) -> list[str]:
    if not pending:
        return []
    lines = [f"Awaiting your confirmation ({len(pending)} proposal(s), nothing written yet):"]
    for p in pending:
        # the exact text the entry writes, never cut (entry_preview)
        what = entry_preview(p)
        missing = missing_fields(p)
        lines.append(
            f"- proposal {p.get('id')} [{p.get('key')}] → {what}"
            + (f" — still needs {', '.join(missing)}" if missing else "")
        )
        lines.append(f"  {confirm_hint(p)}")
    return lines


# A file the questions read that does not parse (SourceError), or a bad
# questions.max_per_day (ValueError): the questions section becomes one line,
# and whatever shows it (the digest with its staging halts, get_queue) still
# renders everything else (review 2026-10-09).
_UNAVAILABLE_ERRORS = (yaml.YAMLError, ValueError)


def _unavailable(e: Exception, where: str) -> str:
    log("questions.unavailable", where=where, error=str(e)[:300])
    return f"Questions unavailable: {e}. Nothing else is affected; fix it and they come back."


def render(
    conn: sqlite3.Connection,
    *,
    all_: bool = False,
    today: datetime.date | None = None,
    cap: int | None = None,
) -> str:
    """`jobkit questions` / MCP get_questions: today's questions (recorded
    against the daily cap), or with all_ the full ranked list (not recorded),
    then the proposals waiting for confirmation. One "Questions unavailable"
    line when a file they read does not parse."""
    try:
        return _render(conn, all_=all_, today=today, cap=cap)
    except _UNAVAILABLE_ERRORS as e:
        return _unavailable(e, "get_questions")


def _render(
    conn: sqlite3.Connection, *, all_: bool, today: datetime.date | None, cap: int | None
) -> str:
    pending = pending_proposals()  # before todays() records anything as asked
    lines: list[str] = []
    if all_:
        ranked = aggregate(conn)
        lines.append(
            f"All {len(ranked)} open question(s), most jobs helped first:"
            if ranked
            else "No open questions: every gap the system can name is answered, declined, "
            "or already in your corpus."
        )
        for n, qn in enumerate(ranked, 1):
            lines += _question_lines(n, qn)
    else:
        d = todays(conn, cap=cap, today=today)
        lines.append(_daily_head(d))
        for n, qn in enumerate(d.shown, 1):
            lines += _question_lines(n, qn)
    if pending:
        lines += ["", *_proposal_lines(pending)]
    return "\n".join(lines)


def _daily_head(d: Daily) -> str:
    if d.cap == 0:
        return "Questions are switched off (questions.max_per_day: 0 in config/filters.yaml)."
    rest = d.open_total - len(d.shown)
    tail = (
        f" {rest} more open; `jobkit questions --all` (get_questions(show_all=true)) lists them."
        if rest > 0
        else ""
    )
    if not d.shown:
        if d.settled_today:
            return (
                f"No more questions today: {d.settled_today} asked today already settled "
                f"(cap {d.cap}/day).{tail}"
            )
        return "No open questions for you." + tail
    return (
        f"Questions for you today ({len(d.shown)} of at most {d.cap}/day"
        + (f"; {d.settled_today} asked earlier today already settled" if d.settled_today else "")
        + f").{tail}"
    )


def digest_lines(conn: sqlite3.Connection, today: datetime.date | None = None) -> list[str]:
    """The digest's "Questions for you" section; empty when there is nothing
    to ask and nothing to confirm; one "Questions unavailable" line when a
    file they read does not parse (the rest of the digest is unaffected)."""
    try:
        pending = pending_proposals()  # before todays() records anything as asked
        d = todays(conn, today=today)
    except _UNAVAILABLE_ERRORS as e:
        return ["## Questions for you", _unavailable(e, "digest"), ""]
    if not d.shown and not pending:
        return []
    lines = ["## Questions for you", _daily_head(d)]
    for n, qn in enumerate(d.shown, 1):
        lines += _question_lines(n, qn)
    if pending:
        lines += ["", *_proposal_lines(pending)]
    lines.append("")
    return lines


def queue_lines(conn: sqlite3.Connection) -> list[str]:
    """A compact block for MCP get_queue: today's questions, one line each,
    and how many proposals wait for confirmation; one "Questions unavailable"
    line when a file they read does not parse (the queue is unaffected)."""
    try:
        pending = pending_proposals()  # before todays() records anything as asked
        d = todays(conn)
    except _UNAVAILABLE_ERRORS as e:
        return [_unavailable(e, "get_queue")]
    if not d.shown and not pending:
        return []
    lines = [f"Questions for you today ({len(d.shown)} of at most {d.cap}; get_questions for why):"]
    for qn in d.shown:
        lines.append(f"- [{qn.key}] {qn.question} — helps {qn.n_jobs} open job(s)")
    if pending:
        lines.append(
            f"{len(pending)} answer(s) wait for your confirmation before they reach the corpus "
            "(get_questions, then confirm_proposal)."
        )
    return lines
