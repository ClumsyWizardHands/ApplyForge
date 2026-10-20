"""Self-review: "ask itself, then adjust" (feature request 2026-10-09).

After each artifact is drafted (resume bullet selection, cover letter, drafted
novel answer) the model reviews its own draft. Only a passing resume selection
is ever revised, and the revision is kept only when every bullet it renders is
its corpus text word for word (sixth review) and it passes every check. A cover
letter or drafted answer is reviewed and reported only, never revised, and a
failing draft's review never reaches its regeneration (fifth review of the
self-review, 2026-10-09). Questions for the human are persisted as events,
and the llm cache stops re-reviews. Offline: every LLM call is faked
(llm.call_json, or the CLI backend under the real llm.call for the cache
test), nothing is rendered."""

from __future__ import annotations

import ast
import copy
import datetime
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import llm, mcp_server
from jobkit.autofill import form_spec
from jobkit.config import Filters, load_filters
from jobkit.pipeline import self_review, tailor
from jobkit.validate import authenticity
from jobkit.validate.claims import drafting_profile
from jobkit.validate.revision import change_vocabulary, unsourced_change_terms
from tests.test_claims import (
    FABRIKAM_JD,
    GOOD_CITED,
    GOOD_LETTER,
    JUDGE_PASS,
    LUMON_LETTER,
    FakeLLM,
)
from tests.test_targeting import GOOD_SEL, _corpus_with_older_role, _sel

TODAY = datetime.date(2026, 10, 8)
REAL_CALL_JSON = llm.call_json
REAL_FORGET = llm.forget
PASS = {"verdict": "pass", "notes": "", "tells": []}
REVISION_MARK = "SELF-REVIEW OF YOUR PREVIOUS DRAFT"

GOOD_REVIEW: dict[str, Any] = {
    "self_check": [
        {
            "question": "Which must-haves are unaddressed though the corpus supports them?",
            "answer": "None: b_052 covers leading teams.",
        },
        {
            "question": "Is there anything I should update or change to give the best answer here?",
            "answer": "No.",
        },
    ],
    "verdict": "good",
    "changes": [],
    "questions_for_human": [],
}


def _revise(what: str, ids: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "self_check": [{"question": "Anything to change?", "answer": what}],
        "verdict": "revise",
        "changes": [{"what": what, "why": "the role asks for it", "bullet_ids": ids}],
        "questions_for_human": [],
        **extra,
    }


# the letter plus one more cited, sourced fact (b_058, on the resume)
REVISED_LETTER = GOOD_LETTER.replace(
    "Since March 2024",
    "At Marlow Payroll I made President's Club in 2019 and 2020. Since March 2024",
)
REVISED_CITED = [*GOOD_CITED, "b_058"]
# the adversarial revision: "mention managing SDRs" — the corpus never says so
SDR_LETTER = GOOD_LETTER.replace(
    "Since March 2024", "At Haulmark I also managed SDRs. Since March 2024"
)
# A change the screen keeps (corpus words only). The adversarial tests below
# pair it with a writer reply that still adds an unsourced claim, as a model
# may, so the checks that gate the revision itself are what is exercised.
TEAM_CHANGE = "Say more about leading the team, from b_052"

# Fifth review of the self-review (2026-10-09), the blocker: the b_052 sentence
# moved word for word to follow the ledgerline sentence, so "there" now
# attributes the 9-AE team to ledgerline. Every word comes from its own cited
# bullet.
TEAM_SENTENCE = (
    "I also led 9 AEs there, reporting to the General Manager, and built the channel-partner "
    "program from 3 to 17 reseller partners in ~18 months."
)
LEDGERLINE_SENTENCE = (
    "Earlier, as Head of Revenue Operations at ledgerline, I launched usage-based pricing for "
    "the SMB lending line, adding $2.4M in net-new ARR in its first year."
)
assert f" {TEAM_SENTENCE}" in GOOD_LETTER and LEDGERLINE_SENTENCE in GOOD_LETTER
MOVED_LETTER = GOOD_LETTER.replace(f" {TEAM_SENTENCE}", "").replace(
    LEDGERLINE_SENTENCE, f"{LEDGERLINE_SENTENCE} {TEAM_SENTENCE}"
)
# ...and claims built only from the cited bullets' words ("merchant" is
# b_049's, "~18 months" b_052's)
RAN_MERCHANT = "I ran the merchant team for ~18 months."


@pytest.fixture()
def env(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[FakeLLM, int]]:
    """A Fabrikam Payments job with a JD, requirements and gaps; fake LLM, no PDF."""
    (jobkit_home / "data" / "career_corpus.yaml").write_text(
        yaml.safe_dump(_corpus_with_older_role())
    )
    monkeypatch.setattr(tailor, "_today", lambda: TODAY)
    monkeypatch.setattr(authenticity, "_default_embed", lambda t: [[1.0, 0.0] for _ in t])

    def fake_render(template: str, data: dict[str, Any], out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data))
        return out

    monkeypatch.setattr(tailor, "render_pdf", fake_render)
    monkeypatch.setattr(tailor, "page_count", lambda p: 1)
    conn.execute(
        "INSERT INTO companies (name, normalized_name) VALUES ('Fabrikam Payments', 'fabrikam')"
    )
    conn.execute("INSERT INTO companies (name, normalized_name) VALUES ('Lumon', 'lumon')")
    jid = conn.execute(
        """INSERT INTO jobs (company_id, external_id, title, normalized_title,
           company_name, normalized_company, location_raw, location_bucket, remote,
           cluster_key, jd_html, jd_hash, apply_url, status)
           VALUES (1, 'f1', 'Director of Revenue Operations', 'director of revenue operations',
                   'Fabrikam Payments', 'fabrikam',
                   'Remote, United States', 'remote-us', 1, 'k', ?, 'h-fabrikam',
                   'https://x', 'approved')""",
        (f"<p>{FABRIKAM_JD}</p>",),
    ).lastrowid
    assert jid is not None
    conn.execute(
        "INSERT INTO job_requirements (job_id, must_have, nice_to_have, implied) "
        "VALUES (?, ?, ?, ?)",
        (
            jid,
            json.dumps(["led a team", "has sold"]),
            json.dumps(["dashboards"]),
            json.dumps(["weekly revenue meeting"]),
        ),
    )
    conn.execute(
        "INSERT INTO scores (job_id, fit_score, gaps) VALUES (?, 80, ?)",
        (jid, json.dumps(["no home-services background"])),
    )
    conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'approved')", (jid,))
    conn.commit()

    def setup(**replies: list[dict[str, Any]]) -> tuple[FakeLLM, int]:
        replies.setdefault("tailor", [_sel(*GOOD_SEL)])
        replies.setdefault("cover", [{"letter": GOOD_LETTER, "bullets_used": GOOD_CITED}])
        replies.setdefault("critic", [PASS])
        replies.setdefault("self_review", [GOOD_REVIEW])
        fake = FakeLLM(dict(replies))
        monkeypatch.setattr(llm, "call_json", fake.call_json)
        monkeypatch.setattr(llm, "forget", fake.forget)
        return fake, jid

    return setup


def _events(conn: sqlite3.Connection, jid: int, kind: str) -> list[dict[str, Any]]:
    return [
        json.loads(r[0])
        for r in conn.execute(
            "SELECT detail FROM events WHERE job_id=? AND kind=? ORDER BY id", (jid, kind)
        )
    ]


def _outcomes(conn: sqlite3.Connection, jid: int) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for d in _events(conn, jid, "self_review"):
        out.setdefault(d["artifact"], []).append((d["verdict"], d["outcome"]))
    return out


def _revisions(prompts: list[str]) -> list[str]:
    return [p for p in prompts if REVISION_MARK in p]


# ---------------------------------------------------------------------------
# 1. good verdict: no revision, review and report only
# ---------------------------------------------------------------------------


def test_good_verdict_means_no_revision(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    fake, jid = env()
    paths = tailor.tailor_job(conn, jid)

    # one review per artifact, no revision call
    assert len(fake.prompts["self_review"]) == 2
    assert len(fake.prompts["tailor"]) == 1 and len(fake.prompts["cover"]) == 1
    assert not _revisions(fake.prompts["tailor"] + fake.prompts["cover"])
    assert _outcomes(conn, jid) == {
        "resume": [("good", "no_change")],
        "cover_letter": [("good", "no_change")],
    }

    # what the review sees: JD requirements, gaps, track, the full labeled
    # corpus (ids, roles, dates, metrics, skill index), the draft, no violations
    resume_review, cover_review = fake.prompts["self_review"]
    assert "YOUR DRAFT (resume bullet selection)" in resume_review
    assert '"led a team"' in resume_review and '"weekly revenue meeting"' in resume_review
    assert "- no home-services background" in resume_review
    assert "ROLE TRACK: sales_exec" in resume_review
    assert "b_041 | Keyward Security · Inside Sales Representative" in resume_review
    assert "metric: 12–15 new accounts/month" in resume_review
    assert "MEDDIC: taught it to the AE team" in resume_review
    assert "role_old | Halvorsen Outfitters · Store Manager" in resume_review
    assert "(none — the draft passed every check)" in resume_review
    assert "Is there anything I should update or change to give the best answer here?" in (
        resume_review
    )
    assert "YOUR DRAFT (cover letter)" in cover_review
    assert "Fabrikam Payments doubled its payment volume" in cover_review
    assert "may cite ONLY the bullets on this resume: b_047, b_052, b_049, b_058" in cover_review
    assert "{" + "draft}" not in cover_review and "{" + "scope}" not in cover_review

    report = Path(paths["self_review"]).read_text()
    assert "## Resume bullet selection" in report and "## Cover letter" in report
    assert report.count("verdict good, no change") == 2
    assert "What it asked itself" in report and "b_052 covers leading teams" in report
    assert "## Questions for Test" in report and "None: the corpus covers" in report
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["self_review"]["outcomes"] == {
        "resume": ["no_change"],
        "cover_letter": ["no_change"],
    }
    assert Path(paths["cover"]).exists()
    assert (Path(paths["resume"]).parent / "cover.txt").read_text() == GOOD_LETTER


# ---------------------------------------------------------------------------
# 2. revision accepted: it passes every check and replaces the draft
# ---------------------------------------------------------------------------


def test_resume_revision_accepted_letter_review_reported_only(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    """The resume's revision passes every check and replaces the selection.
    The letter's review asks for a sourced change, and it is reported only:
    the writer is never called again and the letter that passed ships."""
    revised_sel = _sel(*GOOD_SEL, ("b_044", "long"))
    fake, jid = env(
        tailor=[_sel(*GOOD_SEL), revised_sel],
        cover=[
            {"letter": GOOD_LETTER, "bullets_used": GOOD_CITED},
            {"letter": REVISED_LETTER, "bullets_used": REVISED_CITED},
        ],
        self_review=[
            _revise("Add b_044: the onboarding result answers 'led a team'", ["b_044"]),
            _revise("Use b_058, the strongest unused IC result", ["b_058"]),
        ],
    )
    paths = tailor.tailor_job(conn, jid)

    # the resume revision is one writer call carrying the change list and the draft
    (sel_rev,) = _revisions(fake.prompts["tailor"])
    assert "Add b_044: the onboarding result" in sel_rev and '"bullet_id": "b_047"' in sel_rev
    # the letter is drafted once; its review reaches no writer
    assert len(fake.prompts["cover"]) == 1 and not _revisions(fake.prompts["cover"])
    # the letter's sources follow the revised resume
    assert "b_044 | Haulmark · Regional Sales Manager" in fake.prompts["cover"][0]

    prov = json.loads(Path(paths["provenance"]).read_text())
    assert [b["bullet_id"] for b in prov["bullets"]][-1] == "b_044"
    out = Path(paths["resume"]).parent
    assert (out / "cover.txt").read_text() == GOOD_LETTER
    assert prov["cover_bullets_used"] == GOOD_CITED
    assert _outcomes(conn, jid) == {
        "resume": [("revise", "accepted")],
        "cover_letter": [("revise", "not_revised")],
    }
    resume_ev, cover_ev = _events(conn, jid, "self_review")
    assert resume_ev["added"] == ["b_044"] and resume_ev["dropped"] == []
    assert cover_ev["added"] == [] and cover_ev["original_failed"] is False
    report = Path(paths["self_review"]).read_text()
    assert report.count("verdict revise, revision accepted") == 1
    assert "**Bullets added:** b_044" in report
    cover_section = report.split("## Cover letter")[1]
    assert "verdict revise, not revised" in cover_section
    assert self_review.REPORT_ONLY_PROSE in cover_section
    assert "**Changes it suggested (not applied):**" in cover_section
    assert "Use b_058, the strongest unused IC result" in cover_section
    stage = conn.execute("SELECT stage FROM applications WHERE job_id=?", (jid,)).fetchone()[0]
    assert stage == "tailored"


# ---------------------------------------------------------------------------
# 3. revision rejected by a validator: the original ships (adversarial)
# ---------------------------------------------------------------------------


# Revisions a writer could return for a PASSING letter. Each was a false
# acceptance, or a near one, while a passing letter could be revised: the
# fact check passes most of them, and the word-for-word revision gate that
# replaced it could not see a moved sentence or a claim made of its cited
# bullets' words (fifth review of the self-review, 2026-10-09). A passing
# letter is now never revised, so none of them can ship.
NEVER_REVISED_LETTERS = [
    pytest.param(MOVED_LETTER, id="moved-team-sentence-to-ledgerline"),
    pytest.param(f"{GOOD_LETTER} {RAN_MERCHANT}", id="ran-the-merchant-team"),
    pytest.param(f"{GOOD_LETTER} I owned the entire company revenue.", id="company-revenue"),
    pytest.param(f"{GOOD_LETTER} I led a team of 9 channel managers.", id="channel-managers"),
    pytest.param(
        f"{GOOD_LETTER} I launched usage-based pricing for the sales org.", id="pricing-for-sales"
    ),
    pytest.param(SDR_LETTER, id="sdrs"),
    pytest.param(
        GOOD_LETTER.replace(
            "Since March 2024",
            "At Haulmark I also managed the sales development team. Since March 2024",
        ),
        id="sales-development-team",
    ),
    pytest.param(
        f"{GOOD_LETTER} I would bring years of field service operations leadership.",
        id="modal-claim",
    ),
    pytest.param(
        f"{GOOD_LETTER} You get a manager with years of trade licensing experience.",
        id="target-sentence",
    ),
]


@pytest.mark.parametrize("revised", NEVER_REVISED_LETTERS)
def test_a_passing_letter_is_never_revised(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]], revised: str
) -> None:
    """MANDATORY (Hard Rules 3, 8): the review of a passing letter asks for a
    change, and a writer would return `revised`. No writer is asked: the
    review is reported only, and the letter that passed the fact check and
    the critic ships."""
    fake, jid = env(
        cover=[
            {"letter": GOOD_LETTER, "bullets_used": GOOD_CITED},
            {"letter": revised, "bullets_used": GOOD_CITED},
        ],
        self_review=[GOOD_REVIEW, _revise(TEAM_CHANGE, ["b_052", "b_049"])],
    )
    paths = tailor.tailor_job(conn, jid)

    out = Path(paths["resume"]).parent
    assert len(fake.prompts["cover"]) == 1
    assert not _revisions(fake.prompts["tailor"] + fake.prompts["cover"])
    assert (out / "cover.txt").read_text() == GOOD_LETTER
    assert json.loads(Path(paths["provenance"]).read_text())["cover_bullets_used"] == GOOD_CITED
    (_resume, cover_ev) = _events(conn, jid, "self_review")
    assert (cover_ev["verdict"], cover_ev["outcome"]) == ("revise", "not_revised")
    assert cover_ev["discarded_because"] == [] and cover_ev["original_failed"] is False
    # the shipped letter's critic run is the only one
    rows = conn.execute(
        "SELECT verdict FROM authenticity_runs WHERE job_id=? AND artifact='cover_letter'",
        (jid,),
    ).fetchall()
    assert [r[0] for r in rows] == ["pass"]
    report = Path(paths["self_review"]).read_text().split("## Cover letter")[1]
    assert self_review.REPORT_ONLY_PROSE in report and TEAM_CHANGE in report
    stage = conn.execute("SELECT stage FROM applications WHERE job_id=?", (jid,)).fetchone()[0]
    assert stage == "tailored"


def test_adversarial_resume_revision_with_an_unsourced_team_is_discarded(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    bad = _sel(*GOOD_SEL)
    bad["selected"][1]["rendered"] = "Managed a team of 8 SDRs and 9 AEs."
    fake, jid = env(
        tailor=[_sel(*GOOD_SEL), bad],
        self_review=[_revise(TEAM_CHANGE, ["b_052"]), GOOD_REVIEW],
    )
    paths = tailor.tailor_job(conn, jid)
    prov = json.loads(Path(paths["provenance"]).read_text())
    rendered = " ".join(b["rendered"] for b in prov["bullets"])
    assert "SDR" not in rendered and "Led a team of 9 account executives" in rendered
    resume_ev = _events(conn, jid, "self_review")[0]
    assert (resume_ev["artifact"], resume_ev["outcome"]) == ("resume", "discarded")
    assert "tailor" in fake.forgotten


# Sixth review of the self-review (2026-10-09), the blocker: a revision that
# rendered b_052 as its own text plus a sentence passed provenance (it checks
# numbers, proper nouns and never-claim terms) and authenticity (the drift pass
# flags new words but they never change the verdict), and shipped. TEAM_CHANGE
# passes the change screen, so a review can ask for exactly this. A revision
# now renders every bullet as its named variant's corpus text, word for word.
_B005 = next(b for b in _corpus_with_older_role()["bullets"] if b["id"] == "b_052")
ADDED_SENTENCE = f"{_B005['text_long'].strip()} I also managed the sales development team."
REWORDED = [
    pytest.param(ADDED_SENTENCE, "long", id="added-sentence"),
    # a light trim, as a first draft may make: the partner figures dropped,
    # which the metric-keeping step would restore (the rule runs before it adjusts)
    pytest.param(
        "Led a team of 9 account executives, reporting to the General Manager.",
        "long",
        id="trimmed",
    ),
    pytest.param(_B005["text_short"], "long", id="other-variants-text"),
]


@pytest.mark.parametrize(("rendered", "variant"), REWORDED)
def test_a_resume_revision_never_rewords_a_bullet(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    rendered: str,
    variant: str,
) -> None:
    """MANDATORY (Hard Rules 3, 8): the revision's b_052 is not its named
    variant's corpus text word for word. It is discarded before any check
    adjusts it or records an authenticity run, and the selection that passed
    ships."""
    bad = _sel(*GOOD_SEL)
    bad["selected"][1].update(rendered=rendered, variant=variant)
    fake, jid = env(
        tailor=[_sel(*GOOD_SEL), bad],
        self_review=[_revise(TEAM_CHANGE, ["b_052"]), GOOD_REVIEW],
    )
    paths = tailor.tailor_job(conn, jid)

    (sel_rev,) = _revisions(fake.prompts["tailor"])
    assert "word for word" in sel_rev and TEAM_CHANGE in sel_rev
    prov = json.loads(Path(paths["provenance"]).read_text())
    shipped = {b["bullet_id"]: b["rendered"] for b in prov["bullets"]}
    assert shipped["b_052"] == _B005["text_long"].strip()
    assert "sales development team" not in json.dumps(prov)
    resume_ev = _events(conn, jid, "self_review")[0]
    assert (resume_ev["artifact"], resume_ev["outcome"]) == ("resume", "discarded")
    assert [(v["bullet_id"], v["kind"]) for v in resume_ev["discarded_because"]] == [
        ("b_052", "revision_reworded")
    ]
    assert "tailor" in fake.forgotten
    report = Path(paths["self_review"]).read_text()
    assert "- revision_reworded: b_052 is not its long variant's corpus text" in report
    # only the shipped selection was ever reviewed for authenticity
    runs = conn.execute(
        "SELECT COUNT(*) FROM authenticity_runs WHERE job_id=? AND artifact='resume'", (jid,)
    ).fetchone()[0]
    assert runs == 1
    assert "sales development team" not in fake.prompts["cover"][0]
    stage = conn.execute("SELECT stage FROM applications WHERE job_id=?", (jid,)).fetchone()[0]
    assert stage == "tailored"


def test_a_resume_revision_with_a_new_word_flagged_is_discarded(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the check chain leaves to render is held to the same rule, and a
    drift flag with new words discards the revision (a word-for-word revision
    has none; this guards the chain itself). The drift pass is stubbed to flag
    one on the revision's added bullet."""
    real = authenticity.drift_check

    def drift(
        selected: list[dict[str, Any]], corpus: dict[str, Any], cfg: Any, embed: Any = None
    ) -> tuple[list[dict[str, Any]], int]:
        flags, reverted = real(selected, corpus, cfg, embed)
        if any(s.get("bullet_id") == "b_044" for s in selected):
            flags.append({"bullet_id": "b_044", "new_words": ["sdrs"], "action": "flagged"})
        return flags, reverted

    monkeypatch.setattr(authenticity, "drift_check", drift)
    fake, jid = env(
        tailor=[_sel(*GOOD_SEL), _sel(*GOOD_SEL, ("b_044", "long"))],
        self_review=[_revise("Add b_044", ["b_044"]), GOOD_REVIEW],
    )
    paths = tailor.tailor_job(conn, jid)
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert [b["bullet_id"] for b in prov["bullets"]] == [bid for bid, _v in GOOD_SEL]
    resume_ev = _events(conn, jid, "self_review")[0]
    assert resume_ev["outcome"] == "discarded"
    assert resume_ev["discarded_because"] == [
        {
            "bullet_id": "b_044",
            "kind": "drift_new_words",
            "detail": "b_044 has words its source lacks: sdrs",
        }
    ]
    assert "tailor" in fake.forgotten
    rows = conn.execute(
        "SELECT verdict FROM authenticity_runs WHERE job_id=? AND artifact='resume' ORDER BY id",
        (jid,),
    ).fetchall()
    assert len(rows) == 3 and rows[-1][0] == rows[0][0]  # newest = what ships


def test_reworded_holds_each_bullet_to_its_named_variant() -> None:
    corpus = _corpus_with_older_role()
    ok = [
        {"bullet_id": "b_052", "variant": "long", "rendered": f"  {_B005['text_long']}\n"},
        {"bullet_id": "b_052", "variant": "short", "rendered": _B005["text_short"]},
    ]
    assert tailor._reworded(ok, corpus) == []
    bad: list[object] = [
        {"bullet_id": "b_052", "variant": "long", "rendered": ADDED_SENTENCE},
        {"bullet_id": "b_052", "variant": "long", "rendered": _B005["text_short"]},
        {"bullet_id": "b_052", "variant": "", "rendered": _B005["text_long"]},
        {"bullet_id": "b_999", "variant": "long", "rendered": _B005["text_long"]},
        {"bullet_id": "b_052", "variant": "long", "rendered": ""},
        "b_052",
    ]
    assert len(tailor._reworded(bad, corpus)) == len(bad)
    assert tailor._reworded("junk", corpus) != []


def test_resume_revision_failing_authenticity_keeps_original_and_its_verdict(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resume revision passes provenance but the critic fails it: the
    original selection ships, and its own authenticity run is again the newest
    row. (The structural pass is stubbed, as in the test below.)"""
    monkeypatch.setattr(authenticity, "structural_resume", lambda bullets, cfg: [])
    slop = {"verdict": "fail", "notes": "reads machine-written", "tells": []}
    fake, jid = env(
        tailor=[_sel(*GOOD_SEL), _sel(*GOOD_SEL, ("b_044", "long"))],
        # critic calls: original resume, revised resume, cover letter
        critic=[PASS, slop, PASS],
        self_review=[_revise("Add b_044", ["b_044"]), GOOD_REVIEW],
    )
    paths = tailor.tailor_job(conn, jid)
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert [b["bullet_id"] for b in prov["bullets"]] == [bid for bid, _v in GOOD_SEL]
    assert "tailor" in fake.forgotten
    rows = conn.execute(
        "SELECT verdict FROM authenticity_runs WHERE job_id=? AND artifact='resume' ORDER BY id",
        (jid,),
    ).fetchall()
    assert [r[0] for r in rows] == ["pass", "fail", "pass"]  # newest = what ships
    assert _outcomes(conn, jid)["resume"] == [("revise", "discarded")]
    stage = conn.execute("SELECT stage FROM applications WHERE job_id=?", (jid,)).fetchone()[0]
    assert stage == "tailored"


def test_resume_revision_with_a_worse_authenticity_verdict_is_discarded(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resume is held to the letter's rule: a revision whose authenticity
    verdict is worse than the selection it would replace (soft_flag after
    pass) is discarded, though soft_flag alone would pass its checks. The clean
    selection ships, and its own authenticity run is again the newest row.
    (The test corpus's own punctuation density soft-flags every selection, so
    the structural pass is stubbed: the original passes clean, and the critic's
    verdict is the only difference.)"""
    monkeypatch.setattr(authenticity, "structural_resume", lambda bullets, cfg: [])
    soft = {"verdict": "soft_flag", "notes": "reads a little list-like", "tells": []}
    fake, jid = env(
        tailor=[_sel(*GOOD_SEL), _sel(*GOOD_SEL, ("b_044", "long"))],
        # critic calls: original resume, revised resume, cover letter
        critic=[PASS, soft, PASS],
        self_review=[_revise("Add b_044", ["b_044"]), GOOD_REVIEW],
    )
    paths = tailor.tailor_job(conn, jid)

    assert len(_revisions(fake.prompts["tailor"])) == 1
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert [b["bullet_id"] for b in prov["bullets"]] == [bid for bid, _v in GOOD_SEL]
    assert "b_044 |" not in fake.prompts["cover"][0]  # the letter cites the shipped resume
    assert "tailor" in fake.forgotten
    assert _outcomes(conn, jid)["resume"] == [("revise", "discarded")]
    resume_ev = _events(conn, jid, "self_review")[0]
    assert {
        "kind": "authenticity_verdict",
        "detail": "soft_flag (the original was pass)",
    } in resume_ev["discarded_because"]
    rows = conn.execute(
        "SELECT verdict FROM authenticity_runs WHERE job_id=? AND artifact='resume' ORDER BY id",
        (jid,),
    ).fetchall()
    assert [r[0] for r in rows] == ["pass", "soft_flag", "pass"]  # newest = what ships
    assert "authenticity_verdict" in Path(paths["self_review"]).read_text()


# ---------------------------------------------------------------------------
# 4. original failing: the violations feed a review that is reported only
# ---------------------------------------------------------------------------


# The Lumon letter fixed: the flagged sentences rewritten, every sentence in
# its cited bullet's words (b_047).
LUMON_FIXED = (
    "At Haulmark I grew the Northeast region from ~$3.2M to ~$5.6M in annual bookings. I "
    "added 41 new carrier accounts, routing ~18% of new bookings through reseller partners "
    "by 2025."
)
# The fix as first written: the claim in its bullet's words, the rest about the
# target. A regeneration is checked by the fact check and the critic only, as
# at aca26cc, and it ships.
LUMON_FIXED_TARGET = (
    "I'm excited about the Director of Revenue Operations role at Fabrikam Payments. At "
    "Haulmark I grew the Northeast region from ~$3.2M to ~$5.6M in annual bookings. "
    "Fabrikam Payments doubled its payment volume in 2025."
)
# A sentence the fact check cannot see (no number, no proper noun but the
# cited employer), riding on a letter that fails for another reason (fifth
# review of the self-review, 2026-10-09).
INSIDE_SALES = "At Haulmark I also managed the inside sales team and its hiring pipeline."
KEEP_INSIDE_SALES = "Keep the line about managing the inside sales team; drop the Lumon mention"


@pytest.mark.parametrize("fixed", [LUMON_FIXED, LUMON_FIXED_TARGET])
def test_failing_letter_review_is_reported_only(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]], fixed: str
) -> None:
    """The Lumon letter fails the fact check. Its violations feed the review,
    and the review is reported only: the regeneration carries the fact-check
    feedback alone, as at aca26cc, never the review's changes. An honest
    regeneration that passes the fact check and the critic ships."""
    fake, jid = env(
        cover=[
            {"letter": LUMON_LETTER, "bullets_used": ["b_047"]},
            {"letter": fixed, "bullets_used": ["b_047"]},
        ],
        self_review=[
            GOOD_REVIEW,
            _revise("Remove Lumon; lead with b_047 in its own words", ["b_047"]),
        ],
    )
    paths = tailor.tailor_job(conn, jid)

    cover_review = fake.prompts["self_review"][1]
    fed = cover_review.split("VALIDATOR VIOLATIONS FROM THE LAST ATTEMPT:")[1]
    assert "- wrong_company: names Lumon; the target is Fabrikam Payments" in fed
    assert "the Director of Revenue Operations opening at Lumon" in cover_review
    # the cap is unchanged: one regeneration, carrying the check feedback only
    assert len(fake.prompts["cover"]) == 2
    regen = fake.prompts["cover"][1]
    assert "FAILED THE FACT CHECK" in regen
    assert REVISION_MARK not in regen and "lead with b_047 in its own words" not in regen
    # one review per artifact (max_revision_rounds 1): the passing regeneration
    # is not reviewed again
    assert len(fake.prompts["self_review"]) == 2
    assert (Path(paths["resume"]).parent / "cover.txt").read_text() == fixed
    assert _outcomes(conn, jid)["cover_letter"] == [("revise", "not_revised")]
    cover_ev = _events(conn, jid, "self_review")[1]
    assert (cover_ev["original_failed"], cover_ev["discarded_because"]) == (True, [])
    assert "later_passed" not in cover_ev
    report = Path(paths["self_review"]).read_text()
    assert "Checks the draft had failed (fed to the review)" in report
    assert "wrong_company" in report
    assert "verdict revise, not revised" in report
    assert self_review.REPORT_ONLY_FAILED in report


def test_failing_letter_review_cannot_endorse_an_unseen_claim(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MANDATORY (fifth review of the self-review): the first letter fails on
    Lumon only and also says it managed the inside sales team, which the fact
    check cannot see. The review asks to keep that line. The change took its
    words from the failed draft, rode on the regeneration, and the
    regeneration kept the sentence and shipped. Now the regeneration carries
    the fact-check feedback alone; a writer that keeps the sentence only when
    asked to never writes it, and it does not ship."""
    fake, jid = env(
        cover=[{"letter": f"{LUMON_LETTER} {INSIDE_SALES}", "bullets_used": ["b_047"]}],
        self_review=[GOOD_REVIEW, _revise(KEEP_INSIDE_SALES, ["b_047"])],
    )
    canned = fake.call_json

    def writer(
        conn_: sqlite3.Connection, stage: str, prompt: str, *, cache_key: str | None = None
    ) -> dict[str, Any]:
        reply = canned(conn_, stage, prompt, cache_key=cache_key)
        if stage != "cover" or len(fake.prompts["cover"]) == 1:
            return reply
        # a regeneration keeps the sentence only when its prompt asks for it
        kept = "inside sales team" in prompt
        letter = f"{GOOD_LETTER} {INSIDE_SALES}" if kept else GOOD_LETTER
        return {"letter": letter, "bullets_used": GOOD_CITED}

    monkeypatch.setattr(llm, "call_json", writer)
    paths = tailor.tailor_job(conn, jid)

    assert INSIDE_SALES in fake.prompts["self_review"][1]  # the review saw the sentence
    assert len(fake.prompts["cover"]) == 2
    regen = fake.prompts["cover"][1]
    assert "FAILED THE FACT CHECK" in regen
    assert REVISION_MARK not in regen and "inside sales" not in regen
    shipped = (Path(paths["resume"]).parent / "cover.txt").read_text()
    assert shipped == GOOD_LETTER and "inside sales" not in shipped
    assert _outcomes(conn, jid)["cover_letter"] == [("revise", "not_revised")]
    assert KEEP_INSIDE_SALES in Path(paths["self_review"]).read_text()  # reported only


def test_failing_selection_review_is_reported_only(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    bad = _sel(*GOOD_SEL)
    bad["selected"][0]["rendered"] = "Grew the Northeast region 45% year over year, beating plan."
    fake, jid = env(
        tailor=[bad, _sel(*GOOD_SEL)],
        self_review=[_revise("Render b_047 in its own words, ~18% partner-sourced", ["b_047"])],
    )
    paths = tailor.tailor_job(conn, jid)
    assert "fabricated" in fake.prompts["self_review"][0]
    assert "45%" in fake.prompts["self_review"][0]
    assert len(fake.prompts["tailor"]) == 2
    regen = fake.prompts["tailor"][1]
    assert "FAILED PROVENANCE VALIDATION" in regen
    assert REVISION_MARK not in regen and "Render b_047 in its own words" not in regen
    assert _outcomes(conn, jid)["resume"] == [("revise", "not_revised")]
    assert _events(conn, jid, "self_review")[0]["original_failed"] is True
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert "45%" not in json.dumps(prov["bullets"])


def test_failing_letter_never_ships_and_its_review_is_reported_only(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    """Every draft fails: nothing ships, as at aca26cc, and the report never
    says "the original stands"."""
    lumon = {"letter": LUMON_LETTER, "bullets_used": ["b_047"]}
    fake, jid = env(
        cover=[lumon],
        self_review=[GOOD_REVIEW, _revise("Drop Lumon", ["b_047"])],
    )
    paths = tailor.tailor_job(conn, jid)
    assert paths["cover"] == ""  # rejected, as before: no letter rendered
    assert len(fake.prompts["cover"]) == 3  # the cap is unchanged
    assert not _revisions(fake.prompts["cover"])
    assert _outcomes(conn, jid)["cover_letter"] == [("revise", "not_revised")]
    stage = conn.execute("SELECT stage FROM applications WHERE job_id=?", (jid,)).fetchone()[0]
    assert stage == "approved"
    out = Path(paths["resume"]).parent
    assert (out / "cover_rejected.md").exists()
    report = Path(paths["self_review"]).read_text()
    cover_section = report.split("## Cover letter")[1]
    assert "verdict revise, not revised" in cover_section
    assert self_review.REPORT_ONLY_FAILED in cover_section
    assert "the original stands" not in cover_section
    summary = self_review.summary(conn, jid, out.parent)
    assert "cover_letter round 1" in summary and "verdict revise, not revised" in summary


# ---------------------------------------------------------------------------
# 5. questions for the human are persisted (for package 3)
# ---------------------------------------------------------------------------


QUESTIONS = [
    {
        "key": "SDR Team Size Managed",
        "question": "How many SDRs, if any, reported to you at Haulmark?",
        "why": "The role asks for SDR leadership; the corpus names only AEs.",
        "fact_needed": "number of SDRs managed",
    },
    {  # Hard Rule 5: never asked, never stored
        "key": "current_salary",
        "question": "What is your current salary?",
        "why": "to calibrate",
        "fact_needed": "salary history",
    },
    {"key": "", "question": "no key"},
]


def test_questions_for_human_are_persisted_once_per_job(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    review = {**GOOD_REVIEW, "questions_for_human": QUESTIONS}
    _fake, jid = env(self_review=[review])
    paths = tailor.tailor_job(conn, jid)
    (q,) = _events(conn, jid, "question_open")
    assert q == {
        "key": "sdr_team_size_managed",
        "question": "How many SDRs, if any, reported to you at Haulmark?",
        "why": "The role asks for SDR leadership; the corpus names only AEs.",
        "fact_needed": "number of SDRs managed",
        "job_id": jid,
        "artifact": "resume",
    }
    report = Path(paths["self_review"]).read_text()
    assert "## Questions for Test" in report and "sdr_team_size_managed" in report
    assert "salary" not in report.split("## Questions for Test")[1]
    # tailoring again raises the same key once, not twice
    tailor.tailor_job(conn, jid)
    assert len(_events(conn, jid, "question_open")) == 1


def _summary_tools(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> tuple[Callable[..., str], Callable[[list[str]], str]]:
    from typer.testing import CliRunner

    from jobkit import cli

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    monkeypatch.setattr(cli, "_db", lambda: conn)

    def run_cli(args: list[str]) -> str:
        result = CliRunner().invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        return result.output

    return mcp_server.get_self_review, run_cli


def test_self_review_is_reachable_from_mcp_and_cli(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = _sel(*GOOD_SEL)
    bad["selected"][1]["rendered"] = "Managed a team of 8 SDRs and 9 AEs."
    _fake, jid = env(
        tailor=[_sel(*GOOD_SEL), bad],
        self_review=[
            _revise(TEAM_CHANGE, ["b_052"], questions_for_human=QUESTIONS),
            {**GOOD_REVIEW, "questions_for_human": QUESTIONS},
        ],
    )
    tailor.tailor_job(conn, jid)
    get_self_review, run_cli = _summary_tools(conn, monkeypatch)

    text = get_self_review(job_id=jid)
    assert "resume round 1" in text and "revision discarded" in text
    assert "cover_letter round 1" in text and "verdict good, no change" in text
    assert "questions raised for the candidate: 1" in text
    assert "sdr_team_size_managed: How many SDRs" in text
    assert "report: " in text and "self_review.md" in text
    full = get_self_review(job_id=jid, full=True)
    assert "# Self-review — Fabrikam Payments · Director of Revenue Operations" in full
    conn.executescript(mcp_server.PINS_SCHEMA)
    conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (4, ?)", (jid,))
    assert get_self_review(n=4) == text
    assert "give a queue number" in get_self_review()

    out = run_cli(["self-review", str(jid)])
    assert "sdr_team_size_managed" in out and "revision discarded" in out
    assert "# Self-review" in run_cli(["self-review", str(jid), "--full"])


# ---------------------------------------------------------------------------
# 6. cost guard: the llm cache stops a re-review of the same draft
# ---------------------------------------------------------------------------


def test_cache_prevents_re_review(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-review runs once per artifact per (JD hash, corpus hash, prompt
    versions): tailoring the same job again is served from llm_cache — no
    review, draft or accepted-revision call reaches the model."""
    _fake, jid = env()
    # the real llm.call_json and its cache; only the CLI backend is faked
    monkeypatch.setattr(llm, "call_json", REAL_CALL_JSON)
    monkeypatch.setattr(llm, "forget", REAL_FORGET)
    calls: dict[str, int] = {}

    def backend(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        calls[stage] = calls.get(stage, 0) + 1
        reply: dict[str, Any]
        if stage == "tailor":
            reply = (
                _sel(*GOOD_SEL, ("b_044", "long")) if REVISION_MARK in prompt else _sel(*GOOD_SEL)
            )
        elif stage == "cover":
            reply = {"letter": GOOD_LETTER, "bullets_used": GOOD_CITED}
        elif stage == "claim_judge":
            reply = JUDGE_PASS
        elif stage == "self_review":
            reply = (
                _revise("Add b_044", ["b_044"])
                if "YOUR DRAFT (resume bullet selection)" in prompt
                else GOOD_REVIEW
            )
        else:
            reply = PASS
        return json.dumps(reply), {}

    def shipped_ids(paths: dict[str, str]) -> list[str]:
        prov = json.loads(Path(paths["provenance"]).read_text())
        return [b["bullet_id"] for b in prov["bullets"]]

    monkeypatch.setattr(llm, "_call_claude_cli", backend)
    first = tailor.tailor_job(conn, jid)
    assert calls["self_review"] == 2 and calls["cover"] == 1 and calls["tailor"] == 2
    assert shipped_ids(first)[-1] == "b_044"
    critic_calls = calls["critic"]

    second = tailor.tailor_job(conn, jid)
    assert calls["self_review"] == 2  # no re-review
    assert calls["cover"] == 1 and calls["tailor"] == 2  # the accepted revision is cached too
    assert calls["critic"] > critic_calls  # the critic is not cached (unchanged behavior)
    assert shipped_ids(second) == shipped_ids(first)
    keys = [
        r[0] for r in conn.execute("SELECT cache_key FROM llm_cache WHERE stage = 'self_review'")
    ]
    assert len(keys) == 2

    # a self-review prompt edit is a new prompt version and text: a new review
    sr = (Path("prompts") / "self_review.md").resolve().read_text()
    prompts_copy = Path("prompts_copy")
    prompts_copy.mkdir()
    for path in Path("prompts").glob("*.md"):
        (prompts_copy / path.name).write_text(path.read_text())
    (prompts_copy / "self_review.md").write_text(
        re.sub(r'version: "([^"]+)"', r'version: "\1-test"', sr, count=1)
    )
    monkeypatch.setattr(llm, "PROMPTS_DIR", prompts_copy)
    tailor.tailor_job(conn, jid)
    assert calls["self_review"] == 4


# ---------------------------------------------------------------------------
# 7. the knob: one owner, filters.yaml self_review.max_revision_rounds
# ---------------------------------------------------------------------------


def test_max_revision_rounds_knob(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    jobkit_home: Path,
) -> None:
    assert load_filters().self_review_max_rounds == 1  # the shipped default
    assert Filters(raw={}).self_review_max_rounds == 1
    for bad in (-1, "2", True, 1.5):
        with pytest.raises(ValueError, match="max_revision_rounds"):
            _ = Filters(raw={"self_review": {"max_revision_rounds": bad}}).self_review_max_rounds

    cfg = jobkit_home / "config" / "filters.yaml"
    raw = yaml.safe_load(cfg.read_text())
    raw["self_review"]["max_revision_rounds"] = 0
    cfg.write_text(yaml.safe_dump(raw))
    fake, jid = env(self_review=[_revise("Add b_044", ["b_044"])])
    paths = tailor.tailor_job(conn, jid)
    # 0: the review still runs (report + questions), nothing is revised
    assert len(fake.prompts["self_review"]) == 2
    assert not _revisions(fake.prompts["tailor"] + fake.prompts["cover"])
    assert _outcomes(conn, jid) == {
        "resume": [("revise", "not_revised")],
        "cover_letter": [("revise", "not_revised")],
    }
    assert "max_revision_rounds is 0" in Path(paths["self_review"]).read_text()


def test_two_rounds_review_the_accepted_revision_again(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    jobkit_home: Path,
) -> None:
    cfg = jobkit_home / "config" / "filters.yaml"
    raw = yaml.safe_load(cfg.read_text())
    raw["self_review"]["max_revision_rounds"] = 2
    cfg.write_text(yaml.safe_dump(raw))
    fake, jid = env(
        tailor=[_sel(*GOOD_SEL), _sel(*GOOD_SEL, ("b_044", "long"))],
        self_review=[_revise("Add b_044", ["b_044"]), GOOD_REVIEW],
    )
    tailor.tailor_job(conn, jid)
    # resume: review, revise (accepted), review again (good); cover: review (good)
    assert len(fake.prompts["self_review"]) == 3
    assert "b_044 [Haulmark · Regional Sales Manager]" in fake.prompts["self_review"][1]
    assert _outcomes(conn, jid) == {
        "resume": [("revise", "accepted"), ("good", "no_change")],
        "cover_letter": [("good", "no_change")],
    }


def test_a_failed_review_call_keeps_the_checked_draft(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    """A review that errors, or replies without a valid verdict, is not a
    review: the validated draft ships, no event, the log says why."""
    fake, jid = env(self_review=[{"answer": "not a review"}])
    paths = tailor.tailor_job(conn, jid)
    assert not _revisions(fake.prompts["tailor"] + fake.prompts["cover"])
    assert _events(conn, jid, "self_review") == []
    assert (Path(paths["resume"]).parent / "cover.txt").read_text() == GOOD_LETTER
    assert "review failed, the checked draft stands" in Path(paths["self_review"]).read_text()
    log = (Path("logs") / "jobkit.jsonl").read_text()
    assert "self_review.failed" in log and "no valid verdict" in log


# ---------------------------------------------------------------------------
# 8. drafted novel answers
# ---------------------------------------------------------------------------


VP_ASKED = "Our VP of Sales handed me the channel-partner program."
PARTNERS_ANSWER = (
    "At Haulmark I built the channel-partner program from 3 to 17 reseller partners in ~18 months."
)
PARTNERS_PLUS = PARTNERS_ANSWER + " I also led 9 account executives there."  # never stored


def _novel_session(
    conn: sqlite3.Connection, jid: int, corpus: dict[str, Any], rounds: int = 1
) -> self_review.Session:
    ctx = self_review.context_for_job(
        conn, jid, corpus, self_review.text_hash(drafting_profile(corpus)), "other"
    )
    assert ctx is not None and ctx.requirements["must_have"] == ["led a team", "has sold"]
    return self_review.Session(ctx, rounds)


def _draft(
    conn: sqlite3.Connection, jid: int, corpus: dict[str, Any], session: self_review.Session
) -> tuple[str | None, str | None, list[dict[str, str]]]:
    job = {
        "id": jid,
        "title": "Director of Revenue Operations",
        "company_name": "Fabrikam Payments",
    }
    job["jd_html"] = f"<p>{FABRIKAM_JD}</p>"
    return form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=corpus,
        profile=drafting_profile(corpus),
        job=job,
        label="Tell us about a time you stepped up",
        question="Tell us about a time you stepped up",
        known_companies=["Fabrikam Payments", "Lumon"],
        review=session,
        field_identity="q_story",
    )


def test_failing_novel_answer_review_is_reported_only(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    """The answer fails ("VP of Sales"); its violations feed the review, and
    the review is reported only. The one regeneration carries the fact-check
    feedback alone, as at aca26cc; an honest one that passes is returned."""
    fake, jid = env(
        novel=[
            {"answer": VP_ASKED, "bullets_used": ["b_052"]},
            {"answer": PARTNERS_ANSWER, "bullets_used": ["b_052"]},
        ],
        self_review=[
            _revise("Say the General Manager was who I reported to, as b_052 states", ["b_052"])
        ],
    )
    corpus = _corpus_with_older_role()
    session = _novel_session(conn, jid, corpus)
    answer, decline, violations = _draft(conn, jid, corpus, session)
    assert (answer, decline, violations) == (PARTNERS_ANSWER, None, [])
    review_prompt = fake.prompts["self_review"][0]
    assert "YOUR DRAFT (drafted answer to an application-form question)" in review_prompt
    assert "FORM QUESTION: Tell us about a time you stepped up" in review_prompt
    assert "VP" in review_prompt.split("VALIDATOR VIOLATIONS FROM THE LAST ATTEMPT:")[1]
    assert len(fake.prompts["novel"]) == 2
    regen = fake.prompts["novel"][1]
    assert "FAILED THE FACT CHECK" in regen
    assert REVISION_MARK not in regen and "Say the General Manager" not in regen
    (lg,) = session.logs
    assert lg.artifact == "novel:q_story" and [r.outcome for r in lg.rounds] == ["not_revised"]
    assert lg.rounds[0].original_failed
    assert lg.rounds[0].reason == self_review.REPORT_ONLY_FAILED


def test_failing_novel_answer_review_cannot_endorse_an_unseen_claim(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MANDATORY (fifth review of the self-review): the answer's failing draft
    also claims the inside sales team; the review asks to keep it. The
    regeneration never sees that change, so a writer that keeps the sentence
    only when asked to never writes it."""
    fake, jid = env(
        novel=[{"answer": f"{VP_ASKED} {INSIDE_SALES}", "bullets_used": ["b_052"]}],
        self_review=[_revise(KEEP_INSIDE_SALES, ["b_052"])],
    )
    canned = fake.call_json

    def writer(
        conn_: sqlite3.Connection, stage: str, prompt: str, *, cache_key: str | None = None
    ) -> dict[str, Any]:
        reply = canned(conn_, stage, prompt, cache_key=cache_key)
        if stage != "novel" or len(fake.prompts["novel"]) == 1:
            return reply
        kept = "inside sales team" in prompt
        answer = f"{PARTNERS_ANSWER} {INSIDE_SALES}" if kept else PARTNERS_ANSWER
        return {"answer": answer, "bullets_used": ["b_052"]}

    monkeypatch.setattr(llm, "call_json", writer)
    corpus = _corpus_with_older_role()
    session = _novel_session(conn, jid, corpus)
    answer, _decline, _v = _draft(conn, jid, corpus, session)
    regen = fake.prompts["novel"][1]
    assert REVISION_MARK not in regen and "inside sales" not in regen
    assert answer == PARTNERS_ANSWER
    assert [r.outcome for r in session.logs[0].rounds] == ["not_revised"]


# Answers a writer could return for a PASSING answer: each claims what no
# cited bullet says, and most pass the fact check. A passing answer is never
# revised, so none of them is stored (fifth review of the self-review).
NEVER_REVISED_ANSWERS = [
    pytest.param(f"{PARTNERS_ANSWER} {RAN_MERCHANT}", id="ran-the-merchant-team"),
    pytest.param(f"{PARTNERS_ANSWER} I also managed SDRs.", id="sdrs"),
    pytest.param(
        "At Haulmark I led 9 AEs and also managed the sales development team.",
        id="sales-development-team",
    ),
    pytest.param(
        f"{PARTNERS_ANSWER} I would bring years of field service operations leadership.",
        id="modal-claim",
    ),
    pytest.param("Yes. I can bring the trade credentials.", id="credentials"),
]


@pytest.mark.parametrize("revised", NEVER_REVISED_ANSWERS)
def test_a_passing_novel_answer_is_never_revised(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]], revised: str
) -> None:
    """MANDATORY (Hard Rule 8): the answer passed; its review asks for a
    change, and a writer would return `revised`. No writer is asked: the
    answer that passed the fact check is returned."""
    fake, jid = env(
        novel=[
            {"answer": PARTNERS_ANSWER, "bullets_used": ["b_052"]},
            {"answer": revised, "bullets_used": ["b_052"]},
        ],
        self_review=[_revise(TEAM_CHANGE, ["b_052"])],
    )
    corpus = _corpus_with_older_role()
    session = _novel_session(conn, jid, corpus)
    answer, decline, violations = _draft(conn, jid, corpus, session)
    assert (answer, decline, violations) == (PARTNERS_ANSWER, None, [])
    assert len(fake.prompts["novel"]) == 1 and len(fake.prompts["self_review"]) == 1
    (rnd,) = session.logs[0].rounds
    assert (rnd.outcome, rnd.original_failed) == ("not_revised", False)
    assert rnd.reason == self_review.REPORT_ONLY_PROSE


def test_a_passing_novel_answer_with_a_moved_sentence_is_never_revised(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    """The blocker, for an answer: the passing answer is the letter's text;
    the writer would move the 9-AE sentence after the ledgerline one."""
    fake, jid = env(
        novel=[
            {"answer": GOOD_LETTER, "bullets_used": GOOD_CITED},
            {"answer": MOVED_LETTER, "bullets_used": GOOD_CITED},
        ],
        self_review=[_revise("Move the b_052 sentence after the b_049 one", ["b_052", "b_049"])],
    )
    corpus = _corpus_with_older_role()
    session = _novel_session(conn, jid, corpus)
    answer, _decline, _v = _draft(conn, jid, corpus, session)
    assert answer == GOOD_LETTER
    assert len(fake.prompts["novel"]) == 1
    assert [r.outcome for r in session.logs[0].rounds] == ["not_revised"]


def test_novel_answer_review_reported_by_capture(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    monkeypatch: pytest.MonkeyPatch,
    jobkit_home: Path,
) -> None:
    """End to end through capture_form_specs: the stored answer is the one
    that passed (its review is reported only), the review and its question
    are events, and the job's folder gets self_review.md; tailoring later
    adds its artifacts beside it."""
    fake, jid = env(
        novel=[
            {"answer": PARTNERS_ANSWER, "bullets_used": ["b_052"]},
            {"answer": PARTNERS_PLUS, "bullets_used": ["b_052"]},
        ],
        self_review=[
            _revise(
                "Add the team size from b_052",
                ["b_052"],
                questions_for_human=[QUESTIONS[0]],
            ),
            GOOD_REVIEW,
            GOOD_REVIEW,
        ],
    )
    conn.execute("INSERT INTO boards (company_id, ats, token) VALUES (1, 'greenhouse', 'fabrikam')")
    conn.execute("UPDATE jobs SET board_id = 1 WHERE id = ?", (jid,))
    conn.commit()

    class StubMatcher:
        bank: dict[str, Any] = {"scaffolds": []}

        def match(self, label: str, compliance_type: str | None = None, **kw: Any) -> Any:
            return {"kind": "novel"}

    field = {
        "identity": "q_story",
        "label": "Tell us about a time you stepped up",
        "type": "textarea",
        "required": True,
        "options": [],
        "compliance_type": None,
    }
    monkeypatch.setattr(form_spec, "BankMatcher", StubMatcher)
    monkeypatch.setitem(form_spec.FETCHERS, "greenhouse", lambda c, t, e: [copy.deepcopy(field)])
    stats = form_spec.capture_form_specs(conn)
    assert stats["drafted"] == 1
    drafted = json.loads(
        conn.execute("SELECT drafted_answers FROM form_specs WHERE job_id=?", (jid,)).fetchone()[0]
    )
    assert drafted == {"q_story": PARTNERS_ANSWER}
    assert len(fake.prompts["novel"]) == 1
    assert _outcomes(conn, jid) == {"novel:q_story": [("revise", "not_revised")]}
    assert _events(conn, jid, "question_open")[0]["artifact"] == "novel:q_story"
    (folder,) = list((jobkit_home / "out").glob(f"*-{jid}-2026-10-08"))
    report = (folder / "self_review.md").read_text()
    assert "## Drafted answer: Tell us about a time you stepped up" in report

    # tailoring the same job keeps the answer's section in the job's report
    paths = tailor.tailor_job(conn, jid)
    assert Path(paths["self_review"]).parent == folder
    report = Path(paths["self_review"]).read_text()
    assert "## Drafted answer: Tell us about a time you stepped up" in report
    assert "## Resume bullet selection" in report and "## Cover letter" in report


def test_get_artifacts_points_at_the_self_review(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake, jid = env()
    paths = tailor.tailor_job(conn, jid)
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    conn.executescript(mcp_server.PINS_SCHEMA)
    conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (2, ?)", (jid,))
    text = mcp_server.get_artifacts(2)
    assert f"self_review: {paths['self_review']} (get_self_review(2)" in text


def test_normalize_keeps_only_corpus_ids_and_allowed_questions() -> None:
    corpus = _corpus_with_older_role()
    rv = self_review.normalize(
        {
            "verdict": "REVISE",
            "changes": [
                {"what": "Lead with b_058", "why": "", "bullet_ids": ["b_058", "b_999"]},
                {"what": "", "bullet_ids": ["b_052"]},
                "not a change",
            ],
            "questions_for_human": [
                {"key": "gender", "question": "What is your gender identity?"},
                {"key": "Quota Attainment 2024", "question": "What % of quota in 2024?"},
                {"key": "quota-attainment-2024", "question": "Same key again?"},
            ],
        },
        corpus,
    )
    assert rv.verdict == "revise" and rv.wants_revision
    assert rv.changes == [{"what": "Lead with b_058", "why": "", "bullet_ids": ["b_058"]}]
    assert [q["key"] for q in rv.questions] == ["quota_attainment_2024"]
    good = self_review.normalize({"verdict": "good", "changes": [{"what": "x"}]}, corpus)
    assert good.changes == [] and not good.wants_revision
    with pytest.raises(ValueError, match="no valid verdict"):
        self_review.normalize({"verdict": "pass"}, corpus)


# ---------------------------------------------------------------------------
# 9. a failing draft: its regenerations carry the check feedback alone
# ---------------------------------------------------------------------------
# When the draft FAILED its checks, the review is reported only. Its
# regenerations are exactly those of aca26cc: the fact check's or the critic's
# feedback, gated by the fact check and the critic for a letter, the fact
# check for an answer (fifth review of the self-review, 2026-10-09: a change
# list used to ride on the regeneration, and it could endorse a sentence the
# fact check cannot see).


def test_failing_letter_regenerations_carry_only_check_feedback(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    """The Lumon letter fails the fact check; the first regeneration passes
    it but fails the critic; the second passes both and ships. Neither
    regeneration carries the review's changes, and the round says the review
    was reported only, never "the original stands"."""
    slop = {"verdict": "fail", "notes": "reads machine-written", "tells": []}
    fake, jid = env(
        cover=[
            {"letter": LUMON_LETTER, "bullets_used": ["b_047"]},
            {"letter": REVISED_LETTER, "bullets_used": REVISED_CITED},
            {"letter": GOOD_LETTER, "bullets_used": GOOD_CITED},
        ],
        critic=[PASS, slop, PASS],  # resume, regeneration 1, regeneration 2
        self_review=[GOOD_REVIEW, _revise("Drop Lumon; lead with b_047", ["b_047"])],
    )
    paths = tailor.tailor_job(conn, jid)

    out = Path(paths["resume"]).parent
    assert (out / "cover.txt").read_text() == GOOD_LETTER
    assert len(fake.prompts["cover"]) == 3  # the cap is unchanged
    first_regen, second_regen = fake.prompts["cover"][1:]
    assert "FAILED THE FACT CHECK" in first_regen
    assert "FAILED AUTHENTICITY REVIEW" in second_regen
    assert not _revisions(fake.prompts["cover"])
    assert all("Drop Lumon" not in p for p in fake.prompts["cover"])
    _resume, cover_ev = _events(conn, jid, "self_review")
    assert (cover_ev["outcome"], cover_ev["original_failed"]) == ("not_revised", True)
    report = Path(paths["self_review"]).read_text().split("## Cover letter")[1]
    assert "verdict revise, not revised" in report
    assert "the original stands" not in report


def test_failing_letter_whose_every_regeneration_fails_is_sent_to_human(
    conn: sqlite3.Connection,
    env: Callable[..., tuple[FakeLLM, int]],
    jobkit_home: Path,
) -> None:
    """At max_revision_rounds 2 the two failing drafts are each reviewed, and
    both reviews are reported only. No attempt passed: the letter is rejected
    as at aca26cc."""
    cfg = jobkit_home / "config" / "filters.yaml"
    raw = yaml.safe_load(cfg.read_text())
    raw["self_review"]["max_revision_rounds"] = 2
    cfg.write_text(yaml.safe_dump(raw))
    fake, jid = env(
        cover=[{"letter": LUMON_LETTER, "bullets_used": ["b_047"]}],
        self_review=[GOOD_REVIEW, _revise("Drop Lumon", ["b_047"])],
    )
    paths = tailor.tailor_job(conn, jid)

    assert paths["cover"] == ""
    assert len(fake.prompts["cover"]) == 3
    assert not _revisions(fake.prompts["cover"])
    out = Path(paths["resume"]).parent
    assert not (out / "cover.txt").exists()
    assert "wrong_company" in (out / "cover_rejected.md").read_text()
    assert _outcomes(conn, jid)["cover_letter"] == [
        ("revise", "not_revised"),
        ("revise", "not_revised"),
    ]
    assert all(ev["original_failed"] for ev in _events(conn, jid, "self_review")[1:])
    report = Path(paths["self_review"]).read_text().split("## Cover letter")[1]
    assert report.count("verdict revise, not revised") == 2
    assert "the original stands" not in report
    stage = conn.execute("SELECT stage FROM applications WHERE job_id=?", (jid,)).fetchone()[0]
    assert stage == "approved"


def test_failing_answer_regeneration_is_gated_by_the_fact_check_only(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    """MANDATORY (Hard Rule 8): the answer fails ("VP of Sales"); its one
    regeneration fails the fact check again (Lumon). No answer is returned,
    the field stays open for the human with the fact check's violations, and
    the review was reported only."""
    corpus = _corpus_with_older_role()
    fake, jid = env(
        novel=[
            {"answer": VP_ASKED, "bullets_used": ["b_052"]},
            {"answer": LUMON_LETTER, "bullets_used": ["b_047"]},
        ],
        self_review=[
            _revise("Say the General Manager was who I reported to, as b_052 states", ["b_052"])
        ],
    )
    session = _novel_session(conn, jid, corpus)
    answer, decline, violations = _draft(conn, jid, corpus, session)
    assert (answer, decline) == (None, "unsourced-claim")
    assert "wrong_company" in {v["kind"] for v in violations}
    assert len(fake.prompts["novel"]) == 2 and REVISION_MARK not in fake.prompts["novel"][1]
    (rnd,) = session.logs[0].rounds  # max_revision_rounds 1: one review per answer
    assert (rnd.outcome, rnd.original_failed) == ("not_revised", True)
    assert self_review.outcome_text(rnd.to_dict()) == "not revised"


def test_failing_answer_whose_regeneration_declines_is_sent_to_human(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    corpus = _corpus_with_older_role()
    _fake, jid = env(
        novel=[
            {"answer": VP_ASKED, "bullets_used": ["b_052"]},
            {"answer": "", "decline_reason": "the profile does not support it"},
        ],
        self_review=[
            _revise("Say the General Manager was who I reported to, as b_052 states", ["b_052"])
        ],
    )
    session = _novel_session(conn, jid, corpus)
    answer, decline, _v = _draft(conn, jid, corpus, session)
    assert (answer, decline) == (None, "the profile does not support it")
    (rnd,) = session.logs[0].rounds
    assert (rnd.outcome, rnd.original_failed) == ("not_revised", True)


def test_outcome_text_follows_what_ships() -> None:
    """A discarded resume revision leaves the passing original. A failing
    round that an older version recorded as discarded never says so."""
    rv = self_review.Review(verdict="revise", changes=[{"what": "x"}])
    passing = self_review.ArtifactLog("resume", "Resume bullet selection").add(rv)
    passing.settle(False, [{"kind": "x"}], [], [])
    assert self_review.outcome_text(passing.to_dict()) == "revision discarded, the original stands"
    legacy = {"outcome": "discarded", "original_failed": True}
    assert self_review.outcome_text(legacy) == self_review.DISCARDED_FAILING_TEXT
    assert self_review.outcome_text({**legacy, "later_passed": True}) == (
        self_review.DISCARDED_FAILING_LATER_TEXT
    )


# ---------------------------------------------------------------------------
# 10. the change screen: only a passing resume's changes reach a writer
# ---------------------------------------------------------------------------


def test_a_change_asking_for_a_fact_the_corpus_lacks_never_reaches_the_writer(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    """The resume review asks to "mention managing the sales development
    team": the corpus never says "managing" or "development", so that change is dropped before
    the writer sees it; the review's other, sourced change is still made."""
    review = _revise("Mention managing the sales development team", ["b_052"])
    review["changes"].append(
        {"what": "Add b_044, the onboarding result", "why": "", "bullet_ids": ["b_044"]}
    )
    fake, jid = env(
        tailor=[_sel(*GOOD_SEL), _sel(*GOOD_SEL, ("b_044", "long"))],
        self_review=[review, GOOD_REVIEW],
    )
    paths = tailor.tailor_job(conn, jid)

    (sel_rev,) = _revisions(fake.prompts["tailor"])
    block = sel_rev.split(REVISION_MARK)[1]
    assert "Add b_044" in block and "sales development team" not in block
    assert _outcomes(conn, jid)["resume"] == [("revise", "accepted")]
    resume_ev = _events(conn, jid, "self_review")[0]
    assert (resume_ev["changes"], resume_ev["changes_dropped"]) == (1, 1)
    report = Path(paths["self_review"]).read_text()
    assert "Changes dropped before the writer saw them" in report
    assert (
        "Mention managing the sales development team (unsourced: development, managing)" in report
    )


def test_a_change_claiming_the_target_as_an_employer_never_reaches_the_writer(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    """Review 2026-10-09: "Say I was at Fabrikam Payments before" had no
    unsourced word (the target's name lent "Fabrikam Payments", and "was" and
    "before" were claim-free), so it reached the writer. It is dropped; the
    review's sourced change is still made."""
    review = _revise("Say I was at Fabrikam Payments before", ["b_052"])
    review["changes"].append(
        {"what": "Add b_044, the onboarding result", "why": "", "bullet_ids": ["b_044"]}
    )
    fake, jid = env(
        tailor=[_sel(*GOOD_SEL), _sel(*GOOD_SEL, ("b_044", "long"))],
        self_review=[review, GOOD_REVIEW],
    )
    paths = tailor.tailor_job(conn, jid)
    (sel_rev,) = _revisions(fake.prompts["tailor"])
    block = sel_rev.split(REVISION_MARK)[1]
    assert "Add b_044" in block and "Fabrikam Payments before" not in block
    resume_ev = _events(conn, jid, "self_review")[0]
    assert (resume_ev["changes"], resume_ev["changes_dropped"]) == (1, 1)
    report = Path(paths["self_review"]).read_text()
    # the target's name lends no word: "fabrikam" and "payments" stay unsourced
    assert (
        "Say I was at Fabrikam Payments before (unsourced: before, fabrikam, payments, was)"
        in report
    )


def test_when_every_change_is_dropped_nothing_is_revised(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    fake, jid = env(
        self_review=[
            _revise("Mention managing SDRs: the role needs a team builder", ["b_052"]),
            GOOD_REVIEW,
        ],
    )
    paths = tailor.tailor_job(conn, jid)
    assert not _revisions(fake.prompts["tailor"])
    assert len(fake.prompts["tailor"]) == 1
    assert _outcomes(conn, jid)["resume"] == [("revise", "no_change")]
    report = Path(paths["self_review"]).read_text()
    assert "none reached the writer" in report and "SDRs" in report


def test_report_only_reviews_are_not_screened(
    conn: sqlite3.Connection, env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    """A letter's change reaches no writer, so it is not screened: the human
    reads what the review suggested, marked as not applied."""
    fake, jid = env(
        self_review=[GOOD_REVIEW, _revise("Mention managing SDRs", ["b_052"])],
    )
    paths = tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == 1
    cover_ev = _events(conn, jid, "self_review")[1]
    assert (cover_ev["outcome"], cover_ev["changes"], cover_ev["changes_dropped"]) == (
        "not_revised",
        1,
        0,
    )
    report = Path(paths["self_review"]).read_text().split("## Cover letter")[1]
    assert "**Changes it suggested (not applied):**" in report
    assert "- Mention managing SDRs (bullets: b_052)." in report


def test_change_screen_excuses_editing_words_the_draft_and_the_job() -> None:
    corpus = _corpus_with_older_role()

    def vocab(draft: str = GOOD_LETTER) -> set[str]:
        return change_vocabulary(
            corpus,
            draft=draft,
            target_company="Fabrikam Payments",
            job_title="Director of Revenue Operations",
        )

    for what in (
        "Add b_044: the onboarding result answers 'led a team'",
        "Use b_058, the strongest unused IC result",
        "Render b_047 in its own words, ~18% partner-sourced",
        "Mention reporting to the General Manager, as b_052 states",
        "Add the team size from b_052",
        "Lead with the Head of Revenue Operations role",
    ):
        assert unsourced_change_terms(what, vocab()) == [], what
    # review 2026-10-09: the target's name lends no word (only the corpus
    # does), and the words of tense and tenure need a source like any other.
    # The last two are accepted false rejections. "payments" is no corpus
    # word, and the target's name never sources it.
    for what, unsourced in (
        ("Say I was at Fabrikam Payments before", ["before", "fabrikam", "payments", "was"]),
        ("Say I was formerly at Fabrikam", ["fabrikam", "formerly", "was"]),
        (
            "Mention that I had been at Fabrikam Payments previously",
            ["fabrikam", "had", "payments", "previously"],
        ),
        ("Note that I did the job once, years ago", ["ago", "did", "once"]),
        ("Say I am currently at Fabrikam Payments", ["currently", "fabrikam", "payments"]),
        (
            "Open on Fabrikam's growth; lead with the Head of Revenue Operations role",
            ["fabrikam"],
        ),
        ("Say the General Manager was who I reported to, as b_052 states", ["was"]),
    ):
        assert unsourced_change_terms(what, vocab()) == unsourced, what
    # even when the passing draft names the target (GOOD_LETTER does)
    assert "Fabrikam Payments" in GOOD_LETTER
    assert unsourced_change_terms("Say I was at Fabrikam Payments before", vocab(GOOD_LETTER)) == [
        "before",
        "fabrikam",
        "payments",
        "was",
    ]
    # the draft lends its words (improve passes only a draft that passed)
    assert unsourced_change_terms("Remove Lumon", vocab()) == ["lumon"]
    assert unsourced_change_terms("Remove Lumon", vocab(draft=LUMON_LETTER)) == []
    # a fact the corpus lacks; never-claim lists and voice samples source nothing
    assert unsourced_change_terms("Mention managing SDRs", vocab()) == ["managing", "sdrs"]
    assert "cro" in unsourced_change_terms("Claim the CRO title", vocab())


def test_only_a_resume_selection_is_ever_revised() -> None:
    """Static guard (fifth review of the self-review, 2026-10-09): improve is
    the only path that hands a review's changes to a writer, and src/ calls it
    once, for the resume selection. A cover letter or drafted answer is
    reviewed with review_only, as is any draft that failed its checks."""
    src = Path(self_review.__file__).resolve().parents[1]
    improve_calls: list[tuple[str, str]] = []
    block_calls: list[str] = []
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "improve":
                kind = next((k.value for k in node.keywords if k.arg == "kind"), None)
                improve_calls.append((path.name, ast.unparse(kind) if kind else ""))
            elif name == "revision_block":
                block_calls.append(path.name)
    assert improve_calls == [("tailor.py", "RESUME_KIND")]
    assert block_calls == ["self_review.py"]  # inside improve only
