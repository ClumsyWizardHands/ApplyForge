"""What a cover-letter regeneration is told about the drafts before it
(pipeline/tailor.py, 2026-10-23).

A failed draft is never edited in code. The next draft is written by the
model from the same prompt, as a new letter, and it is told what failed in
every earlier draft of the run: each violation's kind, the sentence it was
found in, the rule in plain words and the checker's own detail. The list is
cumulative (DraftHistory): each regeneration is a fresh prompt, so a draft
told only about the one before could repeat a failure fixed two drafts back.
Fact-check failures and authenticity failures are listed in separate
sections, each grouped by the draft it came from; a violation seen again is
listed once, with the drafts it failed in. The rules themselves are unchanged
(src/jobkit/validate/ is read, never edited here): this module only explains
them.

The earlier drafts themselves are not passed on, only their flagged
sentences and, for a draft that passed the fact check and failed
authenticity, its bullets_used ids, its authenticity flags and the critic's
notes (cut at 400 characters). Those flags and notes can quote sentences no
check flagged, but they go only into the {feedback} slot of the prompt: no
draft text reaches the letter except through a fresh draft that runs the
whole check chain. A sentence no check flagged may still be a claim the
checks cannot see, and carrying a whole draft forward would carry that claim
too (the fifth review of the self-review, tests/test_self_review.py). So the writer is never asked
to "keep the rest" of a letter it cannot see: it writes a new one under the
same rules, without the failed sentences. Every draft is checked from scratch.

Plain explanations are keyed by violation kind; a kind with no entry gets a
generic one, so a new rule never breaks the feedback."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

# kind -> the rule, in plain words, as the writer should read it
RULES: dict[str, str] = {
    # citations
    "unknown_bullet_id": "bullets_used may list only the ids of CANDIDATE FACTS above.",
    "bullet_not_provided": (
        "bullets_used may list only the ids of CANDIDATE FACTS above; this id is not one of them."
    ),
    "missing_citations": (
        "Every bullet id the letter draws on must be listed in bullets_used; a letter "
        "with facts about the candidate needs at least one."
    ),
    "uncited_employer": (
        "An employer of the candidate is named only when a bullet of that employer is "
        "cited in bullets_used."
    ),
    # numbers
    "fabricated_number": (
        "Every number (in digits or words: 'two', 'ninety', 'doubled', '#1') must be in "
        "a cited bullet. A plan, a timeline or a count of your own invention is a number "
        "too. Use the bullet's number or say it without one. A number from the job "
        "description may appear only in the one company-fact sentence, which opens with "
        "the company's name and copies words in order from one sentence of the posting "
        "(see the prompt)."
    ),
    "unhedged_estimate": (
        "This figure is an estimate in its bullet; write it the way FIGURE STYLE says."
    ),
    "dropped_hedge": "This figure carries a hedge in its bullet; keep it as FIGURE STYLE says.",
    "dropped_period": (
        "A rate keeps its period exactly as the bullet writes it ('a week', 'a month', 'a year')."
    ),
    "added_period": "Do not add a period ('a month', 'a year') the bullet does not have.",
    "changed_period": "A rate keeps the bullet's own period; never change it.",
    "collapsed_range": "A range stays whole, exactly as the bullet writes it ('6–10', never '10').",
    "changed_range": "A range stays exactly as the bullet writes it.",
    "added_qualifier": (
        "Do not add 'up to', 'over', 'peaked at', '+' or any bound the bullet does not have."
    ),
    "dropped_qualifier": "Keep 'up to', 'under', 'over' or '+' wherever the bullet has it.",
    # names and titles
    "unsourced_proper_noun": (
        "A name (a team, a product, a program, a title) appears only when a cited bullet "
        "has it. The posting's own names (its teams, programs, titles, products) are never "
        "facts about the candidate, in any capitalization. Use the cited bullet's own words "
        "for the candidate's work, or leave the point out; the posting's names may appear "
        "only in the one company-fact sentence."
    ),
    "unsourced_tool": (
        "A tool or product is named only when it is in CONFIRMED SKILLS. Leave it out."
    ),
    "never_claim_tool": "This tool is on NEVER CLAIM. Leave it out.",
    "never_claim_title": (
        "This title is on NEVER CLAIM: never present it as the candidate's, and name the "
        "job's own title only as the job ('the <title> role at <company>')."
    ),
    "never_claim_phrase": "This phrase is on NEVER CLAIM. Leave it out.",
    "unsourced_title_mention": (
        "Mention a person's or team's title only as a cited bullet words it, or the job's "
        "own title as the job sought."
    ),
    "claimed_job_title": (
        "The job's own title is the role sought, never the candidate's: name it only as "
        "'the <title> role at <company>' or 'your <title> role'."
    ),
    # employers
    "wrong_company": "Name no company except the company applied to and the candidate's employers.",
    "fabricated_employer": (
        "The company applied to is where the candidate wants to work, never where they "
        "work or worked. Do not write about it as 'we', 'our', 'your teams' from inside, or "
        "'<Company> teams partner…' in a sentence about the candidate; name it only as "
        "where the candidate wants to work ('I'd like to join <Company>', 'the <title> "
        "role at <Company>'), or in the one company-fact sentence."
    ),
    "misattributed_claim": "Attach each fact only to the employer its bullet is labeled with.",
    "cross_role_attribution": (
        "Each employer's results stand on their own: never let one employer's result "
        "explain or come from another's. A sentence using facts from two employers names "
        "both, or is split in two."
    ),
    # claims of experience
    "unsourced_experience_claim": (
        "A sentence about what the candidate did, has, knows or would bring uses only the "
        "words of the bullets it cites. Do not paraphrase a bullet into a stronger or more "
        "specific claim, and do not add a skill, a plan or a promise ('I'll come with "
        "sharp questions', 'I'll walk you through it'). Restate the cited bullet in its "
        "own words or cut the sentence."
    ),
    # the claim judge
    "claim_judge_unsupported": (
        "A reviewer who read the cited bullets found this claim in none of them. State "
        "it exactly as a cited bullet does, or cut it."
    ),
    "claim_judge_unparseable": (
        "The claim review could not read its own verdict on this draft. Write the letter "
        "again under the same rules."
    ),
    # authenticity (tailor._authenticity_failure)
    "authenticity": (
        "The letter reads as generic or machine-written here: a banned phrase, a stock "
        "construction or a uniform rhythm. Say it plainly in the candidate's voice."
    ),
    "authenticity_figures": (
        "A figure here is written as a flat assertion where its bullet is an estimate."
    ),
    "critic": "A reader's note on what reads as generic or untrue to the candidate's voice.",
}

GENERIC_RULE = "Fix exactly what the detail says, under the FACT RULES above."


def rule_for(kind: str) -> str:
    """The plain explanation of a violation kind (GENERIC_RULE for one with none)."""
    return RULES.get(kind, GENERIC_RULE)


# validate/claims.py and validate/claim_judge.py keep a violation's sentence to
# its first 160 characters: a sentence that long may have been cut there
SENTENCE_CUT = 160

NEW_LETTER = (
    "Write a new letter under the same rules. Your earlier drafts are not shown "
    "to you, only what failed in them, listed below. Every draft is checked from "
    "scratch, so each failure below must stay fixed, whichever draft it came from. "
    "Do not write any quoted sentence again, reworded or not. If it carried a fact "
    "from a cited bullet, restate that fact in the bullet's own words, or leave the "
    "point out; never put a new claim in its place. A problem tied to no sentence is "
    "fixed where it says (bullets_used, or the whole letter)."
)

Check = Literal["fact_check", "authenticity"]
FACT_CHECK: Final[Literal["fact_check"]] = "fact_check"
AUTHENTICITY: Final[Literal["authenticity"]] = "authenticity"

_FAILED: dict[str, str] = {
    FACT_CHECK: "FAILED THE FACT CHECK",
    AUTHENTICITY: "FAILED AUTHENTICITY REVIEW",
}
_SECTIONS = (
    (FACT_CHECK, "FACT CHECK FAILURES (the rules and the claim review)"),
    (
        AUTHENTICITY,
        "AUTHENTICITY FAILURES (the letter passed the fact check, then read as generic)",
    ),
)


@dataclass
class _Item:
    kind: str
    sentence: str
    detail: str
    drafts: list[int] = field(default_factory=list)


@dataclass
class _Draft:
    number: int
    check: Check
    cited: list[str]
    new: list[_Item] = field(default_factory=list)
    repeated: list[_Item] = field(default_factory=list)


def _sentence_line(sentence: str) -> str:
    if not sentence:
        return "   sentence: (not tied to one sentence)"
    quoted = json.dumps(sentence, ensure_ascii=False)
    if len(sentence) >= SENTENCE_CUT:
        return f"   sentence (its first {SENTENCE_CUT} characters; it may go on): {quoted} …"
    return f"   sentence: {quoted}"


class DraftHistory:
    """Every failed draft of one tailoring run, for the {feedback} of each
    next draft (render). Violations are de-duplicated by (check, kind,
    sentence, detail): one seen again is listed once, under the draft it
    first failed in, with every draft it failed in."""

    def __init__(self, company: str) -> None:
        self.company = company
        self._drafts: list[_Draft] = []
        self._items: dict[tuple[str, str, str, str], _Item] = {}

    def __len__(self) -> int:
        return len(self._drafts)

    def add(
        self,
        draft: int,
        check: Check,
        violations: Iterable[Mapping[str, object]],
        cited: Sequence[str] = (),
    ) -> None:
        """Record draft `draft` (1 for the first) as failed by `check`.
        `cited` is its bullets_used, shown only for a draft that passed the
        fact check (an authenticity failure): its ids, never its text."""
        entry = _Draft(draft, check, [str(c) for c in cited] if check == AUTHENTICITY else [])
        for v in violations:
            kind = str(v.get("kind") or "unknown")
            sentence = str(v.get("sentence") or "").strip()
            detail = str(v.get("detail") or "").strip()
            key = (check, kind, sentence, detail)
            item = self._items.get(key)
            if item is None:
                item = self._items[key] = _Item(kind, sentence, detail)
                entry.new.append(item)
            elif draft not in item.drafts:
                entry.repeated.append(item)
            if draft not in item.drafts:
                item.drafts.append(draft)
        self._drafts.append(entry)

    def render(self) -> str:
        """The {feedback} of the next draft: "" before any draft failed."""
        if not self._drafts:
            return ""
        numbers: dict[int, int] = {}  # id(item) -> its number in the list
        blocks: list[str] = []
        for check, title in _SECTIONS:
            drafts = [d for d in self._drafts if d.check == check]
            if not drafts:
                continue
            parts = [f"{title}:"]
            for d in drafts:
                parts.append(self._draft_header(d, numbers))
                for item in d.new:
                    numbers[id(item)] = len(numbers) + 1
                    parts.append(self._item_text(numbers[id(item)], item))
            blocks.append("\n\n".join(parts))
        n = len(self._drafts)
        return (
            f"\n\nYOUR EARLIER DRAFTS FAILED ({n} {'draft' if n == 1 else 'drafts'} so far). "
            + NEW_LETTER
            + "\n\n"
            + "\n\n".join(blocks)
            + "\n\nState only facts from the candidate facts above, with each number written "
            "as the FIGURE STYLE rule says, list every bullet id you drew on in bullets_used, "
            "attach each fact only to the employer its bullet is labeled with, and name no "
            f"company other than {self.company} and the candidate's own employers."
        )

    @staticmethod
    def _draft_header(d: _Draft, numbers: dict[int, int]) -> str:
        counts = f"{len(d.new)} new" if d.repeated else str(len(d.new))
        head = f"Draft {d.number} {_FAILED[d.check]} ({counts})"
        if d.repeated:
            again = ", ".join(str(numbers[id(i)]) for i in d.repeated)
            head += f", and again: {'item' if len(d.repeated) == 1 else 'items'} {again} above"
        if d.cited:
            head += (
                f". It cited {', '.join(d.cited)}, and those citations passed the fact "
                "check; you may cite the same bullets again"
            )
            return head + (". What failed:" if d.new else ".")
        return head + (":" if d.new else ".")

    @staticmethod
    def _item_text(n: int, item: _Item) -> str:
        lines = [f"{n}. {item.kind}", _sentence_line(item.sentence)]
        lines.append(f"   rule: {rule_for(item.kind)}")
        if item.detail:
            lines.append(f"   detail: {item.detail}")
        if len(item.drafts) > 1:
            lines.append(f"   failed in drafts: {', '.join(str(x) for x in item.drafts)}")
        return "\n".join(lines)
