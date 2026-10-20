"""What a self-review change may ask a writer for.

A self-review (pipeline/self_review.py) returns a change list. Only one kind of
change ever reaches a writer: a change to a resume bullet selection that PASSED
its checks. The revision it asks for is kept only when every bullet it renders
is its named variant's corpus text, word for word, and it then passes the
selection's full check chain (provenance, authenticity) with an authenticity
verdict no worse than the selection it revises (tailor.py check_selection). A
revision may choose, order and swap corpus bullets; it never rewords one.
Provenance alone checks a bullet's numbers, proper nouns and never-claim terms,
not its every word, so a revision that kept a bullet's text and added "I also
managed the sales development team." passed it (sixth review of the
self-review, 2026-10-09).

`unsourced_change_terms` screens such a change before the writer sees it. A
change whose `what` uses a content word that is in none of the corpus (less its
never-claim lists and voice samples), the skill index keys, the passing draft
under review, and the job title is dropped. Editing words such as "add", "lead
with" and "cut" do not count. "Mention managing SDRs" never reaches the writer.
The target company's name lends no word, and the words of tense and tenure
("was", "were", "been", "had", "did", "before", "once", "ever", "ago",
"previously", "formerly", "currently") are content words here, not
claim-free: "Say I was at Fabrikam Payments before" was free of unsourced words
and reached the writer (review 2026-10-09). Each now needs a source like any
other word, so "Say the General Manager was who I reported to" is dropped unless
a source has "was": an accepted false rejection. The screen is not what keeps a claim out
of the resume: "Say more about leading the team" passes it, and only the
word-for-word rule above stops a writer that adds a sentence.

Removed (fifth review of the self-review, 2026-10-09): the revision gate
(`revision_claims`) and with it every prose revision. A self-review could ask
for a rewrite of a cover letter or drafted answer that had passed, and the gate
compared the rewrite with the passing draft word by word. It could not be made
safe simply. A sentence kept word for word was skipped wherever it moved, so
moving "I also led 9 AEs there" after the ledgerline sentence re-attributed the
team to ledgerline. A new sentence built only from its cited bullets' words
("I ran the merchant team for ~18 months.") passed though no bullet says it. So a
cover letter and a drafted answer are now reviewed and reported only, never
revised. A failing draft's change list never rides on its regeneration either:
its vocabulary lent the failed draft's own words to a change that endorsed a
fabricated sentence. A regeneration carries only the check feedback it carried
at aca26cc. The screen therefore never takes words from a failed draft or from
a violation.

Words are compared by a light stem (validate/claims.py _root: plural, -ed,
-ing, a final -e, common irregular past forms). So "managed" matches "manage"
and "managing" but not "managers", and "development" does not match
"developed". The rule has no threshold: every content word must be covered
(fail closed)."""

from __future__ import annotations

from typing import Any

from jobkit.validate.claims import _root, _strings, _surface, _words, terms

# what a change list says about the edit itself, not about the candidate
_EDIT = _words(
    """add use cite mention name state say include keep lead open close end start begin move
    reorder order swap replace drop remove delete cut trim shorten tighten condense merge
    split combine rewrite reword rephrase render pick choose select prefer emphasize
    emphasise highlight foreground surface stress frame connect tie link address answer cover
    fix correct avoid instead rather exactly verbatim word wording phrase phrasing sentence
    paragraph line letter resume draft bullet variant long short version positioning skill
    section opening closing role job posting requirement must nice gap fit strongest strong
    stronger weak weaker generic specific concrete relevant unused used result fact evidence
    example point top bottom earlier later above below current recent older newer clearly
    directly explicitly first last second third size number figure metric amount count make
    sure ensure show tell explain note spell edu corpus source unchanged rest whole tone
    voice"""
)

_EDIT_ROOTS = frozenset(_root(w) for w in _EDIT)

# Words of tense and tenure. claims._TERM_STOP counts them as claim-free, so
# "Say I was at Fabrikam Payments before" passed the screen on the target's name
# and these words alone (review 2026-10-09). Here they are content words: a
# change uses one only when the corpus or the passing draft does.
TENSE_WORDS = frozenset(
    {
        "was",
        "were",
        "been",
        "had",
        "did",
        "before",
        "once",
        "ever",
        "ago",
        "previously",
        "formerly",
        "currently",
    }
)


def change_vocabulary(
    corpus: dict[str, Any], *, draft: str, target_company: str, job_title: str
) -> set[str]:
    """Every content word a self-review change may use: the corpus (less its
    never-claim lists and voice samples), the skill index keys, the draft under
    review, and the job title. `draft` must be a draft that PASSED its checks;
    nothing else lends a word (no failed draft, no violation). The target's
    name lends none: a word of it counts only when the corpus itself has it
    (review 2026-10-09: "Say I was at Fabrikam Payments before" was free). The
    words of tense and tenure (TENSE_WORDS) count as words here, so a change
    uses one only when these sources do."""
    facts = {k: v for k, v in corpus.items() if k not in ("constraints", "voice_samples")}
    index = corpus.get("skill_index") or {}
    keys = list(index) if isinstance(index, dict) else []
    corpus_words = terms(" ".join([*_strings(facts), *keys]), TENSE_WORDS)
    vocabulary = corpus_words | terms(f"{draft} {job_title}", TENSE_WORDS)
    return vocabulary - (terms(target_company, TENSE_WORDS) - corpus_words)


def unsourced_change_terms(what: str, vocabulary: set[str]) -> list[str]:
    """The words of a change's `what` that are neither editing words nor in
    `vocabulary` (change_vocabulary), the words of tense and tenure
    (TENSE_WORDS) included. Non-empty: the change asks the writer for
    something the corpus lacks, and is dropped."""
    words = _surface(what, TENSE_WORDS)
    return sorted(w for r, w in words.items() if r not in vocabulary and r not in _EDIT_ROOTS)
