"""MANDATORY authenticity tests (CLAUDE.md): slop fails pass 1, uniform bullets
fail pass 2, drift past threshold reverts to source, unhedged estimates flagged
(provenance owns that hard-fail), and genuinely good output passes without
manufactured criticism. Offline — embeddings injected, critic mocked."""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any

import pytest

from jobkit import llm
from jobkit.validate import authenticity
from jobkit.validate.authenticity import (
    drift_check,
    lexical_check,
    load_config,
    structural_cover,
    structural_resume,
)
from tests.conftest import FIXTURES

# config/banned_phrases.yaml is the authoritative file. Loaded at import, before
# the per-test sandbox exists, so pin cwd to tests/fixtures/, whose config/ is
# the test profile (the repo's own config/ is personal and absent on a clone).
with contextlib.chdir(FIXTURES):
    CFG = load_config()

SLOP_COVER = (
    "I am excited to apply for this role. I have a proven track record of success "
    "in fast-paced environments. I believe I would be a perfect fit for your team, "
    "and I would love the opportunity to leverage my skills. As you can see from my "
    "resume, my values align with your mission. Thank you for your time and consideration."
)

GOOD_COVER = (
    "Hooli posts its sales compensation bands in every listing, which says a lot about "
    "how the team is run. I led a 9-rep sales team at Haulmark, where Northeast bookings "
    "grew from ~$3.2M to ~$5.6M a year. I also built a reseller program that went from "
    "3 partners to 17 after eighteen months of recruiting one regional dealer at a "
    "time. I have not sold developer tools before. What I can offer instead is two years "
    "of running pricing and the deal desk at a lender, so I know how a finance team "
    "reads a quote before it reaches them. Happy to walk through the numbers."
)


def _fake_embed_same(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.0] for _ in texts]


def _fake_embed_drifted(texts: list[str]) -> list[list[float]]:
    # rendered vectors orthogonal to source vectors -> similarity 0
    return [[1.0, 0.0] if i % 2 == 0 else [0.0, 1.0] for i in range(len(texts))]


CORPUS: dict[str, Any] = {
    "bullets": [
        {
            "id": "b_043",
            "role_id": "r1",
            "metric_confidence": "verified",
            "text_long": "Grew the average reseller partner's annual bookings from ~$20K to $75K+.",
            "text_short": "Grew average partner bookings ~$20K → $75K+ a year.",
            "metric": "~$20K → $75K+",
        },
    ],
}


def test_slop_cover_fails_pass1() -> None:
    hard, _soft = lexical_check(SLOP_COVER, "cover_letter", CFG)
    assert len(hard) >= 4, f"slop must hard-fail pass 1, got {hard}"


def test_good_cover_passes_pass1_and_2() -> None:
    hard, soft = lexical_check(GOOD_COVER, "cover_letter", CFG)
    assert not hard, hard
    flags = structural_cover(GOOD_COVER, "Hooli", CFG)
    assert not flags, f"genuinely good output must not be flagged: {flags}"


def test_uniform_bullet_lengths_fail_pass2() -> None:
    uniform = [
        f"Managed the {c} team to hit the quarterly target on time"
        for c in ("sales", "north", "south", "cloud", "field")
    ]
    flags = structural_resume(uniform, CFG)
    assert any("uniform-bullet-lengths" in f for f in flags)
    assert any("verb-class-monotony" in f for f in flags)


def test_ragged_bullets_pass_pass2() -> None:
    ragged = [
        "Grew average partner bookings ~$20K → $75K+ a year.",
        "Built the reseller program from scratch, with its own deal registration, a "
        "partner tier for the largest dealers and a manager hired from the field team.",
        "Made President's Club at 131% of quota.",
        "Turned a small pilot into remote onboarding for new carriers, 2-3 a month.",
    ]
    assert structural_resume(ragged, CFG) == []


def test_drift_past_threshold_reverts_to_source() -> None:
    selected = [
        {
            "bullet_id": "b_043",
            "variant": "short",
            "rendered": "Spearheaded transformative revenue acceleration initiatives.",
        }
    ]
    flags, reverted = drift_check(selected, CORPUS, CFG, embed=_fake_embed_drifted)
    assert reverted == 1
    assert selected[0]["rendered"] == "Grew average partner bookings ~$20K → $75K+ a year."
    assert flags and flags[0]["action"] == "reverted"


def test_no_drift_no_revert() -> None:
    selected = [
        {
            "bullet_id": "b_043",
            "variant": "short",
            "rendered": "Grew average partner bookings ~$20K → $75K+ a year.",
        }
    ]
    flags, reverted = drift_check(selected, CORPUS, CFG, embed=_fake_embed_same)
    assert reverted == 0
    assert not flags, flags


def test_new_content_words_flagged_even_when_similar() -> None:
    selected = [
        {
            "bullet_id": "b_043",
            "variant": "short",
            "rendered": (
                "Grew average partner bookings ~$20K → $75K+ a year across enterprise verticals."
            ),
        }
    ]
    flags, reverted = drift_check(selected, CORPUS, CFG, embed=_fake_embed_same)
    assert reverted == 0
    assert flags and "new_words" in flags[0]
    assert "enterprise" in flags[0]["new_words"] or "verticals" in flags[0]["new_words"]


@pytest.mark.parametrize("style", ["hedged", "confident"])
def test_unhedged_estimate_flagged_by_provenance(style: str) -> None:
    # the estimate-hedging guarantee lives in the provenance validator (hard
    # fail) — under drift.estimate_style "hedged". Under "confident" the flat
    # figure is the source's own and passes (tests/test_estimate_style.py).
    from jobkit.validate.provenance import validate_tailored

    corpus = {
        "bullets": [
            {
                "id": "b_e",
                "role_id": "r",
                "metric_confidence": "estimate",
                "text_long": "Cut onboarding time ~40%.",
                "text_short": "Onboarding time -~40%.",
                "metric": "~40%",
            }
        ],
        "constraints": {},
    }
    out = validate_tailored(
        [{"bullet_id": "b_e", "rendered": "Cut onboarding time 40%."}], corpus, estimate_style=style
    )
    if style == "hedged":
        assert not out.ok and any(v["kind"] == "unhedged_estimate" for v in out.violations)
    else:
        assert out.ok, out.violations


# ---- audit 2026-10-08: the critic's verdict fails closed, and it actually runs ----


@pytest.fixture()
def job_id(conn: sqlite3.Connection) -> int:
    from tests.test_filter import _insert

    jid = _insert(conn)
    conn.commit()
    return jid


def _critic(verdict: object, calls: list[str]) -> Any:
    def fake(conn: object, text: str, kind: str) -> dict[str, Any]:
        calls.append(kind)
        return {} if verdict is None else {"verdict": verdict, "notes": "n"}

    return fake


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [(None, "fail"), ("", "fail"), ("looks human", "fail"), ("PASS", "pass"), ("fail", "fail")],
)
def test_cover_critic_verdict_normalized(
    conn: sqlite3.Connection,
    job_id: int,
    monkeypatch: pytest.MonkeyPatch,
    verdict: object,
    expected: str,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(authenticity, "run_critic", _critic(verdict, calls))
    rep = authenticity.review_cover(conn, job_id, GOOD_COVER, "Hooli")
    assert calls == ["cover letter"]  # GOOD_COVER is clean, so the critic ran
    assert rep.verdict == expected
    stored = conn.execute("SELECT verdict FROM authenticity_runs").fetchone()[0]
    assert stored == expected


def test_unparseable_critic_reply_is_a_fail(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise ValueError("no JSON object in response")

    monkeypatch.setattr(llm, "call_json", broken)
    out = authenticity.run_critic(conn, "text", "cover letter")
    assert authenticity._normalize_verdict(out.get("verdict")) == "fail"


DASHY: dict[str, Any] = {
    "bullets": [
        {
            "id": f"b_{i}",
            "role_id": "r1",
            "metric_confidence": "verified",
            "text_long": text,
            "text_short": text,
            "metric": None,
        }
        for i, text in enumerate(
            [
                "Launched usage-based pricing — a new SMB lending tier — in one quarter.",
                "Stood up a deal desk for non-standard quotes; discounting got predictable.",
                "Ran remote onboarding for new carriers — two or three go live a month — "
                "and wrote the setup guides their dispatchers used.",
                "Briefed the CFO monthly; trained the partner managers on the new pricing.",
            ]
        )
    ],
}


def _selected(texts: list[str] | None = None) -> list[dict[str, Any]]:
    src = [b["text_short"] for b in DASHY["bullets"]]
    return [
        {"bullet_id": f"b_{i}", "variant": "short", "rendered": t}
        for i, t in enumerate(texts or src)
    ]


def test_resume_critic_runs_when_density_flags_are_the_corpus_own(
    conn: sqlite3.Connection, job_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    for verdict, expected in (("pass", "soft_flag"), ("fail", "fail")):
        calls: list[str] = []
        monkeypatch.setattr(authenticity, "run_critic", _critic(verdict, calls))
        rep = authenticity.review_resume(conn, job_id, _selected(), DASHY, embed=_fake_embed_same)
        assert calls == ["resume bullets"], rep.structural_flags
        assert all(f.startswith("corpus-inherited") for f in rep.structural_flags)
        assert rep.structural_flags, "the inherited flags stay visible"
        # the critic can make the verdict worse, never launder a flag into a pass
        assert rep.verdict == expected


def test_model_added_punctuation_still_blocks_the_critic(
    conn: sqlite3.Connection, job_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(authenticity, "run_critic", _critic("pass", calls))
    src = [b["text_short"] for b in DASHY["bullets"]]
    added = [src[0], src[1].replace(";", " —"), src[2], src[3].replace(";", " — ")]
    rep = authenticity.review_resume(conn, job_id, _selected(added), DASHY, embed=_fake_embed_same)
    assert calls == []
    assert any(f.startswith("em-dash-density") for f in rep.structural_flags)
    assert rep.verdict == "soft_flag"
