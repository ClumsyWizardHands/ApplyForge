"""Tailoring under the prompt_set switch.

Golden: with `prompt_set` absent (the sales test profile in
tests/fixtures/config/), the select_bullets prompt tailor_job sends, its
cache keys, the prompt version recorded in the attempt key and in
provenance.json are byte-identical to snapshots taken before prompt_set
existed (tests/fixtures/golden/sales_select_prompts.json). Regenerate only on
a deliberate change to the sales path: JOBKIT_REGEN_GOLDEN=1 pytest
tests/test_tailor_general.py.

General: prompt_set general sends prompts/select_bullets_general.md with the
job's track emphasis from config, and keys the caches and the attempt on that
prompt.

Offline: every LLM call is faked at llm.call_json."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import llm
from jobkit.config import Filters, load_filters
from jobkit.pipeline import tailor
from tests.conftest import FIXTURES
from tests.test_targeting import GOOD_SEL, _sel, env  # noqa: F401  (env is a fixture)

GOLDEN = FIXTURES / "golden"
REGEN = os.environ.get("JOBKIT_REGEN_GOLDEN") == "1"

SALES_TITLES = (
    "Mid-Market Account Executive",
    "Manager, Sales Development",
    "Revenue Operations Manager",
)


def _golden(name: str, value: Any) -> None:
    path = GOLDEN / name
    text = json.dumps(value, indent=1, sort_keys=True) + "\n"
    if REGEN:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return
    assert path.is_file(), f"no golden file {path}; run with JOBKIT_REGEN_GOLDEN=1"
    assert text == path.read_text(), f"{name} differs from its golden snapshot"


def _run(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    title: str,
) -> dict[str, Any]:
    """tailor_job on one job; what the selection prompt, its cache keys and
    the attempt key were."""
    fake, jid, _ = env([_sel(*GOOD_SEL)], title=title)
    calls: list[dict[str, Any]] = []
    inner = fake.call_json

    def recording(
        c: sqlite3.Connection, stage: str, prompt: str, *, cache_key: str | None = None
    ) -> dict[str, Any]:
        if stage == "tailor":
            calls.append({"cache_key": cache_key, "prompt": prompt})
        out: dict[str, Any] = inner(c, stage, prompt, cache_key=cache_key)
        return out

    monkeypatch.setattr(llm, "call_json", recording)
    keys: list[str] = []
    real_key = tailor._attempt_key

    def attempt_key(*a: Any, **kw: Any) -> str:
        k = real_key(*a, **kw)
        keys.append(k)
        return k

    monkeypatch.setattr(tailor, "_attempt_key", attempt_key)
    paths = tailor.tailor_job(conn, jid)
    prov = json.loads(Path(paths["provenance"]).read_text())
    monkeypatch.setattr(tailor, "_attempt_key", real_key)
    return {
        "tailor_calls": calls,
        "attempt_keys": keys,
        "provenance_tailor_prompt": prov["tailor_prompt"],
        "track": prov["track"],
    }


def test_sales_select_prompts_are_byte_identical(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    got = {title: _run(conn, env, monkeypatch, title) for title in SALES_TITLES}
    _golden("sales_select_prompts.json", got)


# ---------------------------------------------------------------------------
# prompt_set general
# ---------------------------------------------------------------------------

PERSONAS = FIXTURES / "personas"
TEACHER_EMPHASIS = (
    "Units I designed from learning objectives, the assessments I wrote, and LMS work"
)
GENERAL_TAG = "select_bullets_general:1.0"


def _use_persona(home: Path, name: str, **tweak: Any) -> Filters:
    """The persona as the sandbox's config/filters.yaml, with the test
    profile's top-level blocks the persona lacks (location_strategy, resume,
    ...) kept; `tweak` replaces keys of the persona's first track."""
    persona = yaml.safe_load((PERSONAS / f"{name}.yaml").read_text())
    persona["filters"]["tracks"][0].update(tweak)
    path = home / "config" / "filters.yaml"
    raw = {**yaml.safe_load(path.read_text()), **persona}
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_filters()


def test_general_prompt_keeps_the_rules_and_has_no_sales_emphasis() -> None:
    sales = llm.PromptFile("select_bullets")
    general = llm.PromptFile("select_bullets_general")
    assert sales.version == "1.8"  # the sales prompt is not edited
    assert (general.version, general.stage) == ("1.0", "tailor")
    assert set(re.findall(r"\{([a-z_]+)\}", general.body)) == {
        "estimate_style",
        "track",
        "track_emphasis",
        "title",
        "company",
        "requirements",
        "corpus",
    }
    body = " ".join(general.body.split())
    for rule in (
        "You SELECT and lightly REFRAME",
        "8 to 12 bullets total, one-page budget.",
        "At least ONE bullet for every role marked REQUIRED in ROLES",
        '"Earlier experience" line.',
        "Order by relevance WITHIN each role; roles stay reverse-chronological.",
        "Cover the job's must-haves first, then its implied needs.",
        "Never select a bullet that would claim a title or tool in the NEVER CLAIM list",
        'A bullet whose metric_confidence is "estimate" must keep its hedge',
        "NEVER choose a variant that drops a number the other variant",
        "Light trimming never removes a number.",
        "never a bullet's words, and never a claim the corpus does not make",
        "CORPUS (the only allowed source material):",
    ):
        assert rule in body, rule
        assert rule in " ".join(sales.body.split()), rule
    for sales_only in ("sdr_bdr", "account_executive", "sales_exec", "quota", "deals"):
        assert sales_only not in body, sales_only


def test_selection_prompt_follows_prompt_set(jobkit_home: Path) -> None:
    assert tailor.selection_prompt_name(load_filters()) == "select_bullets"
    assert tailor.selection_tag("select_bullets", "1.8") == "1.8"
    assert tailor.selection_tag("select_bullets_general", "1.0") == GENERAL_TAG
    filters = _use_persona(jobkit_home, "teacher_to_id")
    assert tailor.selection_prompt_name(filters) == "select_bullets_general"
    assert tailor.track_emphasis("instructional_designer", filters) == TEACHER_EMPHASIS
    assert tailor.track_emphasis("classroom_teacher", filters) == tailor.NO_TRACK_EMPHASIS
    assert tailor.track_emphasis("other", filters) == tailor.NO_TRACK_EMPHASIS
    assert tailor.track_emphasis(None, filters) == tailor.NO_TRACK_EMPHASIS


def test_teacher_emphasis_reaches_the_general_prompt(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    jobkit_home: Path,
) -> None:
    _use_persona(jobkit_home, "teacher_to_id")
    got = _run(conn, env, monkeypatch, "Instructional Designer")
    prompt = got["tailor_calls"][0]["prompt"]
    assert "ROLE TRACK: instructional_designer\n" in prompt
    assert f"EMPHASIS: {TEACHER_EMPHASIS}\n" in prompt
    assert "{" + "track_emphasis}" not in prompt and "sdr_bdr" not in prompt
    assert prompt.startswith(llm.PromptFile("select_bullets_general").body[:60])
    assert got["tailor_calls"][0]["cache_key"].endswith(f":{GENERAL_TAG}")
    assert got["provenance_tailor_prompt"] == GENERAL_TAG
    assert all(f":{GENERAL_TAG}:" in k for k in got["attempt_keys"])
    assert got["track"] == "instructional_designer"


def test_a_track_without_emphasis_orders_by_relevance(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    jobkit_home: Path,
) -> None:
    _use_persona(jobkit_home, "teacher_to_id")
    got = _run(conn, env, monkeypatch, "Middle School Science Teacher")
    prompt = got["tailor_calls"][0]["prompt"]
    assert "ROLE TRACK: classroom_teacher\n" in prompt
    assert f"EMPHASIS: {tailor.NO_TRACK_EMPHASIS}\n" in prompt


def test_emphasis_with_braces_is_sent_as_written(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    jobkit_home: Path,
) -> None:
    """The emphasis is the person's text: a "{title}" in it is not a slot."""
    _use_persona(jobkit_home, "teacher_to_id", emphasis="Lead with {title} and {corpus} work")
    got = _run(conn, env, monkeypatch, "Instructional Designer")
    prompt = got["tailor_calls"][0]["prompt"]
    assert "EMPHASIS: Lead with {title} and {corpus} work\n" in prompt
    assert "JOB: Instructional Designer at Fabrikam Payments\n" in prompt


def _cover_failure(conn: sqlite3.Connection, job_id: int, attempt: str) -> None:
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'cover_provenance_failed', ?)",
        (job_id, json.dumps({"attempt": attempt})),
    )
    conn.commit()


@pytest.mark.parametrize("persona", [None, "teacher_to_id"])
def test_cover_failure_skip_keys_on_the_prompt_actually_used(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    jobkit_home: Path,
    persona: str | None,
) -> None:
    """tailor_approved skips a job whose cover failed with the same inputs:
    the attempt key it compares names the selection prompt of the current
    prompt_set, so switching prompt_set retries the job."""
    if persona:
        _use_persona(jobkit_home, persona)
    got = _run(conn, env, monkeypatch, "Instructional Designer")
    key = got["attempt_keys"][0]
    jid = int(conn.execute("SELECT MAX(id) FROM jobs").fetchone()[0])
    jd_hash = conn.execute("SELECT jd_hash FROM jobs WHERE id = ?", (jid,)).fetchone()[0]
    _cover_failure(conn, jid, key)
    assert tailor._unchanged_since_cover_failure(conn, jid, jd_hash, {})
    # the other prompt set's key for the same inputs does not match
    other = (
        key.replace(f":{GENERAL_TAG}:", ":1.8:")
        if persona
        else key.replace(":1.8:", f":{GENERAL_TAG}:")
    )
    assert other != key
    _cover_failure(conn, jid, other)
    assert not tailor._unchanged_since_cover_failure(conn, jid, jd_hash, {})
