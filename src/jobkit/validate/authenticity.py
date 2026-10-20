"""Authenticity review (SPEC §7 stage 10b) — four passes, cheapest first.
config/banned_phrases.yaml is AUTHORITATIVE. Pass 3 (drift) is the real
defense: text that came from the corpus, lightly reframed and verifiably
traceable, reads human because it is."""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jobkit import llm
from jobkit.config import read_config_yaml
from jobkit.log import log
from jobkit.validate import style as estimate_styles
from jobkit.validate.provenance import figure_violations

_VERB_CLASSES = {
    "lead": {
        "lead",
        "led",
        "manage",
        "managed",
        "direct",
        "directed",
        "oversee",
        "oversaw",
        "head",
        "headed",
        "ran",
        "run",
    },
    "build": {
        "build",
        "built",
        "create",
        "created",
        "develop",
        "developed",
        "design",
        "designed",
        "ship",
        "shipped",
        "engineered",
    },
    "grow": {
        "grow",
        "grew",
        "increase",
        "increased",
        "scale",
        "scaled",
        "drive",
        "drove",
        "boost",
        "boosted",
        "expand",
        "expanded",
    },
    "own": {"own", "owned"},
    "improve": {
        "improve",
        "improved",
        "optimize",
        "optimized",
        "cut",
        "reduce",
        "reduced",
        "streamline",
        "streamlined",
        "rebuilt",
        "restructured",
    },
}
_ADJ = {
    "strategic",
    "innovative",
    "robust",
    "seamless",
    "dynamic",
    "proven",
    "strong",
    "deep",
    "extensive",
    "comprehensive",
    "holistic",
    "impactful",
    "transformative",
    "scalable",
    "significant",
    "exceptional",
    "outstanding",
    "unique",
    "powerful",
}
_GENERIC_PRAISE = re.compile(
    r"your (mission|team|culture|vision|values|innovative|industry.leading)|"
    r"cutting.edge|world.class|industry.lead|at the forefront|truly (unique|special)",
    re.I,
)
_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "for",
    "with",
    "from",
    "into",
    "that",
    "this",
    "its",
    "our",
    "their",
    "his",
    "her",
    "was",
    "were",
    "is",
    "are",
    "by",
    "at",
    "on",
    "as",
    "it",
    "be",
    "been",
    "over",
    "under",
    "per",
    "via",
}


@dataclass
class AuthenticityReport:
    verdict: str = "pass"  # pass | soft_flag | fail
    lexical_flags: list[str] = field(default_factory=list)
    structural_flags: list[str] = field(default_factory=list)
    drift_flags: list[dict[str, Any]] = field(default_factory=list)
    critic_notes: str = ""
    reverted: int = 0


def load_config() -> dict[str, Any]:
    """config/banned_phrases.yaml (the path is validate/style.py's: the
    estimate_style knob lives in this file too)."""
    cfg: dict[str, Any] = read_config_yaml(estimate_styles.CONFIG_PATH)
    return cfg


# ---- pass 1: lexical (free, deterministic; YAML is authoritative) ----


def lexical_check(text: str, kind: str, cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    section = cfg.get(kind, {}) or {}
    low = text.lower()
    hard = [p for p in section.get("hard_reject", []) if p.lower() in low]
    # regex tics a fixed phrase cannot catch (a contrast construction); a bad
    # regex in the YAML raises here — fail loudly, never skip the check
    for pat in section.get("hard_reject_patterns", []) or []:
        name, rx = (pat.get("name"), pat.get("regex")) if isinstance(pat, dict) else (pat, pat)
        if re.search(str(rx), text, re.I):
            hard.append(f"pattern: {name}")
    soft = [
        p for p in section.get("soft_flag", []) if re.search(rf"\b{re.escape(p.lower())}\b", low)
    ]
    return hard, soft


# ---- pass 2b: phrases repeated ACROSS the candidate's letters ----


def _wordset(words: str) -> frozenset[str]:
    return frozenset(words.split())


# words that carry no phrasing of their own: a shingle made only of these and
# corpus vocabulary is not a tic
_LETTER_STOP = (
    _STOP
    | _wordset(
        "i i'm i've i'd i'll me my mine you you're your yours we we're our us he she they "
        "them their it's that's what which who whom how why when where there here than "
        "then so if but not no nor yes would will can could should shall may might must "
        "have has had having do does did done am being about more most all any each both "
        "also just very much many some such only own same too few other one two up down "
        "out off again further once role roles team teams position company job posting "
        "work"
    )
    # function adverbs and connectives (claims._FUNCTION): "already includes
    # Acme, Globex…" retells the JD; "already" is not phrasing
    | _wordset(
        "already always often sometimes still even now while since until because "
        "although though these those every without within through during across "
        "after before"
    )
    # the hedges a letter may put before a number (claims._LETTER_HEDGE):
    # "roughly 18%" retells the corpus's "~18%" — the hedge is not phrasing
    | _wordset("roughly approximately approx around nearly almost near less fewer least upwards")
)
_TOKEN = re.compile(r"[a-z0-9$~%+'.-]+")
# a figure, range or year ("~$5.6m", "30%", "10+", "3-4", "1990s"): numbers are
# the facts being retold, never the phrasing that makes a tic
_NUMBER = re.compile(
    r"[~$]*\d[\d,.]*[kmb%x+]*(?:-[~$]*\d[\d,.]*[kmb%x+]*)*(?:s|th|st|nd|rd|mm|bn)?"
)
# The close the cover prompt requires ("ask for the conversation") has only a
# few plain wordings. Asking the same way twice is not a tic; whatever the
# letter appends to the ask ("…which accounts your team should phone first") is
# still compared.
_ASK_FORMULA = re.compile(
    r"\b(?:i'd|i would|i'll|i will)\s+(?:like|love|welcome|want)\s+"
    r"(?:(?:the|a)\s+(?:chance|opportunity)\s+)?to\s+(?:talk|discuss|speak|chat)"
    r"(?:\s+with\s+you)?(?:\s+(?:about|through|more))?\b"
    r"|\b(?:i'd|i would)\s+welcome\s+(?:a|the)\s+(?:conversation|call)(?:\s+about)?\b"
    r"|\blet's\s+talk(?:\s+about)?\b"
)


def _norm_letter(text: str) -> str:
    return text.lower().replace("’", "'").replace("‘", "'")


def _letter_token_spans(norm: str) -> list[tuple[str, int, int]]:
    """(token, start, end) over already-normalized text. A leading "~" is a
    hedge, not part of the word: "~18%" and "roughly 18%" carry the same 18%."""
    out: list[tuple[str, int, int]] = []
    for m in _TOKEN.finditer(norm):
        raw = m.group(0)
        t = raw.strip(".-'").lstrip("~")
        if t:
            start = m.start() + raw.index(t)  # the span of the word itself, not "talk."
            out.append((t, start, start + len(t)))
    return out


def _letter_tokens(text: str) -> list[str]:
    return [t for t, _s, _e in _letter_token_spans(_norm_letter(text))]


def _stem(t: str) -> str:
    """Crude suffix stemmer for vocabulary matching only: "routed" and
    "routing" both retell b_047's "routing", and "manager" matches the
    corpus's "manager's". Never used to match the n-grams themselves."""
    t = t.removesuffix("'s").rstrip("'")
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("ly", ""), ("s", "")):
        if t.endswith(suf) and len(t) - len(suf) >= 3 and not (suf == "s" and t.endswith("ss")):
            t = t[: -len(suf)] + rep
            break
    if len(t) > 3 and t[-1] == t[-2] and t[-1] not in "aeioulsz":
        t = t[:-1]  # running → run, staffing → staf (and staff → staf)
    if len(t) > 3 and t.endswith("e"):
        t = t[:-1]  # manage / managed → manag
    return t


def _vocab(text: str) -> frozenset[str]:
    """Stems of every word of `text`, hyphenated words both whole and by
    part ("channel-partner" → channel-partner, channel, partner)."""
    out: set[str] = set()
    for t in _letter_tokens(text):
        out.add(_stem(t))
        if "-" in t:
            out.update(_stem(p) for p in t.split("-") if p)
    return frozenset(out)


def _is_filler(t: str) -> bool:
    """A token that is never phrasing: a stop word, a hedge, a number, or too
    short to carry meaning (the pre-existing len > 2 rule)."""
    return len(t) <= 2 or t in _LETTER_STOP or bool(_NUMBER.fullmatch(t))


def _in_vocab(t: str, vocab: frozenset[str]) -> bool:
    """`t` (by stem) is in `vocab`, or `t` is hyphenated and each of its
    meaningful parts is ("channel-partner" when `vocab` has "channel" and "partner")."""
    if _stem(t) in vocab:
        return True
    words = [p for p in t.split("-") if p and not _is_filler(p)] if "-" in t else []
    return bool(words) and all(_stem(p) in vocab for p in words)


def _exempt_vocab(corpus: dict[str, Any] | None, company: str, jd: str = "") -> frozenset[str]:
    """Words a repeated phrase may consist of without being a tic: corpus fact
    text (bullets, positioning, roles, skills, education), the target's own
    name, and this job's description (a company fact taken from the posting —
    "Acme, Globex, Initech and Vandelay" — repeats across letters to that
    company). Retelling the same bullet in two letters is expected."""
    parts: list[str] = [company, jd]
    if corpus:
        for b in corpus.get("bullets") or []:
            parts += [str(b.get(k) or "") for k in ("text_long", "text_short", "metric")]
        parts += [str(p.get("text") or "") for p in corpus.get("positioning") or []]
        for r in corpus.get("roles") or []:
            parts += [str(r.get("company") or ""), str(r.get("title") or "")]
        parts += [str(k) for k in (corpus.get("skill_index") or {})]
        for e in corpus.get("education") or []:
            parts += [str(v) for v in e.values()]
    return _vocab(" ".join(parts))


def _num_key(t: str) -> str:
    """A figure without its hedge, currency sign or "+": "~$5.6M" and "$5.6M",
    "10+" and "10" are the same retold figure."""
    return t.lstrip("~$").rstrip("+")


def _anchor_vocab(text: str) -> frozenset[str]:
    figures = {
        _num_key(p)
        for t in _letter_tokens(text)
        if _NUMBER.fullmatch(t)
        for p in [t, *t.split("-")]
        if p
    }
    return _vocab(text) | figures


def _anchor_vocabs(
    corpus: dict[str, Any] | None, cited: Sequence[str] | None
) -> list[frozenset[str]]:
    """One vocabulary (words and figures) per bullet the letter cites — every
    bullet when the citations are not known."""
    if not corpus:
        return []
    want = None if cited is None else {str(x) for x in cited}
    return [
        _anchor_vocab(" ".join(str(b.get(k) or "") for k in ("text_long", "text_short", "metric")))
        for b in corpus.get("bullets") or []
        if want is None or str(b.get("id")) in want
    ]


def _covered(t: str, vocab: frozenset[str]) -> bool:
    if _NUMBER.fullmatch(t):
        parts = [p for p in t.split("-") if p]
        return _num_key(t) in vocab or all(_num_key(p) in vocab for p in parts)
    return _in_vocab(t, vocab)


def _is_tic(
    gram: Sequence[str],
    masked: Sequence[bool],
    exempt: frozenset[str],
    anchors: Sequence[frozenset[str]],
) -> bool:
    """Whether an n-gram seen in earlier letters carries phrasing of the
    letter's own. Its own words are the tokens that are not filler, not part
    of the ask-for-a-conversation close, and not exempt vocabulary (corpus,
    target name, JD); an n-gram without one is not a tic. Nor is one that
    retells a single cited bullet: it carries at least one of that bullet's
    words or figures, and no fewer of them than words of its own — "annual
    bookings of ~$5.6M, pushing roughly 18% through resellers" retells b_047 although
    "pushing" is not a corpus word, and "watched the renewal rate move from ~44%"
    retells b_055. A tic longer than the n-gram still shows in the n-grams
    that run past the fact ("…is the real product here")."""
    terms = [t for t, m in zip(gram, masked, strict=True) if not m and t not in _LETTER_STOP]
    own = [t for t in terms if not _is_filler(t) and not _in_vocab(t, exempt)]
    if not own:
        return False
    facts = [t for t in terms if len(t) > 2 or _NUMBER.fullmatch(t)]
    return not any(sum(_covered(t, v) for t in facts) >= len(own) for v in anchors)


def cross_letter_repeats(
    letter: str,
    prior_letters: Sequence[str],
    cfg: dict[str, Any],
    *,
    corpus: dict[str, Any] | None = None,
    company: str = "",
    jd: str = "",
    cited: Sequence[str] | None = None,
) -> list[str]:
    """Phrases of `letter` that already appear in at least
    `cross_letter.min_prior_letters` of the candidate's earlier letters
    (a tic such as a contrast construction can close letter after letter, and
    no single-letter check can see that). Compares word n-grams; an n-gram
    counts only if it has a content word outside the corpus, this job's
    description (`jd`) and the target's name — numbers and hedges never
    count, words are compared by stem, the plain ask for a conversation ("I'd
    like to talk about") is not phrasing, and an n-gram that retells one of
    the `cited` bullets (the letter's bullets_used; None = any bullet) may add
    as many narrative words ("delivered", "beating") as it carries of that
    bullet's words and figures (see _is_tic). Overlapping hits are merged into
    one phrase per flag."""
    c = cfg.get("cross_letter", {}) or {}
    if not c.get("enabled", True) or not prior_letters:
        return []
    n = int(c.get("phrase_words", 5))
    need = int(c.get("min_prior_letters", 2))
    exempt = _exempt_vocab(corpus, company, jd)
    anchors = _anchor_vocabs(corpus, cited)

    def shingles(tokens: list[str]) -> set[tuple[str, ...]]:
        return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}

    seen: Counter[tuple[str, ...]] = Counter()
    for prior in prior_letters:
        seen.update(shingles(_letter_tokens(prior)))
    norm = _norm_letter(letter)
    spans = _letter_token_spans(norm)
    tokens = [t for t, _s, _e in spans]
    asks = [(m.start(), m.end()) for m in _ASK_FORMULA.finditer(norm)]
    masked = [any(a <= s and e <= b for a, b in asks) for _t, s, e in spans]
    hits = [
        i
        for i in range(len(tokens) - n + 1)
        if seen[tuple(tokens[i : i + n])] >= need
        and _is_tic(tokens[i : i + n], masked[i : i + n], exempt, anchors)
    ]
    flags: list[str] = []
    span_start: int | None = None
    span_end = -1
    count = 0
    for i in [*hits, None]:
        if i is not None and span_start is not None and i <= span_end:
            span_end = i + n
            count = max(count, seen[tuple(tokens[i : i + n])])
            continue
        if span_start is not None:
            phrase = " ".join(tokens[span_start:span_end])
            flags.append(f'cross-letter-repeat: "{phrase}" (in {count} earlier letters)')
        if i is not None:
            span_start, span_end = i, i + n
            count = seen[tuple(tokens[i : i + n])]
    return flags


# ---- pass 2: structural (free, deterministic) ----


def structural_resume(bullets: Sequence[str], cfg: dict[str, Any]) -> list[str]:
    s = cfg.get("structural", {}) or {}
    flags: list[str] = []
    if len(bullets) >= 4:
        lengths = [len(b) for b in bullets]
        mean = statistics.mean(lengths)
        stdev = statistics.pstdev(lengths)
        if mean and stdev / mean < float(s.get("bullet_length_stdev_min_pct", 0.15)):
            flags.append(f"uniform-bullet-lengths: stdev {stdev / mean:.0%} of mean")
    counts: dict[str, int] = {}
    for b in bullets:
        first = re.sub(r"[^a-z]", "", b.split()[0].lower()) if b.split() else ""
        for cls, verbs in _VERB_CLASSES.items():
            if first in verbs:
                counts[cls] = counts.get(cls, 0) + 1
    cap = int(s.get("max_bullets_per_verb_class", 2))
    for cls, n in counts.items():
        if n > cap:
            flags.append(f"verb-class-monotony: {n} bullets open with '{cls}' verbs (max {cap})")
    flags += _density_flags(" ".join(bullets), s)
    flags += _adjective_stacking(" ".join(bullets), s)
    return flags


def structural_cover(text: str, company: str, cfg: dict[str, Any]) -> list[str]:
    s = cfg.get("structural", {}) or {}
    flags: list[str] = []
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]
    if len(sentences) >= 4:
        lens = [len(x.split()) for x in sentences]
        if statistics.pstdev(lens) < float(s.get("sentence_length_stdev_min", 5.0)):
            flags.append(f"uniform-sentence-lengths: stdev {statistics.pstdev(lens):.1f}")
    if s.get("company_swap_test", True):
        for sent in sentences:
            if company.lower() not in sent.lower() and _GENERIC_PRAISE.search(sent):
                flags.append(f"company-swap: '{sent[:70]}'")
    flags += _density_flags(text, s)
    flags += _adjective_stacking(text, s)
    return flags


def _density_flags(text: str, s: dict[str, Any]) -> list[str]:
    words = max(1, len(text.split()))
    flags = []
    em = text.count("—") / words * 100
    if em > float(s.get("em_dashes_per_100_words_max", 1.5)):
        flags.append(f"em-dash-density: {em:.1f}/100w")
    semi = text.count(";") / words * 100
    if semi > float(s.get("semicolons_per_100_words_max", 1.0)):
        flags.append(f"semicolon-density: {semi:.1f}/100w")
    return flags


def _adjective_stacking(text: str, s: dict[str, Any]) -> list[str]:
    cap = int(s.get("max_stacked_adjectives", 2))
    run, flags = 0, []
    for w in re.findall(r"[a-zA-Z-]+", text.lower()):
        run = run + 1 if w in _ADJ else 0
        if run > cap:
            flags.append("adjective-stacking")
            run = 0
    return flags


# ---- pass 3: drift (the important one) ----


def _default_embed(texts: list[str]) -> list[list[float]]:
    from jobkit.autofill.answer_bank import _embedder

    return [list(v) for v in _embedder().embed(texts)]


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=True))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    return float(num / (da * db)) if da and db else 0.0


def drift_check(
    selected: list[dict[str, Any]],
    corpus: dict[str, Any],
    cfg: dict[str, Any],
    embed: Callable[[list[str]], list[list[float]]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Diff each rendered bullet against its corpus source. Below threshold →
    revert to source (config on_drift). New content words → flag. Every
    bullet not reverted also has its figures checked against its source under
    `drift.estimate_style` (provenance.figure_violations): an inflated value,
    added precision, a collapsed range or a dropped "up to" is flagged in
    either style; an estimate without "~" is flagged only under "hedged". A
    figure flag carries "figures" and fails the review. Returns (drift_flags,
    reverted_count); mutates `selected` in place on revert."""
    d = cfg.get("drift", {}) or {}
    threshold = float(d.get("similarity_threshold", 0.75))
    revert = d.get("on_drift", "revert_to_source") == "revert_to_source"
    style = estimate_styles.estimate_style_from(cfg)
    embed = embed or _default_embed
    bullets = {b["id"]: b for b in corpus.get("bullets", [])}
    flags: list[dict[str, Any]] = []
    reverted = 0

    pairs = [(item, bullets.get(item.get("bullet_id"))) for item in selected]
    texts: list[str] = []
    for item, src in pairs:
        texts.append(str(item.get("rendered", "")))
        texts.append(str(src["text_long"]) if src else "")
    vecs = embed(texts)

    for k, (item, src) in enumerate(pairs):
        if src is None:
            continue
        rendered = str(item.get("rendered", ""))
        sim = _cos(vecs[2 * k], vecs[2 * k + 1])
        source_blob = (
            str(src["text_long"])
            + " "
            + str(src["text_short"])
            + " "
            + str(src.get("metric") or "")
        ).lower()
        # identical tokenization on both sides: hyphens/dashes split into parts
        source_words = set(re.findall(r"[a-z]+", source_blob))
        new_words = [
            w
            for w in re.findall(r"[a-z]+", rendered.lower())
            if len(w) > 3 and w not in _STOP and w not in source_words
        ]
        if sim < threshold:
            flags.append(
                {
                    "bullet_id": src["id"],
                    "similarity": round(sim, 3),
                    "rendered": rendered[:100],
                    "source": str(src["text_short"])[:100],
                    "action": "reverted" if revert else "flagged",
                }
            )
            if revert:
                variant = item.get("variant", "short")
                item["rendered"] = str(
                    src[f"text_{variant}"] if f"text_{variant}" in src else src["text_short"]
                ).strip()
                reverted += 1
        elif d.get("flag_new_content_words", True) and new_words:
            flags.append(
                {
                    "bullet_id": src["id"],
                    "similarity": round(sim, 3),
                    "new_words": new_words[:8],
                    "action": "flagged",
                }
            )
        if sim >= threshold or not revert:
            # a reverted bullet is its source's own words; the provenance
            # recheck after this review is what gates those
            figures = figure_violations(
                rendered,
                "\n".join(str(src.get(k) or "") for k in ("text_long", "text_short", "metric")),
                src.get("metric_confidence"),
                style,
            )
            if figures:
                flags.append(
                    {
                        "bullet_id": src["id"],
                        "rendered": rendered[:100],
                        "figures": [f"{v['kind']}: {v['detail']}" for v in figures],
                        "action": "flagged",
                    }
                )
    return flags, reverted


# ---- pass 4: adversarial critic (strong model, only if 1-3 clean) ----


def run_critic(conn: sqlite3.Connection, text: str, kind: str) -> dict[str, Any]:
    """The critic's JSON. A reply that never parses comes back as {} — no
    verdict — which _normalize_verdict turns into "fail" (fail closed)."""
    prompt = llm.PromptFile("authenticity_critic")
    try:
        return llm.call_json(conn, "critic", prompt.render(kind=kind, text=text))
    except (ValueError, json.JSONDecodeError) as e:
        log("critic.unparseable", kind=kind, error=str(e)[:200])
        return {"notes": "critic reply was not parseable JSON"}


_VERDICTS = ("pass", "soft_flag", "fail")


def _normalize_verdict(v: object) -> str:
    """Critic output is model text. A verdict outside the schema — missing,
    empty or unparseable — is a FAIL, never a pass (audit 2026-10-08: a
    missing cover verdict used to count as pass). Case is forgiven."""
    s = str(v or "").strip().lower()
    return s if s in _VERDICTS else "fail"


def _worst(*verdicts: str) -> str:
    return max(verdicts, key=_VERDICTS.index)


_DENSITY = {"em-dash-density": "—", "semicolon-density": ";"}


def _mark_inherited_density(
    flags: list[str], rendered: Sequence[str], sources: Sequence[str]
) -> list[str]:
    """A density flag the corpus itself trips (the audited corpus carries ~2.7
    em-dashes per 100 words) is the human's own punctuation, not the model's:
    when the rendered bullets carry no more of that mark than their corpus
    sources do, the flag is kept but marked "corpus-inherited", and it does
    not stop the critic from running (audit 2026-10-08 §2 item 6)."""
    out = []
    for f in flags:
        mark = next((m for k, m in _DENSITY.items() if f.startswith(k)), None)
        if mark and sum(r.count(mark) for r in rendered) <= sum(s.count(mark) for s in sources):
            out.append(f"corpus-inherited {f}")
        else:
            out.append(f)
    return out


# ---- orchestration ----


def review_resume(
    conn: sqlite3.Connection,
    job_id: int,
    selected: list[dict[str, Any]],
    corpus: dict[str, Any],
    embed: Callable[[list[str]], list[list[float]]] | None = None,
    use_critic: bool = True,
) -> AuthenticityReport:
    cfg = load_config()
    rep = AuthenticityReport()
    bullets_text = [str(s["rendered"]) for s in selected]
    by_id = {b["id"]: b for b in corpus.get("bullets", [])}
    source_text = [
        str(by_id[s["bullet_id"]].get(f"text_{s.get('variant', 'short')}") or "")
        for s in selected
        if s.get("bullet_id") in by_id
    ]
    hard, soft = lexical_check(" ".join(bullets_text), "resume", cfg)
    rep.lexical_flags = [f"hard:{p}" for p in hard] + [f"soft:{p}" for p in soft]
    rep.structural_flags = _mark_inherited_density(
        structural_resume(bullets_text, cfg), bullets_text, source_text
    )
    rep.drift_flags, rep.reverted = drift_check(selected, corpus, cfg, embed)
    model_made = [f for f in rep.structural_flags if not f.startswith("corpus-inherited")]
    # a figure its source does not state as written is never a matter of style
    figures = any(f.get("figures") for f in rep.drift_flags)
    clean = (
        not hard
        and not model_made
        and not any(f.get("action") == "flagged" and "new_words" not in f for f in rep.drift_flags)
    )
    if hard or figures:
        rep.verdict = "fail"
    elif rep.structural_flags or soft or not clean:
        rep.verdict = "soft_flag"  # inherited flags still show — never a silent pass
    if clean and use_critic:
        critic = run_critic(conn, "\n".join(f"• {b}" for b in bullets_text), "resume bullets")
        rep.critic_notes = str(critic.get("notes", ""))
        # the critic can only make the verdict worse, never launder a flag
        rep.verdict = _worst(rep.verdict, _normalize_verdict(critic.get("verdict")))
    _record(conn, job_id, "resume", rep)
    return rep


def review_cover(
    conn: sqlite3.Connection,
    job_id: int,
    letter: str,
    company: str,
    use_critic: bool = True,
    *,
    prior_letters: Sequence[str] = (),
    corpus: dict[str, Any] | None = None,
    jd: str = "",
    cited: Sequence[str] | None = None,
) -> AuthenticityReport:
    """`prior_letters`: the candidate's earlier letters, for the cross-letter
    repetition check (pass 2b); `corpus`, this job's `jd` text and the
    letter's `cited` bullet ids exempt retold facts from it."""
    cfg = load_config()
    rep = AuthenticityReport()
    hard, soft = lexical_check(letter, "cover_letter", cfg)
    rep.lexical_flags = [f"hard:{p}" for p in hard] + [f"soft:{p}" for p in soft]
    repeats = cross_letter_repeats(
        letter, prior_letters, cfg, corpus=corpus, company=company, jd=jd, cited=cited
    )
    rep.structural_flags = structural_cover(letter, company, cfg) + repeats
    repeat_fails = bool(repeats) and (cfg.get("cross_letter") or {}).get("on_repeat") == "fail"
    if hard or repeat_fails:
        rep.verdict = "fail"
    elif rep.structural_flags or soft:
        rep.verdict = "soft_flag"
    elif use_critic:
        critic = run_critic(conn, letter, "cover letter")
        rep.critic_notes = (
            str(critic.get("notes", "")) + " | tells: " + str(critic.get("tells", []))[:400]
        )
        rep.verdict = _normalize_verdict(critic.get("verdict"))  # missing → fail
    _record(conn, job_id, "cover_letter", rep)
    return rep


def record_report(
    conn: sqlite3.Connection, job_id: int, artifact: str, rep: AuthenticityReport
) -> None:
    """Record `rep` again as the artifact's latest authenticity run. The
    self-review (pipeline/self_review.py) calls it after discarding a revision
    whose review was recorded: the newest row must describe what ships."""
    _record(conn, job_id, artifact, rep)


def _record(conn: sqlite3.Connection, job_id: int, artifact: str, rep: AuthenticityReport) -> None:
    conn.execute(
        """INSERT INTO authenticity_runs
           (job_id, artifact, verdict, lexical_flags, structural_flags, drift_flags,
            critic_notes, regeneration_count, checked_at, prompt_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, datetime('now'), '1.0')""",
        (
            job_id,
            artifact,
            rep.verdict,
            json.dumps(rep.lexical_flags),
            json.dumps(rep.structural_flags),
            json.dumps(rep.drift_flags),
            rep.critic_notes,
        ),
    )
    conn.commit()
    log(
        "authenticity.run",
        job_id=job_id,
        artifact=artifact,
        verdict=rep.verdict,
        lexical=len(rep.lexical_flags),
        structural=len(rep.structural_flags),
        drift=len(rep.drift_flags),
        reverted=rep.reverted,
    )
