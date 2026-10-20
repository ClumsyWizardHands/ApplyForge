"""Scoring and generation pointed at the target roles (audit 2026-10-08 §2
item 6): the scoring profile carries titles, dates and years; score_fit
anchors on worked role examples for one invented career; cover letters close on a win
and never repeat a phrase across letters; tailoring is track-aware, keeps every
metric, covers recent roles and folds older empty ones. Offline: every LLM
call is faked, PDFs are not rendered (except one template smoke test)."""

from __future__ import annotations

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

from jobkit import llm
from jobkit.config import Filters, load_filters
from jobkit.normalize import jd_text_length
from jobkit.pipeline import tailor, triage
from jobkit.render.pdf import page_count, render_pdf
from jobkit.validate import authenticity
from jobkit.validate.authenticity import cross_letter_repeats, lexical_check, review_cover
from jobkit.validate.provenance import validate_tailored
from tests.conftest import REPO_ROOT, TEST_CONFIG
from tests.test_claims import CORPUS, FABRIKAM_JD, GOOD_CITED, GOOD_LETTER, FakeLLM
from tests.test_filter import _insert

TODAY = datetime.date(2026, 10, 8)
BANNED = yaml.safe_load((TEST_CONFIG / "banned_phrases.yaml").read_text())


def _corpus_with_older_role() -> dict[str, Any]:
    """The shared test corpus plus a 2012-16 retail role with one bullet."""
    c = copy.deepcopy(CORPUS)
    c["roles"].append(
        {
            "id": "role_old",
            "company": "Halvorsen Outfitters",
            "title": "Store Manager",
            "start": "2012-06",
            "end": "2016-08",
        }
    )
    c["bullets"].append(
        {
            "id": "b_067",
            "role_id": "role_old",
            "metric_confidence": "estimate",
            "text_long": "Ran a 14-person outdoor-gear store through peak seasons, hiring and "
            "training 6–8 seasonal staff each spring.",
            "text_short": "Ran a 14-person outdoor-gear store; 6–8 seasonal hires each spring.",
            "metric": "14-person store; 6–8 seasonal hires a year",
        }
    )
    return c


# ---------------------------------------------------------------------------
# 1. scoring profile: titles, companies, dates, years, education, skills
# ---------------------------------------------------------------------------


def test_profile_has_every_role_with_dates_and_years() -> None:
    profile = triage._profile_summary(
        corpus=_corpus_with_older_role(), filters=load_filters(), today=TODAY
    )
    assert "years in role computed as of 2026-10" in profile
    for line in (
        "- Regional Sales Manager · Haulmark · 2024-03 – present (current role) · 2.6 yrs",
        "- Head of Revenue Operations · ledgerline · 2021-11 – 2024-03 · 2.3 yrs",
        "- Mid-Market Account Executive · Marlow Payroll · 2018-05 – 2021-11 · 3.5 yrs",
        "- Inside Sales Representative · Keyward Security · 2016-08 – 2018-05 · 1.8 yrs",
        "- Store Manager · Halvorsen Outfitters · 2012-06 – 2016-08 · 4.2 yrs",
    ):
        assert line in profile, line
    # bullets sit under their own role, so the model can't drift a fact
    ae, bdr = profile.index("- Mid-Market Account Executive ·"), profile.index("- Inside Sales Rep")
    assert ae < profile.index("President's Club 2019 and 2020") < bdr
    # 31 + 28 + 42 + 21 months of sales-titled roles; Store Manager not counted
    assert (
        "TOTAL YEARS IN SALES ROLES: 10.2 (titles counted: Regional Sales Manager; "
        "Head of Revenue Operations; Mid-Market Account Executive; " in profile
    )
    assert "Store Manager" not in profile.split("TOTAL YEARS IN SALES ROLES")[1].split("\n")[0]
    assert "CAREER SPAN: 14.3 yrs" in profile
    assert "EDUCATION: B.S. · Lakeside State University · 2008 – 2012" in profile
    assert "SKILL DEPTH: Salesforce: hands-on" in profile and "MEDDIC: taught" in profile
    assert "NEVER CLAIM (hard constraint): titles ['VP'" in profile
    assert "phrases ['treasury management sales']" in profile


def test_profile_tolerates_a_sparse_corpus() -> None:
    sparse = {
        "roles": [
            {"id": "r1", "company": "Acme", "title": "Account Executive", "start": "2024-01"}
        ],
        "bullets": [{"id": "b1", "role_id": "r1", "text_short": "Closed deals."}],
    }
    profile = triage._profile_summary(corpus=sparse, filters=load_filters(), today=TODAY)
    assert "- Account Executive · Acme · 2024-01 – present (current role) · 2.8 yrs" in profile
    assert "  - Closed deals." in profile
    assert "EDUCATION" not in profile and "NEVER CLAIM" not in profile


def _score_env(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, int], list[str]]:
    calls = {"n": 0}
    prompts: list[str] = []

    def fake_backend(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        calls["n"] += 1
        prompts.append(prompt)
        return json.dumps({"score": 80, "rationale": "a. b.", "gaps": []}), {}

    monkeypatch.setattr(llm, "_call_claude_cli", fake_backend)
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(CORPUS))
    jd = "<p>" + "Own the full sales cycle for mid-market accounts. " * 20 + "</p>"
    assert jd_text_length(jd) > 500
    conn.execute("INSERT INTO companies (name, normalized_name) VALUES ('Acme', 'acme')")
    jid = conn.execute(
        """INSERT INTO jobs (company_id, external_id, title, normalized_title, company_name,
           normalized_company, location_bucket, cluster_key, jd_html, jd_hash, apply_url,
           status)
           VALUES (1, 's1', 'Account Executive', 'ae', 'Acme', 'acme', 'remote-us', 'k',
                   ?, 'h-score', 'https://x', 'filtered')""",
        (jd,),
    ).lastrowid
    conn.execute(
        "INSERT INTO job_requirements (job_id, must_have) VALUES (?, ?)",
        (jid, json.dumps(["2+ yrs closing"])),
    )
    conn.commit()
    return calls, prompts


def test_scorer_sees_roles_and_a_corpus_edit_misses_the_score_cache(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, prompts = _score_env(conn, jobkit_home, monkeypatch)
    assert triage.run_score(conn) == 1
    assert (
        "- Regional Sales Manager · Haulmark · 2024-03 – present (current role) · 2.6 yrs"
        in (prompts[0])
    )
    assert "TOTAL YEARS IN SALES ROLES" in prompts[0] and "EDUCATION: B.S." in prompts[0]
    # unchanged profile: the cache serves it (byte-identical rendered prompt)
    conn.execute("DELETE FROM scores")
    conn.commit()
    assert triage.run_score(conn) == 1 and calls["n"] == 1
    # the resume changes (a corrected end date): new profile, new prompt, new call
    corpus = copy.deepcopy(CORPUS)
    corpus["roles"][1]["end"] = "2024-02"
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(corpus))
    conn.execute("DELETE FROM scores")
    conn.commit()
    assert triage.run_score(conn) == 1 and calls["n"] == 2
    assert "2021-11 – 2024-02 · 2.2 yrs" in prompts[1]


# ---------------------------------------------------------------------------
# 2. score_fit.md: target-role anchors, overqualification is a gap
# ---------------------------------------------------------------------------


def test_score_prompt_reanchored_on_target_roles() -> None:
    p = llm.PromptFile("score_fit")
    assert p.version == "1.5"
    body = p.body
    for anchor in (
        "Account Executive, Mid-Market SaaS",
        "Account Manager, SMB",
        "Senior Account Executive, Mid-Market",
        "Sales Manager, Mid-Market",
        "VP of Sales",
    ):
        assert anchor in body, anchor
    # one invented example profile (account manager, then AE), not a real career
    assert "an invented example candidate, NOT the candidate above" in body
    anchors = re.findall(r'ANCHOR \d+ — "([^".]+)', body)
    assert anchors == [
        "Account Executive, Mid-Market SaaS",
        "Account Manager, SMB",
        "Senior Account Executive, Mid-Market",
        "Sales Manager, Mid-Market",
        "VP of Sales",
    ], anchors
    assert "overqualified:" in body and "NOT disqualifying" in body
    assert 'never write "tenure not documented"' in body
    assert "Do not cluster at 70-85" in body
    scores = [int(s) for s in re.findall(r"ANCHOR (\d+)", body)]
    assert len(scores) == 5 and max(scores) - min(scores) >= 50  # spread, not clustered
    # role examples, not one person's resume: no name and no employer
    for personal in ("jordan", "rivera", "haulmark", "keyward", "ledgerline", "marlow"):
        assert personal not in body.lower(), personal
    for slot in ("{profile}", "{title}", "{company}", "{requirements}"):
        assert slot in body


# ---------------------------------------------------------------------------
# 3. cover letter: P3 closes on a win; repeated-phrase guard
# ---------------------------------------------------------------------------


def test_cover_prompt_closes_on_a_win_not_a_gap() -> None:
    p = llm.PromptFile("cover_letter")
    assert p.version == "1.16"
    assert "honest bridge across the largest gap" not in p.body
    assert "LARGEST GAP" not in p.body
    assert "concrete win" in p.body and "ends on strength" in p.body
    assert "Mention a gap ONLY when it is a stated hard requirement" in p.body
    for slot in ("{must_haves}", "{gaps}", "{feedback}"):
        assert slot in p.body


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("I'd rather walk you through the renewal math than summarize it.", "contrast"),
        ("I’d rather show you the route data than describe it in a letter.", "contrast"),
        ("I would rather point to the dock numbers than make a promise.", "contrast"),
        ("One honest gap worth naming: partner programs are new to me.", "gap announcement"),
        ("If it turns out to disqualify me, I will respect the call.", "apologetic gap close"),
        ("Let me be transparent: fleet software is new territory for me.", "candor preface"),
        ("To date, I've never held a people-manager seat.", "title-never-held"),
        ("I haven’t ever held budget authority for a whole region.", "title-never-held"),
    ],
)
def test_repeated_tics_hard_fail_pass1(text: str, name: str) -> None:
    hard, _soft = lexical_check(text, "cover_letter", BANNED)
    assert any(h.startswith(f"pattern: {name}") for h in hard), hard


def test_plain_close_passes_the_tic_patterns() -> None:
    letter = (
        "Rather than describe the desk, here is the result: the average discount went from "
        "~19% to ~12%. I would like to talk about what that looks like at Hooli."
    )
    hard, _soft = lexical_check(letter, "cover_letter", BANNED)
    assert hard == []


PHRASE = "the forecast is the real product here"
PRIOR = [
    f"Dear team. I think {PHRASE}, and I have built one. Best.",
    f"Hello. Frankly {PHRASE} — so I start there. Thanks.",
]


def test_phrase_reused_across_letters_is_flagged() -> None:
    letter = f"At Pilot, {PHRASE}. I grew annual bookings from ~$3.2M to ~$5.6M at Haulmark."
    flags = cross_letter_repeats(letter, PRIOR, BANNED, corpus=CORPUS, company="Pilot")
    assert len(flags) == 1 and "forecast is the real product" in flags[0]
    assert "(in 2 earlier letters)" in flags[0]
    # one earlier use is not yet a tic
    assert cross_letter_repeats(letter, PRIOR[:1], BANNED, corpus=CORPUS, company="Pilot") == []


def test_retold_corpus_fact_is_not_a_repeat() -> None:
    fact = "I grew annual bookings from ~$3.2M to ~$5.6M at Haulmark"
    prior = [f"{fact}. One.", f"Two. {fact}.", f"{fact}, three."]
    letter = f"{fact}, and I would do it again."
    assert cross_letter_repeats(letter, prior, BANNED, corpus=CORPUS, company="Pilot") == []
    # without the corpus the same words are just a repeated phrase
    assert cross_letter_repeats(letter, prior, BANNED, corpus=None, company="Pilot")


def _thrice(phrase: str) -> tuple[list[str], str]:
    """Two earlier letters and a new one that all carry `phrase`."""
    return [f"{phrase}. One.", f"Two. {phrase}."], f"{phrase}, and I would do it again."


def test_past_tense_hedged_retelling_is_not_a_repeat() -> None:
    """Review of wp4: b_047 says "adding", "routing" and "~18%"; letters
    retell it as "added", "routed" and "roughly 18%" (select_bullets.md allows
    the hedge)."""
    prior, letter = _thrice(
        "At Haulmark I added 41 new carrier accounts and routed roughly 18% of new bookings "
        "through reseller partners by 2025"
    )
    for cited in (["b_047"], None):
        assert (
            cross_letter_repeats(letter, prior, BANNED, corpus=CORPUS, company="Pilot", cited=cited)
            == []
        )
    assert cross_letter_repeats(letter, prior, BANNED, corpus=None, company="Pilot")


@pytest.mark.parametrize("hedge", ["roughly", "about", "approximately", "nearly", "around", "~"])
def test_hedge_reworded_figure_is_not_a_repeat(hedge: str) -> None:
    """'roughly 18%' against the corpus's '~18%' (claims.py accepts the hedge)."""
    sep = "" if hedge == "~" else " "
    prior, letter = _thrice(f"Routed {hedge}{sep}18% of new bookings through reseller partners")
    assert cross_letter_repeats(letter, prior, BANNED, corpus=CORPUS, company="Pilot") == []


def test_narrative_word_around_a_cited_bullet_is_not_a_repeat() -> None:
    """ "pushing" is not a corpus word, but the phrase retells b_047 — when the
    letter cites b_047. Citing another bullet leaves it a repeated phrase."""
    prior, letter = _thrice(
        "I grew annual bookings to ~$5.6M pushing roughly 18% through resellers"
    )
    kw: dict[str, Any] = {"corpus": CORPUS, "company": "Pilot"}
    assert cross_letter_repeats(letter, prior, BANNED, cited=["b_047", "b_052"], **kw) == []
    flags = cross_letter_repeats(letter, prior, BANNED, cited=["b_052"], **kw)
    assert len(flags) == 1 and "pushing roughly" in flags[0]


def test_tic_next_to_a_retold_fact_is_still_flagged() -> None:
    tic = "The part nobody talks about is the cadence"
    prior = [
        f"{tic}: the average discount went from ~19% to ~12%. One.",
        f"Two. {tic}, and the team followed it.",
    ]
    letter = f"{tic}: ~$5.6M in annual bookings, ~18% of new bookings through reseller partners."
    flags = cross_letter_repeats(letter, prior, BANNED, corpus=CORPUS, company="Pilot", cited=None)
    assert len(flags) == 1 and "part nobody talks about is the cadence" in flags[0]


def test_ask_for_the_conversation_is_not_a_repeat_but_its_tail_is() -> None:
    """Cover 1.4 requires the letter to ask for the conversation; the plain
    ask may repeat (also before a full stop), what is appended to it may not."""
    close = "I'd like to talk about which accounts your team should phone before spring"
    prior = [
        f"Letter one. So yes, I'd like to talk. {close}.",
        f"Two. So yes, I'd like to talk. {close}.",
    ]
    kw: dict[str, Any] = {"corpus": CORPUS, "company": "Pilot"}
    assert cross_letter_repeats("Three. So yes, I'd like to talk.", prior, BANNED, **kw) == []
    assert cross_letter_repeats("I'd like to talk about your pipeline.", prior, BANNED, **kw) == []
    flags = cross_letter_repeats(f"Four. {close}.", prior, BANNED, **kw)
    assert len(flags) == 1 and "should phone before spring" in flags[0]


def test_company_fact_from_this_jd_is_not_a_repeat() -> None:
    """Letters to the same company may retell the same JD fact (say, three
    letters to Hooli that each say "already includes Acme, Globex, Initech,
    and Vandelay")."""
    jd = (
        "Hundreds of customers, including Acme, Globex, Initech, and Vandelay, run "
        "their payroll on HOOLI."
    )
    prior, letter = _thrice("Your customers already include Acme, Globex, Initech, and Vandelay")
    kw: dict[str, Any] = {"corpus": CORPUS, "company": "Hooli"}
    assert cross_letter_repeats(letter, prior, BANNED, jd=jd, **kw) == []
    assert cross_letter_repeats(letter, prior, BANNED, **kw)  # no JD: a repeated phrase


def test_review_cover_fails_a_repeated_phrase_before_the_critic(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    critic_calls: list[str] = []

    def critic(*_a: Any, **_k: Any) -> dict[str, Any]:
        critic_calls.append("x")
        return {"verdict": "pass", "notes": "", "tells": []}

    monkeypatch.setattr(llm, "call_json", critic)
    jid = _insert(conn)
    conn.commit()
    letter = f"At Pilot, {PHRASE}. I grew annual bookings from ~$3.2M to ~$5.6M at Haulmark."
    rep = review_cover(conn, jid, letter, "Pilot", prior_letters=PRIOR, corpus=CORPUS)
    assert rep.verdict == "fail" and critic_calls == []
    assert any(f.startswith("cross-letter-repeat") for f in rep.structural_flags)
    # on_repeat: flag downgrades it to a soft flag
    cfg = copy.deepcopy(BANNED)
    cfg["cross_letter"]["on_repeat"] = "flag"
    monkeypatch.setattr(authenticity, "load_config", lambda: cfg)
    rep = review_cover(conn, jid, letter, "Pilot", prior_letters=PRIOR, corpus=CORPUS)
    assert rep.verdict == "soft_flag"


def test_cross_letter_config_in_both_banned_phrase_files() -> None:
    example = yaml.safe_load((REPO_ROOT / "config" / "banned_phrases.example.yaml").read_text())
    assert example["cross_letter"] == BANNED["cross_letter"]
    patterns = BANNED["cover_letter"]["hard_reject_patterns"]
    assert example["cover_letter"]["hard_reject_patterns"] == patterns
    for pat in BANNED["cover_letter"]["hard_reject_patterns"]:
        re.compile(pat["regex"])


# ---------------------------------------------------------------------------
# 4-5. tailoring: track, metric-safe variants, required roles, folding, out/
# ---------------------------------------------------------------------------


def _sel(*items: tuple[str, str], skills: list[str] | None = None) -> dict[str, Any]:
    by_id = {b["id"]: b for b in _corpus_with_older_role()["bullets"]}
    return {
        "positioning_id": "pos_ops",
        "selected": [
            {"bullet_id": bid, "variant": v, "rendered": by_id[bid][f"text_{v}"]}
            for bid, v in items
        ],
        "skills_line": skills or ["Salesforce", "MEDDIC"],
    }


GOOD_SEL = (("b_047", "long"), ("b_052", "long"), ("b_049", "short"), ("b_058", "short"))


@pytest.fixture()
def env(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[FakeLLM, int, list[dict[str, Any]]]]:
    """A Fabrikam-style job with a JD, requirements and gaps; fake LLM, no PDF."""
    corpus_path = jobkit_home / "data" / "career_corpus.yaml"
    corpus_path.write_text(yaml.safe_dump(_corpus_with_older_role()))
    monkeypatch.setattr(tailor, "_today", lambda: TODAY)
    monkeypatch.setattr(authenticity, "_default_embed", lambda t: [[1.0, 0.0] for _ in t])
    rendered: list[dict[str, Any]] = []

    def fake_render(template: str, data: dict[str, Any], out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data))
        if template == "resume.typ":
            rendered.append(data)
        return out

    monkeypatch.setattr(tailor, "render_pdf", fake_render)
    monkeypatch.setattr(tailor, "page_count", lambda p: 1)
    n = {"jobs": 0}

    def setup(
        selections: list[dict[str, Any]],
        *,
        title: str = "Director of Revenue Operations",
        letters: list[dict[str, Any]] | None = None,
        gaps: list[str] | None = None,
        must_have: list[str] | None = None,
        location: str = "Remote, United States",
    ) -> tuple[FakeLLM, int, list[dict[str, Any]]]:
        n["jobs"] += 1
        if n["jobs"] == 1:
            conn.execute(
                "INSERT INTO companies (name, normalized_name) "
                "VALUES ('Fabrikam Payments', 'fabrikam')"
            )
        jid = conn.execute(
            """INSERT INTO jobs (company_id, external_id, title, normalized_title,
               company_name, normalized_company, location_raw, location_bucket, remote,
               cluster_key, jd_html, jd_hash, apply_url, status)
               VALUES (1, ?, ?, 'x', 'Fabrikam Payments', 'fabrikam', ?, 'remote-us', 1, 'k', ?,
                       'h-fabrikam', 'https://x', 'approved')""",
            (f"f{n['jobs']}", title, location, f"<p>{FABRIKAM_JD}</p>"),
        ).lastrowid
        assert jid is not None
        conn.execute(
            "INSERT INTO job_requirements (job_id, must_have) VALUES (?, ?)",
            (jid, json.dumps(must_have or [])),
        )
        conn.execute(
            "INSERT INTO scores (job_id, fit_score, gaps) VALUES (?, 80, ?)",
            (jid, json.dumps(gaps or [])),
        )
        conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'approved')", (jid,))
        conn.commit()
        fake = FakeLLM(
            {
                "tailor": selections,
                "cover": letters or [{"letter": GOOD_LETTER, "bullets_used": GOOD_CITED}],
                "critic": [{"verdict": "pass", "notes": "", "tells": []}],
            }
        )
        monkeypatch.setattr(llm, "call_json", fake.call_json)
        monkeypatch.setattr(llm, "forget", fake.forget)
        return fake, jid, rendered

    return setup


@pytest.mark.parametrize(
    ("title", "track"),
    [
        ("Sales Development Representative", "sdr_bdr"),
        ("Manager, Sales Development", "sdr_bdr_manager"),
        ("Mid-Market Account Executive", "account_executive"),
        ("VP of Sales", "sales_exec"),
        ("Revenue Operations Manager", "other"),
    ],
)
def test_select_prompt_carries_the_job_track(
    conn: sqlite3.Connection, env: Callable[..., Any], title: str, track: str
) -> None:
    fake, jid, _ = env([_sel(*GOOD_SEL)], title=title)
    paths = tailor.tailor_job(conn, jid)
    prompt = fake.prompts["tailor"][0]
    assert f"ROLE TRACK: {track}\n" in prompt
    assert json.loads(Path(paths["provenance"]).read_text())["track"] == track


def test_select_prompt_emphasis_is_selection_only() -> None:
    body = " ".join(llm.PromptFile("select_bullets").body.split())
    assert llm.PromptFile("select_bullets").version == "1.8"
    assert "never a claim the corpus does not make" in body
    for track in ("sdr_bdr:", "sdr_bdr_manager:", "account_executive:", "sales_exec:", "other"):
        assert f"- {track}" in body
    assert "NEVER choose a variant that drops a number" in body
    assert "REQUIRED" in body and "Earlier experience" in body


def test_prompts_carry_the_configured_estimate_style(
    conn: sqlite3.Connection, env: Callable[..., Any], jobkit_home: Path
) -> None:
    """config/banned_phrases.yaml drift.estimate_style reaches the selection
    and cover prompts (2026-10-08: "confident"); flipping it flips both."""
    fake, jid, _ = env([_sel(*GOOD_SEL)])
    tailor.tailor_job(conn, jid)
    for stage in ("tailor", "cover"):
        prompt = fake.prompts[stage][0]
        assert "FIGURE STYLE: confident." in prompt and "{estimate_style}" not in prompt
    cfg = jobkit_home / "config" / "banned_phrases.yaml"
    cfg.write_text(cfg.read_text().replace("estimate_style: confident", "estimate_style: hedged"))
    fake, jid, _ = env([_sel(*GOOD_SEL)])
    tailor.tailor_job(conn, jid)
    for stage in ("tailor", "cover"):
        assert "FIGURE STYLE: hedged." in fake.prompts[stage][0]


def test_required_roles_are_topped_up_and_older_empty_roles_fold(
    conn: sqlite3.Connection, env: Callable[..., Any]
) -> None:
    # the model picks only Haulmark bullets (the current role): the ledgerline
    # role (ended 2024-03) and the Marlow Payroll AE role (ended 2021-11), both
    # inside 5 years, come back empty
    sel = _sel(("b_047", "long"), ("b_052", "long"), ("b_044", "long"))
    fake, jid, rendered = env([sel])
    paths = tailor.tailor_job(conn, jid)
    corpus_listing = fake.prompts["tailor"][0]
    assert (
        "- role_ledgerline | ledgerline · Head of Revenue Operations · Nov 2021 – Mar 2024 "
        "| REQUIRED"
    ) in corpus_listing
    keyward_line = next(
        ln for ln in corpus_listing.splitlines() if ln.startswith("- role_keyward |")
    )
    assert keyward_line == (
        "- role_keyward | Keyward Security · Inside Sales Representative · Aug 2016 – May 2018"
    )  # ended 2018-05: outside the 5-year window, so not required

    data = rendered[-1]
    shown = {r["company"] + "/" + r["title"]: r["bullets"] for r in data["roles"]}
    assert all(shown.values()), f"empty role header rendered: {shown}"
    assert shown["ledgerline/Head of Revenue Operations"]  # topped up: the role's first bullet
    marlow = shown["Marlow Payroll/Mid-Market Account Executive"]
    assert "President's Club" in marlow[0]
    assert [e["company"] for e in data["earlier"]] == [
        "Keyward Security",
        "Halvorsen Outfitters",
    ]
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["selection_adjustments"]["topped_up"] == ["b_053", "b_058"]
    assert prov["required_roles"] == ["role_haulmark", "role_ledgerline", "role_marlow"]
    assert prov["earlier_experience"] == [
        "Inside Sales Representative, Keyward Security",
        "Store Manager, Halvorsen Outfitters",
    ]
    assert {b["bullet_id"] for b in prov["bullets"]} == {
        "b_047",
        "b_052",
        "b_044",
        "b_053",
        "b_058",
    }


def test_required_roles_window_comes_from_config() -> None:
    corpus = _corpus_with_older_role()
    assert tailor.required_role_ids(corpus, TODAY, 5) == {
        "role_ledgerline",
        "role_haulmark",
        "role_marlow",
    }
    assert "role_keyward" not in tailor.required_role_ids(corpus, TODAY, 8)
    assert "role_keyward" in tailor.required_role_ids(corpus, TODAY, 9)
    assert tailor.required_role_ids(corpus, TODAY, 0) == {"role_haulmark"}
    assert tailor._recent_role_years(load_filters()) == 5  # filters.yaml resume block
    assert tailor._recent_role_years(Filters(raw={})) == tailor.DEFAULT_RECENT_ROLE_YEARS


# the audit's b_058: the long variant told the story without the number
OLD_B011 = {
    "id": "b_058",
    "role_id": "role_marlow",
    "metric_confidence": "verified",
    "text_long": "Made President's Club on top individual performance.",
    "text_short": "President's Club 2019 and 2020 (124%, then 131% of annual quota).",
    "metric": "President's Club 2019, 2020; 124% and 131% of annual quota",
}


def test_variant_that_drops_a_metric_is_never_chosen() -> None:
    assert tailor.metric_safe_variant(OLD_B011, "long") == "short"
    assert tailor.metric_safe_variant(OLD_B011, "short") == "short"
    b009 = next(b for b in CORPUS["bullets"] if b["id"] == "b_044")  # long has 2–3 carriers
    assert tailor.metric_safe_variant(b009, "short") == "long"
    # each variant has a number the other lacks: no safe swap, keep the choice
    both = {"text_long": "Ran 3 teams.", "text_short": "Hit 120% of plan."}
    assert tailor.metric_safe_variant(both, "long") == "long"
    assert tailor.metric_safe_variant(both, "short") == "short"


def test_keep_metrics_swaps_variants_and_restores_trimmed_numbers() -> None:
    corpus = copy.deepcopy(CORPUS)
    corpus["bullets"] = [OLD_B011 if b["id"] == "b_058" else b for b in corpus["bullets"]]
    selected = [
        {"bullet_id": "b_058", "variant": "long", "rendered": OLD_B011["text_long"]},
        {"bullet_id": "b_047", "variant": "long", "rendered": "Grew the Northeast region."},
        {"bullet_id": "b_052", "variant": "short", "rendered": "Led 9 AEs."},
    ]
    changes = tailor.keep_metrics(selected, corpus)
    assert (
        selected[0]["variant"] == "short"
        and "President's Club 2019 and 2020" in selected[0]["rendered"]
    )
    assert "~$5.6M" in selected[1]["rendered"]  # trimmed numbers restored from source
    b005_short = next(b for b in corpus["bullets"] if b["id"] == "b_052")["text_short"]
    assert selected[2]["rendered"] == b005_short  # "~18 months" was trimmed away
    assert [c["action"] for c in changes] == [
        "variant long->short",
        "metric restored",
        "metric restored",
    ]


# a rendering that drops its source's numbers AND adds an unsourced one: the
# metric-keeping step must not overwrite it with corpus text (hard rule 3)
FABRICATED_B006 = "Grew the Northeast region 45% year over year, beating plan."
FABRICATED_B009 = "Ran remote onboarding; cut time to first dispatch 45%."  # short; swap → long


def _events(home: Path, name: str) -> list[dict[str, Any]]:
    path = home / "logs" / "jobkit.jsonl"
    if not path.exists():
        return []
    recs = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return [r for r in recs if r["event"] == name]


def test_keep_metrics_never_overwrites_a_fabricated_metric(jobkit_home: Path) -> None:
    corpus = copy.deepcopy(CORPUS)
    assert tailor.metric_safe_variant(
        next(b for b in corpus["bullets"] if b["id"] == "b_044"), "short"
    ) == ("long")  # so b_044 below exercises the variant-swap branch
    selected = [
        {"bullet_id": "b_047", "variant": "long", "rendered": FABRICATED_B006},
        {"bullet_id": "b_044", "variant": "short", "rendered": FABRICATED_B009},
        # drops every number and adds a company the corpus never names
        {"bullet_id": "b_047", "variant": "short", "rendered": "Grew revenue at Google."},
    ]
    before = copy.deepcopy(selected)
    assert tailor.keep_metrics(selected, corpus) == []
    assert selected == before  # untouched: nothing restored, nothing swapped
    kinds = {(v["bullet_id"], v["kind"]) for v in validate_tailored(selected, corpus).violations}
    assert {
        ("b_047", "fabricated_number"),
        ("b_044", "fabricated_number"),
        ("b_047", "unsourced_proper_noun"),
    } <= kinds
    skipped = _events(jobkit_home, "tailor.metric_keep_skipped")
    assert [(e["bullet_id"], e["reason"]) for e in skipped] == [
        ("b_047", "fails provenance"),
        ("b_044", "fails provenance"),
        ("b_047", "fails provenance"),
    ]


def test_keep_metrics_keeps_a_sourced_number_its_replacement_lacks(jobkit_home: Path) -> None:
    """A valid rendering carrying a number from the bullet's `metric` field is
    not replaced by variant text that lacks it: restoring one number must not
    silently drop another."""
    corpus = copy.deepcopy(CORPUS)
    corpus["bullets"].append(
        {
            "id": "b_x",
            "role_id": "role_haulmark",
            "metric_confidence": "verified",
            "text_long": "Ran 3 teams.",
            "text_short": "Ran 3 teams.",
            "metric": "120% of plan",
        }
    )
    selected = [{"bullet_id": "b_x", "variant": "short", "rendered": "Ran teams at 120% of plan."}]
    assert validate_tailored(selected, corpus).ok
    assert tailor.keep_metrics(selected, corpus) == []
    assert selected[0]["rendered"] == "Ran teams at 120% of plan."
    (skip,) = _events(jobkit_home, "tailor.metric_keep_skipped")
    assert skip["reason"] == "restore" and skip["claims"] == ["percent:120"]


def test_tailor_job_rejects_a_fabricated_metric_instead_of_restoring_it(
    conn: sqlite3.Connection, env: Callable[..., Any], jobkit_home: Path
) -> None:
    """End to end through tailor_job: the model's raw selection is validated
    before keep_metrics runs, so the unsourced 45% fails provenance (logged,
    fed back, retried) and never comes out as a 'metric restored' bullet."""
    bad = _sel(*GOOD_SEL)
    bad["selected"][0]["rendered"] = FABRICATED_B006  # b_047, long
    fake, jid, rendered = env([bad, bad])
    with pytest.raises(RuntimeError, match="resume failed twice"):
        tailor.tailor_job(conn, jid)
    failed = _events(jobkit_home, "provenance.failed")
    assert [e["attempt"] for e in failed] == [0, 1]
    for e in failed:
        assert any(
            v["bullet_id"] == "b_047" and v["kind"] == "fabricated_number" and "45" in v["detail"]
            for v in e["violations"]
        )
    assert "metric restored" not in (jobkit_home / "logs" / "jobkit.jsonl").read_text()
    assert _events(jobkit_home, "tailor.selection_adjusted") == []
    retry = fake.prompts["tailor"][1]
    assert "FAILED PROVENANCE VALIDATION" in retry and "45%" in retry
    assert fake.forgotten == ["tailor"]  # the failing selection is not served from cache
    assert rendered == []  # no resume rendered
    assert not list((jobkit_home / "out").glob("*/resume.pdf"))


def test_tailor_job_retries_past_a_fabricated_metric(
    conn: sqlite3.Connection, env: Callable[..., Any], jobkit_home: Path
) -> None:
    bad = _sel(*GOOD_SEL)
    bad["selected"][0]["rendered"] = FABRICATED_B006
    _fake, jid, _ = env([bad, _sel(*GOOD_SEL)])
    paths = tailor.tailor_job(conn, jid)
    assert [e["attempt"] for e in _events(jobkit_home, "provenance.failed")] == [0]
    prov = json.loads(Path(paths["provenance"]).read_text())
    b006 = next(b for b in prov["bullets"] if b["bullet_id"] == "b_047")
    assert "45%" not in b006["rendered"] and "~$5.6M" in b006["rendered"]
    assert not any(
        c["action"] == "metric restored" for c in prov["selection_adjustments"]["metric_changes"]
    )


def test_tailor_swaps_a_metric_dropping_variant(
    conn: sqlite3.Connection, env: Callable[..., Any]
) -> None:
    sel = _sel(*GOOD_SEL, ("b_044", "short"))
    _fake, jid, _ = env([sel])
    paths = tailor.tailor_job(conn, jid)
    prov = json.loads(Path(paths["provenance"]).read_text())
    b009 = next(b for b in prov["bullets"] if b["bullet_id"] == "b_044")
    assert b009["variant"] == "long" and "2–3 carriers go live a month" in b009["rendered"]
    assert {"bullet_id": "b_044", "action": "variant short->long"} in (
        prov["selection_adjustments"]["metric_changes"]
    )


def test_shrink_keeps_metrics_and_required_roles() -> None:
    corpus = _corpus_with_older_role()
    by_id = {b["id"]: b for b in corpus["bullets"]}

    def sel_of(*ids: str, variant: str = "long") -> dict[str, Any]:
        return {
            "selected": [
                {"bullet_id": i, "variant": variant, "rendered": by_id[i][f"text_{variant}"]}
                for i in ids
            ]
        }

    required = {"role_ledgerline", "role_haulmark", "role_marlow"}
    # step 1: short variants, except where short drops a number long has
    sel = sel_of("b_049", "b_047", "b_044", "b_058")
    assert tailor._shrink(sel, corpus, required)
    variants = {s["bullet_id"]: s["variant"] for s in sel["selected"]}
    assert variants == {"b_049": "short", "b_047": "short", "b_044": "long", "b_058": "short"}
    # step 2: drop from the end, skipping a required role's only bullet
    sel = sel_of("b_047", "b_052", "b_041", "b_067", "b_049", "b_058", "b_053", variant="short")
    sel["selected"][0]["variant"] = "short"
    assert tailor._shrink(sel, corpus, required)
    left = [s["bullet_id"] for s in sel["selected"]]
    assert "b_053" not in left  # role_ledgerline still has b_049
    assert tailor._shrink(sel, corpus, required) is False  # 6 left: the floor
    sel = sel_of("b_047", "b_052", "b_041", "b_067", "b_044", "b_058", "b_053", variant="short")
    sel["selected"][4] = {"bullet_id": "b_044", "variant": "long", "rendered": "x"}
    assert tailor._shrink(sel, corpus, required)
    left = [s["bullet_id"] for s in sel["selected"]]
    # b_053 (ledgerline) and b_058 (Marlow Payroll) are their roles' only bullets:
    # b_044 is the last droppable one (role_haulmark keeps b_047 and b_052)
    assert left == ["b_047", "b_052", "b_041", "b_067", "b_058", "b_053"]


def test_out_folder_names_the_job_so_duplicates_do_not_collide(
    conn: sqlite3.Connection, env: Callable[..., Any]
) -> None:
    _f1, j1, _ = env([_sel(*GOOD_SEL)])
    p1 = tailor.tailor_job(conn, j1)
    _f2, j2, _ = env([_sel(*GOOD_SEL)])  # same company, title, day: a duplicate posting
    p2 = tailor.tailor_job(conn, j2)
    d1, d2 = Path(p1["resume"]).parent, Path(p2["resume"]).parent
    assert d1 != d2
    assert d1.name == f"fabrikam-payments-director-of-revenue-operations-{j1}-2026-10-08"
    assert d2.name == f"fabrikam-payments-director-of-revenue-operations-{j2}-2026-10-08"
    assert json.loads(Path(p1["provenance"]).read_text())["job_id"] == j1


def test_cover_gets_must_haves_and_never_an_overqualification_gap(
    conn: sqlite3.Connection, env: Callable[..., Any]
) -> None:
    fake, jid, _ = env(
        [_sel(*GOOD_SEL)],
        gaps=["no home-services background", "overqualified: 2.3 yrs managing AEs"],
        must_have=["5+ years operating experience", "managed people"],
    )
    tailor.tailor_job(conn, jid)
    prompt = fake.prompts["cover"][0]
    assert (
        "JOB MUST-HAVES (the only requirements a gap may be about): "
        "5+ years operating experience; managed people"
    ) in prompt
    assert "no home-services background" in prompt
    assert "overqualified" not in prompt.lower().split("possible gaps from fit scoring")[1]
    assert not re.search(r"\{(must_haves|gaps|feedback|gap|jd|bullets|voice)\}", prompt)


def test_cover_repeating_an_earlier_letter_is_regenerated(
    conn: sqlite3.Connection, env: Callable[..., Any], jobkit_home: Path
) -> None:
    """GOOD_LETTER's opening line already closed two earlier letters: the
    cross-letter guard fails it, and the regeneration prompt names the phrase."""
    for i in (1, 2):
        d = jobkit_home / "out" / f"earlier-{i}"
        d.mkdir(parents=True)
        (d / "cover.txt").write_text(
            f"Pilot grew fast, and growth like that usually outruns the forecast first. Letter {i}."
        )
    fake, jid, _ = env([_sel(*GOOD_SEL)])
    paths = tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == 3
    assert "cross-letter-repeat" in fake.prompts["cover"][1]
    assert "usually outruns the forecast first" in fake.prompts["cover"][1]
    out = Path(paths["resume"]).parent
    assert "cross-letter-repeat" in (out / "cover_critique.md").read_text()
    stage = conn.execute("SELECT stage FROM applications WHERE job_id=?", (jid,)).fetchone()[0]
    assert stage == "approved"  # not staged until a human reviews it


def test_re_tailoring_the_same_job_is_not_a_cross_letter_repeat(
    conn: sqlite3.Connection, env: Callable[..., Any], jobkit_home: Path
) -> None:
    fake, jid, _ = env([_sel(*GOOD_SEL)])
    for day in ("2026-10-01", "2026-10-02"):  # this job's own earlier runs
        d = jobkit_home / "out" / f"fabrikam-payments-director-of-revenue-operations-{jid}-{day}"
        d.mkdir(parents=True)
        (d / "cover.txt").write_text(GOOD_LETTER)
    paths = tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == 1 and Path(paths["cover"]).exists()


def test_cover_unique_letter_passes_with_earlier_letters_present(
    conn: sqlite3.Connection, env: Callable[..., Any], jobkit_home: Path
) -> None:
    d = jobkit_home / "out" / "earlier-1"
    d.mkdir(parents=True)
    (d / "cover.txt").write_text(PRIOR[0])
    fake, jid, _ = env([_sel(*GOOD_SEL)])
    paths = tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == 1 and Path(paths["cover"]).exists()


def test_tailor_exempts_this_jds_facts_and_the_cited_bullets(
    conn: sqlite3.Connection,
    env: Callable[..., Any],
    jobkit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GOOD_LETTER opens on a fact from FABRIKAM_JD that two earlier letters also
    used: the tailor hands review_cover the JD text and the letter's
    bullets_used, so retelling it is not a repeat."""
    for i in (1, 2):
        d = jobkit_home / "out" / f"earlier-{i}"
        d.mkdir(parents=True)
        (d / "cover.txt").write_text(
            f"Fabrikam Payments doubled its payment volume in 2025, which is letter {i}."
        )
    seen: list[dict[str, Any]] = []
    real = tailor.review_cover

    def spy(*a: Any, **k: Any) -> authenticity.AuthenticityReport:
        seen.append(k)
        return real(*a, **k)

    monkeypatch.setattr(tailor, "review_cover", spy)
    fake, jid, _ = env([_sel(*GOOD_SEL)])
    paths = tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == 1 and Path(paths["cover"]).exists()
    assert seen[0]["cited"] == GOOD_CITED
    assert "doubled its payment volume" in seen[0]["jd"]


# ---------------------------------------------------------------------------
# 5. display location: config rules only, nothing hardcoded
# ---------------------------------------------------------------------------


def _job(loc: str, remote: int = 0, bucket: str = "other-unknown") -> dict[str, Any]:
    return {"location_raw": loc, "remote": remote, "location_bucket": bucket}


@pytest.mark.parametrize(
    ("loc", "remote", "bucket", "expected"),
    [
        ("Camden, NJ", 0, "other-camden", "Bristol, PA"),  # job_state: NJ rule
        ("Cherry Hill, New Jersey", 0, "other-cherryhill", "Bristol, PA"),
        ("King of Prussia, PA", 0, "other-kingofprussia", "Philadelphia, PA"),  # job_city_in
        ("Remote - US", 1, "remote-us", "Philadelphia, PA"),  # remote rule
        ("Austin, TX", 0, "other-tx", "Philadelphia, PA"),  # default
    ],
)
def test_display_location_follows_filters_rules(
    loc: str, remote: int, bucket: str, expected: str
) -> None:
    assert tailor._display_location(_job(loc, remote, bucket), load_filters(), CORPUS) == expected


def test_display_location_for_another_user_follows_only_their_rules() -> None:
    other = Filters(
        raw={
            "location_strategy": {
                "default": "Denver, CO",
                "rules": [
                    {"when": {"job_state": "CA"}, "use": "Oakland, CA"},
                    {"when": {"job_city_in": ["Boulder"]}, "use": "Boulder, CO"},
                    {"when": {"typo_key": True}, "use": "Nowhere"},
                ],
            }
        }
    )
    assert tailor._display_location(_job("San Francisco, California"), other, {}) == "Oakland, CA"
    assert tailor._display_location(_job("Boulder, Colorado"), other, {}) == "Boulder, CO"
    assert tailor._display_location(_job("Newark, NJ"), other, {}) == "Denver, CO"
    # no default: the corpus display_location, then fail loudly
    bare = Filters(raw={"location_strategy": {"rules": []}})
    ident = {"identity": {"display_location": "Portland, OR"}}
    assert tailor._display_location(_job("Newark, NJ"), bare, ident) == "Portland, OR"
    with pytest.raises(ValueError, match="no display location"):
        tailor._display_location(_job("Newark, NJ"), bare, {})


def test_no_city_is_hardcoded_in_tailor() -> None:
    src = (REPO_ROOT / "src" / "jobkit" / "pipeline" / "tailor.py").read_text()
    assert "Bristol" not in src  # the test profile's NJ rule city
    assert not re.search(r"""return ["'][A-Z][a-z]+(?: [A-Z][a-z]+)*, [A-Z]{2}["']""", src)


@pytest.mark.parametrize(
    ("loc", "state"),
    [
        ("Trenton, NJ", "NJ"),
        ("Remote (NJ)", "NJ"),
        ("Camden, New Jersey", "NJ"),
        ("Seattle, Washington", "WA"),
        ("Washington, DC", "DC"),
        ("Charleston, West Virginia", "WV"),
        ("Remote - US", None),
        ("United States", None),
        ("", None),
    ],
)
def test_job_state(loc: str, state: str | None) -> None:
    assert tailor.job_state(loc) == state


# ---------------------------------------------------------------------------
# resume template: the "Earlier experience" line renders on one page
# ---------------------------------------------------------------------------


def test_resume_template_renders_earlier_experience(tmp_path: Path) -> None:
    data = {
        "identity": {
            "name": "Test Candidate",
            "display_location": "Denver, CO",
            "phone": "+1 555 555 0100",
            "email": "candidate@example.com",
            "linkedin": "linkedin.com/in/example",
        },
        "positioning": "Sales leader.",
        "skills": ["Salesforce"],
        "roles": [
            {
                "company": "Haulmark",
                "title": "Regional Sales Manager",
                "dates": "Mar 2024 – Present",
                "bullets": [],
            }
        ],
        "earlier": [
            {
                "company": "Halvorsen Outfitters",
                "title": "Store Manager",
                "dates": "Jun 2012 – Aug 2016",
            }
        ],
        "education": [
            {"school": "Lakeside State University", "degree": "B.S.", "years": "2008 – 2012"}
        ],
    }
    pdf = render_pdf("resume.typ", data, tmp_path / "resume.pdf")
    assert page_count(pdf) == 1
