"""Claims validator for free prose an employer reads: cover letters (tailor.py)
and AI-drafted answers to novel form questions (form_spec.py). Audit
2026-10-08 §2 item 3 — resumes went through provenance, but letters did not: a
letter could say the ~$5.6M in Haulmark bookings "came from usage-based
pricing for merchant accounts" (that is a separate $2.4M at ledgerline), a
drafted answer could invent "our VP of Sales asked me to…" (the only General
Manager in the corpus is one the candidate reported to), and a cached
letter written for one company could be sent to another.

Contract with the generator: the model sees only labeled sources (each bullet
with its employer, title and dates) and returns the ids it used. Then, per
claim unit (a sentence, joined with the next one when that one refers back to
it — "that revenue", "most of it"):

- every number, WITH its unit, must appear in a cited source. A sentence about
  the target may also use numbers from the job description — that is the
  company fact P1 is built on. Such a sentence has the target plainly as its
  subject: it opens with the target's name ("Fabrikam Payments doubled its
  payment volume in 2025") or, when it says nothing happened, with
  "you"/"your" or "the role" ("Your Account Associates book 35 walkthroughs a
  week"); never with a gerund, a pronoun or anything else ("Booking 35
  walkthroughs a week with a team like your Account Associates was the job" is
  about the candidate). It has no I/my/me/we/our at all, a reaction's
  included ("caught my eye", "matters to me": "…, which matters to me" ends
  a claim as easily), names none of the candidate's employers, points back
  at or compares with nothing ("there", "then", "too", "the same", "like",
  "that team", "they"), introduces no person and does not name the
  candidate ("a leader who…", "a closer booking…"), and promises nothing
  ("will", "would", "can") (fourth review of validator-refine). After its
  opening it restates the posting word for word — one run of words from one
  of the posting's sentences, in the posting's order — and has no word of
  someone's history ("previous", "experience", "used to"): "Contoso's 5x
  ticket volume within 120 days was standard at the last job" and "Contoso's 35
  walkthroughs a week was the job for years" are about the candidate, even when
  the posting has every one of their words. A comment on the fact goes in a
  sentence of its own, which borrows no JD number (fifth review of
  validator-refine). Observation
  verbs ("I saw", "I noticed") make it a candidate claim too: "At Haulmark I
  saw revenue double" (review of wp2). What this cannot see: a later
  sentence that hands the fact to the candidate without restating it
  ("Contoso's Account Associates book 35 walkthroughs a week. That was my job for
  years."), like any claim with no number in it (below). Durations are numbers
  too: a plan's window ("the first sixty days", "within 30 days") needs a
  cited source like any other number, in a letter and in an answer (third
  review of validator-refine);
- in every style a rate keeps its source's period ("$1.8M a year" never
  "$1.8M a month", "5–7 deals a month" never "5–7 deals";
  provenance.period_mismatch);
- under estimate_style "hedged", an estimate (or always-hedged) number keeps
  a hedge next to it; under "confident" an approximation mark may go, but a
  range, "+", "up to", "under", "over", "or more" stay as the source writes
  them and are never added (provenance.figure_mismatch;
  config/banned_phrases.yaml drift.estimate_style);
- every proper noun must appear in a cited source, the skill index, the
  candidate's own role titles, the target company or job title, or (in a
  sentence about the target, as above) the JD. A first-person sentence never
  borrows a JD word, not even for the target's own people ("I'll coach your
  Account Associates" needs a cited source for "Associates"): an allowance for
  that, guarded by a list of experience words, kept passing new phrasings of
  experience ("as their former peer", "I'll still coach…") and was removed
  (fourth review of validator-refine). A never-claim title's words are not
  excused by the job title in a sentence naming a candidate employer, unless
  that occurrence names the target's job ("the VP of Sales role at Oscorp"),
  and in a sentence that makes a claim about the candidate the job title is
  never claimed as held ("I was the Inside Sales Manager at Haulmark" —
  claimed_job_title; "I want to be an Inside Sales Manager" names the
  role sought, and "I haven't held the Inside Sales Manager title" is a
  disclaimer). A capital at the start of a sentence makes a proper noun only
  when the word is not a common one (validate/common_words.py) or it is a
  known name: "Success shows up later." passes beside a JD with
  "Customer Success", "Brightwave hired me…" fails, and so does "Oracle was
  one of my accounts…" beside a JD with "Oracle Cloud Infrastructure"
  (reviews 2026-10-10, _CAP_RUN). A standard contraction there ("Let's",
  "Don't", "We'll") is never a name (2026-10-15, _CONTRACTIONS);
- a candidate employer named in a sentence must own a cited source, and every
  fact the sentence pins to an employer (its numbers and proper nouns, the
  titles the candidate held there, plus vocabulary that occurs in only one
  employer's bullets, or one phrase like "usage-based pricing" that does) must belong
  to an employer the sentence names. Facts from two employers in one sentence
  need both employers named — that is the cross-role attribution check. A
  sentence that refers back to the previous one ("It came from…", "The revenue
  came from…", "…came from them") is checked together with it, and one
  employer's result credited to another's work ("came from", "drove",
  "behind") fails even with both employers named;
- a company that is neither the target, nor one of the candidate's employers,
  nor mentioned in the JD or the corpus is a HARD fail (wrong_company);
- a company the JD, corpus or skill index mentions is excused only as a tool
  or a client. Named as where the candidate worked ("Account Executive at
  Salesforce", "I spent my early career at Salesforce", "I worked at Gong") it
  is a HARD fail (fabricated_employer) unless it is an employer or a school.
  In a first-person sentence every "at <Name>" is such a cue, except "<client
  noun> at <Name>" ("accounts at Nakatomi") when a cited bullet or the JD
  names that client and it is not a skill_index tool (audit review wp2);
- the target is named only as where the candidate wants to work ("I'd love to
  join Contoso", "the role at Contoso"), never as a past or current employer:
  "I previously worked at Contoso", "Yes, I was at Contoso before.", "I've been
  with Contoso", "my time at Contoso", "a Contoso alum", "ex-Contoso" are a HARD
  fail (fabricated_employer) unless the corpus lists the target as a role
  company (target_tenure_cues; review 2026-10-09). The check fails closed
  (second review 2026-10-09): in a sentence with any I/me/my/we/our, any
  mention of the target, in any case ("contoso", "monday.com"), fails unless
  the sentence only aspires, names the job sought or the target's people,
  compares, or describes the target; a seat cue fails it in any sentence,
  also earlier in a list ("I worked at Haulmark, then Contoso"), as an employer
  label ("Current employer: Contoso") or a résumé line ("Account Executive,
  Contoso"); and "you"/"your" after a seat cue is the target ("I was on your
  sales team"). The third review of 2026-10-09 closed "you"/"your" the same
  way: in a sentence with any I/me/my/we/our, any "you"/"your" fails unless
  the sentence only aspires, or it is "thank you", the job sought ("I saw
  your posting") or the target's people met or sought ("I enjoyed meeting
  your team"): "I reported to your CEO", "I carried your quota", "I have run
  your weekly revenue meeting" fail. An aspiration or a description of the
  target has no "my" beyond a plan ("Contoso is on my resume"), no "me" as a
  verb's object ("Contoso will remember me") and no re-contact ("I'd love to
  reconnect with Contoso"), and a preposition governs the job sought only after
  a word of wanting or applying ("I was promoted to the role at Contoso"
  fails). A client a cited bullet names ("accounts at Nakatomi" to
  Nakatomi) is still a client. A drafted answer is checked with its
  question: to "Have you previously worked for Fabrikam Payments?" only a plain
  "No" passes (asks_target_tenure);
- constraints.never_claim_titles / never_claim_phrases / never_claim_tools hold
  (see provenance.never_claim_violations);
- a sales tool or method of the questions' vocabulary (pipeline/questions.py
  TOPICS: Clari, Outreach, MEDDPICC…) is named only when a skill_index key is
  it (questions.in_skill_index), or the sentence fails as unsourced_tool. A
  cited bullet naming it never sources it: a bullet can say someone else used
  it ("My manager ran Clari forecast calls at Haulmark"), and "At Haulmark I ran
  Clari forecast calls every week." citing that bullet passed (review
  2026-10-09, skill-answers). Who the sentence's subject is is not judged, so
  a sentence about the target ("Fabrikam Payments runs its forecast on Clari")
  fails too: an accepted false rejection. Only the target's own name, when
  the target IS the tool ("Aircall" in a letter to Aircall), may be named as the
  company applied to: the job sought, an aspiration, or the subject of a
  sentence about the company, never a tool the candidate used (2026-10-15,
  _tool_is_target), and only while no sentence of the text points back at
  its product ("it", "the product", "the dialer") in a claim about the
  candidate or the candidate's employers (_handoff_pointer), an aspiring
  sentence's own pointers included ("I'd love to join Aircall after using
  it"; _pointer_claims). A phrase that describes the candidate as the
  target's customer or user ("I'd love to join Wonka as one of its earliest
  customers", "…, a power user of it since 2022") makes any sentence more
  than an aspiration (_described_claim; fourth review of 2026-10-15);
- a first-person sentence that claims experience, a skill, credentials or a
  background uses only words of the bullets the text cites, their employer
  and title included (unsourced_experience_claim; sixth review of the
  self-review, 2026-10-09). It is marked by a form of "bring" or "I
  offer/contribute/lend", by a word such as experience, expertise,
  background, knowledge, track record, skills, credentials, licensed,
  certified, degree or years, by "I am a/an/the", by a verb of having after a
  modal or future "I"/"we" ("I'd have the trade chops", "I'll draw on…"),
  or by a "my"/"our" that is not a plan in a modal or future sentence ("My
  field service leadership would serve your team"). A modal does not make it a
  plan: "I would bring years of field service operations leadership to Fabrikam
  Payments.", "I can bring the trade credentials your team needs." and "I am
  a licensed electrician." fail unless a cited bullet has every word ("bring"
  and the like aside; "years", "expertise" and the target's name need a
  source too), and such a sentence is never one that claims nothing about
  the candidate (_no_candidate_claim; review 2026-10-09). The second review
  of 2026-10-09 added: knowing or understanding after a modal ("I'd know
  exactly how equipment financing works", "I'd come in knowing…"), needing no
  ramp ("on day one", "hit the ground running", "Nobody would need to teach
  me…"), ease, readiness, credibility or fit ("…is second nature to me", "I'd
  be productive immediately", "that's me"), an identity ("As a former
  electrician, I'd…", "…as a fellow tradesperson", "I'd be the electrician in the
  room"), a past claim or a non-forward "I" inside a modal clause ("I'd be
  the rare Director of Revenue Operations who has run a plumbing shop", "…, as I always have"), and,
  with no first person, a hire the target would make ("You'd be hiring a
  licensed electrician", "Fabrikam Payments would get a seasoned … leader"). The
  third review of 2026-10-09 added: possession brought along on arrival ("I'd
  walk in with the trade chops…", "I'd show up with…", "armed with"),
  familiarity ("Equipment financing won't be new to me", "None of this would be
  new to me", "I've seen all of this before"), an ability ("I can navigate
  building-code compliance", "I could teach anyone trade licensing"; "I can help"
  and "I can start" are offers) and a skill verb after a modal ("I'd speak
  the technicians' language", "I'll train your reps…"); and a claim that names
  nothing a cited bullet could source besides its own frame and markers ("I
  can do this", "None of this would be new to me" beside a bullet with
  "new-business") fails whatever is cited. These are lists: other wording of
  the same claim is the blind spot below. One verb needs no source of its
  own: a neutral change verb (climbed, grew, rose, increased, improved, went
  from) whose clause restates a move a cited bullet states: the move's own
  words as its subject, all of them and nothing else but a leading "the",
  "our" or "its" ("AI renewal rate" is not "renewal rate"), its
  figures, and nothing after them before the next punctuation mark ("…, and
  renewal rate climbed from 44% to 71%"; reviews 2026-10-10,
  _CHANGE_VERB).

Fail closed: any violation means the letter or answer is not used as final.
What this cannot see: any other claim with no number, no proper noun and no
employer-specific vocabulary ("I managed SDRs", "I also led the sales
development team."), including one whose every word comes from its cited
bullets though none of them says it ("I ran the merchant team for ~18 months.",
"I owned the entire company revenue."). Word coverage cannot see those, and
a rule that held every first-person sentence to its cited bullets' words
would also fail honest sentences ("I coach deals on MEDDIC"). The LLM claim
judge (validate/claim_judge.py, package 4) is the gate for those: it reads
every letter and drafted answer this check passes, and can only fail one;
the authenticity critic and the human reviewing the staged form stand behind
it (tests/test_claims.py BLIND_SPOT). Because of that blind spot, a
self-review never revises a letter or answer that passed this check, and a
failing draft's review never steers its regeneration
(pipeline/self_review.py)."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from jobkit.pipeline.questions import SKILL_KINDS, TOPICS, Topic, in_skill_index
from jobkit.validate import style as estimate_styles
from jobkit.validate.common_words import is_common_word
from jobkit.validate.provenance import (
    _AFTER_GAP,
    _BEFORE_GAP,
    _CLAUSE_END,
    _NUMWORD,
    _PERIOD_MARK,
    _PROPER_ALLOW,
    _TRANSITION,
    NumClaim,
    ProvenanceResult,
    _classify_title,
    _corpus_vocab,
    _period_marks,
    _target_linked,
    _walk_text,
    claim_keys,
    constraints,
    figure_mismatch,
    mask_periods,
    never_claim_violations,
    numeric_claims,
    period_mismatch,
    proper_tokens,
    skill_index_keys,
    states_period,
    target_job_spans,
    text_periods,
    text_vocab,
)

_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# a violation of one of these kinds is never a matter of rewording
HARD_KINDS = frozenset(
    {"wrong_company", "fabricated_employer", "never_claim_title", "never_claim_phrase"}
)

_LETTER_ALLOW = {
    "Dear",
    "Hiring",
    "Team",
    "Sincerely",
    "Best",
    "Regards",
    "Thanks",
    "Thank",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    # generic posting vocabulary, not facts about the candidate
    "JD",
    "OKR",
    "OKRs",
    "KPI",
    "KPIs",
    "ROI",
}


# function words that are capitalized only because they start a sentence
def _words(s: str) -> frozenset[str]:
    return frozenset(s.split())


_FUNCTION = _words(
    """a an and as at but by for from had has have he her his how i if in into is it its
    me my no not of on or our she so than that the their them then there these they this
    those to too us was we were what when where which while who why will with would you
    your yes after before since until because although though once also still even just
    most much many more less few some any each every both all one two many here now today
    over under during across through without within about again already always never often
    sometimes"""
)

_FIRST_PERSON = re.compile(
    r"\bI\b|\bI['’](?:m|ve|d|ll)\b|\b(?:[Mm]e|[Mm]y|[Mm]ine|[Mm]yself|[Ww]e|[Oo]ur|[Oo]urs|us)\b"
)
# First-person phrases that report a reaction or an intent, not experience:
# "X caught my attention", "I'd like to talk". They don't make the sentence a
# claim about the candidate (the job-title and employer-cue checks set them
# aside), but they still keep the JD's own facts out of it: "Your Account
# Associates caught my eye as a former peer" (fifth review of
# validator-refine; see jd_sources).
# Observation verbs ("I saw", "I noticed", "I read", "I think", "I expect") are
# NOT on this list: "At Haulmark I saw revenue double" is a claim about the
# candidate's past, and it borrowed the JD's "doubled" (review of wp2). Neither
# is a bare "to me" ("the book went to me"), nor "means a lot to me" or
# "exciting to me": "…, which means a lot to me" closes a claim as easily as a
# reaction (third review of validator-refine).
_PERCEPTION = re.compile(
    r"\b(?:caught|got|grabbed|holds?|held|has) my (?:attention|interest|eye)\b"
    r"|\b(?:tells?|told|shows?|showed|suggests?) me\b"
    r"|\b(?:stood|stands?) out to me\b"
    r"|\b(?:matters?|appeals?|speaks?|resonates?|resonated) to me\b"
    r"|\bI(?:['’]d| would) (?:like|love|want) to\b|\bI(?:['’]m| am) (?:drawn|curious|interested)\b"
    r"|\bI(?:['’]d| would) (?:like|love|welcome)\b|\blet(?:['’]s| us)\b",
    re.I,
)
# A sentence may borrow the JD's numbers and names only when the target is
# plainly its subject (fourth review of validator-refine: "Booking 35 walkthroughs
# a week with a team like your Account Associates was the job" credited the JD's
# "35" to the candidate, with or without a reaction after it). It opens with
# the target's name ("Fabrikam Payments doubled its payment volume in 2025") or,
# when it says nothing happened, with "you"/"your" or "the role" ("Your Account
# Associates book 35 walkthroughs a week", "The role works with…"). "Your team
# doubled…" is an accepted false rejection. A gerund, a pronoun or anything
# else in front never qualifies: "Growing ticket volume 5x within 120 days, as your
# team does, was the job there". Nor does a sentence holding any of the
# patterns below, after its opening.
_TARGET_OPENING = re.compile(
    r"(?:you|your|yours)\b"
    r"|(?:the|this)\s+(?:role|position|job|posting|listing|job\s+description|JD|opportunity)\b",
    re.I,
)
# …a pointer back or a comparison, which can hand the JD's fact to the
# candidate's team in an earlier sentence: "Contoso's 5x ticket volume within 120 days
# was the norm there", "…, as the old team did", "…, like the team before",
# "…, and they did too" (and the _ANAPHORA phrases: "that team", "the same
# pipeline")
_JD_POINTER = re.compile(
    r"\b(?:there|here|then|too|also|again|likewise|similar|similarly|same|like|unlike"
    r"|as\s+well|he|she|him|his|her|hers|they|them|their|theirs)\b"
    r"|\bas\s+(?:[\w'’-]+\s+){0,4}?(?:do|does|did|done|had|has|have|was|were)\b",
    re.I,
)
# …a person it introduces, who may be the candidate: "Contoso gets a leader
# who books 35 walkthroughs a week", "…a closer that books…", "…a closer booking…",
# "…a closer with…" (and the candidate's own name: see jd_sources). Nouns that
# are also verbs ("leads", "heads", "coaches", "partners with") are left out.
_JD_PERSON = re.compile(
    r"\b(?:who|whom|whose)\b"
    r"|\b(?:leader|operator|candidate|person|someone|somebody|builder|pro|veteran|mentor"
    r"|salesperson|rep|seller|closer|manager|executive|director|representative|associate"
    r"|specialist|consultant)s?\s+(?:that|with|[a-z]+ing)\b",
    re.I,
)
# …a promise: a JD fact is the target's present or past, never what the
# candidate will deliver ("Contoso's ticket volume will grow 5x within 120 days" is
# "I'll grow your ticket volume 5x" without the "I")
_JD_PROMISE = re.compile(
    r"\b(?:will|would|shall|should|can|could|might|must|cannot|gonna|(?-i:may))\b"
    r"|\bgoing\s+to\b|['’](?:ll|d)\b|\b(?:won|wouldn|shouldn|couldn|can|mustn)['’]t\b",
    re.I,
)
# The patterns above are lists, and a list of what recasts a fact keeps
# missing new wording: "Contoso's 5x ticket volume within 120 days was standard at the
# last job", "Your Account Associates book 35 walkthroughs a week, an old habit"
# (fifth review of validator-refine). A set of allowed words is no better: a
# real posting has "was", "job", "for" and "years", and "Contoso's 35 walkthroughs
# a week was the job for years" is made of them. So the sentence restates the
# posting word for word: after its opening it is one run of words from one
# sentence of the posting, in the posting's order (case, punctuation and a
# plural "s" aside; _jd_tokens). "Fabrikam Payments doubled its payment volume
# in 2025" restates "…and it doubled its payment volume in 2025…";
# a comment on the fact goes in the next sentence, which borrows no JD number.
# A run of the posting's words is still checked against the lists above and
# this one: a posting's requirements have words of someone's history
# ("previous sales leadership experience", "a proven track record"), and they
# never ride along with the JD's facts: "Contoso's ideal candidate has
# previous experience booking 35 walkthroughs a week"
_JD_HISTORY = re.compile(
    r"\b(?:former(?:ly)?|previous(?:ly)?|prior|ex|experience[ds]?|background|career|tenure"
    r"|track\s+record|first-?hand|long-?time|familiar|habit|used\s+to|routine|alumn\w*)\b",
    re.I,
)
# Letters may hedge in more words than a resume bullet; there was no check on
# letters before, so a wider list relaxes nothing.
_LETTER_HEDGE = re.compile(
    r"(?:~|≈|<|>|\b(?:roughly|about|approximately|approx\.?|around|nearly|almost|near|close to"
    r"|some|more than|less than|fewer than|at least|over|under|up to|upwards of|north of"
    r"|just over|just under))\s*$",
    re.I,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
# a sentence that refers back to the previous one's facts is checked together
# with it: "…to ~$5.6M. Most of that revenue came from usage-based pricing…"
_ANAPHORA = re.compile(
    r"\b(?:that|this|those|these|the same)\s+(?:revenue|book|number|numbers|figure|figures"
    r"|growth|money|result|results|business|pipeline|deal|deals|sales|run-rate|metric|metrics"
    r"|program|team|org|work|success)\b"
    r"|\b(?:most|much|all|some|part|half)\s+of\s+(?:it|that|this|those|them)\b",
    re.I,
)
# Weaker back-references (review of wp2): a sentence that opens with a pronoun
# or a definite noun phrase ("It came from…", "The revenue came from…", "The
# engine behind it…") or that ends on a pronoun object ("…came from them") is
# linked to the previous sentence for the employer-attribution checks only, so
# that "At Haulmark I grew annual bookings to ~$5.6M. It came from usage-based
# pricing for merchant accounts." is read as one claim. Numbers, names and
# first-person rules stay per sentence.
_BACKREF_START = re.compile(
    r"^(?:(?:and|but|so|yet|still)\s+)?(?:it|its|they|their|them|this|that|these|those|such"
    r"|the\s+[a-z][\w-]*)\b",
    re.I,
)
_PRONOUN = r"(?:it|them|that|this|those|these|they)"
_BACKREF_OBJECT = re.compile(
    rf"\b(?:from|behind|by|of|on|via|through|into|with|drove|drives?|driving|powered|powers"
    rf"|fuel(?:ed|led|s)?|built|generated|produced|enabled)\s+{_PRONOUN}\b",
    re.I,
)
# Phrases that make one fact the cause or source of another. Facts from two
# employers on either side of one of these are a cross-role claim even when
# both employers are named: "The ~$5.6M in Haulmark bookings came from
# usage-based pricing I launched at ledgerline."
_CAUSAL = re.compile(
    r"\b(?:c(?:ame|ome|omes|oming)\s+(?:from|out\s+of|via|through)|driven\s+by"
    r"|dr(?:ove|ives|ive|iving)|powered(?:\s+by)?|powers|fuel(?:ed|led|s)?(?:\s+by)?|behind"
    r"|thanks\s+to|because\s+of|(?:the\s+)?results?\s+of|resulted\s+from|due\s+to"
    r"|generated\s+by|built\s+on|on\s+the\s+back\s+of|credit(?:ed)?\s+to|attributable\s+to"
    r"|sourced\s+from|stemm(?:ed|ing|s)\s+from|produced\s+by|grew\s+out\s+of|owed\s+to"
    r"|rooted\s+in|enabled\s+by|made\s+possible\s+by)\b",
    re.I,
)
_SIDE_BACKREF = re.compile(rf"\b{_PRONOUN}\b|^\s*the\s+[a-z]", re.I)
_SEGMENT = re.compile(r"(?<=[.!?])\s+|\s*;\s*|\n+")

# generic words that say nothing about WHICH employer a fact belongs to
_COMMON = _words(
    """about above across after again against also along among around because been before
    being below between both came come comes could does doing done down during each even
    ever every from have having here into itself just last less like made make makes making
    many more most much must next only onto other over same should since some such than that
    their them then there these they this those through together under until upon very were
    what when where which while will with within without would your year month week daily
    weekly monthly annual annually first second third time still well back full part whole
    entire early later long short high higher large larger small smaller best better least
    real right work worked working build built building lead leading help helped helping grow
    grew growing growth owned owning team people company organization business role sale
    sales seller revenue customer deal number result program process product strategy
    strategic core used using based level leader leadership manage managed management
    managing member staff group plan goal target new-business including delivering delivered
    ran running across repeating"""
)


@dataclass(frozen=True)
class Source:
    """One citable fact source: a corpus bullet or an education entry."""

    id: str
    text: str  # every citable word: text_long, text_short, metric
    employer: str | None  # the role's company; None for education
    title: str
    dates: str
    confidence: str
    years: frozenset[int]


def _fmt_ym(ym: object) -> str:
    if not ym:
        return "Present"
    y, m = str(ym).split("-")[:2]
    return f"{_MONTHS[int(m)]} {y}"


def _year(ym: object) -> int | None:
    m = re.match(r"(\d{4})", str(ym or ""))
    return int(m.group(1)) if m else None


def source_registry(corpus: dict[str, Any]) -> dict[str, Source]:
    roles = {r["id"]: r for r in corpus.get("roles", [])}
    out: dict[str, Source] = {}
    for b in corpus.get("bullets", []):
        role = roles.get(b.get("role_id"), {})
        years = {y for y in (_year(role.get("start")), _year(role.get("end"))) if y}
        out[str(b["id"])] = Source(
            id=str(b["id"]),
            text="\n".join(
                str(b.get(k) or "").strip() for k in ("text_long", "text_short", "metric")
            ),
            employer=str(role["company"]) if role.get("company") else None,
            title=str(role.get("title") or ""),
            dates=f"{_fmt_ym(role.get('start'))} – {_fmt_ym(role.get('end'))}" if role else "",
            confidence=str(b.get("metric_confidence") or "none"),
            years=frozenset(years),
        )
    for i, e in enumerate(corpus.get("education", []) or [], start=1):
        text = " ".join(str(e.get(k) or "") for k in ("school", "degree", "years"))
        out[f"edu_{i}"] = Source(
            id=f"edu_{i}",
            text=text,
            employer=None,
            title=str(e.get("degree") or ""),
            dates=str(e.get("years") or ""),
            confidence="verified",
            years=frozenset(int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", text)),
        )
    return out


def labeled_sources_for_prompt(
    corpus: dict[str, Any], ids: Sequence[str] | None = None, *, include_education: bool = False
) -> str:
    """The ONLY candidate facts a generator may use, each labeled with its
    employer, title and dates so a fact can't drift to another role."""
    reg = source_registry(corpus)
    order = list(ids) if ids is not None else [k for k in reg if not k.startswith("edu_")]
    if include_education:
        order += [k for k in reg if k.startswith("edu_") and k not in order]
    bullets = {str(b["id"]): b for b in corpus.get("bullets", [])}
    lines: list[str] = []
    for sid in order:
        src = reg.get(sid)
        if src is None:
            continue
        if src.employer is None:
            lines.append(f"- {sid} | Education | {src.text.strip()}")
            continue
        b = bullets[sid]
        lines.append(
            f"- {sid} | {src.employer} · {src.title} · {src.dates} | "
            f"metric_confidence: {src.confidence}\n"
            f"  long: {str(b.get('text_long') or '').strip()}\n"
            f"  short: {str(b.get('text_short') or '').strip()}"
            + (f"\n  metric: {b['metric']}" if b.get("metric") else "")
        )
    return "\n".join(lines)


def drafting_profile(corpus: dict[str, Any]) -> str:
    """The profile novel-question drafting sees: every bullet and education
    entry with its citable id and its employer · title · dates label, the
    confirmed skills, and the never-claim lists (audit 2026-10-08: the old
    profile had bullet text only, so facts drifted between employers)."""
    skills = skill_index_keys(corpus)
    return (
        labeled_sources_for_prompt(corpus, None, include_education=True)
        + f"\nCONFIRMED SKILLS: {', '.join(skills) if skills else '(none)'}"
        + f"\nNEVER CLAIM (hard constraint): {never_claim_for_prompt(corpus)}"
    )


def never_claim_for_prompt(corpus: dict[str, Any]) -> str:
    cons = constraints(corpus)
    return (
        f"titles {cons['never_claim_titles']}, tools {cons['never_claim_tools']}, "
        f"phrases {cons['never_claim_phrases']}"
    )


def employers(corpus: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for r in corpus.get("roles", []):
        c = str(r.get("company") or "").strip()
        if c and c not in seen:
            seen.append(c)
    return seen


def _name_patterns(name: str) -> list[re.Pattern[str]]:
    """Full name (any case) plus, for a multi-word name, its first word when
    it is capitalized and 4+ letters ("Marlow", "Fabrikam")."""
    words = name.split()
    pats = [re.compile(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b", re.I)]
    if len(words) > 1 and len(words[0]) >= 4 and words[0][0].isupper():
        pats.append(re.compile(rf"\b{re.escape(words[0])}\b"))
    return pats


def _names_in(text: str, name: str) -> bool:
    return any(p.search(text) for p in _name_patterns(name))


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


def _jd_tokens(text: str) -> list[str]:
    """Words and numbers, lowercased, a plural "s" dropped, for comparing a
    sentence with the posting it restates ("Contoso expects 5x ticket volume"
    restates "we expect 5x ticket volume")."""
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())]


def _content_terms(text: str) -> set[str]:
    return {
        t
        for t in (_stem(w) for w in re.findall(r"[a-z]+", text.lower()))
        if len(t) >= 4 and t not in _COMMON
    }


# ---- stemmed content words: the experience-claim check (validate_claims) and
# the self-review's change screen (validate/revision.py) ----

# irregular past forms → the base form, so "led" matches "leads" and "leading"
_IRREGULAR: dict[str, str] = {
    "led": "lead",
    "ran": "run",
    "grew": "grow",
    "grown": "grow",
    "built": "build",
    "rebuilt": "rebuild",
    "sold": "sell",
    "outsold": "outsell",
    "won": "win",
    "drove": "drive",
    "driven": "drive",
    "took": "take",
    "taken": "take",
    "made": "make",
    "brought": "bring",
    "taught": "teach",
    "held": "hold",
    "kept": "keep",
    "met": "meet",
    "began": "begin",
    "begun": "begin",
    "became": "become",
    "wrote": "write",
    "written": "write",
    "spoke": "speak",
    "spoken": "speak",
    "found": "find",
    "gave": "give",
    "given": "give",
    "got": "get",
    "gotten": "get",
    "went": "go",
    "gone": "go",
    "saw": "see",
    "seen": "see",
    "oversaw": "oversee",
    "overseen": "oversee",
    "knew": "know",
    "known": "know",
    "shown": "show",
    "spent": "spend",
    "sent": "send",
    "paid": "pay",
    "said": "say",
    "told": "tell",
    "thought": "think",
    "sought": "seek",
    "fought": "fight",
    "bought": "buy",
    "caught": "catch",
    "lost": "lose",
    "felt": "feel",
    "stood": "stand",
    "understood": "understand",
    "rose": "rise",
    "risen": "rise",
    "chose": "choose",
    "chosen": "choose",
    "broke": "break",
    "broken": "break",
    "drew": "draw",
    "drawn": "draw",
}

# words that carry no claim: function words, auxiliaries, quantifiers, number
# words (numbers have their own check) and month names (dates do too)
_TERM_STOP = (
    _FUNCTION
    | _words(
        """am are be been being do does did doing done is was were has have had having can
        could should shall may might must not nor yet also just then there here very really
        quite which who whom whose whether either neither own same such per via upon onto
        among toward towards between against around including ago previously formerly
        currently ever once twice again already still even only more most less least much
        many few several other another each every any some all none plus etc"""
    )
    | _words(
        """one two three four five six seven eight nine ten eleven twelve thirteen fourteen
        fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy
        eighty ninety hundred thousand million billion dozen half"""
    )
    | _words(
        """january february march april may june july august september october november
        december jan feb mar apr jun jul aug sep sept oct nov dec"""
    )
)


def _root(w: str) -> str:
    """A light stem: irregular past → base, then one of -ies/-ied → -y, -ing,
    -ed (a doubled final consonant undone: "planned" → "plan"), -es, -s, then
    a final -e ("manage", "managed", "managing" → "manag"). So "managed"
    matches "managing" but not "managers", and "development" does not match
    "developed"."""
    w = _IRREGULAR.get(w, w)
    if len(w) > 4 and w.endswith(("ies", "ied")):
        return w[:-3] + "y"
    for suf in ("ing", "ed", "es", "s"):
        if not w.endswith(suf) or len(w) - len(suf) < 3:
            continue
        if suf == "s" and w.endswith(("ss", "us", "is")):
            break
        w = w[: -len(suf)]
        if suf in ("ing", "ed") and w[-1] == w[-2] and w[-1] not in "lsz":
            w = w[:-1]
        break
    if len(w) >= 4 and w.endswith("e"):
        w = w[:-1]
    return w


def _surface(text: str, keep: frozenset[str] = frozenset()) -> dict[str, str]:
    """root → the first word in `text` that has it, for every content word
    (three letters or more, not in _TERM_STOP unless in `keep`)."""
    out: dict[str, str] = {}
    low = re.sub(r"n['’]t\b", "", text.lower())  # "don't" → "do": no stray "don"
    for w in re.findall(r"[a-z]+", low):
        if len(w) < 3 or (w in _TERM_STOP and w not in keep):
            continue
        out.setdefault(_root(w), w)
    return out


def terms(text: str, keep: frozenset[str] = frozenset()) -> set[str]:
    """The content words of `text`, stemmed (_root); `keep` lists _TERM_STOP
    words that count as content words here."""
    return set(_surface(text, keep))


def _distinctive_terms(corpus: dict[str, Any]) -> dict[str, str]:
    """term → the ONE employer whose bullets use it (whole corpus)."""
    reg = source_registry(corpus)
    seen: dict[str, set[str]] = {}
    for src in reg.values():
        if src.employer is None:
            continue
        for t in _content_terms(src.text):
            seen.setdefault(t, set()).add(src.employer)
    return {t: next(iter(e)) for t, e in seen.items() if len(e) == 1}


def _phrase_bigrams(text: str) -> set[tuple[str, str]]:
    """Adjacent content-word pairs within a phrase ("BI dashboards" → ("bi",
    "dashboard")). Short words count here ("bi"); generic ones do not."""
    out: set[tuple[str, str]] = set()
    for chunk in re.split(r"[^\w\s'’-]+", text.lower()):
        words = [_stem(w) for w in re.findall(r"[a-z]+", chunk)]
        for a, b in zip(words, words[1:], strict=False):
            if len(a) < 2 or len(b) < 2 or {a, b} & (_FUNCTION | _COMMON):
                continue
            out.add((a, b))
    return out


def _distinctive_bigrams(corpus: dict[str, Any]) -> dict[tuple[str, str], str]:
    """bigram → the ONE employer whose bullets use it. One such phrase is
    enough evidence of that employer's fact: "usage-based pricing" in a sentence about
    the Haulmark bookings is ledgerline's work (review of wp2)."""
    seen: dict[tuple[str, str], set[str]] = {}
    for src in source_registry(corpus).values():
        if src.employer is None:
            continue
        for bg in _phrase_bigrams(src.text):
            seen.setdefault(bg, set()).add(src.employer)
    return {bg: next(iter(e)) for bg, e in seen.items() if len(e) == 1}


def _units(text: str) -> list[str]:
    units: list[str] = []
    for s in (x.strip() for x in _SENTENCE.split(text)):
        if not s:
            continue
        if units and _ANAPHORA.search(s):
            units[-1] = units[-1] + " " + s
        else:
            units.append(s)
    return units


def _proper_parts(unit: str) -> list[tuple[str, int]]:
    """Proper-noun tokens, with hyphenated compounds split into their
    capitalized parts ("Ashgrove's-caliber" → "Ashgrove", "AI-native" → "AI")."""
    out: list[tuple[str, int]] = []
    for word, pos in proper_tokens(unit):
        offset = 0
        for part in word.split("-"):
            clean = re.sub(r"['’]s?$", "", part)
            if len(clean) >= 2 and clean[0].isupper():
                out.append((clean, pos + offset))
            offset += len(part) + 1
    return out


def _sentence_initial(unit: str, pos: int) -> bool:
    """True when the token at `pos` starts a sentence (or a list item / quote),
    i.e. it may be capitalized only for position."""
    prev = unit[:pos].rstrip()
    return prev == "" or prev[-1] in ".!?:;\"“‘'(—–-•*\n"


def _strict_initial(text: str, pos: int) -> bool:
    prev = text[:pos].rstrip()
    return prev == "" or prev[-1] in ".!?\n"


def _capitalized_elsewhere(texts: Iterable[str]) -> set[str]:
    """Lowercased words that appear capitalized somewhere OTHER than the start
    of a sentence — evidence a sentence-initial capital is a real name."""
    out: set[str] = set()
    for t in texts:
        for m in re.finditer(r"(?<![\w'’])[A-Z][A-Za-z0-9&'’-]*", t):
            if not _strict_initial(t, m.start()):
                for part in m.group().split("-"):
                    out.add(re.sub(r"['’]s?$", "", part).lower())
    return out


# ---- a capital at the start of a sentence (review 2026-10-10) ----
# It is a name only when the word is not a common one (validate/common_words.py,
# the bundled list and nothing else: a lowercase spelling in the corpus or the
# JD does not make a word common) or it is a known name:
# the target, a candidate employer or school, a skill_index key, a never-claim
# tool, a tool or method of questions.TOPICS, a company of the DB (any of these
# matched whole: "Union College", not "union dues"), a name the JD or
# the corpus uses (_prose_names), or any word the text itself capitalizes past
# the start of a sentence. Letters were rejected for
# "Success shows up later." (the JD has "Customer Success"), "People budget
# against it, …" (Lumon's JD has "People teams") and "Pick any afternoon
# …": until then a capital anywhere in the corpus
# or the JD made the word a name. A word that is not common is now a name
# even with no capital elsewhere ("Brightwave hired me to rebuild its sales
# team." passed before and fails unsourced_proper_noun now). The head of a
# title-cased run in the JD or the corpus is a name as before ("Oracle" of
# "Oracle Cloud Infrastructure", "Apple" of "Apple Pay Later", "Customer" of
# "Customer Success"), and so is a lone capital the source also writes in
# lowercase ("Slack" of "async in Slack and keep slack in the schedule"):
# second review 2026-10-10, where "Oracle was one of my enterprise accounts
# at Keyward Security." had passed. Only two shapes are set aside: a word after
# the head of a run of common words ("Success" of "Customer Success", "Pay"
# of "Apple Pay"), and a department-style capital, one the source also writes
# in lowercase and that a lowercase team/org/function/department follows
# ("People" of "People teams"). What this cannot see: a name that is also a
# common word and is known nowhere ("Lattice hired me…"), as before, nor one
# the JD and the corpus write only in those two shapes, or only at the start
# of a sentence, when the letter's sentence starts with that word alone ("Pay
# opened with me…" beside "Apple Pay"). Those still need a DB company or a
# capital elsewhere in the text to be caught.
_CAP_RUN = re.compile(
    r"(?<![\w'’])[A-Z][A-Za-z0-9&'’-]*(?:[^\S\n]+(?:(?:of|&)[^\S\n]+)?[A-Z][A-Za-z0-9&'’-]*)*"
)
# a lowercase noun of a department after a capital: "People teams"
_DEPARTMENT_AFTER = re.compile(
    r"[^\S\n]+(?:team|teams|org|orgs|function|functions|department|departments)\b"
)


def _plain_word(word: str) -> bool:
    """A common word written with one leading capital at most ("Success",
    not "AI" or "HubSpot")."""
    return not re.search(r"[A-Z0-9]", word[1:]) and is_common_word(word)


# ---- a contraction at the start of a sentence (2026-10-15) ----
# "Let's set up a conversation." failed as an unsourced proper noun "Let"
# beside a posting that writes "✨ Let’s make…", where a
# capital after an emoji or a bullet is no sentence start to _strict_initial,
# so "let" became one of the JD's names (_prose_names). "Don’t" and "You’ll"
# failed the same way beside a posting's "• Don’t tick every box?" and
# "• You’ll partner with…". A standard English contraction ("let's", "we'll",
# "don't", "here's") is a function word with a clitic: capitalized at the start
# of a sentence it is position, never a name, whatever the JD or the corpus
# capitalizes. The list is closed: a possessive or a clitic on any other word is
# judged as before, so "Brightwave hired me.", "Brightwave's team hired me." and,
# beside a JD with "Oracle Cloud Infrastructure", "Oracle's team was one of my
# accounts." and "Oracle'd be my first call." still fail. What this cannot see
# is unchanged: "Let me know…" (no clitic) still fails beside that posting.
_CONTRACTIONS = _words(
    """let's that's it's here's there's what's who's where's when's how's why's he's she's
    you'd we'd they'd he'd she'd it'd that'd there'd who'd what'd
    you'll we'll they'll he'll she'll it'll that'll there'll who'll what'll
    you're we're they're who're what're
    you've we've they've who've could've would've should've might've must've
    don't doesn't didn't isn't aren't wasn't weren't haven't hasn't hadn't can't couldn't
    won't wouldn't shouldn't mustn't needn't shan't"""
)
_CONTRACTION_TOKEN = re.compile(r"[A-Za-z]+['’][A-Za-z]+(?![\w'’-])")


def _initial_contraction(unit: str, pos: int) -> bool:
    """True when the token at unit[pos] is a standard contraction (see
    _CONTRACTIONS), in either apostrophe: "Let's", "Don’t", "We'll"."""
    m = _CONTRACTION_TOKEN.match(unit, pos)
    return m is not None and m.group().lower().replace("’", "'") in _CONTRACTIONS


def _prose_names(texts: Iterable[str], lowercase: Collection[str]) -> tuple[set[str], set[str]]:
    """The names `texts` (the JD, the corpus) use: (words, phrases). A word,
    lowercased, is one capitalized past the start of a sentence, unless it
    follows the head of a title-cased run of common words or it is a
    department-style capital (see _CAP_RUN): "Notion" in "tools like Notion
    and Slack" is a name, and so are "Oracle" in "Oracle Cloud
    Infrastructure" and "Customer" in "Customer Success"; "Success" there is
    not, nor is Lumon's "People teams" (its JD also writes "people",
    `lowercase`). A run of common words is also a phrase (_norm_name), a name
    as a whole: "Customer Success reported to me" starts with one, "Success
    shows up later." does not. A word that is not common needs none
    of this: it is a name wherever it stands (see above)."""
    words: set[str] = set()
    phrases: set[str] = set()
    for t in texts:
        for run in _CAP_RUN.finditer(t):
            parts = [
                (re.sub(r"['’]s?$", "", p.group()), run.start() + p.start(), run.start() + p.end())
                for p in re.finditer(r"[A-Za-z0-9&'’]+", run.group())
                if p.group()[0].isupper()
            ]
            plain_run = len(parts) >= 2 and all(_plain_word(w) for w, _, _ in parts)
            if plain_run:
                phrases.add(_norm_name(run.group()))
            for i, (word, pos, end) in enumerate(parts):
                low = word.lower()
                if plain_run and i > 0:
                    continue  # "Success" of "Customer Success"
                if _strict_initial(t, pos):
                    continue  # capitalized for its position
                if low in lowercase and _DEPARTMENT_AFTER.match(t, end):
                    continue  # "People teams" beside "people"
                words.add(low)
    return words, phrases


def _strings(x: Any) -> Iterable[str]:
    if isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)
    elif x is not None:
        yield str(x)


def _norm_name(s: str) -> str:
    s = re.sub(r"['’]s\b", "", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _contains_name(haystack_norm: str, key: str) -> bool:
    return f" {key} " in f" {haystack_norm} "


def _company_mentions(
    unit: str, known: dict[str, str], excluded_norm: str, ordinary: set[str]
) -> list[str]:
    """Known company names (DB companies table) named in the unit and not
    excused by the target/employers/JD/corpus text in `excluded_norm`."""
    toks = [(m.group(), m.start()) for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9&.'’+-]*", unit)]
    found: list[str] = []
    i = 0
    while i < len(toks):
        tok, pos = toks[i]
        if not tok[0].isupper():
            i += 1
            continue
        hit = 0
        for n in (4, 3, 2, 1):
            if i + n > len(toks):
                continue
            key = _norm_name(" ".join(t for t, _ in toks[i : i + n]))
            if not key or key not in known:
                continue
            hit = n
            sentence_initial = n == 1 and _sentence_initial(unit, pos)
            if _contains_name(excluded_norm, key):
                break
            if sentence_initial and tok.lower().rstrip(".,") in ordinary:
                break  # "Pilot programs ran…" is the word, not the company
            found.append(known[key])
            break
        i += max(hit, 1)
    return found


# ---------------------------------------------------------------------------
# employment cues (audit review wp2)
# ---------------------------------------------------------------------------
# A company the JD, the corpus or the skill index mentions is excused above
# only because it may be a tool ("I live in Salesforce") or a client ("accounts
# at Nakatomi"). Named as where the candidate WORKED — "I was an Account
# Executive at Salesforce", "I spent my early career at Salesforce", "I worked
# at Gong" — it is a fabricated employer, a HARD fail.

_NAME_TOKEN = r"(?!I\b|I['’])[A-Z][A-Za-z0-9&'’+-]*(?:\.[A-Za-z0-9]+)*"
_NAME_RE = re.compile(rf"{_NAME_TOKEN}(?:\s+(?:of\s+|&\s+)?{_NAME_TOKEN})*")
_NAME_SEP = re.compile(r"\s*,\s*(?:(?:and|or)\s+)?|\s+(?:and|or|&)\s+")
_CAP_AHEAD = r"(?=(?!I\b|I['’])[A-Z])"
# in a first-person sentence, every "at <Name>" says where the candidate was
_AT_CUE = re.compile(r"\b(?i:at)\s+" + _CAP_AHEAD)
# explicit employment wording
_EMPLOY_CUE = re.compile(
    r"\b(?i:work(?:ed|ing|s)?\s+(?:at|for)|employed\s+(?:at|by)|hired\s+(?:at|by|into)"
    r"|recruited\s+(?:to|by|into)|join(?:ed|ing|s)?|promoted\s+at"
    r"|(?:career|stint|tenure)\s+(?:at|with)|(?:was|were|been|am|i['’]m)\s+(?:at|with)"
    r"|(?:sold|selling|sell|sells|closing|closed|prospecting|prospected|interned|interning"
    r"|consulted|consulting|repped|carried\s+(?:a\s+)?(?:bag|quota)"
    r"|carrying\s+(?:a\s+)?(?:bag|quota))\s+for)\s+" + _CAP_AHEAD
)
_CLAUSE_SPLIT = re.compile(r"[.;:!?\n(—–]")
# words that may precede a subjectless, résumé-style employment line
# ("Previously worked at X", "Formerly Account Executive at X")
_LEAD_ADVERBS = _words(
    "previously formerly before that then earlier also and most recently currently"
)
_ROLE_WORDS = _words(
    """rep ae sdr bdr representative seller closer manager executive director lead founder
    cofounder co-founder intern consultant specialist associate analyst engineer officer
    chief head vp svp evp cro ceo coo cto president partner staff"""
)
_TITLE_FILLER = _words("of and &")
_DETERMINERS = _words("the a an this that these those their your its his her our my")
# a tool or client named attributively is not an employer: "joined Gong calls"
_ATTRIBUTIVE = _words(
    """call calls recording recordings report reports dashboard dashboards data sequence
    sequences cadence cadences instance admin workflow workflows note notes field fields
    object objects flow flows review reviews library libraries snippet snippets integration
    integrations certification certifications certified training trainings license licenses
    seat seats rollout migration implementation deployment stack setup hygiene adoption
    forecast forecasts forecasting view views alert alerts scorecard scorecards account
    accounts user users deal deals contract contracts renewal renewals pilot pilots session
    sessions demo demos meeting meetings"""
)
# "<client noun> at X" names a client, not an employer: "accounts at Nakatomi"
_CLIENT_NOUNS = _words(
    """account accounts buyer buyers stakeholder stakeholders contact contacts executives
    execs leaders leadership champion champions people customer customers client clients
    users decision-makers prospect prospects meeting meetings deal deals opportunity
    opportunities logo logos office offices site sites procurement folks partners cfo cfos
    cro cros ceo ceos cio cios vp vps director directors head heads committee contract
    contracts renewal renewals expansion expansions pilot pilots rollout rollouts program
    programs someone everyone anyone"""
)
# …and so does a team-like noun, unless it is the candidate's own ("my team at X")
_TEAM_NOUNS = _words(
    "team teams department departments group groups division divisions unit units org orgs"
)
# "I was a buyer at X", "As an account executive at X" — a self-description
_COPULA_NP = re.compile(
    r"\b(?:was|am|is|are|were|been|be|became|become|as|i['’]m)\s+"
    r"(?:(?:an?|the|my|our)\s+)?(?:(?!(?:to|with|for|from|into|in|on|of)\b)[\w-]+\s+){0,2}"
    r"[\w-]+\s+$",
    re.I,
)
_GENERIC_ORG = _words("Series Fortune Big Four Inc Seed Startup Startups")


def _cue_names(unit: str, pos: int, allow_comma: bool) -> tuple[list[str], int]:
    """The company name(s) starting at `pos`: one name, or a list closed by
    "and"/"or" ("at Nakatomi, Ashgrove and Pemberton Mills"). Returns (names, end)."""
    m = _NAME_RE.match(unit, pos)
    if not m:
        return [], pos
    names, end = [m.group()], m.end()
    pending: list[str] = []
    while (s := _NAME_SEP.match(unit, end)) and (n := _NAME_RE.match(unit, s.end())):
        if "," in s.group() and not re.search(r"\b(?:and|or)\b|&", s.group()):
            if not allow_comma:
                break
            pending.append(n.group())
        else:
            names += [*pending, n.group()]
            pending = []
        end = n.end()
    return [x.rstrip(".'’-") for x in names], end


def _generic_name(name: str) -> bool:
    """True when every capitalized part is generic vocabulary ("SaaS",
    "Series B", "AI-native", "Fortune"), i.e. no company is named."""
    parts = [re.sub(r"['’]s?$", "", p) for tok in name.split() for p in tok.split("-")]
    return all(
        p in _PROPER_ALLOW
        or p in _LETTER_ALLOW
        or p in _GENERIC_ORG
        or p.lower() in _FUNCTION
        or len(p) < 2
        or re.fullmatch(r"[QH]\d", p) is not None
        for p in parts
        if p and p[0].isupper()
    )


def _title_line(clause: str) -> bool:
    """A subjectless, résumé-style title before "at": "Account Executive",
    "Previously Head of Sales", "Top rep"."""
    words = clause.split()
    while words and words[0].lower().rstrip(",") in _LEAD_ADVERBS:
        words = words[1:]
    if not words or len(words) > 7 or words[0].lower() in _DETERMINERS:
        return False
    low = [w.lower().rstrip(",") for w in words]
    ok = all(
        w[0].isupper() or lw in _ROLE_WORDS or lw in _TITLE_FILLER
        for w, lw in zip(words, low, strict=True)
    )
    return ok and any(lw in _ROLE_WORDS for lw in low)


def _client_context(before: str) -> bool:
    """True when the words before "at" name the client side ("accounts at",
    "the procurement team at") rather than the candidate's seat."""
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", before[-80:])
    if not words:
        return False
    noun, prev = words[-1].lower(), {w.lower() for w in words[-4:-1]}
    if noun in _TEAM_NOUNS:
        if prev & {"my", "our"}:
            return False
    elif noun not in _CLIENT_NOUNS:
        return False
    head = before[: before.lower().rfind(noun)]
    return not _COPULA_NP.search(head + noun + " ")


@dataclass(frozen=True)
class _Cue:
    """A company named as where the candidate worked (_employment_cues)."""

    cue: str  # the cue's own text ("worked at", "at", "join")
    name: str
    client_ok: bool  # a bare "at" after a client noun ("accounts at Nakatomi")
    start: int  # where the cue starts in the unit
    at: bool  # a bare "at" (_AT_CUE), not explicit employment wording
    title_line: bool  # a subjectless résumé line ("Account Executive at X")


def _employment_cues(unit: str, first_person: bool) -> list[_Cue]:
    """Each company named as where the candidate worked. `client_ok` is True
    for a bare "at" after a client noun: that one is excused when the name is
    a sourced client, not a tool. `first_person` is the reaction-filtered
    flag; explicit employment wording counts in any first-person sentence
    ("I'd love to join X" included)."""
    hits: dict[int, tuple[str, list[str], int, bool, int, bool, bool]] = {}
    any_first_person = bool(_FIRST_PERSON.search(unit))
    for m in _EMPLOY_CUE.finditer(unit):
        clause = _CLAUSE_SPLIT.split(unit[: m.start()])[-1]
        subjectless = all(w.lower().rstrip(",") in _LEAD_ADVERBS for w in clause.split())
        if any_first_person or subjectless:
            names, end = _cue_names(unit, m.end(), True)
            hits[m.end()] = (m.group().strip(), names, end, False, m.start(), False, False)
    for m in _AT_CUE.finditer(unit):
        if m.end() in hits:
            continue
        before = unit[: m.start()]
        clause = _CLAUSE_SPLIT.split(before)[-1]
        initial = not clause.strip()
        title_line = False
        if first_person:
            client_ok = _client_context(before)
        elif _title_line(clause):
            client_ok, title_line = False, True
        else:
            continue
        names, end = _cue_names(unit, m.end(), not initial)
        hits[m.end()] = (m.group().strip(), names, end, client_ok, m.start(), True, title_line)
    out: list[_Cue] = []
    for cue, names, end, client_ok, start, at, title_line in hits.values():
        if not names:
            continue
        nxt = re.match(r"\s+([a-z][a-z-]*)", unit[end:])
        if nxt and nxt.group(1) in _ATTRIBUTIVE and not re.search(r"['’]s?$", names[-1]):
            continue  # "joined Gong calls": the tool, not the company
        out += [
            _Cue(cue, n, client_ok, start, at, title_line)
            for n in names
            if n and not _generic_name(n)
        ]
    return out


# ---------------------------------------------------------------------------
# sentences that make no claim about the candidate
# ---------------------------------------------------------------------------
# A letter sentence such as "For your Account Associates, that means notes on
# every recorded call, not only the calls that fit around my meetings." is
# rejected on "Associates", the JD's own noun for Contoso's people. That rejection
# stands. Two allowances were tried and removed because neither could be made
# safe simply:
# - a plan's window ("the first sixty days", "within 30 days") passed
#   unsourced results, tenure and availability ("I'll never forget hitting
#   quota within the first 30 days"); every duration needs a cited source, as
#   every number does (third review of validator-refine);
# - a JD word naming the target's own people in a forward-looking first-person
#   sentence ("I'll coach your Account Associates") was guarded by a list of
#   experience words, and new phrasings kept passing: "I'll coach your Account
#   Associates as their former peer", "…as a longtime closer", "I'll continue
#   to coach your Account Associates", "I'll still coach your Account Associates"
#   each claim a seat or an activity the corpus does not state (fourth review
#   of validator-refine). A first-person sentence borrows no JD word, as at
#   aca26cc.
#
# What is left decides only where the job's own title may be checked as a
# claimed seat (claimed_job_title, which aca26cc did not have), and which
# sentences validate/revision.py checks. A sentence makes no claim about the
# candidate when it names no candidate employer, makes no past-tense claim,
# every "I"/"we" in it takes a future, modal or stance verb ("I'll", "I'd", "I
# can", "I think"), and every "my"/"our" is a plan ("my plan", "my first 90
# days", whose number still needs a source) or closes a phrase ("around my
# meetings"): "my reps ramp", "our team coaches", "every rep on my
# team books" and "I'd say my average deal" are claims (review of
# validator-refine). "I'd" before a verb whose past participle is its base
# form ("run", "come", "become", "set", "put") may be "I had", so it is read
# as a claim. "me" after a preposition ("the Account Associates under me",
# "reporting to me") and any "myself" make the sentence about the candidate
# just as "my" does (second review of validator-refine).

# past-tense forms that are not also present-tense ones (the ambiguous ones —
# "cut", "set", "hit", "put" — are _AMBIGUOUS_TENSE below)
_PAST_FORMS = _words(
    """was were had did been led ran built grew sold made took won drove saw became began got
    kept held spent met taught found left went came brought bought caught told said rose
    wrote spoke chose broke gave knew drew stood sent felt meant sat overcame oversaw
    rebuilt outsold outgrew fell shrank shrunk paid lost sank sunk struck swung threw flew
    fought sought slid sped hung rode shook wore tore froze stole swore rang sang sprang swam
    shot slept swept wept dealt lent bent fled heard laid understood withdrew undertook
    overtook outran outdid forgot forgave dug spun stuck strove arose awoke leapt knelt"""
)
# participles that follow have/has/had ("I've seen", "has grown")
_PERFECT = re.compile(
    r"\b(?:have|has|had|having|['’]ve)\s+(?:(?:[a-z]+ly|also|already|just|never|always|since"
    r"|both|all)\s+)*(?:been|seen|done|grown|driven|taken|written|given|known|shown|gone|run"
    r"|won|led|built|made|sold|begun|spoken|chosen|broken|risen|come|become|met|kept|held"
    r"|spent|got|gotten|taught|brought|found|hit|cut|set|put|beaten|[a-z]+ed)\b",
    re.I,
)
_PAST_ADVERB = re.compile(
    r"\b(?:ago|previously|formerly|historically|used\s+to|to\s+date|so\s+far|back\s+then"
    r"|at\s+the\s+time|in\s+the\s+past|last\s+(?:year|quarter|month|week)"
    r"|(?:my|our)\s+(?:last|previous|prior|former|old)\s+(?:role|job|team|company|employer"
    r"|seat|position|org))\b",
    re.I,
)
# "-ed" words that are not verbs at all
_NOT_PAST_ED = _words(
    "hundred kindred sacred naked wicked rugged ragged crooked beloved embed shred jagged"
)
# before an "-ed" word, these make it an adjective or a present passive
# ("your dedicated team", "with motivated reps", "I'm excited", "are measured")
_ADJ_CONTEXT = _words(
    """a an the your their its his her our my these those every each some any more most less
    very highly well fully newly recently with for of to into from among on in at about
    without across through toward towards under over like than am is are be being become
    becomes get gets getting stay stays remain remains feel feels seem seems look looks
    i'm we're you're they're it's that's"""
)
_SKIPPABLE_ADVERB = _words("also just already recently still never always then really")


def _tokens(unit: str) -> list[tuple[str, int]]:
    return [(m.group().lower(), m.start()) for m in re.finditer(r"[A-Za-z]+(?:['’][a-z]+)?", unit)]


def _word_before(toks: list[tuple[str, int]], i: int) -> str:
    """The word before token i, adverbs skipped ("I'll also cut" → "i'll")."""
    j = i - 1
    while j >= 0 and (toks[j][0] in _SKIPPABLE_ADVERB or toks[j][0].endswith("ly")):
        j -= 1
    return toks[j][0].replace("’", "'") if j >= 0 else ""


def _past_claim(unit: str) -> bool:
    """True when the sentence says something happened: a past-tense verb, a
    perfect ("I've led", "has grown"), or a past adverb ("previously",
    "last year"). Fail closed: an adjective that looks like a past tense
    ("built to scale") only withdraws the allowance, it fails nothing."""
    if _PERFECT.search(unit) or _PAST_ADVERB.search(unit):
        return True
    toks = _tokens(unit)
    for i, (w, pos) in enumerate(toks):
        if (pos and unit[pos - 1] == "-") or unit[pos + len(w) : pos + len(w) + 1] == "-":
            continue  # "purpose-built", "AI-led", "data-driven"
        if w in _PAST_FORMS:
            if w in ("was", "were", "had", "did", "been"):
                return True
        elif not (len(w) >= 5 and w.endswith("ed")) or w in _NOT_PAST_ED:
            continue
        elif w.endswith("eed") and w not in ("agreed", "guaranteed", "freed"):
            continue  # "speed", "proceed", "exceed", "succeed", "indeed"
        if _word_before(toks, i) in _ADJ_CONTEXT:
            continue
        return True
    return False


_FP_SUBJECT = re.compile(r"\b(?:I|[Ww]e)(?:['’](?P<c>ll|d|m|ve|re))?(?![\w'’])")
_MODALS = _words("will would can could should might may must shall")
# present-tense first-person verbs that state an intent or a view, not a fact
_STANCE = _words("want plan intend aim hope expect look think believe imagine suspect bet")
# what may follow "I'm"/"I am"/"we're": intent or reaction, not a present activity
_FORWARD_STATE = _words(
    """going ready excited eager keen planning hoping looking happy glad confident sure
    convinced curious interested drawn applying prepared committed aiming intending here"""
)
# tense-ambiguous verbs: past unless a modal, "to" or "'ll" governs them, or a
# determiner makes them a noun ("a cut", "the set")
_AMBIGUOUS_TENSE = _words(
    "hit cut set put beat quit shut spread cost hurt read split upset burst cast let shed"
)
# after "I'd", a past participle makes it "I had": "I'd coached", "I'd grown",
# and — fail closed — a participle that is also the base form ("I'd run the sales
# floor alone", "I'd become the go-to coach", "I'd set up"), which may be either
_PARTICIPLE_AFTER_D = _AMBIGUOUS_TENSE | _words(
    """been seen done grown driven taken written given known shown gone led built made sold
    had run come become overcome outrun overrun begun undergone swum won met kept held spent
    got gotten brought bought caught taught thought sought found fought felt heard left lost
    paid said sent told sat stood understood rebuilt outgrown overseen overtaken undertaken
    ridden risen spoken chosen broken beaten forgotten forgiven drawn flown thrown worn torn
    sworn frozen stolen rung sung sprung sunk struck stuck spun dug hung slid fled dealt lent
    bent laid slept swept wept fed bred bound wound shot sped striven arisen awoken leapt
    knelt withdrawn outdone outsold"""
)
# a first-person possessive: "my reps ramp", "our team coaches", "…of mine"
_POSS_FP = re.compile(r"\b(?i:my|our|mine|ours)\b")
# …that names a plan, not something the candidate has: "my plan", "my 30-60-90
# day plan", "my first priority", "in my first 90 days", "our first quarter"
_POSS_PLAN = re.compile(
    rf"(?i:my|our)\s+(?:(?:[\w-]+\s+){{0,3}}?(?:plan|plans|goal|goals|aim|aims|priority"
    rf"|priorities|focus|intent|intention|roadmap)\b"
    rf"|first\s+(?:\d|(?:{_NUMWORD})\b|(?:day|week|month|quarter)s?\b))",
)
_PREPOSITIONS = _words(
    """with of for to from in on at by into onto about like than without within across
    through over under toward towards behind beside among around instead"""
)
# what may close a prepositional phrase: "…the calls that fit around my meetings."
_PHRASE_CLOSE = re.compile(r"\s*(?:$|[.,;:!?)\]—–]|(?:and|but|or|so|yet|I|we)\b)")
# "me" after a preposition, and any "myself", point at the candidate the way
# "my" does: "the Account Associates under me", "reporting to me", "as they do
# for me", "onboard myself". Unlike "around my meetings" no phrase-closing
# excuse applies: what sits under, with or for the candidate is the
# candidate's. Reactions ("matters to me") are set aside before this runs.
_ME_PREPOSITIONS = _PREPOSITIONS | _words(
    """below beneath alongside after before near past beyond against via upon besides except
    including unlike per"""
)
_CANDIDATE_OBJECT = re.compile(
    r"\b(?i:myself)\b"
    rf"|\b(?i:{'|'.join(sorted(_ME_PREPOSITIONS))})\s+(?i:me)\b"
)


def _possessive_claim(unit: str) -> bool:
    """True when "my"/"our"/"mine"/"ours" makes the sentence about something
    the candidate has or does: "My reps ramp…", "Our team coaches…", "every
    Account Associate on my team books…", "I'd say my average deal…", "I'll
    bring my onboarding program". Excused: a plan ("my plan", "my first 90
    days") and a one-word object closing a phrase ("around my meetings")."""
    for m in _POSS_FP.finditer(unit):
        if m.group().lower() in ("mine", "ours"):
            return True
        if _POSS_PLAN.match(unit, m.start()):
            continue
        before = re.findall(r"[A-Za-z]+", unit[: m.start()])
        obj = re.match(r"\s+(?:own\s+)?[A-Za-z][\w'’-]*", unit[m.end() :])
        if (
            before
            and before[-1].lower() in _PREPOSITIONS
            and obj
            and _PHRASE_CLOSE.match(unit, m.end() + obj.end())
        ):
            continue
        return True
    return False


def _fp_forward(unit: str) -> bool:
    """True when every first-person subject ("I", "we") in the sentence takes
    a future, modal or stance verb: "I'll coach", "I'd build", "the calls I
    can sit in on", "I think", and no "my"/"our" (_possessive_claim) or "me"
    after a preposition or "myself" (_CANDIDATE_OBJECT) makes it about what the
    candidate has. "I coach", "I coached", "I've coached", "I'm coaching",
    "I'd coached", "I'd run the sales floor", "my team coaches" and "the Account
    Associates reporting to me" are claims about the candidate."""
    if _possessive_claim(unit) or _CANDIDATE_OBJECT.search(unit):
        return False
    return _fp_subjects_forward(unit)


def _fp_subjects_forward(unit: str) -> bool:
    """True when every first-person subject ("I", "we") in `unit` takes a
    future, modal or stance verb (_fp_forward without its "my"/"me" checks)."""
    for m in _FP_SUBJECT.finditer(unit):
        c = (m.group("c") or "").lower()
        if c == "ll":
            continue
        if c == "ve":
            return False
        words = [w.lower() for w in re.findall(r"[A-Za-z]+", unit[m.end() : m.end() + 80])]
        while words and (words[0] in _SKIPPABLE_ADVERB or words[0].endswith("ly")):
            words = words[1:]
        w = words[0] if words else ""
        if c == "d":
            if w in _PARTICIPLE_AFTER_D or (w.endswith("ed") and len(w) >= 5):
                return False  # "I'd coached" is "I had coached"
            continue
        if c in ("m", "re"):
            if w not in _FORWARD_STATE:
                return False
            continue
        if not w or w in _MODALS or w in _STANCE:
            continue
        if w in ("am", "are") and len(words) > 1 and words[1] in _FORWARD_STATE:
            continue
        return False
    return True


def _no_candidate_claim(unit: str, named: set[str]) -> bool:
    """True when the sentence makes no claim about the candidate (see
    "sentences that make no claim about the candidate" above): it names no
    candidate employer (`named`), makes no past claim, every "I"/"we" takes a
    future, modal or stance verb, no "my"/"our" goes beyond a plan, and no
    "me" follows a preposition. A reaction is not a past claim: "your posting
    caught my eye". A claim of experience, credentials, years, background,
    knowledge or a skill is a claim in any frame (_experience_claim): "I would
    bring years of field service operations leadership" is what the candidate has,
    not a plan (review 2026-10-09)."""
    if _experience_claim(unit) or _hire_claim(unit):
        return False
    spoken = _PERCEPTION.sub(" ", unit)
    return not named and not _past_claim(spoken) and _fp_forward(spoken)


# ---------------------------------------------------------------------------
# the target named as the candidate's employer (review 2026-10-09; second
# review 2026-10-09)
# ---------------------------------------------------------------------------
# The company applied to may be named as where the candidate WANTS to work
# ("I'd love to join Contoso", "the Account Associate role at Contoso"), never as a
# past or current employer. The fabricated-employer check used to excuse the
# target like one of the candidate's own employers, so "I previously worked at
# Contoso", "Yes, I was at Contoso before.", "I've been with Contoso", "I have
# worked for Contoso" and "I've been employed by Contoso" passed. Unless the
# corpus lists the target as a role company, the target named as where the
# candidate worked or works is a HARD fail (fabricated_employer).
#
# A list of cues kept missing new wording (second review): "I worked at
# Haulmark, then Contoso", "I'd love to stay at Contoso", "Contoso gave me my
# first sales job", "I was Contoso's first AE", "I led Contoso's enterprise
# team", "I came from Contoso", "Current employer: Contoso", "I previously
# worked at contoso" and "I was on your sales team" all passed. So the check
# fails closed. The target's name, in any case, is judged in every sentence:
# - a cue that only a past or current seat has fails it always (_TENURE_BEFORE,
#   _TENURE_AFTER): "worked at/for/with", "was/were/been/am at|with",
#   "employed by", "spent three years at", "joined", "left", "came (over)
#   from", "returned to", "back at", "stay/remain/continue at", "come home
#   to", "while at", "ex-Contoso", "former Contoso AE", "previously of", "alum
#   of", "a Contoso alum", "Contoso hired me", "Contoso gave me", "Contoso and I
#   go back"; such a cue earlier in a list that runs on to the name ("worked
#   at Haulmark, then Contoso", "at places like Contoso", "at both Haulmark and
#   Contoso"); a seat after a past or present copula ("I was Contoso's first
#   AE", "I'm an early Contoso hire", "I served as Contoso's Inside Sales
#   Manager"); an employer label ("Current employer: Contoso", "Background:
#   Contoso", "References: Contoso"); a résumé line ("Account Executive,
#   Contoso", "Contoso | Account Executive", "Contoso, 2019–2021"); a bare
#   answer ("Yes, Contoso.", "Yes: at Contoso.");
# - in a sentence with any I/me/my/we/our/us (a present-tense reaction such
#   as "matters to me" or "caught my eye" aside), any other mention fails
#   unless the whole sentence only aspires (_aspires): it has a forward word
#   ("would", "I'd", "I'll", "want", "excited", "top choice"…), no past
#   claim, every "I"/"we" takes a future, modal or stance verb, no "my"/"our"
#   beyond a plan, and no word of tenure ("again", "back", "before", "still",
#   "stay", "continue", "home"…). "I'd love to join Contoso" passes; "I'd love
#   to rejoin Contoso", "At Contoso I ran the SDR team", "I led Contoso's
#   enterprise team", "Contoso taught me enterprise sales" and "I work at
#   Contoso" fail. Two mentions are excused in any such sentence: the job
#   sought or the target's own people ("I saw the role at Contoso", "…prepared
#   me for the role at Contoso", "to meet the team at Contoso", "I saw the
#   Contoso posting"), unless "my"/"our" owns that noun or the rest of its
#   clause is a claim ("the role at Contoso that I held"); and a present
#   description of the target whose first person is only inside a comparison
#   with it or as a present attitude ("Fabrikam is larger than any business
#   I have worked in, and I would want to learn how contractors buy", "Contoso is
#   solving a problem I care about"); a comparison or an example ("a stack
#   like Contoso", "a team Contoso's size"); and a client a cited bullet names
#   ("accounts at Nakatomi");
# - in a sentence with no first person, the cues of _employment_cues, a time
#   or tenure noun ("years at Contoso taught…"), "<target>, where…" and a
#   résumé tagline ("Contoso AE turned sales leader") are judged the same way;
#   a sentence about the target that restates the JD has none of them;
# - "you"/"your"/"yours" stand for the target after a seat cue: "I was on your
#   sales team", "I was one of your AEs", "I'm one of your current reps", "I
#   previously sold your platform", "I was your Inside Sales Manager", "I
#   led your enterprise team", "Your company was my home" fail; "I'd love to
#   be on your team" aspires and passes. In a sentence with any first person
#   they fail closed as the target's name does (third review 2026-10-09: "I
#   reported to your CEO", "I carried your quota", "I miss your sales floor",
#   "Your sales playbook is the one I wrote", "I have run your weekly revenue
#   meeting" passed with no seat cue): any other "you"/"your" there fails
#   unless the sentence only aspires, or it is "thank you", the job sought ("I
#   saw your posting", "the role with you") or the target's people after a
#   word of meeting or seeking ("I enjoyed meeting your team", "For your
#   reps, I'd…") (_you_excused). "…, which is what your team needs" after a
#   cited fact is an accepted false rejection: the comment goes in a sentence
#   of its own ("Your team needs that."), which has no first person and is
#   not judged;
# - an aspiration has no "my"/"our" beyond a plan or "my top choice" (_owns:
#   "You'll see Contoso on my resume"), no "me"/"us" as a verb's object
#   (_object_me: "Contoso will remember me") and no re-contact ("reconnect",
#   "remember me": _TENURE_WORD), and a description of the target has none of
#   them either ("Contoso is on my resume"); a present reaction with a word of
#   tenure is an old one ("Contoso still matters to me"), and a past "showed
#   me" is no reaction ("…the team at Contoso who showed me the ropes") (third
#   review 2026-10-09);
# - a preposition governs the job sought or the target's people only when it
#   opens the clause or follows a word of wanting or applying (_governed): "I
#   was promoted to the role at Contoso" and "I reported to the team at Contoso"
#   fail, "I'm excited about the role at Contoso" and "…prepared me for the role
#   at Contoso" pass (third review 2026-10-09).
# A lowercase spelling counts ("I previously worked at contoso", "monday.com",
# "hims & hers"), except that a target whose lowercase name is an ordinary
# word of the corpus or the JD ("pilot" for Pilot: "a small pilot") is
# judged in lowercase only after a seat cue ("I previously worked at pilot"),
# and so is a lowercase first word of a longer name ("fabrikam").
# Fail closed: "I led 9 AEs at Haulmark and I'd love to join Contoso" fails on
# its past tense, "I'd love to build my career at Contoso" on its "my", "The
# team at Contoso impressed me" on its past tense, "What drew me to Contoso is
# its mission" on its past tense, "I'm a former pilot" (applying to Pilot)
# on its cue, and "No, I have never worked at Contoso" on its cue: a negation
# excuses nothing ("I never left Contoso" says the candidate is still there).
# Accepted false rejections; the human answers "Have you worked for us
# before?" (a drafted answer to that question passes only as a plain "No":
# validate_claims `question`).
# What this cannot see: a later sentence that points back at the target
# without naming it ("I'd love to join Contoso. I worked there before."), like
# any claim with no number, name or cue (the module docstring).

_TARGET_ADVERBS = (
    r"(?:(?:also|still|once|then|already|currently|previously|formerly|originally|there"
    r"|first|[a-z]+ly)\s+)*"
)
# a cue that only a past or current seat has (anchored before the name by
# _TENURE_BEFORE; found anywhere by _TENURE_ANY for a list that runs on to it)
_TENURE_CUE = (
    r"(?:"
    r"\b(?:ex|former|formerly|previous|previously|prior|onetime|one-time|erstwhile|longtime"
    r"|long-time)(?:[\s,-]+[\w'’&.-]+){0,3}?[\s,-]+"
    r"|\b(?:worked|employed|hired|recruited|promoted|interned|consulted|contracted|freelanced"
    r"|served|stayed|started|began|begun|was|were|been|am|i['’]m|sold|repped|closed|prospected"
    r"|stay|stays|staying|remain|remains|remained|remaining|continue|continues|continued"
    r"|continuing|(?:keep|keeps|kept|keeping|continue|continues|continued|continuing)"
    r"\s+(?:to\s+)?work(?:ing)?|carried\s+(?:a\s+)?(?:bag|quota)|spent(?:\s+[\w'’~-]+){0,5}?)\s+"
    + _TARGET_ADVERBS
    + r"(?:at|for|with|by|inside|within|in|into|on|on\s+behalf\s+of|(?:a\s+)?part\s+of"
    r"|(?:a\s+)?member\s+of|one\s+of)\s+"
    r"|\b(?:joined|re-?join(?:ed|ing|s)?|return(?:ed|ing|s)?\s+to"
    r"|(?:went|go|goes|going|come|comes|coming|came|get|getting|be|being)\s+back\s+(?:to|at|with|in)"
    r"|(?:went|go|goes|going|come|comes|coming|came|get|getting|be|being|head|heading)"
    r"\s+(?:back\s+)?home\s+(?:to|at|with)"
    r"|back\s+(?:at|with|in|to)|left|leaving|quit|quitting|departed|departing|exited"
    r"|c(?:ame|ome|omes|oming)\s+(?:over\s+|here\s+|across\s+)?from"
    r"|mov(?:ed|ing)\s+(?:over\s+|here\s+)?from|transitioned\s+from|jumped\s+(?:ship\s+)?from"
    r"|poached\s+from|recruited\s+(?:away\s+)?from"
    r"|represented|representing)\s+"
    r"|\balum(?:nus|na|ni|nae|s)?\s+(?:of\s+)?"
    r"|\b(?:while|whilst|when)\s+(?:at|with|in)\s+"
    r"|\breferences?\s+(?:from|at|with)\s+"
    r"|\b(?:experience|background|history|career|resume|résumé|cv|employers)\s+(?:includes?"
    r"|included|including|spans?|spanned|covers?|covered|features?|lists?)\s+"
    r")"
)
_TENURE_BEFORE = re.compile(_TENURE_CUE + r"(?:the\s+)?$", re.I)
_TENURE_ANY = re.compile(_TENURE_CUE, re.I)
# a "former …" cue never runs across a comma into a first-person clause: "As a
# former electrician, I'd understand your customers" names no seat at "your"
_CROSSES = re.compile(r",\s*(?:I|we)\b|,\s*I['’]")
# what may sit between such a cue and the target in a list: other names and
# list words ("worked at Haulmark, then Contoso", "at companies like Contoso")
_LIST_WORDS = _words(
    """both either neither and or nor then later also plus as well like such including
    after before that eventually finally most recently briefly the a an companies company
    places firms startups organizations orgs teams shops brands employers names among
    others other two three several few various"""
)
# …or right after it: "a Contoso alum", "Contoso-alum", "Contoso hired me",
# "Contoso gave me my first sales job", "Contoso was my first employer",
# "Contoso is my employer", "Contoso and I go way back"
_TENURE_AFTER = re.compile(
    r"^[\s-]+(?:alum(?:nus|na|ni|nae|s)?|veteran|vet|old-timer|lifer|tenure|stint|boomerang)\b"
    r"|^(?:['’]s)?\s+(?:[\w'’-]+\s+){0,2}?(?:employed|hired|rehired|promoted|paid|fired|onboarded"
    r"|trained|recruited|laid\s+off|gave|made|taught|mentored|coached|named|offered|relocated"
    r"|transferred|kept|poached|sponsored|brought\s+on|took\s+on)\s+(?:me|us)\b"
    r"|^\s+(?:was|were|has\s+been|had\s+been|became|used\s+to\s+be)\s+(?:my|our)\b"
    r"|^\s+(?:is|remains)\s+(?:my|our)\s+(?:(?!dream|next|future|ideal|top)[\w-]+\s+){0,2}?"
    r"(?:employer|workplace|company|job|team|org|home)\b"
    r"|^\s+and\s+(?:I|me|we)\s+(?:go|went|have\s+gone|have\s+(?:a\s+)?history"
    r"|share\s+(?:a\s+)?history|worked|have\s+worked)\b",
    re.I,
)
# a seat after a past or present copula: "I was Contoso's first AE", "I was an
# early Contoso hire", "I'm one of your current reps", "I've been your
# Inside Sales Manager", "I served as Contoso's …" ("I'd be a great Contoso
# rep" is no such copula; "excited about", "interested in" are no seat)
_COPULA_SEAT_BEFORE = re.compile(
    r"\b(?:(?:I|we)\s+(?:was|were|am|are)|I['’]m|we['’]re"
    r"|(?:I|we)(?:['’](?:ve|d)|\s+(?:have|had))\s+(?:(?:\w+ly|always|already|also|just|once)\s+)*"
    r"been|(?:I|we)\s+used\s+to\s+be"
    r"|(?:I|we)\s+(?:(?:\w+ly|also|once|then|first)\s+)*(?:became|served\s+as|worked\s+as"
    r"|started\s+(?:out\s+)?as|began\s+as|joined\s+as|signed\s+on\s+as|came\s+on\s+as"
    r"|stayed\s+on\s+as))"
    r"\s+(?:(?:\w+ly|also|once|still|already|then)\s+)*"
    r"(?:(?:an?|the|one\s+of)\s+(?:(?!(?:about|for|to|with|of|in|on|at|by|from|into|toward"
    r"|towards|like|than|as)\b)(?![\w-]+(?:ed|ing)\b)[\w-]+\s+){0,2}?)?$",
    re.I,
)
_SEAT_WORDS = _ROLE_WORDS | _words(
    """employee employees staffer staffers hire hires insider insiders teammate teammates
    colleague colleagues member members alum alumni alumnus alumna reps aes sdrs bdrs
    managers executives directors leads associates"""
)
_SEAT_WORD_AFTER = re.compile(
    r"^(?:['’]s)?\s+(?!(?:next|future|potential|prospective|incoming|ideal|new)\b)"
    r"(?:(?!(?:needs?|has|have|is|are|was|were|will|would|can|could|should|gets?|deserves?"
    r"|wants?|hires?|hiring|and|or|but|that|who|which|an?|the|to|for|with|of|in|on|at)\b)"
    rf"[\w-]+\s+){{0,3}}?(?:{'|'.join(sorted(_SEAT_WORDS))})\b",
    re.I,
)
# an employer label: "Current employer: Contoso", "Experience — Contoso"
_EMPLOYER_LABEL = re.compile(
    r"\b(?:employers?|company|companies|workplace|organi[sz]ation|employment|work\s+history"
    r"|experience|background|career|history|resume|résumé|cv|previously|formerly|prior|before"
    r"|references?"
    r"|(?:current|present|previous|prior|last|most\s+recent)\s+(?:role|position|job))"
    r"\s*(?::|=|\|)\s*(?:the\s+)?$",
    re.I,
)
_RESUME_SEP_END = re.compile(r"\s*(?:,|\||•|·|@|–|—|-|/)\s*$")
_RESUME_SEP_START = re.compile(r"^\s*(?:,|\||•|·|@|–|—|-|/|:)\s*")
# "you"/"your"/"yours" after a seat cue stand for the target (no other
# mention of them does: "your team needs" is not one)
_YOU = re.compile(r"\b(?:you|your|yours)\b", re.I)
# "Your company was my home", "your sales floor is where I learned", "on
# your floor", "in your org"
_YOUR_WAS_MY = re.compile(
    r"^\s+(?:[\w-]+\s+){0,2}?(?:was|were|has\s+been|had\s+been|became|used\s+to\s+be)"
    r"\s+(?:my|our)\b",
    re.I,
)
_YOUR_WHERE = re.compile(r"^\s+(?:[\w-]+\s+){0,2}?(?:is|was)\s+where\s+(?:I|we)\b", re.I)
_YOUR_ACTED_BEFORE = re.compile(r"\b(?:I|we)\b(?P<v>(?:\s+[\w'’-]+){1,3}?)\s+$")
_YOUR_PLACE = re.compile(r"\b(?:on|in|inside|within|across|from|at|onto|into|off)\s+$", re.I)
_PLACE_AFTER = re.compile(
    r"^\s+(?:[\w-]+\s+){0,2}?(?:floor|team|teams|org|orgs|organi[sz]ation|company|office"
    r"|offices|payroll|staff|bench|roster|crew|squad|desk|side|ranks|books)\b",
    re.I,
)
_YOUR_SEAT_SOFT = re.compile(
    r"\b(?:be|being|join|joining|work|works|working|worked|sit|sitting|sat|serve|serving"
    r"|sell|sells|selling|sold|carry|carrying)\s+"
    + _TARGET_ADVERBS
    + r"(?:(?:on|in|with|for|at|inside|within|alongside|among|(?:a\s+)?part\s+of|one\s+of"
    r"|(?:a\s+)?member\s+of)\s+)?$",
    re.I,
)
# cues an aspiring sentence may have (_aspires): a time or tenure noun before
# the name ("my time at", "years at"), "my"/"our" before it, "<target>, where I…"
_TENURE_NOUN_BEFORE = re.compile(
    r"\b(?:time|times|days|years|months|weeks|tenure|stint|career|stay|chapter|history|past)"
    r"\s+(?:at|with|inside|in|for)\s+(?:the\s+)?$",
    re.I,
)
_OWNED_BEFORE = re.compile(r"\b(?:my|our)\s+(?:[\w'’-]+\s+){0,2}?$", re.I)
_WHERE_AFTER = re.compile(
    r"^(?:['’]s)?[\s,]+(?:(?:is|was)\s+)?(?:where|when|while)\s+(?:I|we)\b", re.I
)
# "a Contoso employee", "a Contoso AE", "the Contoso rep": a seat at the target
_ARTICLE_BEFORE = re.compile(r"\b(?:an?|the)\s+$", re.I)
_SEAT_AFTER = re.compile(
    r"^\s+(?:[\w-]+\s+){0,2}?(?:employee|employees|staffer|staffers|hire|insider|insiders"
    rf"|{'|'.join(sorted(_ROLE_WORDS))})\b",
    re.I,
)
# a sentence that only aspires has one of these…
_FORWARD_MARK = re.compile(
    r"\b(?:will|would|could|can|might|should|shall|want|wants|wanting|hope|hoping|plan"
    r"|planning|aim|aiming|goal|goals|eager|excited|keen|ready|love|like|forward|apply"
    r"|applying|interested|chance|opportunity|choice|dream)\b|['’](?:ll|d)\b",
    re.I,
)
# …and none of these. Re-contact says the candidate was there before too
# (third review 2026-10-09): "I'd love to reconnect with Contoso", "Contoso
# will remember me", "Your team will remember me from Contoso"
_TENURE_WORD = re.compile(
    r"\b(?:again|anew|once\s+more|before|previously|formerly|former|prior|back|ago|earlier"
    r"|return\w*|rejoin\w*|re-join\w*|still|ex|alum\w*|tenure|stint|currently|already"
    r"|continu\w*|stay|stays|stayed|staying|remain\w*|keep|keeps|kept|keeping|home"
    r"|rehir\w*|re-hir\w*|boomerang|second\s+(?:run|stint|time|tour|chapter)"
    r"|one\s+more\s+time|another\s+(?:run|stint|tour|go|chapter)|reunit\w*|full\s+circle"
    r"|re-?connect\w*|re-?acquaint\w*|re-?engag\w*|re-?unit\w*|catch(?:es|ing)?\s+up|caught\s+up"
    r"|(?:remember|remembers|remembered|remembering|recogni[sz]e|recogni[sz]es|recogni[sz]ed"
    r"|recall|recalls|recalled|miss|misses|missed|know|knows|knew|known|vouch(?:es|ed)?\s+for"
    r"|speak(?:s)?\s+for|spoke\s+for)\s+(?:me|us)"
    r")\b",
    re.I,
)
# a first-person reaction in the present (or a glance: "caught my eye") says
# nothing of where the candidate worked: "Contoso's mission matters to me".
# "Contoso showed me how to sell" and "Contoso taught me" are not reactions.
_TARGET_REACTION = re.compile(
    r"\b(?:catch(?:es)?|caught|grabbed|grabs|holds|has|got) my (?:attention|interest|eye)\b"
    r"|\b(?:matters?|appeals?|speaks?|resonates?) to me\b|\bresonates? with me\b"
    r"|\b(?:stands?|stood) out to me\b|\b(?:jumps?|jumped) out at me\b"
    r"|\b(?:excites|inspires|energizes|interests) me\b|\b(?:caught|struck|hooked) me\b",
    re.I,
)
# a mention that only compares or gives an example ("a stack like Salesloft",
# "a team Lumon's size", "bigger than Contoso"): no seat at the target. A
# seat cue before it still fails ("I've worked at places like Contoso")
_COMPARED_BEFORE = re.compile(
    r"\b(?:like|unlike|than|such\s+as|including|similar\s+to|versus|vs\.?|compared\s+(?:to|with))"
    r"\s+(?:the\s+)?$",
    re.I,
)
_SIZE_AFTER = re.compile(r"^['’]s\s+(?:size|scale|stage|headcount|growth\s+stage)\b", re.I)
# "at <target>" after these names the job sought or the target's own people
_SOUGHT_NOUNS = _words(
    """role roles position positions job jobs opening openings opportunity opportunities
    posting postings listing listings vacancy vacancies req requisition interview interviews
    application applications offer process search"""
)
# …when what governs that noun is one of these (or nothing: it opens the clause)
_SOUGHT_GOVERNORS = _words(
    """for about to toward towards regarding re into see saw seeing seen find found finding
    notice noticed noticing read reading spot spotted discover discovered apply applying
    applied consider considering considered discuss discussing explore exploring pursue
    pursuing learn learned hear heard interview interviewing"""
)
_PEOPLE_GOVERNORS = _SOUGHT_GOVERNORS | _words("meet meeting join joining")
# words of a noun phrase between its governor and its noun ("the open Inside
# Sales Manager role"): capitalized words are skipped too
_NP_WORDS = (
    _DETERMINERS
    | _TITLE_FILLER
    | _words(
        """open new current posted advertised remote hybrid onsite full-time part-time senior
    junior sales account accounts team"""
    )
)
# "<target>'s Inside Sales Manager role", "the Contoso posting"
_SOUGHT_AFTER_NAME = re.compile(
    rf"^(?:['’]s)?\s+((?:[\w-]+\s+){{0,3}}?)(?:{'|'.join(sorted(_SOUGHT_NOUNS))})\b", re.I
)
# …and after a target that is also a tool (target_tool_ok): "the Seismic
# process" and "the Aircall search" read as the tool's own, so neither names the
# job sought there (review 2026-10-20). "the Aircall interview process" still
# does, by "interview"; "the process at Aircall" is the company's
_SOUGHT_AFTER_TOOL_NAME = re.compile(
    rf"^(?:['’]s)?\s+((?:[\w-]+\s+){{0,3}}?)"
    rf"(?:{'|'.join(sorted(_SOUGHT_NOUNS - _words('process search')))})\b",
    re.I,
)
# …and the words that may stand between the name and that noun: a title's own
# (capitalized, with its "of"/"and"), or a word of _NP_WORDS that is no
# determiner ("Aircall's open role", "the Aircall sales posting"). Never a verb or
# a determiner: "Aircall created the opportunity", "Aircall played a big role" and
# "Aircall shaped the process" name no job sought (review 2026-10-15, round 4)
_SOUGHT_SPAN_WORDS = (_NP_WORDS - _DETERMINERS) | _words("job")


def _sought_span(span: str) -> bool:
    """True when every word of `span` may stand between the target's name
    and the job sought (_SOUGHT_SPAN_WORDS)."""
    return all(w[:1].isupper() or w.lower() in _SOUGHT_SPAN_WORDS for w in span.split())


_PREP_BEFORE = re.compile(r"\b(?:at|with|for|on|in)\s+(?:the\s+)?$", re.I)
# "Fabrikam is larger than …": the target described in the present tense
_DESCRIBED = re.compile(
    r"^(?:['’]s\s+[\w-]+(?:\s+[\w-]+)?)?\s+(?:is|are)\s+"
    r"(?!(?:my|our|where|when|while|home|the\s+(?:place|company|team|org)\b))",
    re.I,
)
_COORDINATE = re.compile(r",?\s+\b(?:and|but|so|yet)\s+(?=(?:I|we)\b)")


# a "my" that names where the candidate wants to be: "Contoso is my top choice"
_ASPIRE_POSS = re.compile(
    r"\b(?:my|our)\s+(?:(?:top|first|dream|ideal|number[- ]one)\s+(?:choice|pick|option"
    r"|company|employer|destination)|next\s+(?:step|chapter|move))\b",
    re.I,
)


def _owns(text: str) -> bool:
    """True when a "my"/"our"/"mine"/"ours" in `text` is the candidate's own,
    not a plan ("my plan", "my first 90 days") or the target as a choice ("my
    top choice", "my next step"). Unlike _possessive_claim no phrase-closing
    excuse applies: "Contoso is on my resume", "You'll see Contoso on my
    resume", "the next step in my career" put the candidate's history beside
    the target (third review 2026-10-09)."""
    for m in _POSS_FP.finditer(text):
        if m.group().lower() in ("mine", "ours"):
            return True
        if _POSS_PLAN.match(text, m.start()) or _ASPIRE_POSS.match(text, m.start()):
            continue
        return True
    return False


# "me"/"us" as the object of a verb: "Contoso will remember me", "Your team
# trusts me", "Contoso would be lucky to have me". Only a causative ("Working at
# Contoso would let me coach every day") and a verb of the application itself
# ("I hope you'll consider me", "…contact me", "…interview me") are excused,
# and a present reaction ("matters to me") is set aside first. After a
# preposition it is _CANDIDATE_OBJECT's ("for me", "under me"), which
# _fp_forward holds
_OBJECT_ME = re.compile(r"\b(?:[Mm]e|us)\b")
_OBJECT_ME_EXCUSED = re.compile(
    r"\b(?:let|lets|letting|allow|allows|allowing|enable|enables|enabling|consider|considers"
    r"|considering|contact|contacting|reach|reaching|call|calling|email|emailing|interview"
    r"|interviewing)\s+$",
    re.I,
)
_PREP_BEFORE_ME = re.compile(rf"\b(?i:{'|'.join(sorted(_ME_PREPOSITIONS))})\s+$")


def _object_me(text: str) -> bool:
    """True when `text` has a "me"/"us" that is a verb's object: not a
    causative's ("let me"), a preposition's ("for me") or a present
    reaction's ("matters to me", "caught my eye")."""
    spoken = _PERCEPTION.sub(" ", _TARGET_REACTION.sub(" ", text))
    for m in _OBJECT_ME.finditer(spoken):
        head = spoken[: m.start()]
        if not (_OBJECT_ME_EXCUSED.search(head) or _PREP_BEFORE_ME.search(head)):
            return True
    return False


# "me" after a preposition other than "for" in the rest of a clause that names
# the job sought or the target's people: "…your reps, who would learn from
# me", "…the team at Contoso under me" ("the next step for me" stays)
_ME_AFTER_PREP = re.compile(
    rf"\b(?i:myself)\b|\b(?i:{'|'.join(sorted(_ME_PREPOSITIONS - {'for'}))})\s+(?:me|us)\b"
)


# A phrase that says what the candidate IS, beside the target, makes a
# sentence more than an aspiration (fourth review of 2026-10-15): an "as" or
# "being" phrase or an appositive, after the aspired object or before the
# subject, or a fragment after a dash, a colon, a semicolon or a parenthesis,
# whose head is a word of holding or using the target's product ("I'd love
# to join Aircall as one of its earliest customers", "I'd love to join Aircall,
# its customer since 2022", "As a longtime user, I'd…", "A longtime
# customer, I'd…", "I'd love to join Aircall — longtime customer here", "…after
# being one of its customers", "I'd love to join Wonka as one of its
# earliest customers"), or that has a pronoun as a preposition's object
# ("I'd like to join Aircall, a power user of it since 2022"); and a customer
# turned candidate ("a customer turned applicant", "from customer to
# colleague"). Such a phrase describes the candidate: the target is not its
# own customer. A phrase whose head is something else describes the target
# or names the seat sought: "I'd love to join Aircall, a company building…",
# "…as its next Director of Sales Enablement", "…as part of its wholesale team", "…as
# its customers grow" ("customers" there is a subject, not a head), "…as a
# customer advocate". Fail closed: an appositive that renames the target's
# customers fails too ("I'd love to help patients, its users, book faster"),
# an accepted false rejection.
_HOLDING_HEAD_WORDS = (
    r"(?:customers?|clients?|users?|adopters?|subscribers?|licensees?|buyers?|purchasers?"
    r"|admins?|administrators?|fans?)"
)
_DESCRIBED_OPENER = (
    r"(?:an?|its|their|one|someone|somebody|another|some|many|each|any|all|both|several|part"
    r"|members?|longtime|long-time|lifelong|longstanding|long-standing|early|earliest|first"
    r"|original|loyal|happy|satisfied|avid|devoted|heavy|daily|power|proud|former|existing"
    r"|current|repeat|paying|fellow|big|biggest|huge|veteran|regular|frequent|active"
    r"|enthusiastic)"
)
# where such a phrase starts: "as"/"being"/"been" before a noun phrase ("as
# one of its…", not "as it grows", "such as", "as well as", "as soon as"); a
# comma, a parenthesis, a colon, a semicolon or a dash before one; or the
# start of the sentence, perhaps after "long"/"once" ("A longtime customer,
# I'd…", "Customer since 2022.")
_DESCRIBED_START = re.compile(
    r"(?<!such\s)(?<!well\s)(?<!soon\s)(?<!much\s)(?<!long\s)(?<!far\s)(?<!many\s)(?<!same\s)"
    rf"\b(?P<subject>as|being|been)\s+(?=(?:{_DESCRIBED_OPENER}|{_HOLDING_HEAD_WORDS})\b)"
    rf"|(?:[,(:;—–]|\s-)\s*(?=(?:the|{_DESCRIBED_OPENER}|{_HOLDING_HEAD_WORDS})\b)"
    r"|(?P<initial>(?:^|(?<=[.!?])\s+)[\"'“‘(]*(?:(?:long|once|twice)\s+)?)"
    rf"(?=(?:{_DESCRIBED_OPENER}|{_HOLDING_HEAD_WORDS})\b)",
    re.I,
)
# …and where it ends: a mark, or a conjunction that opens another clause
_DESCRIBED_END = re.compile(
    r"[,;:.!?()\[\]—–]|\s-\s|\s(?:and|but|so|yet|because|while|whereas|where|which)\b", re.I
)
# its head is a word of holding: openers, capitalized words and at most one
# other word before it, and nothing that continues the noun after it ("a
# customer advocate", "its customers grow" have another head)
_HOLDING_HEAD = re.compile(
    rf"^(?:(?:{_DESCRIBED_OPENER}|the|of|(?-i:[A-Z])[\w'’&.-]*)\s+)*"
    rf"(?:(?-i:[a-z])[\w'’-]*\s+)?(?:{_DESCRIBED_OPENER}\s+)*"
    rf"{_HOLDING_HEAD_WORDS}\b(?![-–](?!turned\b)\w)"
    r"(?=\s*(?:$|[,;:.!?)\]—–]|(?:of|since|for|from|at|in|on|with|who|that|and|or|here|now"
    r"|today|myself|too|already|back|turned)\b)|[-–]turned\b)",
    re.I,
)
# a pronoun as a preposition's object: "a power user of it", "fluent in it"
_POINTER_OBJECT = re.compile(
    r"\b(?:of|with|on|in|inside|into|using)\s+(?:it|them|itself|themselves)\b"
)
# a customer turned candidate, anywhere: "a customer turned applicant",
# "from customer to colleague"
_HOLDING_TURNED = re.compile(
    r"\b(?:customer|client|user|subscriber|fan|admin|buyer)s?[\s-]+turned[\s-]+\w"
    r"|\bfrom\s+(?:(?:an?|longtime|long-time|loyal|happy|early|paying|(?-i:[A-Z])[\w'’-]*)\s+)"
    r"{0,2}(?:customer|client|user|subscriber|fan|admin|buyer)\s+to\s+(?:(?:an?|the|your)\s+)?"
    r"(?:colleague|teammate|employee|candidate|applicant|insider|team\s+member|hire|builder"
    r"|seller|rep)\b",
    re.I,
)


def _described_spans(unit: str) -> list[tuple[int, int, int, str]]:
    """(start, body start, end, form) of every phrase of `unit` that may
    describe the sentence's subject (_DESCRIBED_START, _DESCRIBED_END). The
    form is "subject" for an "as"/"being"/"been" phrase, "fragment" for a
    whole sentence that may be one ("Customer since 2022."), else
    "appositive". A phrase at the start of a sentence is one only before a
    comma ("A longtime customer, I'd…") or as the whole sentence."""
    out: list[tuple[int, int, int, str]] = []
    for m in _DESCRIBED_START.finditer(unit):
        stop = _DESCRIBED_END.search(unit, m.end())
        end = stop.start() if stop else len(unit)
        form = "appositive"
        if m.group("subject"):
            form = "subject"
        elif m.group("initial") is not None:
            mark = stop.group() if stop else ""
            if mark in (".", "!", "?", ""):
                form = "fragment"
            elif mark != ",":
                continue
        out.append((m.start(), m.end(), end, form))
    return out


def _described_claim(unit: str) -> str:
    """The first phrase of `unit` that describes the candidate as the
    target's customer or user (see above): its head a word of holding, or a
    pronoun the object of its preposition (not in a whole sentence: "Some of
    it is luck."), or a customer turned candidate. "" when there is none."""
    for start, body, end, form in _described_spans(unit):
        text = unit[body:end]
        if _HOLDING_HEAD.match(text) or (form != "fragment" and _POINTER_OBJECT.search(text)):
            return unit[start:end].strip(" ,(:;—–-\"'“‘")
    if m := _HOLDING_TURNED.search(unit):
        return m.group()
    return ""


def _aspires(unit: str) -> bool:
    """True when the sentence only aspires to the target (see above): "I'd
    love to join Contoso", "I want to work at Contoso", "Joining Contoso would…",
    "Contoso is my top choice". Any "my"/"our" beyond a plan or a choice
    (_owns), any "me"/"us" as an object (_object_me) and any word of re-contact
    (_TENURE_WORD) make it more than an aspiration: "You'll see Contoso on my
    resume", "Contoso will remember me", "I'd love to reconnect with Contoso"
    (third review 2026-10-09). So does a phrase that describes the candidate
    as the target's customer or user (_described_claim): "I'd love to join
    Contoso as one of its earliest customers" (fourth review of 2026-10-15)."""
    spoken = _ASPIRE_POSS.sub(" ", _PERCEPTION.sub(" ", unit))
    return (
        _FORWARD_MARK.search(unit) is not None
        and _TENURE_WORD.search(unit) is None
        and not _past_claim(spoken)
        # a past "told me"/"showed me" is no reaction: "I'd love to meet the
        # team at Contoso who showed me the ropes" (third review 2026-10-09)
        and not _past_claim(_TARGET_REACTION.sub(" ", unit))
        and not _owns(spoken)
        and not _object_me(unit)
        and _fp_forward(spoken)
        and not _described_claim(unit)
    )


def _clause_bounds(unit: str, start: int, end: int) -> tuple[int, int]:
    """The clause (_CLAUSE_SPLIT) holding unit[start:end]."""
    head = [m.end() for m in _CLAUSE_SPLIT.finditer(unit, 0, start)]
    tail = _CLAUSE_SPLIT.search(unit, end)
    return (head[-1] if head else 0), (tail.start() if tail else len(unit))


def _rest_claims(rest: str) -> bool:
    """True when the rest of a clause after the target's name may say the
    candidate had what it names: a word of tenure ("…, formerly held"), or a
    first person that is not only forward ("…, which I held", "… I led", "…
    my team"), or a "me" as a verb's object or after a preposition other than
    "for" ("…, who knows me", "…, who would learn from me"). With no first
    person, a past tense is the role's own ("… that opened last week")."""
    spoken = _PERCEPTION.sub(" ", rest)
    if _TENURE_WORD.search(spoken):
        return True
    # a past "told me"/"showed me" is no reaction (third review 2026-10-09):
    # "…the team at Contoso, who showed me the ropes"
    heard = _TARGET_REACTION.sub(" ", rest)
    if _FIRST_PERSON.search(heard) and _past_claim(heard):
        return True
    if not _FIRST_PERSON.search(spoken):
        return False
    return (
        _past_claim(spoken)
        or not _fp_subjects_forward(spoken)
        or _possessive_claim(spoken)
        # "me" as a verb's object or after a preposition other than "for"
        # (third review 2026-10-09): "Your VP knows me", "your reps, who would
        # learn from me"
        or _object_me(rest)
        or _ME_AFTER_PREP.search(_TARGET_REACTION.sub(" ", spoken)) is not None
    )


# A preposition that governs the job sought or the target's people ("excited
# about the role", "apply for the role", "…prepared me for the role at
# Contoso") follows a word of wanting or applying, never a verb that puts the
# candidate in the seat or under its people, nor thanks for it: "I was
# promoted to the role at Contoso", "I moved into the role at Contoso", "I
# reported to the team at Contoso", "I sold to the team at Contoso" and "Thanks
# for the role at Contoso; I loved it" passed (third review 2026-10-09)
_PREP_GOVERNORS = _words("for about to toward towards regarding re into")
_BEFORE_PREP_OK = _words(
    """excited interested eager keen ready enthusiastic curious thrilled delighted happy glad
    apply applying application applications candidacy consideration questions question more
    writing write out"""
)
# "<verb> me for the role": the job sought only after these
_ME_FOR_OK = _words(
    "consider considers considering prepare prepares prepared preparing readied qualify qualifies"
)


def _governed(words: Sequence[str], governors: frozenset[str]) -> bool:
    """True when the noun phrase that `words` ends in (its noun excluded) is
    governed by one of `governors`, or by nothing (it opens the clause). A
    capitalized word is part of the phrase ("the Inside Sales Manager
    role") unless it opens the clause and is no phrase word: "Ask anyone at
    Contoso about me" is governed by "ask". A governing preposition must open
    the clause or follow a word of wanting or applying (_BEFORE_PREP_OK, or
    "considering me for"): "promoted to the role" is not governed."""
    i = len(words) - 1
    while i >= 0:
        low = words[i].lower()
        if low in ("my", "our"):
            return False
        if low in _NP_WORDS or (i > 0 and words[i][0].isupper()):
            i -= 1
            continue
        break
    if i < 0:
        return True
    gov = words[i].lower()
    if gov not in governors:
        return False
    if gov not in _PREP_GOVERNORS or i == 0:
        return True
    prev = words[i - 1].lower().replace("’", "'")
    if prev in _BEFORE_PREP_OK:
        return True
    return gov == "for" and prev in ("me", "us") and i >= 2 and words[i - 2].lower() in _ME_FOR_OK


def _sought_at(unit: str, prep_start: int, name_end: int, people_ok: bool) -> bool:
    """True when "at|with|for|on|in <target>" (the preposition at
    `prep_start`) follows the job sought ("…for the Inside Sales Manager
    role at Contoso", "I saw the role at Contoso") or, when `people_ok`, the
    target's own people ("to meet the team at Contoso"): what governs that noun
    is a word of seeking, nothing (the noun phrase opens the clause) or, for
    people, "meet"/"join"; no "my"/"our" owns it; and the rest of the clause
    claims nothing (_rest_claims)."""
    c_start, c_end = _clause_bounds(unit, prep_start, name_end)
    if _rest_claims(unit[name_end:c_end]):
        return False
    words = re.findall(r"[A-Za-z][\w'’-]*", unit[c_start:prep_start])
    if not words:
        return False
    noun = words[-1].lower()
    if noun in _SOUGHT_NOUNS:
        governors = _SOUGHT_GOVERNORS
    elif people_ok:
        governors = _PEOPLE_GOVERNORS
    else:
        return False
    return _governed(words[:-1], governors)


def _names_seat_sought(unit: str, start: int, end: int, *, tool: bool = False) -> bool:
    """True when the target's name at unit[start:end] names the job sought or
    the target's own people (_sought_at), or modifies the job sought ("I saw
    the Contoso posting", "Contoso's Inside Sales Manager role is…").
    `tool`: the target is also a tool (_SOUGHT_AFTER_TOOL_NAME)."""
    c_start, c_end = _clause_bounds(unit, start, end)
    if m := _PREP_BEFORE.search(unit[c_start:start]):
        prep = c_start + m.start()
        return _sought_at(unit, prep, end, _client_context(unit[:prep]))
    after_name = _SOUGHT_AFTER_TOOL_NAME if tool else _SOUGHT_AFTER_NAME
    if m := after_name.match(unit[end:c_end]):
        if not _sought_span(m.group(1)) or _rest_claims(unit[end + m.end() : c_end]):
            return False
        return _governed(re.findall(r"[A-Za-z][\w'’-]*", unit[c_start:start]), _SOUGHT_GOVERNORS)
    return False


# what a first-person sentence may still name with "you"/"your" (third review
# 2026-10-09; target_tenure_cues): "thank you", the job sought ("I saw your
# posting", "…prepared me for your Inside Sales Manager role", "the role
# with you") and the target's people ("I enjoyed meeting your team", "For
# your Account Associates, that means…"), each governed as for the target's
# name (_governed: "I reported to your CEO" and "I sold to your team" are not).
# Narrower than for the target's name: "your sales process" is no job sought,
# and "learned", "heard" or "interview" govern neither ("I learned your
# job inside out").
_YOUR_SOUGHT_NOUNS = _words(
    """role roles position positions opening openings opportunity opportunities posting
    postings listing listings vacancy vacancies req requisition application applications
    job jobs description descriptions jd"""
)
_YOUR_SOUGHT = re.compile(
    rf"^\s+(?:[\w-]+\s+){{0,3}}?(?:{'|'.join(sorted(_YOUR_SOUGHT_NOUNS))})\b", re.I
)
_NOT_YOUR_GOVERNORS = _words("learn learned hear heard interview interviewing")
_YOUR_SOUGHT_GOVERNORS = _SOUGHT_GOVERNORS - _NOT_YOUR_GOVERNORS
_YOUR_PEOPLE_GOVERNORS = (_PEOPLE_GOVERNORS - _NOT_YOUR_GOVERNORS) | _words("met")
_YOUR_PEOPLE = re.compile(
    r"^\s+(?:[\w-]+\s+){0,2}?(?:"
    + "|".join(
        sorted(_TEAM_NOUNS | _SEAT_WORDS | _words("people folks recruiter recruiters founders"))
    )
    + r")\b",
    re.I,
)
_THANK_BEFORE = re.compile(r"\bthank\s+$", re.I)


def _you_excused(unit: str, start: int, end: int) -> bool:
    """True when "you"/"your" at unit[start:end] is "thank you", names the
    job sought, or names the target's people met (see above), with nothing
    in the rest of its clause that claims (_rest_claims)."""
    named = unit[start:end].lower()
    c_start, c_end = _clause_bounds(unit, start, end)
    if named == "you" and _THANK_BEFORE.search(unit[:start]):
        # "Thank you for considering me", never "Thank you for promoting me"
        return not _rest_claims(unit[end:c_end])
    if named == "your":
        words = re.findall(r"[A-Za-z][\w'’-]*", unit[c_start:start])
        for pattern, governors in (
            (_YOUR_SOUGHT, _YOUR_SOUGHT_GOVERNORS),
            (_YOUR_PEOPLE, _YOUR_PEOPLE_GOVERNORS),
        ):
            if m := pattern.match(unit[end:c_end]):
                return not _rest_claims(unit[end + m.end() : c_end]) and _governed(words, governors)
        return False
    if m := _PREP_BEFORE.search(unit[c_start:start]):  # "the role with you"
        return _sought_at(unit, c_start + m.start(), end, False)
    return False


def _client_mention(unit: str, start: int, end: int) -> bool:
    """True when the name at unit[start:end] follows a client noun and a
    preposition ("accounts at Nakatomi", "buyers at Nakatomi")."""
    c_start, _c_end = _clause_bounds(unit, start, end)
    m = _PREP_BEFORE.search(unit[c_start:start])
    return m is not None and _client_context(unit[: c_start + m.start()])


def _describes_target(unit: str, start: int, end: int) -> bool:
    """True when the sentence opens with the target as its subject in the
    present tense ("Fabrikam is…", "Contoso's mission is…") and its first person
    comes only (a) inside a comparison with a thing ("than any business I
    have worked in"), every later "I"/"we" clause forward: "Fabrikam is larger than
    any business I have worked in, and I would want to learn how contractors buy";
    or (b) as a present attitude, with no past claim and no "my"/"me":
    "Contoso is solving a problem I care about", "Contoso's mission is one I
    share". "Contoso is better than anywhere else I've worked", "Contoso is
    bigger than when I joined" and "Contoso is where I started" are neither,
    and neither is a sentence with a "my"/"our" beyond a plan (_owns) or a
    "me"/"us" as an object (_object_me): "Contoso is on my resume", "Contoso is
    a company that knows me" (third review 2026-10-09)."""
    if unit[:start].strip(" \t\"'“‘("):
        return False
    m = _DESCRIBED.match(unit[end:])
    if m is None:
        return False
    rest = unit[end + m.end() :]
    fp = _FIRST_PERSON.search(rest)
    if fp is None:
        return True
    if _owns(_PERCEPTION.sub(" ", rest)) or _object_me(rest):
        return False
    if re.search(r"\b(?:else|other|besides|apart|there|then|since|anymore)\b", rest, re.I):
        return False
    # a word of tenure, outside a compound adjective ("earlier-stage")
    if _TENURE_WORD.search(re.sub(r"\w+(?:-\w+)+", " ", rest)):
        return False
    if _COMPARED_THING.search(rest[: fp.start()]):
        later = _COORDINATE.split(rest[fp.start() :])[1:]
        return all(not _past_claim(p) and _fp_forward(p) for p in later)
    spoken = _PERCEPTION.sub(" ", rest)
    return not _past_claim(spoken) and _fp_attitude(spoken)


# "than anything", "than any team", "than most startups", "than the teams":
# a comparison with a thing, not a time ("than when I joined")
_COMPARED_THING = re.compile(
    r"\bthan\s+(?:anything|anyone|anybody|any(?:\s+[\w-]+){1,3}|most(?:\s+[\w-]+){0,3}"
    r"|what|the(?:\s+[\w-]+){1,3}|an?(?:\s+[\w-]+){1,3}|those(?:\s+[\w-]+){0,3})\s+$",
    re.I,
)
# present verbs of attitude: "a problem I care about", "a mission I share"
_ATTITUDE = _STANCE | _words(
    "care cares believe admire respect love value share like appreciate follow champion"
)


def _fp_attitude(text: str) -> bool:
    """True when every "I"/"we" in `text` takes a present verb of attitude or
    a forward verb (_fp_subjects_forward) and nothing makes it about what the
    candidate has (_possessive_claim, _CANDIDATE_OBJECT)."""
    if _possessive_claim(text) or _CANDIDATE_OBJECT.search(text):
        return False
    for m in _FP_SUBJECT.finditer(text):
        words = [w.lower() for w in re.findall(r"[A-Za-z]+", text[m.end() : m.end() + 40])]
        if m.group("c") is None and words and words[0] in _ATTITUDE:
            continue
        if not _fp_subjects_forward(text[m.start() :]):
            return False
    return True


def _tenure_before(before: str) -> re.Match[str] | None:
    """The cue that only a past or current seat has, right at the end of
    `before` (_TENURE_BEFORE), not running across a comma into a
    first-person clause (_CROSSES)."""
    for w in re.finditer(r"\b\w", before):
        hit = _TENURE_BEFORE.match(before, w.start())
        if hit and not _CROSSES.search(hit.group()):
            return hit
    return None


def _list_tail(text: str) -> bool:
    """True when `text` holds only other names and list words: what may sit
    between a cue and the target in "worked at Haulmark, then Contoso"."""
    toks = re.findall(r"[A-Za-z0-9][\w&.'’+-]*|[^\s\w]", text)
    if len(toks) > 16:
        return False
    for t in toks:
        if t in {",", "&", "/", "+"}:
            continue
        if t[0].isupper() and t != "I" and not t.startswith(("I'", "I’")):
            continue
        if t.lower() in _LIST_WORDS:
            continue
        return False
    return True


# "Contoso AE turned sales leader": a singular seat right after the name that
# opens a clause with no first person, a résumé tagline ("Contoso's Account
# Associates book…" and "Contoso AEs book…" are the target's people)
_TAGLINE_AFTER = re.compile(
    r"^\s+(?:[A-Z][\w-]*\s+){0,2}?(?:employee|staffer|hire|insider|alum|veteran"
    rf"|{'|'.join(sorted(_ROLE_WORDS))})\b(?!['’]?s\b)",
    re.I,
)
_ANSWER_FILLER = _words(
    """yes yeah yep sure correct indeed absolutely at with for the in by from to until through
    since between and present now currently today also too both"""
)


def _bare_answer(unit: str, start: int, end: int) -> bool:
    """True when the unit is the target's name with nothing but "yes", "at",
    dates and punctuation around it: "Yes, Contoso.", "Yes: at Contoso."."""
    rest = re.sub(r"\b(?:19|20)\d{2}\b", " ", unit[:start] + " " + unit[end:])
    words = re.findall(r"[A-Za-z]+", rest)
    return all(w.lower() in _ANSWER_FILLER for w in words)


def _resume_line(unit: str, start: int, end: int) -> bool:
    """True when the unit is a résumé line pairing a title with the target:
    "Account Executive, Contoso", "Contoso | Account Executive (2019 – 2021)"."""
    head = unit[:start].strip()
    tail = re.sub(r"[()\[\]]", " ", unit[end:])
    tail = re.sub(r"\b(?:(?:19|20)\d{2}|present|current|now)\b", " ", tail, flags=re.I)
    tail = re.sub(r"[\s–—,.;:!?-]+$", "", tail)
    if head and not tail:
        m = _RESUME_SEP_END.search(head)
        if m is not None and _title_line(head[: m.start()]):
            return True
    if tail and not head:
        m = _RESUME_SEP_START.match(tail)
        return m is not None and _title_line(tail[m.end() :])
    # the name and its dates alone: "Contoso, 2019–2021", "2019 – 2021: Contoso"
    dates = r"[\s,()\[\]:–—-]*(?:(?:19|20)\d{2}|present|current|now)"
    return bool(
        re.fullmatch(rf"(?:{dates})*[\s.,:;–—-]*", head + " " + tail, re.I)
        and re.search(r"\b(?:19|20)\d{2}\b", unit)
    )


def _target_spans(unit: str, target: str, ordinary: Collection[str]) -> list[tuple[int, int, bool]]:
    """(start, end, judged) per mention of the target in `unit`, in any case:
    its full name, and the first word of a longer name when that word has 4+
    letters and is capitalized in the name ("Fabrikam"). `judged` is False for a
    mention that is judged only after a seat cue: a lowercase spelling of an
    ordinary word of the corpus or the JD (`ordinary`: "pilot" for Pilot, or
    "bright" for Bright Horizons)."""
    words = target.split()
    out: dict[tuple[int, int], bool] = {}
    full = re.compile(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b", re.I)
    for m in full.finditer(unit):
        text = m.group()
        lower = text[0].islower() or text == text.lower()
        plain = re.findall(r"[a-z0-9]+", text.lower())
        out[(m.start(), m.end())] = not (lower and all(w in ordinary for w in plain))
    if len(words) > 1 and len(words[0]) >= 4 and words[0][0].isupper():
        first = re.compile(rf"\b{re.escape(words[0])}\b", re.I)
        for m in first.finditer(unit):
            if not any(s <= m.start() < e for s, e in out):
                out[(m.start(), m.end())] = (
                    m.group()[0].isupper() or m.group().lower() not in ordinary
                )
    return [(s, e, judged) for (s, e), judged in sorted(out.items())]


# A sentence joined to the target's sentence by a back-reference (_units) is
# no part of what the candidate aspires to when it is a generic statement: "A
# partner program built with care keeps the resellers who sell and stay. I
# want to build that program at Tailspin." read "built" and "stay" as the
# candidate's own and failed "at Tailspin" (review 2026-10-12). A generic
# sentence opens with an article, a quantifier or a demonstrative before a
# lowercase word, and has no first person, no "you"/"your", no mention of the
# target in any case, and no word of tenure but one of staying ("resellers
# who stay"). Every other sentence stays in: "The program I built with care
# keeps resellers. I want to build that program at Tailspin." is no
# aspiration.
_GENERIC_OPENING = re.compile(
    r"^\W*(?:a|an|the|every|each|any|most|many|some|all|this|that|these|those)\s+(?-i:[a-z])",
    re.I,
)
_STAYING = re.compile(
    r"(?:stay|stays|stayed|staying|remain\w*|keep|keeps|kept|keeping|continu\w*|still)",
    re.I,
)


# A word that points at a place or company ("there", "here", "this company")
# makes a sentence about somewhere specific, so it is never generic: "The team
# left behind there still needs that." must not launder a tenure claim at the
# target into the next sentence's aspiration (review 2026-10-12).
_POINTS_AT_PLACE = re.compile(
    r"\b(?:there|here|this (?:company|team|org(?:anization)?)|the company|your)\b", re.I
)


def _generic_sentence(sentence: str, target: str, ordinary: Collection[str]) -> bool:
    """True when `sentence` is a generic statement (see above)."""
    return (
        _GENERIC_OPENING.match(sentence) is not None
        and not _FIRST_PERSON.search(sentence)
        and not _YOU.search(sentence)
        and not _POINTS_AT_PLACE.search(sentence)
        and not _target_spans(sentence, target, ordinary)
        and all(_STAYING.fullmatch(m.group()) for m in _TENURE_WORD.finditer(sentence))
    )


def _aspiration_scope(unit: str, target: str, ordinary: Collection[str]) -> str:
    """`unit` less its generic sentences (_generic_sentence), for _aspires."""
    sentences = [s for s in _SENTENCE.split(unit) if s.strip()]
    kept = [s for s in sentences if not _generic_sentence(s, target, ordinary)]
    return " ".join(kept) if kept and len(kept) < len(sentences) else unit


def target_tenure_cues(
    unit: str,
    target_company: str,
    cues: Sequence[_Cue],
    ordinary: Collection[str] = frozenset(),
    client: bool = False,
) -> list[str]:
    """What in `unit` names the target (`target_company`) as where the
    candidate worked or works (see above). `cues` are the unit's
    _employment_cues; `ordinary` the lowercase words of the corpus and the
    JD (_target_spans); `client` True when a cited bullet names the target,
    so that "accounts at Nakatomi" stays a client. The caller asks only when
    the corpus does not list the target as a role company."""
    target = target_company.strip()
    key = _norm_name(target)
    if not key:
        return []
    found: list[str] = []
    aspires: bool | None = None
    described = ""

    def soft(cue: str) -> None:
        nonlocal aspires, described
        if aspires is None:
            scope = _aspiration_scope(unit, target, ordinary)
            aspires = _aspires(scope)
            # "…as one of its earliest customers": quoted beside the cue
            described = _described_claim(scope)
        if not aspires:
            found.append(cue)
            if described:
                found.append(described)

    # a present reaction ("matters to me") makes no first-person sentence,
    # unless a word of tenure says it is an old one: "Your sales floor still
    # speaks to me", "Contoso still matters to me" (third review 2026-10-09)
    first_person = bool(_FIRST_PERSON.search(_TARGET_REACTION.sub(" ", unit))) or bool(
        _FIRST_PERSON.search(unit) and _TENURE_WORD.search(unit)
    )
    for s, e, judged in _target_spans(unit, target, ordinary):
        c_start, c_end = _clause_bounds(unit, s, e)
        before, after = unit[c_start:s], unit[e:c_end]
        named = unit[s:e]
        listed = [
            m
            for m in _TENURE_ANY.finditer(before)
            if m.end() < len(before) and _list_tail(before[m.end() :])
        ]
        if m := _tenure_before(before):
            found.append(f"{m.group().strip()} {named}")
        elif listed:
            found.append(f"{listed[0].group().strip()} … {named}")
        elif m := _TENURE_AFTER.match(after):
            found.append(f"{named}{m.group().rstrip()}")
        elif (m := _COPULA_SEAT_BEFORE.search(before)) and _SEAT_WORD_AFTER.match(after):
            found.append(f"{m.group().strip()} {named}{after[:40].rstrip()}")
        elif m := _EMPLOYER_LABEL.search(unit[:s]):
            found.append(f"{m.group().strip()} {named}")
        elif _resume_line(unit, s, e):
            found.append(unit.strip()[:80])
        elif not judged:
            continue
        elif _bare_answer(unit, s, e):
            found.append(unit.strip()[:80])  # "Yes, Contoso.", "Yes: at Contoso."
        elif not first_person and not before.strip() and (tag := _TAGLINE_AFTER.match(after)):
            soft(f"{named}{tag.group()}")  # "Contoso AE turned sales leader"
        elif m := _TENURE_NOUN_BEFORE.search(before) or _OWNED_BEFORE.search(before):
            soft(f"{m.group().strip()} {named}")
        elif first_person:
            # any other mention in a first-person sentence: "I led Contoso's
            # enterprise team", "Contoso taught me", "I came over to Contoso…"
            if not (
                _names_seat_sought(unit, s, e)
                or _describes_target(unit, s, e)
                or (client and _client_mention(unit, s, e))
                or _COMPARED_BEFORE.search(before)
                or _SIZE_AFTER.match(after)
            ):
                soft(f"{named} (in a sentence about the candidate)")
        else:
            # "Contoso, where I…", "a Contoso employee"
            seat = _SEAT_AFTER.match(after) if _ARTICLE_BEFORE.search(before) else None
            if m := _WHERE_AFTER.match(after) or seat:
                soft(f"{named}{m.group()}")
    # "you"/"your" after a seat cue: "I was on your sales team"
    for m in _YOU.finditer(unit):
        c_start, c_end = _clause_bounds(unit, m.start(), m.end())
        before, after = unit[c_start : m.start()], unit[m.end() : c_end]
        named = m.group()
        if hit := _tenure_before(before):
            found.append(f"{hit.group().strip()} {named}")
        elif hit := _TENURE_AFTER.match(after):
            found.append(f"{named}{hit.group().rstrip()}")
        elif (hit := _COPULA_SEAT_BEFORE.search(before)) and _SEAT_WORD_AFTER.match(after):
            found.append(f"{hit.group().strip()} {named}{after[:40].rstrip()}")
        elif hit := _YOUR_WAS_MY.match(after):
            found.append(f"{named}{hit.group().rstrip()}")
        elif hit := _YOUR_SEAT_SOFT.search(before):
            soft(f"{hit.group().strip()} {named}")
        elif hit := _YOUR_WHERE.match(after):
            soft(f"{named}{hit.group()}")
        elif (hit := _YOUR_PLACE.search(before)) and (place := _PLACE_AFTER.match(after)):
            soft(f"{hit.group().strip()} {named}{place.group().rstrip()}")
        elif (
            (hit := _YOUR_ACTED_BEFORE.search(before))
            and _past_claim(hit.group("v"))
            and (place := _PLACE_AFTER.match(after))
        ):
            # "I led your enterprise team", "I helped build your sales org"
            soft(f"{hit.group().strip()} {named}{place.group().rstrip()}")
        elif first_person and not _you_excused(unit, m.start(), m.end()):
            # any other "you"/"your" in a first-person sentence, as for the
            # target's name (third review 2026-10-09): "I reported to your
            # CEO", "I carried your quota", "I miss your sales floor", "Your
            # sales playbook is the one I wrote", "I have run your weekly
            # revenue meeting"
            soft(f"{named} (in a sentence about the candidate)")
    for cue in cues:
        name_key = _norm_name(cue.name)
        if not name_key or not (_contains_name(name_key, key) or _contains_name(key, name_key)):
            continue
        what = f"{cue.cue} {cue.name}"
        if cue.title_line:
            found.append(what)  # "Account Executive at Contoso": a résumé line
            continue
        if cue.at:
            name_at = unit.find(cue.name, cue.start)
            if name_at >= 0 and _sought_at(unit, cue.start, name_at + len(cue.name), cue.client_ok):
                continue
        soft(what)
    return list(dict.fromkeys(found))


def _capitalized_run(unit: str, pos: int) -> tuple[int, int]:
    """The span of the capitalized phrase holding offset `pos` ("Inside
    Sales Manager"), for the claimed_job_title check."""
    for m in _NAME_RE.finditer(unit):
        if m.start() <= pos < m.end():
            start = m.start()
            # a capitalized determiner opening the sentence is not part of it:
            # "Your Account Associates…", "Every Account Associate…"
            while (w := re.match(r"([A-Za-z]+)\s+", unit[start:pos])) and w.group(
                1
            ).lower() in _FUNCTION:
                start += w.end()
            return start, m.end()
        if m.start() > pos:
            break
    return pos, pos + 1


# A hyphenated title word of a corpus role ("Mid-Market" of "Mid-Market Account
# Executive") splits into capitalized parts (_proper_parts), and its "Mid" is in
# no vocabulary: "At Marlow Payroll, where I was a Mid-Market Account Executive,
# …" and "As a Mid-Market rep at Marlow Payroll, …" failed on an unsourced "Mid"
# (review 2026-10-12). Such a part is the held title's own word only inside a
# title run that a corpus role holds, word for word ("&", "and" and a comma
# alike): "As Mid-Market Account Director at Marlow Payroll" and "As Mid-Level
# Account Executive at Marlow Payroll" keep "Mid" unsourced, whatever the cited
# bullets say of a Director.
_HYPHENATED = re.compile(r"[A-Za-z0-9'’]+(?:-[A-Za-z0-9'’]+)+")
_TITLE_TOKEN = r"(?!I\b|I['’])[A-Z][A-Za-z0-9'’]*(?:-[A-Za-z0-9'’]+)*"
_TITLE_RUN = re.compile(rf"{_TITLE_TOKEN}(?:(?:\s*,\s*|\s+(?:(?:&|and|of)\s+)?){_TITLE_TOKEN})*")


def _title_words(text: str) -> tuple[str, ...]:
    """A title's words, lowercased, with "&" and a comma read as "and" and a
    possessive dropped: "Mid-Market Account Executive & Team Lead" →
    ("mid-market", "account", "executive", "and", "team", "lead")."""
    text = re.sub(r"\s*[&,]\s*", " and ", re.sub(r"['’]s\b", "", text.lower()))
    return tuple(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text))


def _compound_at(unit: str, pos: int) -> str:
    """The hyphenated word holding offset `pos`, lowercased, its possessive
    dropped ("mid-market" for the "Mid" of "Mid-Market's"); "" for none."""
    for m in _HYPHENATED.finditer(unit):
        if m.start() <= pos < m.end():
            return re.sub(r"['’]s?$", "", m.group().lower())
        if m.start() > pos:
            break
    return ""


def _title_run(unit: str, pos: int) -> str:
    """The capitalized title run holding offset `pos`: capitalized words joined
    by spaces, "&", "and", "of" or a comma, less the capitalized function words
    that open it ("As", "At")."""
    for m in _TITLE_RUN.finditer(unit):
        if m.start() <= pos < m.end():
            start = m.start()
            while (w := re.match(r"([A-Za-z]+)\s+", unit[start:pos])) and w.group(
                1
            ).lower() in _FUNCTION:
                start += w.end()
            return unit[start : m.end()]
        if m.start() > pos:
            break
    return ""


def _held_title(run: str, held: Collection[tuple[str, ...]]) -> bool:
    """True when the title run is a corpus role's title, or a contiguous part
    of one, word for word (_title_words)."""
    words = _title_words(run)
    n = len(words)
    return n > 0 and any(t[i : i + n] == words for t in held for i in range(len(t) - n + 1))


def _claimed_as_own(unit: str, start: int, end: int, target_company: str) -> bool:
    """True when the capitalized phrase at unit[start:end] is used as the
    candidate's own title ("As an Account Associate I…", "I was the Inside
    Sales Manager…"). Tied to the target's job ("As your Inside Sales
    Manager, I'd…") it is the candidate's only when its clause makes a past
    claim or has an "I"/"we" that is not forward."""
    clause = _CLAUSE_END.split(unit[:start])[-1]
    rest = unit[end:]
    if _classify_title(clause, rest) != "claim":
        return False
    if not _target_linked(clause, rest, target_company):
        return True
    # tied to the target's job, it is the role sought only in a clause that
    # claims nothing: "I was your Inside Sales Manager", "I've already
    # been your …", "I served as Contoso's …" are seats held (second review
    # 2026-10-09)
    whole = clause + unit[start:end] + _CLAUSE_END.split(rest)[0]
    spoken = _PERCEPTION.sub(" ", whole)
    return _past_claim(spoken) or not _fp_subjects_forward(spoken)


# the job's title named as the role sought, not a seat held: "apply as an
# Inside Sales Manager", "want to be…", "becoming…", "thrive as…", "an
# Inside Sales Manager candidate" (review of validator-refine)
_ASPIRE_BEFORE = re.compile(
    r"\b(?:to\s+(?:be|become|serve\s+as|work\s+as|grow\s+into|step\s+into)"
    r"|becom(?:e|es|ing)|apply(?:ing)?(?:\s+\w+)?\s+as|applied(?:\s+\w+)?\s+as"
    r"|application\s+as|candidacy\s+as|join(?:ing)?(?:\s+\w+)?\s+as"
    r"|(?:thrive|succeed|excel|grow)\s+as)\s+(?:(?:an?|the)\s+)?$",
    re.I,
)
_SOUGHT_AFTER = re.compile(r"^[\s-]+(?:candidate|candidacy|applicant|application)s?\b", re.I)
# an honest disclaimer: the negation governs the seat itself, right before the
# title — "I haven't held the … title", "I have not been an …", "I've never
# held the title of …", "I'm not yet an …" (second review of validator-refine).
# "not just", "not only", "never stopped being", "wasn't officially" and "not
# an AE but the …" do not end this way, so they stay claims.
_NEGATED_SEAT = re.compile(
    r"(?:\bnot|n['’]t|\bnever|\bno\s+longer)\s+(?:(?:yet|ever|once|previously)\s+)*"
    r"(?:(?:been|be|being|become|became|held|hold|had|have|carried|carry|worn|wear|earned"
    r"|gotten|got|landed|reached|titled|called|named|served\s+as|serve\s+as|worked\s+as"
    r"|work\s+as|promoted\s+to)\s+)?"
    r"(?:(?:the|a|an)\s+(?:title|role|position|seat|job)\s+(?:of\s+)?)?"
    r"(?:(?:an?|the|my)\s+)?$",
    re.I,
)
_NEGATION = re.compile(
    r"\b(?:not|never|nobody|none|nothing|nor|neither|hardly|without|deny|denied)\b|n['’]t\b"
    r"|\bno\b(?!\s*[,.!;—–-])",  # not the answer's "No, I haven't held…"
    re.I,
)


def _negated_seat(clause: str) -> bool:
    """True when the clause before the title negates holding it, once: a
    second negation ("Nobody would say I wasn't the …") makes it a claim."""
    m = _NEGATED_SEAT.search(clause)
    if m is None:
        return False
    return not _NEGATION.search(clause[: m.start()])


def _claims_job_title(unit: str, start: int, end: int, target_company: str) -> bool:
    """True when the job's own title at unit[start:end] is claimed as a seat
    the candidate holds or held: "I was the Inside Sales Manager at
    Haulmark", "As Inside Sales Manager at Haulmark, I led…". The caller
    asks only for a sentence that makes a claim about the candidate (see
    _no_candidate_claim); naming the role sought never counts, and neither
    does an honest disclaimer ("I haven't held the Inside Sales Manager
    title, but…")."""
    clause = _CLAUSE_END.split(unit[:start])[-1]
    if _ASPIRE_BEFORE.search(clause) or _SOUGHT_AFTER.match(unit[end:]):
        return False
    if _negated_seat(clause):
        return False
    return _claimed_as_own(unit, start, end, target_company)


# A first-person sentence that claims experience, a skill, credentials or a
# background (sixth review of the self-review, 2026-10-09; Hard Rule 8). Each of
# these passed every other check, in a letter and in a drafted answer: "I would
# bring years of field service operations leadership.", "I can bring the trade
# credentials.", "I'd bring deep experience in warranty claims management.", "I
# have deep expertise in equipment financing.", "I am a licensed electrician." They
# have no number or proper noun to check, and a modal ("would bring") does not
# make one a plan: what the candidate would bring is what the candidate has.
# Such a sentence uses only words of the bullets the text cites, their employer
# and title included, or it fails (unsourced_experience_claim). It is marked by
# any of:
# - a form of "bring", or "I offer", "I contribute", "I lend" ("I'd bring",
#   "what I bring", "I can offer");
_BRING = re.compile(
    r"\bbr(?:ing|ings|inging|ought)\b"
    r"|\bI(?:['’](?:d|ll)|\s+(?:would|will|can|could|might|may|also|now|still))*"
    r"\s+(?:offer|contribute|lend)\b",
    re.I,
)
# - a word of experience, skill, credentials or background, anywhere in it;
_EXPERIENCE = re.compile(
    r"\b(?:experience[ds]?|expertise|experts?|background|knowledge(?:able)?|know-how"
    r"|track\s+record|skill(?:s|ed|set|sets)?|abilit(?:y|ies)|capabilit(?:y|ies)"
    r"|competen(?:ce|cy|cies|t)|credential(?:s|ed)?|certifi(?:ed|cate|cates|cation|cations)"
    r"|licen[cs](?:e|es|ed|ure)|degrees?|qualifi(?:ed|cation|cations)|proficien(?:t|cy)"
    r"|fluen(?:t|cy)|familiar(?:ity)?|versed|seasoned|veteran|speciali[sz](?:t|ts|ed|ation)"
    r"|specialt(?:y|ies)|mastery|trained|accredited|years|decades?)\b",
    re.I,
)
# - "I am a/an/the …" ("I'm a former HVAC technician");
_IDENTITY = re.compile(
    r"\bI(?:['’]m|\s+am)\s+(?:(?:also|now|still|really|truly|already)\s+)?(?:an?|the)\b",
    re.I,
)
# - a verb of having after a modal or future "I"/"we": "I'd have the trade
#   chops your team needs", "I'll draw on…", "I could lean on…" ("I'd have to
#   learn…" is not one); or a "my"/"our" that is not a plan (_possessive_claim)
#   in a sentence with a modal or a future: "I'd draw on my supplier network", "My
#   field service leadership would serve your team" (review 2026-10-09: the modal
#   frame made such a sentence read as a plan, not a claim).
_MODAL_HAVE = re.compile(
    r"\b(?:I|we)(?:['’](?:d|ll)|\s+(?:would|will|can|could|might|may|should|shall|must"
    r"|want|plan|hope|intend|aim|expect))"
    r"(?:\s+(?:also|still|now|already|certainly|definitely|really|surely|then|[a-z]+ly))*"
    r"(?:\s+(?:love|like|want|hope|plan|expect|aim|be\s+able|be\s+ready|get))?(?:\s+to)?"
    r"\s+(?:have(?!\s+to\b)|possess|draw\s+(?:on|upon|from)|lean\s+on|rely\s+on|tap\s+into"
    r"|leverage)\b",
    re.I,
)
# - a claim of knowing or understanding in a modal or future frame, or of
#   needing no ramp (second review 2026-10-09): "I'd know exactly how equipment
#   financing works", "I would already know the billing side", "I'll know
#   the billing side cold", "I'd come in knowing…", "I'd hit the ground
#   running", "I could run your licensing function on day one", "Nobody
#   would need to teach me warranty claims management" ("I'd love to know
#   more" and "I want to understand your goals" are none);
_KNOW = re.compile(
    r"\b(?:I|we)(?:['’](?:d|ll)|\s+(?:would|will|can|could|might|may|should|shall))"
    r"(?:\s+(?:also|still|now|already|certainly|definitely|really|surely|then|instantly"
    r"|immediately|[a-z]+ly))*(?:\s+be\s+able\s+to)?"
    r"\s+(?:know|understand|grasp|relate|empathi[sz]e|recogni[sz]e|anticipate)\b"
    r"|\bknowing\b|\balready\s+(?:know|knew|understand|speak)\b"
    r"|\bspeak(?:s|ing)?\s+(?:[\w'’-]+\s+){0,3}?(?:language|languages|lingo|dialect|jargon)\b"
    r"|\bhit\s+the\s+ground\s+running\b|\bturnkey\b|\bplug[- ]and[- ]play\b"
    r"|\b(?:no|zero|minimal|little|without\s+(?:a|any))\s+(?:[\w-]+\s+)?ramp(?:-up|\s+up|ing)?\b"
    r"|\b(?:on|from)\s+day\s+(?:one|1)\b|\bday[- ]one\b"
    r"|\bfrom\s+the\s+(?:start|get-go|first\s+day)\b"
    r"|\b(?:nobody|no\s+one|no-one|never|not|n['’]t)\b[^.;!?]{0,40}?\b(?:teach|train|show"
    r"|onboard|coach|ramp)\s+me\b",
    re.I,
)
# - a claim of ease, readiness, credibility or fit, in any frame: "I'd be
#   comfortable running equipment financing", "Equipment financing is second
#   nature to me", "…is in my wheelhouse", "I'm no stranger to…", "I could do
#   your licensing in my sleep", "I'd navigate equipment financing easily",
#   "I'd be productive immediately", "I'd need no onboarding", "Technicians
#   would trust me instantly", "Your customers would recognize me as a peer",
#   "Nothing about warranty claims management would surprise me", "I've been
#   there before", "Running HVAC crews is what I do", "This role needs a
#   tradesperson, and that's me", "I'm one" (second review 2026-10-09). Such
#   phrasings have no end; these are the common ones, and the rest are the
#   blind spot of the module docstring;
_COMPETENCE = re.compile(
    r"\b(?:comfortabl[ey]|at\s+home|at\s+ease|second\s+nature|wheelhouse|old\s+hat"
    r"|no\s+stranger|familiar\s+territory|home\s+turf|comfort\s+zone|in\s+my\s+element"
    r"|bread\s+and\s+butter|in\s+my\s+sleep|with\s+ease|easily|effortless(?:ly)?|seamlessly"
    r"|naturally|natural\s+fit|confidently|expertly|skill(?:ful)?ly|deftly|fluently|instinct\w*"
    r"|intuitive(?:ly)?|straightforward|easy\s+for\s+me|simple\s+for\s+me"
    r"|productive|effective|contribut\w*\s+(?:immediately|instantly|right\s+away|at\s+once)"
    r"|up\s+to\s+speed|credib(?:le|ility)|insider|one\s+of\s+(?:their|your|its)\s+own"
    r"|(?:been|done|seen)\s+(?:it|this|that|there)(?:\s+all)?\s+before|been\s+there"
    r"|from\s+the\s+inside|inside\s+out|first-?hand"
    r"|(?:without|no|zero|minimal|little)\s+(?:[\w-]+\s+)?(?:training|onboarding|hand-?holding"
    r"|learning\s+curve|supervision|guidance)"
    r"|(?:n['’]t|not|never)\s+(?:[\w-]+\s+){0,3}?(?:any\s+)?(?:training|onboarding|hand-?holding"
    r"|learning\s+curve|supervision)"
    r"|(?:on|from)\s+(?:the|my)\s+(?:first|very\s+first)\s+day"
    r"|(?:surprise|surprises|faze|fazes|intimidate|intimidates|daunt|daunts)\s+me"
    r"|no\s+surprises?"
    r"|(?:would|will|could|can|['’]d|['’]ll)\s+(?:[a-z]+ly\s+)?(?:trust|respect|recogni[sz]e"
    r"|accept|see|view|regard|follow|listen\s+to|relate\s+to)\s+me"
    r"|what\s+I(?:['’]ve|\s+have)?\s+(?:always\s+)?(?:do|did|done)"
    r"|what\s+I(?:['’]d|\s+would)\s+do\s+best"
    r"|the\s+(?:work|job|thing)s?\s+I(?:['’]ve|\s+have)?\s+(?:always\s+)?(?:do|did|done)"
    r"|that(?:['’]s|\s+is|\s+would\s+be|['’]d\s+be)\s+(?:exactly\s+)?me"
    r"|I(?:['’]m|\s+am)\s+(?:exactly\s+)?(?:one|it|that\s+(?:person|candidate|leader|profile))"
    r"|fit(?:s)?\s+the\s+bill|(?:check|tick)(?:s|ed)?\s+(?:that|every|each|all|the)\s+box(?:es)?"
    r"|meet(?:s)?\s+(?:every|each|all|the)\s+(?:requirement|qualification|criteri)\w*)\b",
    re.I,
)
# The third review of 2026-10-09 found more wording of the same claims, each
# with no word of the lists above, passing in letters and drafted answers:
# - possession brought along on arrival in a modal or future frame, and
#   "armed/equipped with": "I'd walk in with the trade chops your team
#   needs", "I'd show up with deep building-code compliance chops", "I'll come
#   armed with…" ("I'd start with the weekly revenue meeting" is a plan, and
#   "I'd love to come in and meet with your team" is no arrival with
#   anything);
_MODAL_FP = (
    r"\b(?:I|we)(?:['’](?:d|ll)|\s+(?:would|will|can|could|might|may|should|shall))"
    r"(?:\s+(?:also|still|now|already|certainly|definitely|really|surely|then|easily|instantly"
    r"|immediately|[a-z]+ly))*"
)
_ARRIVE_WITH = re.compile(
    _MODAL_FP
    + r"(?:\s+(?:love|like|want|hope|plan|expect|aim|be\s+(?:able|ready|happy|glad))\s+to)?"
    r"\s+(?:show(?:\s+up)?|turn\s+up|walk(?:\s+(?:in|into|through)(?:\s+(?:the|your)\s+door)?)?"
    r"|come(?:\s+(?:in|aboard|on\s+board|over|along))?|arrive|step\s+in|enter)"
    r"(?:\s+(?:[\w'’-]+\s+){0,3}?(?:already|ready|armed|equipped|loaded|fully|prepared))?"
    r"\s+with\b"
    r"|\b(?:armed|equipped|loaded|stocked)\s+with\b",
    re.I,
)
#   A forward courtesy is no possession, though (review 2026-10-12): "Choose a
#   slot that suits you, and I'll bring a short agenda." That is a modal or future
#   "I"/"we", then "come with", "bring", "prepare" or "have", then questions,
#   ideas, a plan, an agenda or notes (after an article or quantifier and up to
#   two plain adjectives of a short list), closing the sentence: nothing may
#   follow the noun. Only the matches of _ARRIVE_WITH, _BRING, _MODAL_HAVE and
#   _ABILITY inside one are set aside; every other marker is read in the whole
#   sentence. "I'll come with deep aviation experience", "I'd bring years of
#   field service leadership", "I'll come with a book of enterprise relationships"
#   and "I'll bring a short agenda and a book of …" stay claims. So does
#   any "about/on/for …" after the noun, which can carry a claim of its own
#   ("I'll come with ideas about aviation sales after running pilots", "I'll
#   bring notes on franchise licensing wins at Haulmark"): it is a false
#   rejection of "I'll have an agenda for the call", taken over a false
#   acceptance (Hard Rule 8);
_COURTESY_ADJ = (
    r"sharp|specific|pointed|concrete|good|real|honest|direct|hard|tough|practical"
    r"|thoughtful|detailed|clear|simple|short|rough|initial|early|fresh|open|candid|focused"
)
_COURTESY = re.compile(
    r"\b(?:I|we)(?:['’](?:d|ll)|\s+(?:would|will|can|could|shall))"
    r"(?:\s+(?:also|gladly|happily|then|certainly|definitely))*"
    r"(?:\s+(?:love|like|want|be\s+(?:happy|glad|ready))\s+to)?"
    r"\s+(?:come(?:\s+(?:in|along|over))?\s+with|bring(?:\s+along)?|prepare|have)"
    r"\s+(?:(?:a|an|the|some|a\s+few|a\s+couple\s+of|few|several|more)\s+)?"
    rf"(?:(?:{_COURTESY_ADJ})\s+){{0,2}}"
    r"(?:questions?|ideas?|plans?|agendas?|notes)\b(?![-'’])"
    r"(?=\s*(?:[.!?]+(?:\s|$)|$))",
    re.I,
)
#   …and it is set aside only when nothing else in its unit (_units) can hold a
#   claim (review 2026-10-12): the courtesy is the whole unit, or all that
#   comes before it is a short imperative scheduling clause and "and", "and
#   then", "then" or "so" ("Choose a slot, and …", "Send over a time that works
#   for you, and …"). That clause is a verb of _COURTESY_LEAD_VERBS and at most
#   nine words of _COURTESY_LEAD_WORDS, one of them a time or a meeting
#   (_COURTESY_LEAD_NOUNS): no capital past its first letter, no figure, no
#   first person, no "with", "having", "given" or "as". Set aside in any other
#   unit, the courtesy left the words before it unread, and these passed:
#   "With deep franchise relationships, I'll bring a short agenda.", "I know
#   casino procurement cold, and I'll bring a short agenda.", "I ran
#   franchise licensing at Haulmark, so I'll bring a short agenda." There the
#   courtesy is possession again, and every word of the unit is checked: a
#   false rejection of "Happy to talk, and I'll bring a few ideas." or "Send me
#   a time, and …", taken over a false acceptance (Hard Rule 8);
_COURTESY_LEAD_VERBS = _words(
    "pick choose name suggest propose send share book grab find schedule set"
)
_COURTESY_LEAD_NOUNS = _words(
    "time times day days date dates slot slots window windows call meeting chat conversation"
)
_COURTESY_LEAD_WORDS = _COURTESY_LEAD_NOUNS | _words(
    "a an the any some this next that up over along on in for works suits is good best "
    "convenient week morning afternoon you your"
)
_COURTESY_LEAD = re.compile(
    r"\W*(?P<clause>[A-Za-z][a-z]*(?:\s+[a-z]+){0,9}?)\s*,?\s+(?:and(?:\s+then)?|then|so)\s+"
)


def _bare_courtesy(unit: str, start: int, end: int) -> bool:
    """True when the forward courtesy at unit[start:end] (_COURTESY) is all the
    unit says, but for a scheduling lead-in before it (see above) and the
    sentence's closing mark after it."""
    if not re.fullmatch(r"[.!?]*\s*", unit[end:]):
        return False
    lead = unit[:start]
    if not lead.strip():
        return True
    m = _COURTESY_LEAD.fullmatch(lead)
    if m is None:
        return False
    verb, *rest = m.group("clause").lower().split()
    return (
        verb in _COURTESY_LEAD_VERBS
        and all(w in _COURTESY_LEAD_WORDS for w in rest)
        and any(w in _COURTESY_LEAD_NOUNS for w in rest)
    )


# - familiarity: "Equipment financing won't be new to me", "Licensing would
#   not be new to me", "None of this would be new to me", "Contractor billing is
#   nothing new for me", "I'm not new to equipment financing", "I've seen all
#   of this before", "I've seen it all" ("Construction is new to me" and "I
#   haven't seen this before" are honest disclaimers, and pass);
_FAMILIAR = re.compile(
    r"(?:\b(?:not|nothing|none|never|no\s+longer|hardly|scarcely)\b|n['’]t\b)[^.;!?]{0,40}?"
    r"\b(?:new|unfamiliar|foreign|alien|strange|unknown|novel|uncharted)(?:\s+[\w-]+){0,2}?"
    r"\s+(?:to|for)\s+(?:me|us)\b"
    r"|(?:\b(?:not|never|hardly)|n['’]t)\s+(?:be\s+)?(?:an?\s+)?(?:new|newcomer|novice|beginner"
    r"|rookie|green|newbie)\b"
    r"|\bno\s+(?:newcomer|novice|beginner|rookie|newbie)\b"
    r"|\bold\s+(?:news|ground|territory)\b"
    r"|\b(?:seen|done|heard|handled)\s+it\s+all\b",
    re.I,
)
_SEEN_BEFORE = re.compile(
    r"\b(?:seen|done|heard|handled|faced|solved|tackled|lived|been\s+through|dealt\s+with"
    r"|navigated|worked\s+through)\s+(?:[\w'’-]+\s+){0,4}?before\b",
    re.I,
)
_NEGATED = re.compile(r"\b(?:not|never|no|nothing|none|hardly)\b|n['’]t\b", re.I)


def _familiar(unit: str) -> bool:
    """True when `unit` claims familiarity (_FAMILIAR), or says the candidate
    has seen or done it before with no negation earlier in its clause."""
    if _FAMILIAR.search(unit):
        return True
    for m in _SEEN_BEFORE.finditer(unit):
        clause = _CLAUSE_SPLIT.split(unit[: m.start()])[-1]
        if not _NEGATED.search(clause):
            return True
    return False


# - an ability: "I/we can/could <verb>" says what the candidate is able to do
#   ("I can navigate building-code compliance", "I could teach anyone
#   trade licensing", "I can teach your team equipment financing"), except an
#   offer or availability ("I can help", "I can start…", "I can talk…");
_ABILITY = re.compile(
    r"\b(?:I|we)\s+(?:can|could)"
    r"(?:\s+(?:also|still|now|already|certainly|definitely|really|surely|then|easily|instantly"
    r"|immediately|[a-z]+ly))*(?:\s+be\s+able\s+to)?"
    r"\s+(?!(?:help|start|begin|talk|chat|meet|discuss|share|answer|connect|relocate|travel"
    r"|commute|see|imagine|be\s+reached)\b)[a-z]",
    re.I,
)
# - a skill verb after any modal or future: "I'd speak the technicians'
#   language", "I'll teach your reps equipment financing", "I'd navigate
#   trade licensing", "I'd love to train your team on it" ("I'd love to speak
#   with you" is a conversation, not a skill).
_SKILL_VERB = re.compile(
    _MODAL_FP
    + r"(?:\s+(?:love|like|want|hope|plan|expect|aim|be\s+(?:able|ready|happy|glad))\s+to)?"
    r"\s+(?:teach|train|navigate|translate|demystify|decode"
    r"|speak(?!\s+(?:with|to|about|further|more|soon|again|briefly)\b))\b",
    re.I,
)
# - "I'd be the/a/your <lowercase words>" with no role word in it: "I'd be
#   the electrician in the room", "I'd be the trade voice on your team"
#   ("I'd be a great Contoso rep" and "I'd be the Inside Sales Manager your
#   team needs" name the role sought; a capitalized title is checked as one);
_BE_THE = re.compile(
    r"\b(?:I|we)(?:['’](?:d|ll)|\s+(?:would|will|could|can|might|may|should))"
    r"(?:\s+(?:also|really|then|certainly|definitely))?\s+be\s+(?:the|an?|your)\s+"
    r"(?P<np>(?:(?-i:[a-z])[\w-]*\s+){0,3}?(?-i:[a-z])[\w-]*)\b"
    r"(?=\s*(?:$|[.,;:!?)—–]|(?:in|on|at|for|to|with|who|that|your|of|among|between)\b))",
    re.I,
)
# - "as a/an <lowercase noun>" said of the candidate, not of a thing ("I'd
#   stand out as a tradesperson", "…would see me as a peer"; "relaunched it as
#   a higher-touch service" is said of "it");
_AS_A_PREDICATE = re.compile(
    r"(?<!\bit)(?<!\bthem)(?<!\bthis)(?<!\bthat)(?<!\bthese)(?<!\bthose)(?<!\bone)"
    r"(?<!\bsomething)(?<!\banything)(?<!\beverything)"
    r"\s+as\s+an?\s+(?!(?:result|whole|team|group|rule|matter|first|second|third|next|final"
    r"|starting|way|means|bonus|priority|reminder|follow-up|token|courtesy|baseline|benchmark"
    r"|pilot|test|default|minimum|start|quick|brief|small|side|step|thank|thanks|candidate"
    r"|applicant|finalist|contender|reference|example|unit|package|practical|general|last"
    r"|aside)\b)(?-i:[a-z])[\w-]*",
    re.I,
)
# - an identity phrase: "As a former electrician, I'd…", "As an electrician
#   myself…", "…as a fellow tradesperson", "As someone who has run an
#   HVAC business…". That is "as a/an/someone …" opening the sentence before an
#   "I"/"we" with a lowercase word after the article (a capitalized title is
#   the job's title or a proper noun, checked as such: "As an Inside Sales
#   Manager, I'd build pipeline"), "as a/an" before a word of standing
#   ("former", "fellow", "longtime"…), "as a/an <noun> myself / by training",
#   or "as someone/somebody/one who". "Relaunched it as a higher-touch
#   service" and "as a first step" are none;
_AS_A = re.compile(
    r"^\W*as\s+(?:someone|somebody|one|a\s+person|an?\s+(?!(?:result|whole|team|group|rule"
    r"|matter|first|second|third|next|final|starting|way|means|bonus|priority|reminder"
    r"|follow-up|token|courtesy|baseline|benchmark|pilot|test|default|minimum|start|quick"
    r"|brief|small|side|step|thank|thanks|candidate|applicant|finalist|contender|reference"
    r"|example|unit|package|practical|general|last|aside)\b)(?-i:[a-z])[\w-]*)\b[^.;!?]*?"
    r"\b(?:I|we)(?:['’]\w+)?\b"
    r"|\bas\s+an?\s+(?:former|fellow|longtime|long-time|lifelong|onetime|one-time|recovering"
    r"|practicing|practising|licensed|certified|trained|seasoned|veteran|ex-?\w*|retired"
    r"|working|native|proud)\b"
    r"|\bas\s+an?\s+[\w-]+(?:\s+[\w-]+)?\s+(?:myself|by\s+(?:training|trade|background"
    r"|profession))\b"
    r"|\bas\s+(?:someone|somebody|one|a\s+person)\s+(?:who|whose|that)\b",
    re.I,
)
# - a past claim inside a modal or future clause, or an "I"/"we" in it that
#   is not forward: "I'd be the rare Director of Revenue Operations who has run a plumbing shop",
#   "As someone who has run an HVAC business, I'd…", "I'd thrive leading
#   technicians, as I always have". Clauses joined by "and"/"but" before an
#   "I" are judged one by one: "Fabrikam is larger than any business I
#   have worked in, and I would want to learn how contractors buy" has no modal in
#   its past clause.
_MODAL_SPLIT = re.compile(
    r"[;:]|(?<=[.!?])\s+|,?\s+\b(?:and|but|so|yet|while|whereas)\s+(?=(?:I|we|my|our)\b)"
)


def _modal_past(unit: str) -> bool:
    """True when a clause of `unit` has a modal or future and a first person,
    and also a past claim or an "I"/"we" that is not forward (see above)."""
    for part in _MODAL_SPLIT.split(unit):
        if not _FIRST_PERSON.search(part) or not _JD_PROMISE.search(part):
            continue
        spoken = _PERCEPTION.sub(" ", part)
        if _PERFECT.search(spoken) or _past_claim(spoken) or not _fp_subjects_forward(spoken):
            return True
    return False


# - with no first person, a hire the target would make: "You'd be hiring a
#   licensed electrician", "Fabrikam Payments would get a seasoned field
#   service operations leader", "You'd get a seasoned …", "Your team gets a leader
#   who…": a subject (you, your …, a name), then a modal and a verb of
#   getting or hiring (or "get"/"gain" alone), then a person or a word of
#   experience. "Contoso is hiring an Inside Sales Manager" is the
#   posting's own fact, not one.
_HIRE_VERB = re.compile(
    r"\b(?P<verb>get|gets|getting|gain|gains|gaining|hire|hires|hiring|land|lands|landing|add"
    r"|adds|adding|have|has|having|find|finds|finding|welcome|welcomes|welcoming|onboard"
    r"|onboards|onboarding|bring\s+on|bringing\s+on)\s+(?:in\s+me\s+)?"
    r"(?:an?|someone|somebody|one|me)\b",
    re.I,
)
_HIRE_SUBJECT = re.compile(
    r"(?:\b(?i:you|y['’]all)|\b(?i:your)\s+(?:[\w-]+\s+){0,2}?[\w-]+|(?<![\w'’])[A-Z][\w&.'’-]*)"
    r"(?P<aux>['’](?:d|ll|re)|\s+(?i:would|will|could|can|might|should|shall|are|is))?"
    r"(?:\s+(?i:also|then|now|really|certainly|definitely|surely|instantly|immediately))*"
    r"(?:\s+(?i:be))?\s+$"
)
_MODAL_AUX = frozenset({"'d", "'ll", "would", "will", "could", "can", "might", "should", "shall"})
_PERSON_NOUNS = _words(
    """leader leaders operator operators clinician clinicians nurse nurses therapist
    therapists practitioner practitioners physician physicians doctor doctors professional
    professionals pro pros veteran veterans expert experts specialist specialists closer
    closers seller sellers salesperson rep reps manager managers executive executives
    director directors coach coaches builder builders hire hires candidate candidates partner
    partners player players person someone somebody colleague colleagues teammate teammates
    strategist strategists consultant consultants analyst analysts engineer engineers founder
    founders generalist generalists insider insiders native natives hand hands mind minds
    me ae aes sdr sdrs head heads chief officer"""
)
# the hire claim's own frame needs no source
_HIRE_FRAME = frozenset(
    _root(w) for w in ("get", "gain", "hire", "land", "add", "find", "welcome", "onboard")
)


def _hire_claim(unit: str) -> bool:
    """True when `unit` says what the target would get in the candidate with
    no first person (see _HIRE_VERB)."""
    for m in _HIRE_VERB.finditer(unit):
        head = _CLAUSE_SPLIT.split(unit[: m.start()])[-1]
        sub = _HIRE_SUBJECT.search(head)
        if sub is None:
            continue
        aux = (sub.group("aux") or "").strip().lower().replace("’", "'")
        verb = m.group("verb").lower()
        if aux not in _MODAL_AUX and not verb.startswith(("get", "gain")):
            continue
        obj = re.findall(r"[A-Za-z][\w'’-]*", m.group() + " " + unit[m.end() : m.end() + 80])
        words = obj[1:8]
        if any(w.lower() in _PERSON_NOUNS for w in words) or _EXPERIENCE.search(" ".join(words)):
            return True
    return False


# The frame needs no source ("bring" is in no bullet); every other content word
# does, the words of experience themselves included ("years", "expertise").
_FRAME = frozenset(
    _root(w)
    for w in ("bring", "offer", "contribute", "lend", "possess", "draw", "lean", "rely", "tap")
) | {_root("leverage")}
# A claim needs something a cited bullet could source besides its own frame
# and markers (third review 2026-10-09): "None of this would be new to me"
# passed beside a bullet with "new-business", and "I can do this" has no
# content word at all. Such a claim is unbounded and fails whatever is cited;
# the markers below still need a source in a claim that names something
_CLAIM_MARKERS = _FRAME | frozenset(
    _root(w)
    for w in _words(
        """new unfamiliar foreign alien strange unknown novel uncharted newcomer novice
        beginner rookie green newbie territory ground news old seen see heard hear handled
        handle faced face solved solve tackled tackle lived live navigated navigate dealt deal
        worked work through walk show come arrive step enter door armed equipped loaded stocked
        teach train translate demystify decode speak language languages lingo dialect jargon"""
    )
)

# ---- a cited move restated with a neutral change verb (review 2026-10-10) ----
# "I trained the account managers to work every renewal from one Salesforce
# queue, and renewal rate climbed from 44% to 71%." used to fail on "climbed"
# alone: b_055 says "raising the renewal rate from ~44% to ~71%". In a claim
# of experience these verbs, and no others, need no cited word when their
# clause restates a move a cited bullet states:
# - the clause's subject is the move's own words in the bullet, all of them and
#   in their order, from the start of its clause to the move ("renewal
#   rate"; one leading verb of change may be left out: "raising",
#   "lifting", "Grew"). The move's words start after a clause mark, or after
#   by/through/via/with/for/while/of when a verb of change follows it
#   ("cutting CAC by lifting win rate from ~17% to ~26%" is a move of win
#   rate; "win rate for enterprise deals from …" is one of all five words).
#   Every word is compared as written, lowercased: two-letter words, stop
#   words and hyphenated parts included, none stemmed ("AI renewal
#   rate", "AI-only renewal rate" and "only renewal rate" are not "renewal
#   rate"). One leading "the", "our" or "its" may be added
#   or left out (_SUBJECT_DETERMINERS); nothing else;
# - no negation or modal stands between the subject and the verb ("never
#   climbed", "didn't climb", "would climb");
# - its figures are the move's own, in the bullet's order ("from 44% to 71%",
#   or "to 71%" alone), and a verb of increase needs the second to be larger;
# - nothing follows them up to the next punctuation mark or the end of the
#   sentence but a "+" or a period ("a month");
# - the sentence names no year and no span or point of time that is not a
#   figure's own rate (_TIME_WORD: "within a month, …", "Each month, …", "In
#   2022, …"; "55–70 requests a week" is a rate).
# Still failing: "win rate climbed from 44% to 71%", "credit-limit requests
# climbed from 44% to 71%", "rate climbed from 44% to 71%", "AI renewal
# rate climbed from 44% to 71%", "my renewal rate climbed…" (not the
# move's words), "CAC climbed from 17% to 26%" beside that CAC bullet, "…climbed from
# 71% to 44%", "…climbed from 50% to 95%", "…climbed from 44% to 71% in a
# week", "…that week", "…before Q3", "within a week, renewal rate
# climbed…", "onboarding time rose 40%" (no move), and any causative use ("I grew
# revenue from…": the candidate is the subject, and "grow" then needs a cited
# word like any other verb). The words around the verb are checked as before.
# Second review 2026-10-10: a subset of the six words before the move was
# enough of a subject, and a subordinator ended the words after the move.
# Third review 2026-10-10: the subject was compared on its content words of
# three letters or more, so "AI renewal rate climbed from 44% to 71%",
# "AE renewal rate…" and "AI-only renewal rate…" passed as b_055's
# move (and nothing else flags "AI" or "AE"); every word is compared now.
_CHANGE_VERB = re.compile(
    r"\b(?:climb(?:s|ed|ing)?|gr(?:ew|ow|ows|own|owing)|r(?:ose|ise|ises|isen|ising)"
    r"|increas(?:e|es|ed|ing)|improv(?:e|es|ed|ing)"
    r"|(?:went|go|goes|gone|going)(?=[^\S\n]+from\b))\b",
    re.I,
)
_INCREASE_VERB = re.compile(r"(?:climb|gr[eo]w|r[io]s|increas)", re.I)
# where a clause ends, read with every figure masked (a range's dash, "$1.3M")
_CHANGE_CLAUSE = re.compile(
    r"[;:!?()\[\]—–,]|\.(?!\w)"
    r"|\b(?:and|but|while|whereas|yet|so|as|because|after|before|when|which|who|that)\b",
    re.I,
)
# a subject that is a person or a pointer, not a metric: "I grew…", "it rose…";
# or one followed by a negation or a modal, which no bullet's move states
# ("renewal rate never climbed…", "…didn't climb…", "…would climb…")
_CHANGE_SUBJECT_NOT_METRIC = re.compile(
    r"\bI\b|\b(?i:we|you|it|they|he|she|this|that|these|those|me|us|them)\b"
    r"|\b(?i:not|never|no|nor|neither|hardly|barely|could|would|might|may|will|can|should"
    r"|shall|must|wo|ca)\b|n['’]t\b"
)
_CHANGE_FROM = re.compile(r"[^\S\n]+from[^\S\n]+[~≈]?")
_CHANGE_TO = re.compile(r"[^\S\n]+to[^\S\n]+[~≈]?")
_CHANGE_TAIL = re.compile(rf"\+?(?:[^\S\n]*(?:{_PERIOD_MARK.pattern}))?[^\S\n]*")
# where the words after a restated move end: a punctuation mark or the end of
# the sentence, never a word ("…to 71% that month", "…and did so that month")
_CHANGE_TAIL_END = re.compile(r"[;:!?()\[\]—–,]|\.(?!\w)")
# the verbs of change a move's words may open with ("raising the
# renewal rate", "Grew monthly revenue"), stemmed (_root). Not a verb that is also
# the head of a metric's name ("take rate", "drive time")
_MOVE_VERBS = frozenset(
    _root(w)
    for w in _words(
        """improve lift raise grow increase boost cut reduce decrease double triple
        quadruple expand climb rise"""
    )
)
# a preposition that may open a move's words, when a verb of change follows it:
# "cutting CAC by lifting win rate from ~17% to ~26%"
_MOVE_STOP = re.compile(
    r"\b(?:by|through|via|with|for|while|of)[^\S\n]+(?=(?P<verb>[A-Za-z]+)\b)", re.I
)


# a span or point of time: a restated move never sits in a sentence naming one
# that is no figure's own rate ("55–70 requests a week" is one), wherever it stands:
# "…, and within a week, renewal rate climbed from 44% to 71%", "In
# 2022, …" and "Each month, …" add a timeline the move does not state, and
# the bullet's own "month" or year would source their words
_TIME_WORD = re.compile(
    r"\b(?i:days?|weeks?|months?|quarters?|years?|overnight)\b"
    r"|\b(?:January|February|March|April|May|June|July|August|September|October"
    r"|November|December|Q[1-4]|H[12])\b"
)


def _timeline(unit: str) -> bool:
    """True when `unit` names a year, or a span or point of time that is no
    figure's own rate period (see _TIME_WORD)."""
    claims = numeric_claims(unit)
    if any(c.kind == "year" for c in claims):
        return True
    rates = [
        (ms, me)
        for ms, me, cls in _period_marks(unit)
        if any(
            c.kind != "year"
            and c.period == cls
            and bool(
                _AFTER_GAP.fullmatch(unit, c.end, ms) or _BEFORE_GAP.fullmatch(unit, me, c.start)
            )
            for c in claims
        )
    ]
    return any(not any(s <= t.start() < e for s, e in rates) for t in _TIME_WORD.finditer(unit))


# the only word a restated move's subject may open with that its bullet's words
# lack, or lack that its bullet's words open with: a determiner that adds no
# qualifier ("the renewal rate", "our renewal rate"). Not "my": it
# moves a team's metric onto the candidate ("my win rate climbed…" for "lifting
# win rate…" after a team restructure); a false rejection is acceptable
_SUBJECT_DETERMINERS = frozenset({"the", "our", "its"})


def _subject_words(text: str) -> tuple[str, ...]:
    """Every word of `text`, lowercased, in its order: letters and digits, any
    length, none dropped and none stemmed ("AI-only renewal rate" → ai,
    only, renewal, rate). A restated move's subject is compared on these
    (third review 2026-10-10): content words alone (terms()) drop "AI", "AE"
    and "only", and "AI renewal rate climbed…" passed as b_055's move."""
    return tuple(re.findall(r"[^\W_]+", text.lower()))


def _subject_key(words: tuple[str, ...]) -> tuple[str, ...]:
    """`words` less one leading determiner (_SUBJECT_DETERMINERS)."""
    return words[1:] if words[:1] and words[0] in _SUBJECT_DETERMINERS else words


def _masked_figures(text: str) -> str:
    """`text` with every figure blanked out ("0"), a range's dash included,
    so its offsets stay valid."""
    chars = list(text)
    claims = numeric_claims(text)
    for c in claims:
        chars[c.start : c.end] = "0" * (c.end - c.start)
    for a, b in zip(claims, claims[1:], strict=False):
        if a.range_keys is not None and a.range_keys == b.range_keys:
            chars[a.end : b.start] = "0" * (b.start - a.end)
    return "".join(chars)


def _figures(claims: Sequence[NumClaim]) -> list[list[NumClaim]]:
    """Claims grouped into figures: a range's two ends are one figure."""
    out: list[list[NumClaim]] = []
    for c in claims:
        last = out[-1] if out else []
        if (
            len(last) == 1
            and last[0].range_keys is not None
            and last[0].range_keys == c.range_keys
            and last[0].range_keys[1] == c.key
        ):
            last.append(c)
        else:
            out.append([c])
    return out


_Move = tuple[list[NumClaim], list[NumClaim], frozenset[tuple[str, ...]]]


def _moves(source_text: str) -> list[_Move]:
    """Every move a source states, one line at a time: (from figure, to
    figure, the subjects a restatement may give it). A subject is every word
    (_subject_words) from the start of the move's clause to its first figure
    but a last "from", or those less one leading verb of change (_MOVE_VERBS),
    each less one leading determiner (_subject_key); the clause starts after a
    clause mark, or after by/through/via/with/for/while/of when a verb of
    change follows it. "renewal rate ~44% → ~71%" and "raising
    the renewal rate from ~44% to ~71%" are moves of "renewal rate",
    "cutting CAC by lifting win rate from ~17% to ~26%" one of "win rate",
    "~$12K/week → $30–40K/week" one with no subject."""
    out: list[_Move] = []
    for line in source_text.split("\n"):
        figs = _figures([c for c in numeric_claims(line) if c.kind != "year"])
        masked = _masked_figures(line)
        for a, b in zip(figs, figs[1:], strict=False):
            between = re.sub(r"[~≈]", " ", mask_periods(line)[a[-1].end : b[0].start])
            if not _TRANSITION.fullmatch(between):
                continue
            cut = max((m.end() for m in _CHANGE_CLAUSE.finditer(masked, 0, a[0].start)), default=0)
            for m in _MOVE_STOP.finditer(masked, cut, a[0].start):
                if _root(m.group("verb").lower()) in _MOVE_VERBS:
                    cut = m.end()
            words = _subject_words(line[cut : a[0].start])
            if words[-1:] == ("from",):
                words = words[:-1]  # "from ~44% to ~71%": the move's own syntax
            subjects = {_subject_key(words)}
            if len(words) > 1 and _root(words[0]) in _MOVE_VERBS:
                subjects.add(_subject_key(words[1:]))
            out.append((a, b, frozenset(s for s in subjects if s)))
    return out


def _change_roots(unit: str, cited: Sequence[Source]) -> frozenset[str]:
    """The stems (_root) of the neutral change verbs in `unit` that restate a
    cited move (see _CHANGE_VERB), each only when every word of that stem in
    `unit` is such a verb: "I grew the team, and renewal rate grew from
    44% to 71%" excuses no "grew"."""
    moves = [mv for s in cited for mv in _moves(s.text)]
    if not moves or _timeline(unit):
        return frozenset()
    masked = _masked_figures(unit)
    claims = [c for c in numeric_claims(unit) if c.kind != "year"]
    spans: list[tuple[int, int]] = []
    for m in _CHANGE_VERB.finditer(unit):
        start = max((b.end() for b in _CHANGE_CLAUSE.finditer(masked, 0, m.start())), default=0)
        # the words after the move run to a punctuation mark, not to a
        # subordinator: "…to 71% that month" is checked whole
        stop = _CHANGE_TAIL_END.search(masked, m.end())
        end = stop.start() if stop else len(unit)
        subject = unit[start : m.start()]
        # every word of it, stop words and two-letter ones included: "AI
        # renewal rate" and "only renewal rate" are not b_055's move
        subject_seq = _subject_key(_subject_words(subject))
        if (
            not subject_seq
            or _CHANGE_SUBJECT_NOT_METRIC.search(subject)
            or any(start <= c.start < m.start() for c in claims)
        ):
            continue
        figs = _figures([c for c in claims if m.end() <= c.start and c.end <= end])
        x: list[NumClaim] | None = None
        if fm := _CHANGE_FROM.match(unit, m.end()):
            # "from X to Y", nothing else between
            if len(figs) < 2 or figs[0][0].start != fm.end():
                continue
            x, y = figs[0], figs[1]
            between = re.sub(r"[~≈]", " ", mask_periods(unit)[x[-1].end : y[0].start])
            if not _TRANSITION.fullmatch(between):
                continue
        elif tm := _CHANGE_TO.match(unit, m.end()):
            # "to Y"
            if not figs or figs[0][0].start != tm.end():
                continue
            y = figs[0]
        else:
            continue
        # nothing after Y up to the next punctuation mark but a "+" or its
        # period: "…to 71% in a month" and "…to 71% that month" add a fact
        # the move does not state
        if not _CHANGE_TAIL.fullmatch(unit, y[-1].end, end):
            continue
        for sx, sy, subjects in moves:
            if [c.key for c in sy] != [c.key for c in y]:
                continue
            if x is not None and [c.key for c in sx] != [c.key for c in x]:
                continue
            if subject_seq not in subjects:
                continue  # the move's own words, all of them, in order
            if _INCREASE_VERB.match(m.group()) and not (
                {c.kind for c in sx} == {c.kind for c in sy}
                and min(c.value for c in sy) > max(c.value for c in sx)
            ):
                continue
            spans.append((m.start(), m.end()))
            break
    roots = {_root(unit[s:e].lower()) for s, e in spans}
    for w in re.finditer(r"[A-Za-z]+", unit):
        if _root(w.group().lower()) in roots and not any(s <= w.start() < e for s, e in spans):
            roots.discard(_root(w.group().lower()))
    return frozenset(roots)


def _experience_claim(unit: str) -> bool:
    """True when `unit` is a first-person claim of experience, a skill,
    credentials or a background (see _BRING, _EXPERIENCE, _IDENTITY,
    _MODAL_HAVE, _KNOW, _COMPETENCE, _ARRIVE_WITH, _FAMILIAR, _ABILITY,
    _SKILL_VERB, _BE_THE, _AS_A, _AS_A_PREDICATE, _modal_past; a forward
    courtesy, _COURTESY, is no possession). Any
    I/my/me/we/our counts, a reaction's included: "I'd love to bring years of
    …" is a claim, and so is any modal or future sentence that says what the
    candidate has ("I would bring…", "I can bring…", "I'd have…", "my …
    would…", "I'd know…", "I'd walk in with…", "I can navigate…")."""
    if not _FIRST_PERSON.search(unit):
        return False
    # a reaction's own modal ("…, the reason I'd like to talk") frames no "my"
    spoken = _PERCEPTION.sub(" ", unit)
    # a forward courtesy ("I'll bring a short agenda") sets aside only the
    # possession markers inside it (_COURTESY), and only when nothing else in
    # the unit can hold a claim (_bare_courtesy)
    courtesy = [
        m.span() for m in _COURTESY.finditer(unit) if _bare_courtesy(unit, m.start(), m.end())
    ]

    def possesses(pattern: re.Pattern[str]) -> bool:
        return any(
            not any(s <= m.start() and m.end() <= e for s, e in courtesy)
            for m in pattern.finditer(unit)
        )

    return bool(
        possesses(_BRING)
        or _EXPERIENCE.search(unit)
        or _IDENTITY.search(unit)
        or possesses(_MODAL_HAVE)
        or (_possessive_claim(spoken) and _JD_PROMISE.search(spoken))
        or _KNOW.search(unit)
        or _COMPETENCE.search(unit)
        or possesses(_ARRIVE_WITH)
        or _familiar(unit)
        or possesses(_ABILITY)
        or _SKILL_VERB.search(unit)
        or _AS_A.search(unit)
        or _AS_A_PREDICATE.search(unit)
        or any(not (_words(m.group("np").lower()) & _SEAT_WORDS) for m in _BE_THE.finditer(unit))
        or _modal_past(unit)
    )


# A generic reason closing a claim is no part of it (review 2026-10-12):
# "I also built the channel-partner program for reseller partners, because a program only
# grows when the people running it can teach." failed on "running" and
# "teach", words of the reason, not of the claim. The words of a reason are
# left out of the claim's words (unsourced_experience_claim) only when it is
# "because a/an/every/any <noun> …" in lowercase, running to the end of its
# sentence, whose noun is a word of the cited bullets ("a program"), with no
# first person, no "you"/"your", no capital or digit (no "I", no name, no
# figure), no number word, no past tense, perfect or word of tenure, no
# mention of the target, and no marker of a claim of its own (bringing,
# arriving with, "as a …", experience, competence, knowing or familiarity).
# A clause with any first person is never one: "…, because a program I
# built only grows when …" and "…, because a program only grows when the
# people running it can teach, and I made sure mine could" stay claims. The rest of
# the sentence is still read whole, and every other check reads the reason
# too.
_BECAUSE_GENERIC = re.compile(r"\bbecause\s+(?:a|an|every|any)\s+(?P<noun>[a-z][a-z-]*)\b")
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")
_NUMBER_WORD = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen|twenty|thirty"
    r"|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|half|double"
    r"|triple|twice|thrice)\b"
)
_GENERIC_DENY = (
    _FIRST_PERSON,
    _YOU,
    _TENURE_WORD,
    _PERFECT,
    _PAST_ADVERB,
    _NUMBER_WORD,
    # a marker that makes a claim with no first person in it ("brings",
    # "armed with", "as a former …")
    _BRING,
    _ARRIVE_WITH,
    _AS_A,
    _AS_A_PREDICATE,
    _EXPERIENCE,
    _COMPETENCE,
    _KNOW,
    _FAMILIAR,
    _SEEN_BEFORE,
)


def _past_form(text: str) -> bool:
    """True when `text` has any past-looking word: a past form or a word in
    -ed of five letters or more (fail closed: "seasoned" counts)."""
    return any(
        w in _PAST_FORMS or (len(w) >= 5 and w.endswith("ed") and not w.endswith("eed"))
        for w, _pos in _tokens(text)
    )


def _generic_reasons(unit: str, sourced: Collection[str], target: str) -> list[tuple[int, int]]:
    """The spans of the generic reasons of `unit` (see above). `sourced` holds
    the cited bullets' content words (terms)."""
    spans: list[tuple[int, int]] = []
    low_target = target.strip().lower()
    for m in _BECAUSE_GENERIC.finditer(unit):
        end_m = _SENTENCE_END.search(unit, m.end())
        end = end_m.start() if end_m else len(unit)
        clause = unit[m.start() : end]
        if (
            re.search(r"[A-Z0-9]", clause)
            or numeric_claims(clause)
            or any(p.search(clause) for p in _GENERIC_DENY)
            or _past_claim(clause)
            or _past_form(clause)
            or (low_target and re.search(rf"\b{re.escape(low_target)}\b", clause))
            or _root(m.group("noun")) not in sourced
        ):
            continue
        spans.append((m.start(), end))
    return spans


# ---------------------------------------------------------------------------
# the target's own name that is also a tool (2026-10-15)
# ---------------------------------------------------------------------------
# A letter to a company whose name is also a tool failed unsourced_tool in
# every sentence that named the company. The examples here use Aircall and an
# invented posting (tests/test_claims_false_positives_1015.py): "I'm applying
# for the Director of Sales Enablement role at Aircall.", "I'd like to join
# Aircall.", "Aircall grew revenue 80% last year."
# Aircall is a dialer of questions.TOPICS, and so are Dialpad, Orum and
# other tools whose makers a seller may apply to. When the
# target IS such a tool (its name, less a legal or domain suffix, is the
# tool's whole name: _tool_is_target), each mention is excused only as the
# company applied to, never as a tool the candidate used, and only in a
# sentence target_tenure_cues finds nothing in:
# - the mention names the job sought or the target's people
#   (_names_seat_sought: "the role at Aircall", "the Aircall posting"), in any
#   sentence, unless its clause credits it with something (_seat_credits_target:
#   "The Aircall role is the reason.", "The role at Aircall grew…");
# - in a sentence with any I/me/my/we/our, the sentence only aspires (_aspires:
#   "I'd like to join Aircall", "I'm excited about what Aircall is building");
# - in a cover letter's sentence with none (a present reaction such as
#   "caught my eye" aside), the target is plainly its subject: it opens with the target's
#   name, names none of the candidate's employers nor the candidate, has no
#   word of tenure, history, pointing back, a person introduced or a promise
#   (_TENURE_WORD, _JD_HISTORY, _JD_POINTER, _ANAPHORA, _JD_PERSON,
#   _JD_PROMISE), no hire or experience claim, and says nothing happened
#   unless it restates the posting word for word (jd_sources): "Aircall makes a
#   cloud phone and dialer…", "Aircall grew revenue 80% last year." beside a
#   posting that says so.
# The excuse is withdrawn when any sentence of the text may point back at the
# target's product in a claim about the candidate or the candidate's employers
# (_handoff_pointer, validate_claims handoffs): "I'd like to join Aircall. I
# used it daily at Haulmark.", "…Haulmark reps lived in the product."
# Still unsourced_tool: "I used Aircall daily at Haulmark.", "At Haulmark my SDRs
# dialed with Aircall.", "Haulmark ran its outbound floor on Aircall.", "Aircall was
# the team's dialer for years.", "I'd love to keep using Aircall.", "Aircall is
# the dialer I know best.", and any tool that is not the target ("…and bring
# Orum's playbook"). Accepted false rejections, as before for every tool: a
# first-person description ("Aircall is solving a problem I care about"), and
# "Aircall helps reps hit their number" (a pronoun that may point back). A
# target named longer than the tool ("Apollo Global Management" for Apollo)
# is no tool: its "Apollo" still fails. A drafted answer whose question names
# the tool or asks about tools, use or experience ("Have you used Aircall?",
# "Which tools are you proficient in?") gets no exception at all: any mention
# of the target answers it. Any other drafted answer gets only the first two
# (the job sought, an aspiration), never the third: a sentence that opens with
# the target's name reads as the answer, whatever the question's words
# (third review of 2026-10-15: "What did your reps dial with?" "Aircall is the
# dialer modern floors run on."; "What's your outbound setup?" "Aircall, end
# to end."; "Have you been a customer?" "Aircall is the best on the market."
# each passed with no violation). Accepted false rejection: "Why do you want
# to join our team?" "Aircall makes a cloud phone and dialer…".
_TOOL_QUESTION = re.compile(
    r"\b(?:use|used|uses|using|usage|experience[ds]?|familiar\w*|proficien\w*|hands-on|tools?"
    r"|tooling|stack|software|platforms?|dialers?|work(?:ed|ing)?\s+with)\b",
    re.I,
)
_TARGET_NAME_SUFFIX = re.compile(
    r"(?:,?\s+(?:inc|llc|ltd|corp|corporation|co|labs|technologies|software|hq|ai)\.?"
    r"|\.(?:ai|io|com))+$",
    re.I,
)


# Another sentence may hand the target's product to the candidate without
# naming it: "I'd like to join Aircall. I used it daily at Haulmark." (second
# review of 2026-10-15: a check of only the next sentence, and only of a
# pronoun beside a first person, let "…join Aircall. I used the product daily at
# Haulmark.", "…join Aircall. Haulmark ran its floor on it.", "…join Aircall.
# Thanks. I used it daily at Haulmark." and "…At Haulmark I led 9 AEs. We used
# it daily." pass with no violation at all). So every excused mention is
# withdrawn when ANY sentence of the text, its own included, has
# - a word that may point back at the product (_handoff_pointer): a pronoun
#   ("it", "its", "they", "them", "their"; "IT" the department is no
#   pronoun); a word of the tool's own kind, with or without a determiner
#   ("the dialer", "an AI dialer", "the platform", "our tools", "that
#   software", "the workspace"; "the methodology" for a method); or a
#   product word after the/this/that/these/those/its/their/your ("the
#   product", "this product", "your solution", "the stack"), a modifier aside
#   ("the product team", "product-led"); a demonstrative with no noun after
#   it, or "the same" ("on this every day", "dialed on that.", "That's what
#   we ran", "This was our backbone", "the same thing"; _HANDOFF_DEMONSTRATIVE);
#   or a word of buying or holding it, bare or not ("a customer", "my reps
#   were customers", "seats", "the purchase", "our contract"; _HANDOFF_USER);
# - in a sentence about the candidate or the candidate's employers: one that
#   names or states a fact of an employer (employers_in: "Haulmark reps lived
#   in the product", "It cut time to first dispatch by ~40%") or the candidate, or
#   that has a first person or a "there" (not "there is") that does more than
#   aspire ("My team knows this product well", "The team there lived in it");
#   in a sentence that only aspires, a pronoun or product word a usage word
#   governs, or one inside a phrase about the subject that is not its head
#   ("I'd love to join Aircall after using it", "…as an advocate for the
#   platform"; _pointer_claims); or in a sentence with no first person, a
#   phrase that can only describe the candidate ("Longtime customer,
#   first-time applicant."; _described_claim). A phrase that describes the
#   candidate as the target's customer or user ("I'd love to join Aircall as one
#   of its earliest customers", "…, a power user of it since 2022") makes a
#   sentence no aspiration at all (_aspires; fourth review of 2026-10-15).
# Fail closed: "At Haulmark I grew the program and its revenue", "I coached
# reps until they hit their number" and "I built the tools the floor used"
# withdraw the excuse in a letter to Aircall. Each is a false rejection the
# letter is regenerated from, with the sentence and its word in the reason; a
# false acceptance puts a tool the candidate never used in a letter (Hard
# Rule 8). So do "At Haulmark I sold to our customers", "I learned that in
# enablement, adoption is everything" and "That's why I rebuilt onboarding
# at Haulmark" (third review of 2026-10-15: "…join Aircall. I was a
# customer.", "…I championed the purchase.", "…My reps dialed on this every
# day." and "…My floor ran on the same thing." passed with no violation).
# Not seen: a sentence that points back with none of these words ("Haulmark
# was on the vendor's side of the table", "That fueled growth at Haulmark": a
# verb off _SUBJECT_VERB's list before a bare noun); the claim judge reads
# those.
_HANDOFF_PRONOUN = re.compile(
    r"\b(?:[Ii]t|[Ii]ts|[Ii]tself|[Tt]hey|[Tt]hem|[Tt]heirs?|themselves)\b"
)
# a noun that is a modifier names something else: "the product team",
# "product-led", "the platform launch", "solutions engineer"
_HANDOFF_MODIFIED = (
    r"(?![-–]\w|\s+(?:team|teams|org|orgs|organization|marketing|manager|managers|management"
    r"|roadmap|roadmaps|leader|leaders|leadership|launch|launches|line|lines|side|function"
    r"|group|people|engineer|engineers|engineering|design|designer|designers|consultant"
    r"|consultants|architect|architects|owner|owners|sense|knowledge)\b)"
)
_HANDOFF_KIND_NOUNS: dict[str, str] = {
    "tool": r"dialers?|platforms?|software|apps?|tools?|tooling|workspaces?|sequencers?",
    "method": r"methodology|methodologies|frameworks?|curriculum|curricula|certifications?",
}
_HANDOFF_DEFINITE_NOUNS: dict[str, str] = {
    "tool": r"products?|solutions?|stack|suite|offerings?|technology|tech|systems?",
    "method": r"methods?|training|systems?|approach|programs?|playbooks?",
}
_HANDOFF_DETERMINER = r"\b(?:the|this|that|these|those|its|their|your)\s+(?:[\w'’-]+\s+){0,2}?"
# a word of buying or holding the product, bare or with a determiner or an
# adjective: "At Haulmark we were a customer", "my reps were customers", "I
# bought seats for my floor", "I championed the purchase", "our contract
# renewed". Only a hyphen compound is something else ("customer-facing",
# "subscription-based"), and a capitalized name the corpus itself
# writes ("Customer Success", "Client Services"; _handoff_pointer)
_HANDOFF_USER = re.compile(
    r"\b(?:customers?|clients?|users?|adopters?|subscribers?|subscriptions?|subscrib(?:e|ed|ing)"
    r"|seats?|licen[cs]es?|purchas(?:e|es|ed|ing|ers?)|bought(?!\s+in(?:to)?\b)|contracts?"
    r"|renewals?)\b(?![-–]\w)",
    re.I,
)
# A demonstrative with no noun after it is a pronoun (third review of
# 2026-10-15): it is followed by the end of the sentence, a mark or a
# contraction ("dialed on that.", "That's what we ran"), or by a word no noun
# phrase starts with (a preposition, a conjunction, a verb's particle,
# "every day"/"all week"), perhaps after an adverb ("on this every day", "on
# this daily.", "I've seen that firsthand at Haulmark", "I rolled this out").
# At the start of a sentence (or after ;:—) it is a pronoun before a verb of
# the lists below ("This was our backbone", "That ran our floor", "That
# powered our floor"). "The same" counts with no noun
# or a stand-in noun ("ran on the same thing", "I did the same at Haulmark"),
# as does a demonstrative before one ("that thing", "this one"). A
# determiner before any other noun is not one ("that gap", "this role";
# a product noun has its own list above), nor is a "that" before a subject
# ("I know that we…", "the floor that I ran").
_DEMONSTRATIVE = r"(?:this|that|these|those)"
_STAND_IN = r"(?:thing|things|one|ones|stuff)"
_NO_NOUN_AFTER = (
    r"(?:\s+(?:[a-z]+ly|firsthand|first-hand|myself|ourselves|personally|too|also|again|still"
    r"|together|alone|hands-on))*"
    r"(?:\s*(?:$|[.,;:!?)\]\"”'’—–])"
    r"|\s+(?:at|in|on|with|for|from|to|into|onto|over|across|through|throughout|during|before"
    r"|after|since|until|by|via|without|within|and|or|but|so|because|when|while|as|if|than"
    # a verb's particle: "I rolled this out", "I set that up"
    r"|out|up|off|down|back|around|away|aside|along)\b(?![-–]\w)"
    # "on this every day", not "that every rep"
    r"|\s+(?:every|each|all)\s+(?:single\s+)?(?:day|week|month|quarter|year|morning|night"
    r"|shift|time)s?\b)"
)
_SUBJECT_VERB = (
    r"(?:is|was|are|were|has|had|have|will|would|can|could|did|does|do|may|might|must|should"
    r"|became|become|becomes|remains?|remained|stays?|stayed|seems?|seemed|ran|runs|made|makes"
    r"|got|gets|gave|gives|kept|keeps|took|takes|drove|drives|built|builds|led|leads|held|holds"
    r"|brought|brings|grew|grows|won|wins|paid|pays|meant|means|let|lets|sold|sells|bought"
    r"|helps|powers|matters|works|lives|saves|shows|changes|carries|carried"
    # a regular past before an object, a preposition or a mark: "That powered our
    # floor", "That doubled my reps' dials" ("That seasoned team" is no verb)
    r"|[a-z]+ed(?=\s*(?:$|[.,;:!?—–]|\s(?:the|a|an|my|our|their|its|his|her|your|every|each"
    r"|all|me|us|them|it|everything|everyone|what|how|why|[a-z]+ly|at|in|on|with|for|to|from"
    r"|into|across|through|up|out|off|down)\b)))\b"
)
_HANDOFF_DEMONSTRATIVE = re.compile(
    rf"\b{_DEMONSTRATIVE}\b(?={_NO_NOUN_AFTER})"
    rf"|(?:^|[.!?;:—–]\s*)[\"'“‘(]*\s*(?:(?:and|but|so|yet)\s+)?{_DEMONSTRATIVE}\s+{_SUBJECT_VERB}"
    rf"|\bthe\s+same\b(?:\s+(?:{_STAND_IN}|tools?|products?|platforms?|dialers?|software"
    rf"|systems?|stack)\b|(?={_NO_NOUN_AFTER}))"
    rf"|\b{_DEMONSTRATIVE}\s+{_STAND_IN}\b",
    re.I,
)
_HANDOFF_NOUN_PATTERNS: dict[str, re.Pattern[str]] = {
    kind: re.compile(
        rf"\b(?:{_HANDOFF_KIND_NOUNS[kind]})\b{_HANDOFF_MODIFIED}"
        rf"|{_HANDOFF_DETERMINER}(?:{_HANDOFF_DEFINITE_NOUNS[kind]})\b{_HANDOFF_MODIFIED}",
        re.I,
    )
    for kind in _HANDOFF_KIND_NOUNS
}
# "there" as a place pointed back at ("the team there"), not "there is/was"
_HANDOFF_THERE = re.compile(
    r"\b[Tt]here\b(?!['’]s|\s+(?:is|are|was|were|will|would|has|have|had|isn|aren|wasn"
    r"|weren|may|might|must|could|should|can|seems?|seemed|remains?)\b)"
)


_NEXT_CAPITALIZED = re.compile(r"\s+[A-Z][\w'’&.-]*")


def _sourced_name(unit: str, m: re.Match[str], sourced: str) -> bool:
    """True when the capitalized word at `m` begins a name the corpus itself
    writes, case and all ("Customer Success", "Client Services")."""
    if not m.group()[:1].isupper():
        return False
    nxt = _NEXT_CAPITALIZED.match(unit, m.end())
    if nxt is None:
        return False
    phrase = unit[m.start() : nxt.end()].rstrip(".")
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", sourced) is not None


def _handoff_matches(unit: str, kind: str, sourced: str = "") -> list[tuple[re.Match[str], str]]:
    """Every word or phrase of `unit` that may point back at the target's
    product, a tool or method of `kind`, without naming it (see above), in
    order, each with what it is: "pronoun", "noun" (a product word),
    "demonstrative" or "user" (a word of buying or holding). `sourced` is the
    corpus's own text: a word of buying that begins a name it writes is no
    pointer."""
    found: list[tuple[re.Match[str], str]] = []
    noun = _HANDOFF_NOUN_PATTERNS.get(kind)
    for p, tag in (
        (_HANDOFF_PRONOUN, "pronoun"),
        (noun, "noun"),
        (_HANDOFF_DEMONSTRATIVE, "demonstrative"),
    ):
        if p is not None:
            found += [(m, tag) for m in p.finditer(unit)]
    found += [
        (m, "user") for m in _HANDOFF_USER.finditer(unit) if not _sourced_name(unit, m, sourced)
    ]
    return sorted(found, key=lambda f: f[0].start())


def _pointer_word(m: re.Match[str]) -> str:
    """A pointer's text as the violation quotes it."""
    return m.group().strip(" .!?;:—–\"'“‘(")


def _handoff_pointer(unit: str, kind: str, sourced: str = "") -> str:
    """The first word or phrase of `unit` that may point back at the target's
    product, a tool or method of `kind` (_handoff_matches); "" when there is
    none."""
    found = _handoff_matches(unit, kind, sourced)
    return _pointer_word(found[0][0]) if found else ""


# What makes a mention of the target's name the object of an aspiration in
# its clause: "join Aircall", "work at/for Aircall", "the team at Aircall", "apply
# to Aircall", "be part of Aircall", "excited about what Aircall is building", "come
# over to Aircall" (review 2026-10-12: the aspiration used to excuse every
# mention in an aspiring sentence).
_ASPIRED_OBJECT = re.compile(
    r"(?:\bjoin(?:s|ing)?|\bat|\bfor|\b(?:apply|applies|applying)\s+(?:to|at|with)"
    r"|\bpart\s+of|\b(?:about|in)\s+what|\b(?:come|coming|move|moving)\s+(?:over\s+)?to"
    r"|\binterest(?:ed)?\s+in|\b(?:excited|passionate|curious)\s+(?:about|by)"
    r"|\bbelieve\s+in|\bdrawn\s+to)"
    r"\s+(?:the\s+(?:team|people|folks)\s+at\s+)?$",
    re.I,
)
# A usage word right before the target's name, or a product word right after
# it: the mention is the tool, never the company ("using Aircall", "rolled
# Aircall out", "three years on Aircall", "dialing with Aircall", "in Aircall",
# "Outreach sequences").
_USED_WORDS = (
    r"\b(?:use|uses|used|using|run|runs|ran|running|roll|rolls|rolled|rolling|deploy\w*"
    r"|implement\w*|adopt\w*|migrat\w*|switch\w*|dial\w*|call\w*|live|lives|lived|living"
    r"|operat\w*|administ\w*|configur\w*|onboard\w*|train\w*|master\w*|learn\w*|know"
    r"|knew|via|through|inside|within|on|in|with|onto|into|from|after|off)"
)
_USED_BEFORE = re.compile(rf"{_USED_WORDS}\s+(?:(?:the|a|an|my|our|their|his|her)\s+)?$", re.I)
# (after the bare name only: "Aircall's dialer" is the company's product, a
# reaction to it is judged as before; "Aircall sequences" are a user's)
_USED_AFTER = re.compile(
    r"^\s+(?:out\b|(?:sequences?|sequencing|cadences?|dialers?|dialing|floors?"
    r"|licen[cs]es?|seats?|instances?|integrations?|workflows?|playbooks?|reports?"
    r"|dashboards?|accounts?\s+(?:I|we)\b)\b)",
    re.I,
)


def _used_as_tool(unit: str, start: int, end: int) -> bool:
    """True when the target's name at unit[start:end] is governed by a usage
    word before it or a product word after it (_USED_BEFORE, _USED_AFTER)."""
    head = unit[:start]
    used = _USED_BEFORE.search(head) and not _ASPIRED_OBJECT.search(head)
    return bool(used or _USED_AFTER.match(unit[end:]))


# A pointer in a sentence that only aspires (fourth review of 2026-10-15). The
# aspiration used to excuse every pointer in its sentence, so a phrase about
# the candidate beside it handed the product over: "I'd love to join Aircall as
# one of its earliest customers", "I'd like to join Aircall, a power user of it
# since 2022", "I'd love to join Aircall, a longtime admin of the platform"
# passed. A phrase whose head is a word of holding, or that has a pronoun as
# its preposition's object, makes the sentence no aspiration at all
# (_described_claim, _aspires). Each other pointer is judged on its own, as
# a mention of the target's name is (_used_as_tool): a pronoun ("it",
# "them") or a product word ("the platform", "the dialer") still hands the
# product over when
# - a usage word governs it ("after using it", "after years on it", "(on it
#   since 2022)", "live in the platform"; _USED_WORDS, as for the name), or
# - it sits inside an "as"/"being" phrase, an appositive or a fragment after
#   a dash, a colon, a semicolon or a parenthesis (_described_spans: "as an
#   advocate for the platform", "as a platform expert"), unless it is that
#   phrase's own head, the target itself ("Aircall, the dialer modern floors
#   run on", "as its platform matures"), or the phrase is an appositive that
#   names a company ("Aircall, a team building the dialer every floor needs").
# A possessive ("its", "their"), a demonstrative and a word of buying are
# judged by the sentence alone (_aspires): "I'd like to join Aircall and help
# scale its revenue team", "…help its customers ramp", "I'd love to build on
# that". What still passes: "I'd like to join Aircall and help it grow", "I
# hope to be part of Aircall as it grows", "I want to fix it". Fail closed, as
# for the name: "I'd love to build on it", "…grow with it", "…help your reps
# live in the platform" withdraw the excuse (accepted false rejections).
_POINTER_USED_BEFORE = re.compile(
    rf"{_USED_WORDS}\s+(?:(?:the|a|an|its|their|this|that|these|those|your|my|our)\s+"
    r"(?:[\w'’-]+\s+){0,2}?)?$",
    re.I,
)
_POSSESSIVE_POINTER = frozenset({"its", "their", "theirs"})
# the phrase's own head: a determiner and capitalized words before it…
_PHRASE_HEAD_LEAD = re.compile(r"(?:(?:the|its|their)\s+)?(?:(?-i:[A-Z])[\w'’-]*\s+){0,2}", re.I)
# …and, after it, the phrase's end or a word that does not continue the noun
_PHRASE_HEAD_FOLLOW = re.compile(
    r"\s*(?:$|[,;:.!?)\]—–])"
    r"|\s+(?:behind|that|which|who|for|from|of|by|with|used|trusted|built|chosen|loved"
    r"|every|modern|so|is|are|was|were|['’]s|grows?|scales?|matures?|evolves?|expands?"
    r"|keeps?|continues?|moves?|takes?|reaches?|enters?|helps?|powers?)\b",
    re.I,
)
# an appositive whose head is a company: "a team building…", "the company
# behind…"
_NAMES_A_COMPANY = re.compile(
    r"(?:an?|the|one\s+of\s+the)\s+(?:[\w'’-]+\s+){0,2}?(?:company|companies|team|startup|lab"
    r"|business|org|organi[sz]ation|firm|leader|pioneer|maker|builder|vendor|provider)\b",
    re.I,
)


def _pointer_claims(unit: str, m: re.Match[str], tag: str) -> bool:
    """True when the pointer `m` (tagged by _handoff_matches) still hands the
    target's product to the candidate in a sentence that only aspires (see
    above): a pronoun or a product word that a usage word governs, or that
    sits inside a phrase describing the subject without being its head."""
    if tag not in ("pronoun", "noun") or _pointer_word(m).lower() in _POSSESSIVE_POINTER:
        return False
    if _POINTER_USED_BEFORE.search(unit[: m.start()]):
        return True
    for _start, body, end, form in _described_spans(unit):
        # (a whole sentence that opens like such a phrase is judged by
        # _described_claim alone: "One of my goals is to help it grow.")
        if form != "fragment" and body <= m.start() < end:
            lead = unit[body : m.start()]
            head = _PHRASE_HEAD_LEAD.fullmatch(lead) and _PHRASE_HEAD_FOLLOW.match(unit, m.end())
            # an appositive that names a company is the target's, not the
            # candidate's ("Aircall, a team building the dialer every floor
            # needs"); an "as"/"being" phrase is always the subject's
            company = form == "appositive" and _NAMES_A_COMPANY.match(unit, body)
            return not (head or company)
    return False


# past tenses spelled like the present, after the target's name: _past_claim
# cannot tell "Aircall cut ramp time" from a present (review 2026-10-12)
_SAME_SPELLED_PAST = re.compile(
    r"^(?:['’]s\s+[\w-]+\s+)?\s*(?:cut|put|set|hit|let|spread|shut|quit|cost|beat|split"
    r"|shed|bet|burst|cast|hurt|upset|read|forecast|broadcast|reset)\b",
    re.I,
)

# A sentence that opens with the target can credit the target's product with
# what another sentence states (review 2026-10-15, round 3): "At Haulmark I grew
# surcharge revenue from ~$12K/week to $30–40K/week. Aircall is a big part of
# why." passed, as did "Aircall is the reason.", "Aircall deserves the
# credit.", "Aircall is how." and "Aircall is a large part of the story."
# employer_fact and the number checks read one sentence, and handoffs() looks
# for a pointer in a claim about the candidate, never in the target's own
# sentence. A word of cause or credit points past its sentence at a result,
# whoever's it is. Fail closed: a sentence about the target that does not
# restate the posting (jd_sources) excuses the mention only when what follows
# the name says something of its own (_own_predicate). Accepted false
# rejections: "Aircall is how modern floors connect.", "Aircall is hiring."
#
# Round 4: _own_predicate was a list of credit words, and any rest with two
# content words and none of them passed, so credit worded any other way did
# too: "Aircall is the real catalyst.", "Aircall is the driving force.", "Aircall
# played a big role.", "Aircall fuels that kind of growth.", "Aircall earns all
# the kudos." No list of words can name every way to give credit, so it is
# now an allowlist (_own_predicate): only the posting's own words, or a
# present reaction ("Aircall's mission resonates with me"). The credit words
# below and the pointers stay as vetoes, for a posting whose own vocabulary
# has them ("the engine of our growth").
_CREDIT_ROLE = (
    r"(?:play(?:s|ed|ing)?|had|has|have|having)\s+(?:[\w'’-]+\s+){0,3}?roles?"
    r"|(?:big|bigger|biggest|major|key|large|larger|huge|central|important|critical|pivotal"
    r"|significant|vital|leading|starring|crucial|outsized|meaningful|real|core|main|primary"
    r"|decisive|active|direct|supporting)\s+roles?"
    r"|roles?\s+(?:in|behind)"
)
_TARGET_CREDIT = re.compile(
    r"\b(?:why|how|because|reasons?|credit(?:s|ed|ing)?|secrets?|behind|thanks?"
    r"|thank(?:ed|ing)|owe[sd]?|owing|part\s+of|story|stories|caus(?:e|es|ed|ing)"
    r"|responsib\w*|deserv(?:e|es|ed|ing)|due\s+to|explain(?:s|ed|ing)?|attribut\w*"
    r"|difference"
    # round 4 ("role" only as credit: "Aircall's Director of Sales Enablement role
    # caught my eye" names the job sought)
    rf"|{_CREDIT_ROLE}|factors?|contribut\w*|instrumental|driv(?:e|es|en|er|ers|ing)|drove"
    r"|fuel\w*|power(?:s|ed)?\s+behind|engines?|catalysts?|ingredients?|levers?"
    r"|hero(?:es)?|kudos|applause|earn(?:s|ed|ing)?|sources?|forces?)\b",
    re.I,
)
# words that point at another sentence even with a noun after them ("the
# source of those gains", "that kind of growth", "such growth", "growth like
# this", "helps here")
_TARGET_POINTER = re.compile(
    r"\b(?:this|that|these|those|such|here|there|thereof|therein|above|aforementioned"
    r"|said|same|likewise|similar(?:ly)?)\b",
    re.I,
)
# words that say nothing of their own after the target's name: "Aircall is the
# one.", "Aircall gets the credit.", "Aircall's …"
_LIGHT_PREDICATE = _FUNCTION | _words(
    """s am are be been being do does did done get gets got gotten getting make makes
    made making very really truly quite"""
)
_BARE_IT = re.compile(r"\bit\b", re.I)
# a present reaction to the target or one of its own things, and nothing else:
# "Aircall caught my eye.", "Aircall's AI dialer caught my eye.", "Aircall's
# mission resonates with me."
_REACTION_ONLY = re.compile(
    r"^\s*(?:['’]s?\s+(?:[\w'’-]+\s+){1,6}?)?(?:[a-z]+ly\s+)?"
    rf"(?:{_TARGET_REACTION.pattern})\s*[.!]*\s*$",
    re.I,
)


def _own_predicate(rest: str, posting: Collection[str]) -> bool:
    """True when `rest`, what follows the target's name in a sentence that
    opens with it, says something of its own and credits the target with
    nothing: no word of cause or credit (_TARGET_CREDIT), no "it" and no
    word that points at another sentence (_HANDOFF_DEMONSTRATIVE,
    _TARGET_POINTER: "Aircall made that possible", "Aircall fuels that kind of
    growth"), and then either a present reaction and nothing else
    (_REACTION_ONLY: "Aircall caught my eye", "Aircall's mission resonates with
    me") or at least two content words, every one of them the posting's own
    (`posting`: its words, their stems, the company and the job title):
    "Aircall makes a cloud phone and dialer for sales and support teams". An allowlist:
    "Aircall is the real catalyst.", "Aircall played a big role." and "Aircall is
    key." fail (review 2026-10-15, round 4)."""
    if _TARGET_CREDIT.search(rest) or _BARE_IT.search(rest):
        return False
    if _HANDOFF_DEMONSTRATIVE.search(rest) or _TARGET_POINTER.search(rest):
        return False
    if _REACTION_ONLY.match(rest):
        return True
    words = [
        w
        for w in re.findall(r"[a-z][a-z'’-]*", _TARGET_REACTION.sub(" ", rest).lower())
        if w not in _LIGHT_PREDICATE
    ]
    return len(words) >= 2 and all(w in posting or _stem(w) in posting for w in words)


# a relative clause after the job sought makes it that clause's subject: "I
# saw the Aircall posting, which grew…"
_RELATIVE_AFTER = re.compile(r"\b(?:which|who|whose)\b", re.I)


def _np_governor(words: Sequence[str]) -> int:
    """The index in `words` (what stands before a noun in its clause, the
    noun excluded) of the word that governs the noun's phrase, as _governed
    reads it; -1 when none does: the phrase opens the clause, its subject."""
    i = len(words) - 1
    while i >= 0 and (words[i].lower() in _NP_WORDS or (i > 0 and words[i][0].isupper())):
        i -= 1
    return i


def _seat_credits_target(unit: str, start: int, end: int) -> bool:
    """True when the target's name at unit[start:end] names the job sought or
    the target's people (_names_seat_sought, tool=True) and the clause credits
    it with something all the same, however the clause opens:
    - a word of cause or credit, "it", or a word that points at another
      sentence, after the name ("Aircall's role in that growth is clear.",
      "The Aircall role is the reason.", "The role at Aircall deserves the
      credit.", "I'm applying for the Aircall role because it drove…");
    - with the job sought the clause's subject (nothing governs it) or
      before "which"/"who", something that happened (a present reaction
      aside) or a number: "The Aircall role grew the weekend-pickup surcharge
      to $30–40K/week." A job sought did nothing; "The Aircall posting caught
      my eye." passes.
    An opening prepositional phrase ("For the Director of Sales Enablement
    role at Aircall, the first project would be…") ends at its comma: what
    follows has its own subject, and only an "it" there, which may be the
    target, fails. Such a mention is judged as any other about the target, not excused as
    naming the job sought (review 2026-10-15, round 4). Review 2026-10-20:
    the clause was read only when the name opened it, so "The Aircall process
    is the reason." and "The role at Aircall is the reason." passed."""
    c_start, c_end = _clause_bounds(unit, start, end)
    before, after = unit[c_start:start], unit[end:c_end]
    if m := _PREP_BEFORE.search(before):
        lead = re.findall(r"[A-Za-z][\w'’-]*", before[: m.start()])[:-1]  # the noun excluded
        noun_end = 0
    elif m := _SOUGHT_AFTER_TOOL_NAME.match(after):
        lead = re.findall(r"[A-Za-z][\w'’-]*", before)
        noun_end = m.end()
    else:
        return False
    gov = _np_governor(lead)
    main = ""
    if gov == 0 and lead[0].lower() in _PREP_GOVERNORS and "," in after[noun_end:]:
        cut = after.index(",", noun_end)
        after, main = after[:cut], after[cut + 1 :]
    rest = after[noun_end:]
    if _BARE_IT.search(main) or _TARGET_CREDIT.search(after) or _BARE_IT.search(rest):
        return True
    if _HANDOFF_DEMONSTRATIVE.search(rest) or _TARGET_POINTER.search(rest):
        return True
    if gov >= 0:
        rel = _RELATIVE_AFTER.search(rest)
        if rel is None:
            return False
        rest = rest[rel.end() :]
    return _past_claim(_TARGET_REACTION.sub(" ", rest)) or bool(numeric_claims(rest))


def _tool_is_target(topic: Topic, target_company: str) -> bool:
    """True when the tool `topic` is the target company itself: the target's
    name, or that name less a legal or domain suffix ("Aircall, Inc.",
    "Orum Labs", "Dialpad.ai"), is the tool's whole name."""
    name = target_company.strip()
    if not name:
        return False
    return any(topic.pattern.fullmatch(n) for n in {name, _TARGET_NAME_SUFFIX.sub("", name)})


def validate_claims(
    text: str,
    cited_ids: Iterable[object],
    corpus: dict[str, Any],
    *,
    target_company: str,
    job_title: str = "",
    jd_text: str = "",
    allowed_ids: Iterable[str] | None = None,
    known_companies: Iterable[str] = (),
    estimate_style: str | None = None,
    question: str = "",
) -> ProvenanceResult:
    """Check a cover letter or drafted answer against the sources it cites.
    Fail closed: every violation is fatal for the text. `estimate_style` None
    means the configured one (validate/style.py). `question` is the form
    question a drafted answer answers: one that asks whether the candidate
    worked or works at the target (asks_target_tenure) is answered only with
    a plain "No" unless the corpus lists the target as a role company."""
    style = estimate_styles.resolve(estimate_style)
    reg = source_registry(corpus)
    allowed = set(allowed_ids) if allowed_ids is not None else set(reg)
    violations: list[dict[str, str]] = []

    def bad(kind: str, detail: str, unit: str = "") -> None:
        violations.append({"kind": kind, "detail": detail, "sentence": unit[:160]})

    cited: list[Source] = []
    for raw in cited_ids:
        sid = str(raw).strip()
        if sid not in reg:
            bad("unknown_bullet_id", sid)
        elif sid not in allowed:
            bad("bullet_not_provided", sid)
        elif reg[sid] not in cited:
            cited.append(reg[sid])

    emps = employers(corpus)
    cited_emps = {s.employer for s in cited if s.employer}
    src_claims = {s.id: numeric_claims(s.text) for s in cited}
    src_vocab = {s.id: text_vocab(f"{s.text} {s.employer or ''} {s.title}") for s in cited}
    cited_text = "\n".join(s.text for s in cited)
    # every word an experience claim may use (_experience_claim): the cited
    # bullets' own, with their employer and title, and the claim's frame
    cited_terms = terms(" ".join(f"{s.text} {s.employer or ''} {s.title}" for s in cited))
    claim_terms = cited_terms | _FRAME
    cited_years = {y for s in cited for y in s.years}
    skills = skill_index_keys(corpus)
    skills_vocab = text_vocab(" ".join(skills))
    target_vocab = text_vocab(f"{target_company} {job_title}")
    target_keys = claim_keys(f"{target_company} {job_title}")
    ident = str((corpus.get("identity") or {}).get("name") or "").strip()
    ident_vocab = text_vocab(ident)
    employer_vocab = text_vocab(" ".join(emps))
    jd_vocab = text_vocab(jd_text)
    jd_stems = {_stem(w) for w in jd_vocab}  # "Account Associate" for the JD's "Account Associates"
    # every word a sentence about the target may use to say something of its own
    # (_own_predicate): the posting's, and the company's and job title's
    posting_vocab = frozenset(jd_vocab | jd_stems | target_vocab)
    jd_keys = claim_keys(jd_text)
    # what a sentence borrowing the JD's facts may say: a run of one of these
    jd_runs = [f" {' '.join(_jd_tokens(s))} " for s in _SENTENCE.split(jd_text)]
    corpus_vocab = _corpus_vocab(corpus)
    corpus_raw = _walk_text({k: v for k, v in corpus.items() if k != "constraints"})
    ordinary = set(re.findall(r"\b[a-z][a-z'’-]*\b", f"{corpus_raw} {text} {jd_text}")) | _FUNCTION
    title_owners: dict[str, set[str]] = {}
    for r in corpus.get("roles", []):
        for w in text_vocab(str(r.get("title") or "")):
            title_owners.setdefault(w, set()).add(str(r.get("company") or ""))
    # every role's title, word for word: a hyphenated title word's parts are
    # sourced only inside one of these (_held_title)
    held_titles = [
        words
        for words in (_title_words(str(r.get("title") or "")) for r in corpus.get("roles", []))
        if words
    ]
    distinctive = _distinctive_terms(corpus)
    distinctive_bigrams = _distinctive_bigrams(corpus)
    excluded_terms = (
        _content_terms(jd_text)
        | _content_terms(f"{target_company} {job_title}")
        | _content_terms(" ".join(emps))
    )
    excluded_bigrams = (
        _phrase_bigrams(jd_text)
        | _phrase_bigrams(f"{target_company} {job_title}")
        | _phrase_bigrams(" ".join(emps))
    )
    # words of the never-claim titles: the job title never sources them in a
    # sentence about one of the candidate's employers ("I was the VP of Sales
    # at Haulmark" while applying for VP of Sales — review of wp2)
    never_title_vocab = text_vocab(" ".join(constraints(corpus)["never_claim_titles"]))
    # the job title's own words, less the company's and the never-claim titles'
    # (those have their own check)
    job_title_vocab = text_vocab(job_title) - text_vocab(target_company) - never_title_vocab
    known = {_norm_name(c): c for c in known_companies if len(_norm_name(c)) >= 3}
    excused_companies = _norm_name(
        " | ".join([target_company, *emps, jd_text, corpus_raw, " ".join(skills)])
    )
    never_tools = [t.lower() for t in constraints(corpus)["never_claim_tools"]]
    schools = [str(e.get("school") or "") for e in corpus.get("education", []) or []]
    # the candidate's own employers and schools. Not the target: it may be
    # named only as where the candidate wants to work (target_tenure_cues),
    # unless the corpus lists it as a role company (review 2026-10-09)
    own_orgs = [o for o in (_norm_name(x) for x in [*emps, *schools]) if o]
    target_norm = _norm_name(target_company)
    target_is_employer = bool(target_norm) and any(
        _contains_name(target_norm, o) or _contains_name(o, target_norm)
        for o in (_norm_name(e) for e in emps)
        if o
    )
    skill_norms = {_norm_name(k) for k in skills}
    # the lowercase words of the corpus and the JD, the target's own spelling
    # left out: a lowercase target that is one of them ("pilot" for Pilot) is
    # judged only after a seat cue (target_tenure_cues)
    plain_src = f"{corpus_raw}\n{jd_text}"
    if target_company.strip():
        plain_src = plain_src.replace(target_company.strip(), " ")
    plain_words = set(re.findall(r"\b[a-z][a-z0-9]*\b", plain_src))
    target_ordinary = frozenset(plain_words | {_stem(w) for w in plain_words})
    # a cited bullet names the target as a client: "accounts at Nakatomi"
    # (never a skill_index tool, as for any other company: "deals at Salesforce")
    target_client = (
        bool(target_norm)
        and target_norm not in skill_norms
        and any(all(w in src_vocab[s.id] for w in target_norm.split()) for s in cited)
    )
    # the tools and methods of the questions' vocabulary that are not a
    # skill_index key: named anywhere, they are unsourced (below)
    unindexed_tools = [t for t in TOPICS if t.kind in SKILL_KINDS and not in_skill_index(t, corpus)]
    # …less, mention by mention, the one that IS the target, named as the
    # company applied to ("Aircall" for a Aircall job: target_tool_ok)
    # A drafted answer is read with its question: to "Have you used Aircall?" or
    # "Which tools have you used?" any mention of Aircall answers it, so there
    # is no exception then (_TOOL_QUESTION). Any non-empty `question`, even
    # whitespace, marks a drafted answer (callers pass "<label>\n<question>")
    answer = bool(question)
    target_tools = frozenset(
        t.label
        for t in unindexed_tools
        if target_norm
        and not target_is_employer
        and _tool_is_target(t, target_company)
        and not (question and (t.pattern.search(question) or _TOOL_QUESTION.search(question)))
    )
    # what makes a capital at the start of a sentence a name (see _CAP_RUN).
    # The source's whole-word lowercase spellings serve only the
    # department-style shape ("People teams" beside "people"), and only for a
    # word on the bundled list: a lowercase spelling never makes a word common
    # (review 2026-10-10, below)
    source_lowercase = {
        w for w in re.findall(r"(?<![A-Za-z])[a-z]+(?![A-Za-z])", plain_src) if is_common_word(w)
    }
    prose_names, prose_phrases = _prose_names(
        [*_strings({k: v for k, v in corpus.items() if k != "constraints"}), jd_text],
        source_lowercase,
    )
    text_names = _capitalized_elsewhere([text])
    name_keys = (
        {
            key
            for name in [target_company, *emps, *schools, *skills, *never_tools]
            for part in re.split(r"[,;()|]", name)
            if (key := _norm_name(part))
        }
        | set(known)
        | prose_phrases
    )
    skill_topics = [t for t in TOPICS if t.kind in SKILL_KINDS]

    def initial_name(word: str, unit: str, pos: int) -> bool:
        """True when `word`, capitalized at the start of a sentence at
        unit[pos], is a name rather than a common word (see _CAP_RUN)."""
        low = word.lower()
        if low in _FUNCTION:
            return False
        if re.search(r"[A-Z0-9]", word[1:]):
            return True  # "AI", "HubSpot", "B2B"
        # common ONLY by the bundled list (review 2026-10-10): a word the JD or
        # the corpus also writes in lowercase somewhere is not common for it
        # ("brightwave.io" or a lowercase product name in a posting must not
        # excuse "Brightwave hired me…")
        if not is_common_word(word):
            return True  # "Brightwave", "Contoso"
        if low in text_names or low in prose_names:
            return True
        head = f"{_norm_name(unit[pos : pos + 120])} "
        if any(head.startswith(f"{key} ") for key in name_keys):
            return True  # "Pilot programs…" for a DB company Pilot, "Union College"
        return any(t.pattern.match(unit, pos) for t in skill_topics)

    def evidence(fragment: str) -> set[str]:
        """Employers whose own vocabulary the fragment uses: two terms found
        in only that employer's bullets, or one such phrase ("usage-based pricing")."""
        counts: dict[str, int] = {}
        for t in _content_terms(fragment) - excluded_terms:
            if t in distinctive:
                counts[distinctive[t]] = counts.get(distinctive[t], 0) + 1
        for bg in _phrase_bigrams(fragment) - excluded_bigrams:
            if bg in distinctive_bigrams:
                counts[distinctive_bigrams[bg]] = counts.get(distinctive_bigrams[bg], 0) + 2
        return {e for e, n in counts.items() if n >= 2}

    def employers_in(fragment: str) -> set[str]:
        """Employers a fragment names or states a fact of: a number or proper
        noun only that employer's cited bullets contain, or its vocabulary."""
        found = {e for e in emps if _names_in(fragment, e)}
        for c in numeric_claims(fragment):
            owners = {
                s.employer
                for s in cited
                if s.employer and any(sc.key == c.key for sc in src_claims[s.id])
            }
            if len(owners) == 1:
                found |= owners
        for word, _pos in _proper_parts(fragment):
            low = word.lower()
            if word in _PROPER_ALLOW or word in _LETTER_ALLOW or low in _FUNCTION:
                continue
            if low in employer_vocab or low in ident_vocab or low in skills_vocab:
                continue
            owners = {s.employer for s in cited if s.employer and low in src_vocab[s.id]}
            if len(owners) == 1:
                found |= owners
        return found | evidence(fragment)

    def target_opening(unit: str) -> tuple[str, bool] | None:
        """(what follows, opened with the target's name) when the sentence
        opens with the target: its name ("Fabrikam Payments doubled…", "Fabrikam
        grew…", "Contoso's Account Associates book…"), "you"/"your" or "the
        role"/"the posting". None otherwise; "Contoso-style teams…" does not
        open with it."""
        name = target_company.strip()
        head = unit.lstrip(" \t\"'“‘(")
        for p in _name_patterns(name) if name else []:
            m = p.match(head)
            if m and head[m.end() : m.end() + 1] != "-":
                return head[m.end() :], True
        if m := _TARGET_OPENING.match(head):
            return head[m.end() :], False
        return None

    def jd_sources(unit: str, named: set[str]) -> bool:
        """True when the JD's own numbers and names may back this sentence:
        the target is plainly its subject (see _JD_POINTER and the rules
        above it) and it restates the posting word for word (_jd_tokens). A
        sentence that says something happened opens with the target's name:
        "Fabrikam Payments doubled…", never "Your team doubled…". One that names
        the candidate ("Contoso gets <name>, booking…") is about the
        candidate. Any I/my/me/we/our counts, a reaction's too: "Your Account
        Associates caught my eye as a former peer" and "…, which matters to
        me" are about the candidate (fifth review of validator-refine)."""
        opening = target_opening(unit)
        if opening is None or _FIRST_PERSON.search(unit) or named:
            return False
        if ident and _names_in(unit, ident):
            return False
        rest, by_name = opening
        if not by_name and _past_claim(unit):
            return False
        # "Contoso's Account Associates book…": the possessive is the opening's
        said = " ".join(_jd_tokens(re.sub(r"^['’]s\b", "", rest)))
        if not said or not any(f" {said} " in run for run in jd_runs):
            return False
        return not (
            _JD_POINTER.search(rest)
            or _ANAPHORA.search(rest)
            or _JD_PERSON.search(rest)
            or _JD_PROMISE.search(rest)
            or _JD_HISTORY.search(rest)
        )

    def target_tool_ok(unit: str, start: int, end: int, named: set[str], jd_ok: bool) -> bool:
        """True when the mention at unit[start:end] of a tool that IS the
        target names the company applied to, not a tool the candidate used
        (see _tool_is_target). The caller asks only for a unit in which
        target_tenure_cues found nothing.

        Each mention is judged on its own (the caller needs every one to
        pass). A mention a usage word governs ("using Aircall", "rolling Aircall
        out", "three years on Aircall", "Outreach sequences") is the tool,
        whatever else the sentence says. In a first-person sentence, the
        aspiration excuses only a mention that is itself its object ("join
        Aircall", "work at Aircall", "excited about what Aircall is building") in
        a clause that aspires, and only in a sentence that states none of the
        candidate's own facts (employer_fact): review 2026-10-12, "I'd like
        to join your team after using Aircall at Haulmark" passed because the
        sentence aspired somewhere."""
        if _used_as_tool(unit, start, end):
            return False  # "after using Aircall at Haulmark", "running Outreach sequences"
        if _names_seat_sought(unit, start, end, tool=True) and not _seat_credits_target(
            unit, start, end
        ):
            return True  # "the role at Aircall", "the Aircall posting"
        if _FIRST_PERSON.search(unit):
            c_start, c_end = _clause_bounds(unit, start, end)
            clause = unit[c_start:c_end]
            if (
                _ASPIRED_OBJECT.search(unit[c_start:start])
                and _aspires(_aspiration_scope(clause, target_company, target_ordinary))
                and not employer_fact(unit)
            ):
                return True  # "I'd like to join Aircall"
            if _FIRST_PERSON.search(_TARGET_REACTION.sub(" ", unit)):
                return False  # "I used Aircall daily at Haulmark"
        # a drafted answer: a sentence about the target reads as the answer
        # ("What did your reps dial with?" "Aircall is what modern floors run
        # on."), so only the job sought and an aspiration name it (see
        # _TOOL_QUESTION)
        if answer:
            return False
        # the target plainly the subject ("Aircall makes a cloud phone and dialer…"), a
        # present reaction aside ("Aircall's AI dialer caught my eye")
        opening = target_opening(unit)
        if opening is None or not opening[1] or named:
            return False
        if ident and _names_in(unit, ident):
            return False
        if _TENURE_WORD.search(unit) or _hire_claim(unit) or _experience_claim(unit):
            return False
        rest = opening[0]
        if (
            _JD_POINTER.search(rest)
            or _ANAPHORA.search(rest)
            or _JD_PERSON.search(rest)
            or _JD_PROMISE.search(rest)
            or _JD_HISTORY.search(rest)
        ):
            return False
        # the target credited with what another sentence states ("At Haulmark
        # I grew the program… Aircall is a big part of why.", "Aircall is the
        # driving force."): only the posting's words, or a present reaction,
        # with no word of cause or credit and nothing that points elsewhere
        # (_own_predicate; review 2026-10-15, rounds 3 and 4)
        if not jd_ok and not _own_predicate(rest, posting_vocab):
            return False
        # the candidate's own facts credited to the target's product ("Aircall
        # is how 9 AEs hit quota", "Aircall drives the weekend-pickup
        # surcharge's $30–40K/week"; review 2026-10-12): a cited bullet's
        # number or an employer's vocabulary, or any number the posting does
        # not state
        if employer_fact(unit):
            return False
        if any(
            not (c.key in target_keys or (jd_ok and c.key in jd_keys)) for c in numeric_claims(unit)
        ):
            return False
        # a past tense spelled like the present ("Aircall cut ramp time for new
        # reps", "Aircall put the floor on autopilot"): only the posting's words
        if not jd_ok and _SAME_SPELLED_PAST.match(rest):
            return False
        # something happened ("Aircall grew revenue 80%…"): only the posting's own words
        return jd_ok or not _past_claim(_TARGET_REACTION.sub(" ", rest))

    def employer_fact(fragment: str) -> bool:
        """True when a fragment names one of the candidate's employers or
        states a fact of one: a number a cited bullet states, or an
        employer's own vocabulary (evidence). Unlike employers_in, a capital
        that only starts the sentence ("Growth like that…") is no name."""
        if any(_names_in(fragment, e) for e in emps):
            return True
        keys = {c.key for s in cited for c in src_claims[s.id]}
        return any(c.key in keys for c in numeric_claims(fragment)) or bool(evidence(fragment))

    handoff_cache: dict[str, list[tuple[str, str]]] = {}

    def handoffs(kind: str) -> list[tuple[str, str]]:
        """(sentence, word) for every sentence of the text that may point back
        at the target's product, a tool or method of `kind`, in a claim about
        the candidate or the candidate's employers (_handoff_pointer and the
        rules above it). Any one withdraws every excused mention."""
        if kind not in handoff_cache:
            found: list[tuple[str, str]] = []
            for u in units:
                pointers = _handoff_matches(u, kind, corpus_raw)
                if not pointers:
                    continue
                word = _pointer_word(pointers[0][0])
                spoken = _TARGET_REACTION.sub(" ", u)
                if employer_fact(u) or (ident and _names_in(u, ident)):
                    found.append((u, word))
                elif _FIRST_PERSON.search(spoken) or _HANDOFF_THERE.search(spoken):
                    if not _aspires(u):
                        found.append((u, word))
                    # a sentence that only aspires: each pointer on its own
                    # (_pointer_claims; fourth review of 2026-10-15)
                    elif claimed := [m for m, tag in pointers if _pointer_claims(u, m, tag)]:
                        found.append((u, _pointer_word(claimed[0])))
                elif _described_claim(u):
                    # no first person, but a phrase that can only describe
                    # the candidate: "Longtime customer, first-time applicant."
                    found.append((u, word))
            handoff_cache[kind] = found
        return handoff_cache[kind]

    units = _units(text)
    candidate_facts = any(_names_in(text, e) for e in emps) or any(
        _FIRST_PERSON.search(u) and numeric_claims(u) for u in units
    )
    if candidate_facts and not cited:
        bad("missing_citations", "the text states candidate facts but cites no source ids")

    unit_named: list[set[str]] = []
    unit_facts: list[set[str]] = []
    cross_flagged: set[int] = set()
    for ui, unit in enumerate(units):
        first_person = bool(_FIRST_PERSON.search(_PERCEPTION.sub(" ", unit)))
        named = {e for e in emps if _names_in(unit, e)}
        pinned: list[tuple[str, set[str]]] = []  # (what, employers it belongs to)
        # The JD's own numbers and names are usable only in a sentence whose
        # subject is plainly the target: not first person (review of wp2: "At
        # Haulmark I saw revenue double" borrowed the JD's "doubled"), naming
        # none of the candidate's employers, opening with the target's name,
        # and pointing at no one else (fourth review of validator-refine:
        # "Booking 35 walkthroughs a week with a team like your Account Associates
        # was the job" borrowed the JD's "35"), and restating the posting
        # word for word (fifth review: "…, an old habit"). No sentence
        # with I/my/me/we/our borrows a JD word, a reaction's included, not
        # even for the target's own people.
        jd_ok = jd_sources(unit, named)
        # a sentence that makes no claim about the candidate never claims the
        # job's title as held: "As an Inside Sales Manager, I'd…"
        fwd_safe = _no_candidate_claim(unit, named)
        job_spans = target_job_spans(unit, corpus, job_title, target_company)

        for e in sorted(named - cited_emps):
            if first_person or not cited:
                bad("uncited_employer", f"{e} is named but none of its bullets is cited", unit)

        # ---- numbers, with units ----
        unit_periods = text_periods(unit)
        period_hits: list[tuple[str, str]] = []
        for c in numeric_claims(unit):
            srcs = [s for s in cited if any(sc.key == c.key for sc in src_claims[s.id])]
            if not srcs:
                if c.kind == "year" and int(c.value) in cited_years:
                    continue
                if c.key in target_keys or (jd_ok and c.key in jd_keys):
                    continue  # a fact about the employer, from its own posting
                bad("fabricated_number", f"{c.describe()} is in no cited source", unit)
                continue
            occ = [sc for s in srcs for sc in src_claims[s.id] if sc.key == c.key]
            # a rate keeps its period in every style: "$1.8M a month" is not
            # "$1.8M a year" (review of render-confident)
            if hit := period_mismatch(c, occ, unit_periods):
                if hit not in period_hits:  # a range's two ends are one figure
                    period_hits.append(hit)
                    bad(hit[0], f"{hit[1]} ({srcs[0].id})", unit)
                pinned.append((c.describe(), {s.employer for s in srcs if s.employer}))
                continue
            if style == estimate_styles.HEDGED:
                estimate = all(s.confidence == "estimate" for s in srcs)
                hedged = c.hedged or bool(_LETTER_HEDGE.search(unit[: c.start]))
                if (estimate or all(o.hedged for o in occ)) and not hedged:
                    bad(
                        "unhedged_estimate" if estimate else "dropped_hedge",
                        f"{c.describe()} is hedged in its source ({srcs[0].id}); keep the hedge",
                        unit,
                    )
            elif hit := figure_mismatch(c, [o for o in occ if states_period(c, o, unit_periods)]):
                bad(hit[0], f"{hit[1]} ({srcs[0].id})", unit)
            pinned.append((c.describe(), {s.employer for s in srcs if s.employer}))

        # ---- proper nouns ----
        claimed_runs: set[tuple[int, int]] = set()
        for word, pos in _proper_parts(unit):
            low = word.lower()
            if word in _PROPER_ALLOW or word in _LETTER_ALLOW or word.startswith(("I'", "I’")):
                continue
            if _sentence_initial(unit, pos) and (
                _initial_contraction(unit, pos) or not initial_name(word, unit, pos)
            ):
                continue  # capitalized only because it starts the sentence
            if low in employer_vocab or low in ident_vocab or low in skills_vocab:
                continue
            # a word of the job's own title, in a sentence naming none of the
            # candidate's employers, is about the job — it pins nothing
            about_job = low in target_vocab and not named
            owners = [s for s in cited if low in src_vocab[s.id]]
            if owners:
                if not about_job:
                    pinned.append((word, {s.employer for s in owners if s.employer}))
                continue
            if low in title_owners:  # a title the candidate held — at THAT employer
                if not about_job:
                    pinned.append((word, title_owners[low]))
                continue
            # a part of a hyphenated title word a corpus role holds ("Mid" of
            # "Mid-Market"), inside a run that is that held title — at THAT
            # employer, as above. Any other run leaves the part unsourced
            title_run = ""
            compound = _compound_at(unit, pos)
            if compound != low and compound in title_owners:
                title_run = _title_run(unit, pos)
                if _held_title(title_run, held_titles):
                    if not about_job:
                        pinned.append((word, title_owners[compound]))
                    continue
            # the job's own title words are excused — but a never-claim title
            # word in a sentence naming a candidate employer only where it
            # names the target's job ("the VP of Sales role at Oscorp")
            job_word = low in target_vocab and (
                not (named and low in never_title_vocab) or any(s <= pos < e for s, e in job_spans)
            )
            if job_word and low in job_title_vocab and not fwd_safe:
                # …as the job, never as the candidate's own seat, in a sentence
                # that makes a claim about the candidate: "I was the Inside
                # Sales Manager at Haulmark". "I want to be an Inside Sales
                # Manager" and "As an Inside Sales Manager, I'd…" name the
                # role sought (never-claim titles have their own check)
                run_s, run_e = _capitalized_run(unit, pos)
                if _claims_job_title(unit, run_s, run_e, target_company):
                    if (run_s, run_e) not in claimed_runs:
                        claimed_runs.add((run_s, run_e))
                        bad(
                            "claimed_job_title",
                            f"{unit[run_s:run_e]!r} is the job's own title, claimed as the "
                            "candidate's; name it only as the job ('your …', 'the … role')",
                            unit,
                        )
                    continue
            if job_word or (jd_ok and low in jd_vocab):
                continue
            if title_run:
                where = f"part of {title_run!r}, a title no corpus role holds"
            elif low in corpus_vocab:
                where = "in the corpus but in no cited source"
            elif _stem(low) in jd_stems:
                where = (
                    "from the job description: use it only in a sentence about "
                    f"{target_company or 'the company'} itself that opens with its name "
                    "and uses only the posting's own words, with no I/my/me/we/our"
                )
            else:
                where = "unsourced"
            bad("unsourced_proper_noun", f"{word} ({where})", unit)

        # ---- attribution: facts must belong to the employer(s) named ----
        if named:
            for what, owners_emp in pinned:
                if owners_emp and not owners_emp & named:
                    bad(
                        "misattributed_claim",
                        f"{what} belongs to {sorted(owners_emp)}, sentence names {sorted(named)}",
                        unit,
                    )
        facts = evidence(unit) | {e for _, o in pinned if len(o) == 1 for e in o}
        unnamed = facts - named
        if len(facts | named) >= 2 and unnamed:
            cross_flagged.add(ui)
            bad(
                "cross_role_attribution",
                f"mixes facts from {sorted(facts | named)} but names only "
                f"{sorted(named) or 'no employer'}",
                unit,
            )
        unit_named.append(named)
        unit_facts.append(facts)

        # ---- the wrong company ----
        for company in _company_mentions(unit, known, excused_companies, ordinary):
            bad("wrong_company", f"names {company}; the target is {target_company}", unit)

        # ---- a fabricated employer: a tool/client/JD company named as where
        # the candidate worked ("Account Executive at Salesforce") ----
        seen_emp: set[str] = set()
        target_cues: list[_Cue] = []
        for cue in _employment_cues(unit, first_person):
            key = _norm_name(cue.name)
            if not key:
                continue
            if any(_contains_name(key, o) or _contains_name(o, key) for o in own_orgs):
                continue  # the candidate's employer or school
            words = key.split()
            client = cue.client_ok and key not in skill_norms
            if client and any(all(w in src_vocab[s.id] for w in words) for s in cited):
                continue  # "accounts at Nakatomi": a client a cited bullet names
            if target_norm and (
                _contains_name(key, target_norm) or _contains_name(target_norm, key)
            ):
                target_cues.append(cue)  # judged below; the JD never sources it
                continue
            if key in seen_emp:
                continue
            seen_emp.add(key)
            if client and all(w in jd_vocab for w in words):
                continue  # "accounts at Nakatomi": a client the JD names
            bad(
                "fabricated_employer",
                f"{cue.name} is named as where the candidate worked ({cue.cue!r}); the "
                f"candidate's employers are {emps}",
                unit,
            )
        # …and the target named as where the candidate worked or works
        # ("I previously worked at Contoso"): it may be named only as where the
        # candidate wants to work, unless it is one of the corpus's role
        # companies (review 2026-10-09)
        tenure: list[str] = []
        if target_norm and not target_is_employer:
            tenure = target_tenure_cues(
                unit, target_company, target_cues, target_ordinary, target_client
            )
            if tenure:
                bad(
                    "fabricated_employer",
                    f"{target_company} is the company applied to, named as where the "
                    f"candidate worked or works ({', '.join(repr(t) for t in tenure)}); name "
                    f"it only as where the candidate wants to work. In a sentence with "
                    f"I/my/me/we/our, name it or 'you'/'your' only to say what the candidate "
                    f'wants or would do ("I\'d love to join your team") or to name the role '
                    f'sought ("I saw your posting"); say anything else about the company in a '
                    f"sentence with no I/my/me/we/our. The candidate's employers are {emps}",
                    unit,
                )

        # ---- never-claim constraints ----
        # the JD sources a title mention only where it sources numbers: in a
        # sentence with no first person ("I'll partner with your Director of Sales"
        # needs a cited bullet, as at aca26cc)
        for v in never_claim_violations(
            unit,
            corpus,
            source_text=cited_text + ("\n" + jd_text if jd_ok else ""),
            ref="text",
            job_title=job_title,
            target_company=target_company,
            candidate_context=bool(named or facts),
            first_person=first_person,
        ):
            bad(v["kind"], v["detail"], unit)
        low_unit = unit.lower()
        for t in never_tools:
            if t in low_unit:
                bad("never_claim_tool", t, unit)

        # ---- a sales tool or method: only a skill_index key, never a cited
        # bullet (review 2026-10-09, skill-answers). A bullet can name a tool
        # someone else used ("My manager ran Clari forecast calls at
        # Haulmark"), and "At Haulmark I ran Clari forecast calls every week."
        # citing it passed. Who used it is not checked: any sentence naming a
        # tool or method of questions.TOPICS that no skill_index key is
        # (questions.in_skill_index) fails, whoever its subject is and
        # whatever the JD says ("Your reps live in Clari"). A false rejection
        # costs a sentence; a false acceptance puts a tool the candidate never
        # used in a letter. One exception (2026-10-15): a tool that IS the
        # target ("Aircall" in a letter to Aircall) named only as the company
        # applied to (_tool_is_target, target_tool_ok) ----
        for tool in unindexed_tools:
            hits = list(tool.pattern.finditer(unit))
            if not hits:
                continue
            is_target = tool.label in target_tools
            handoff: list[tuple[str, str]] = []
            if (
                is_target
                and not tenure
                and all(target_tool_ok(unit, m.start(), m.end(), named, jd_ok) for m in hits)
            ):
                # …unless any sentence points back at it in a claim about the
                # candidate or the candidate's employers: "I'd like to join
                # Aircall. I used it daily at Haulmark.", "…Haulmark reps lived in
                # the product." (handoffs, _handoff_pointer)
                handoff = handoffs(tool.kind)
                if not handoff:
                    continue
            detail = (
                f"{tool.label} is not in the skill index; a {tool.kind} is named only "
                "when it is a skill_index key, never on a cited bullet's word "
                "alone. Leave it out"
            )
            if is_target:
                subject = (
                    ""
                    if answer
                    else (
                        ", or as the subject of a present-tense sentence about it with no "
                        "I/my/me/we/our that opens with its name and uses only the "
                        f"posting's own words (or says only '{target_company} caught my "
                        "eye'), never crediting it with a result ('why', 'the reason', "
                        "'the driving force', 'that kind of growth')"
                    )
                )
                detail += (
                    f". {target_company} is also the company applied to: name it only as "
                    f"that company, in the job sought ('the role at {target_company}') or "
                    f"in a sentence that only aspires ('I'd like to join {target_company}')"
                    f"{subject}; never as a tool the candidate used"
                )
            shown = unit
            if handoff:
                quoted = "; ".join(f"{u[:120]!r} ({w!r})" for u, w in handoff[:3])
                more = f" and {len(handoff) - 3} more" if len(handoff) > 3 else ""
                detail += (
                    ", nor in a text with a sentence pointing back at it ('it', 'they', "
                    "'this', 'the same thing', 'the product', 'the dialer', 'a customer', "
                    "'seats') in a claim about the candidate or the candidate's employers: "
                    f"{quoted}{more}. In each, name what that word means or cut the sentence"
                )
                if handoff[0][0] != unit:
                    shown = f"{unit} {handoff[0][0]}"
            bad("unsourced_tool", detail, shown)

        # ---- a claim of experience, skill, credentials or background: only
        # the cited bullets' words (a reaction's own words aside) ----
        # …and, with no first person, a hire the target would make: "You'd be
        # hiring a licensed electrician" (second review 2026-10-09)
        experience = _experience_claim(unit)
        hire = _hire_claim(unit)
        if experience or hire:
            # …less a generic reason that closes it (_generic_reasons)
            claim_text = unit
            if experience and not hire:
                for g_start, g_end in reversed(_generic_reasons(unit, cited_terms, target_company)):
                    claim_text = f"{claim_text[:g_start]} {claim_text[g_end:]}"
            said = _surface(_PERCEPTION.sub(" ", claim_text))
            allowed_terms = claim_terms | (_HIRE_FRAME if hire else frozenset())
            unsourced = {r for r in said if r not in allowed_terms}
            if unsourced:
                # a neutral change verb restating a cited move (review 2026-10-10)
                unsourced -= _change_roots(unit, cited)
            missing = sorted(said[r] for r in unsourced)
            if missing:
                bad(
                    "unsourced_experience_claim",
                    f"{', '.join(missing)}: in no cited bullet. A first-person claim of "
                    "experience, skill, credentials or background (what the candidate "
                    "has, would bring or is) may use only the words of the bullets it "
                    "cites; restate a cited bullet or cut the sentence",
                    unit,
                )
            elif experience and not set(said) - _CLAIM_MARKERS:
                bad(
                    "unsourced_experience_claim",
                    "the claim names nothing a cited bullet could source ('I can do this', "
                    "'None of this would be new to me'): a first-person claim of experience, "
                    "skill or familiarity must restate what a cited bullet says; restate it "
                    "or cut the sentence",
                    unit,
                )

    # ---- a form question about working at the target (second review
    # 2026-10-09): the answer is checked without its question, so "Yes." to
    # "Have you previously worked for Fabrikam Payments?" named no employer. Unless
    # the corpus lists the target, only a plain "No" may be drafted; anything
    # else stays open for the human ----
    if (
        question
        and target_norm
        and not target_is_employer
        and asks_target_tenure(question, target_company)
        and not plain_no(text)
    ):
        bad(
            "fabricated_employer",
            f"the question asks whether the candidate worked or works at {target_company}, "
            f"and the corpus lists no role there (the candidate's employers are {emps}); "
            "answer only with a plain 'No', or decline and leave it to the human",
            text,
        )

    # ---- cross-role attribution across sentences (review of wp2) ----
    # A sentence that refers back to the previous one ("It came from…", "The
    # revenue came from…", "…came from them") is one claim with it: together
    # they must name every employer whose facts they state.
    groups: list[list[int]] = []
    for ui, unit in enumerate(units):
        if groups and (_BACKREF_START.match(unit) or _BACKREF_OBJECT.search(unit)):
            groups[-1].append(ui)
        else:
            groups.append([ui])
    for g in groups:
        if len(g) < 2 or cross_flagged & set(g):
            continue
        g_named = set().union(*(unit_named[i] for i in g))
        g_facts = set().union(*(unit_facts[i] for i in g))
        if len(g_facts | g_named) >= 2 and g_facts - g_named:
            cross_flagged.update(g)
            bad(
                "cross_role_attribution",
                f"linked sentences mix facts from {sorted(g_facts | g_named)} but name only "
                f"{sorted(g_named) or 'no employer'}",
                " ".join(units[i] for i in g),
            )
    # …and one employer's result is never the product of another employer's
    # work, named or not: "The ~$5.6M in Haulmark bookings came from usage-based
    # pricing I launched at ledgerline." A side with no facts of its own that
    # refers back ("It came from…", "…came from them") stands for the previous
    # segment.
    segments = [(ui, seg) for ui, unit in enumerate(units) for seg in _SEGMENT.split(unit) if seg]
    seg_emps = [employers_in(seg) for _ui, seg in segments]
    for k, (ui, seg) in enumerate(segments):
        if ui in cross_flagged:
            continue
        for m in _CAUSAL.finditer(seg):
            sides: list[set[str]] = []
            for side in (seg[: m.start()], seg[m.end() :]):
                emp = employers_in(side)
                if not emp and _SIDE_BACKREF.search(side):
                    emp = next(
                        (seg_emps[j] for j in range(k - 1, max(k - 3, -1), -1) if seg_emps[j]),
                        set(),
                    )
                sides.append(emp)
            if sides[0] and sides[1] and not sides[0] & sides[1]:
                cross_flagged.add(ui)
                bad(
                    "cross_role_attribution",
                    f"credits {sorted(sides[0])} facts to {sorted(sides[1])} work "
                    f"({m.group()!r}); each employer's results stand on their own",
                    units[ui],
                )
                break

    return ProvenanceResult(ok=not violations, violations=violations)


# ---------------------------------------------------------------------------
# a form question about working at the target (second review 2026-10-09)
# ---------------------------------------------------------------------------
# "Have you previously worked for Fabrikam Payments?", "Have you worked with us
# before?", "Are you a current or former employee?", "Previous Contoso
# employee?", "Have you ever been employed by Contoso or its subsidiaries?",
# "Do you currently work at Contoso?", "Are you a rehire?". "Tell us about your
# work experience", "Have you worked in a startup?", "When could you start
# working with us?" and "Are you authorized to work for any employer in the
# US?" are not.
_ORG_US = r"(?:(?-i:us)|here|(?:our|this)\s+(?:company|organi[sz]ation|firm|employer|business))"
_EMPLOYEE_NOUN = r"(?:employee|employees|staff|staffer|contractor|intern|team\s+member|alum\w*)"
_TENURE_QUESTION = re.compile(
    r"\b(?:former|previous|past|current|ex-?)\s+(?:[\w-]+\s+)?" + _EMPLOYEE_NOUN + r"s?\b"
    r"|\b(?:re-?hire[ds]?|boomerang|returning\s+(?:employee|candidate|applicant))\b"
    r"|\b(?:worked|been\s+employed|employed)\s+here\b",
    re.I,
)


def asks_target_tenure(question: str, target_company: str) -> bool:
    """True when a form question asks whether the candidate worked or works
    at the target (by name, or as "us"/"here"/"our company"), or is or was
    one of its employees (see above)."""
    if _TENURE_QUESTION.search(question):
        return True
    who = [_ORG_US]
    target = target_company.strip()
    if target:
        words = target.split()
        who.append(r"\s+".join(re.escape(w) for w in words))
        if len(words) > 1 and len(words[0]) >= 4:
            who.append(re.escape(words[0]))
    org = "(?:" + "|".join(who) + ")"
    return bool(
        re.search(
            r"\b(?:worked|been\s+employed|employed|interned|consulted|contracted|served|been"
            r"|(?:currently|presently|now|still|you)\s+work(?:s|ing)?)"
            r"\s+(?:[\w-]+\s+){0,3}?(?:for|at|with|by|in)\s+(?:the\s+)?" + org + r"\b"
            r"|\b" + _EMPLOYEE_NOUN + r"s?\s+(?:of|at|with|for)\s+(?:the\s+)?" + org + r"\b"
            r"|\b" + org + r"(?:['’]s)?\s+(?:[\w-]+\s+)?" + _EMPLOYEE_NOUN + r"s?\b",
            question,
            re.I,
        )
    )


_PLAIN_NO = re.compile(
    r"^\s*(?:no\b(?:[\s.,;!—–-]*(?:i\s+(?:have\s+not|haven['’]t|have\s+never|never\s+have"
    r"|did\s+not|didn['’]t|am\s+not|was\s+not|wasn['’]t)|i['’](?:ve\s+never|m\s+not)))?"
    r"|n/?a\b|not\s+applicable\b)[\s.!]*$",
    re.I,
)


def plain_no(text: str) -> bool:
    """True for a plain "No" ("No.", "No, I have not.", "No, I haven't.") or
    "N/A"."""
    return _PLAIN_NO.match(text) is not None


def has_hard(result: ProvenanceResult) -> bool:
    return any(v.get("kind") in HARD_KINDS for v in result.violations)


def coerce_ids(raw: object) -> list[str]:
    """bullets_used as returned by a model: list, comma string, or junk."""
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []
