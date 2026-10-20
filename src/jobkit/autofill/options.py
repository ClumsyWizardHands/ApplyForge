"""Choosing an option on a form: which <option>, radio, checkbox, dropdown
entry or location suggestion carries the answer the bank holds. Pure
functions, no browser: browser.py reads the option texts and acts on the
index returned here.

A rule that takes any option whose first word matches the answer's first
word would answer "North Dakota" with "North Carolina" ("north"). The order
here is deliberately strict, and every step that is not an exact or known-equivalent
match refuses rather than guesses. No match means the field is left blank and
flagged for the human, never filled with a near miss:

1. a decline answer ("I don't wish to answer") picks the form's decline
   option, whatever its wording ("Decline to self-identify", "Prefer not to
   say", ...), and nothing else. Only an option that is purely a decline
   counts: "I am a protected veteran, but I choose not to self-identify ..."
   states a status, and is never chosen for a decline (nor for a yes/no, an
   acknowledgement or a fuzzy answer);
2. exact match after normalization (case, quotes, punctuation, "&");
3. known equivalences for places: US state names and 2-letter codes
   ("PA" = "Pennsylvania" = "Pennsylvania (PA)"), and the United States'
   spellings ("United States" = "United States of America" = "USA" = "US" =
   "U.S."). A place answer never goes past this step;
4. the bank entry's select_preference, in order (exact or equivalent);
5. yes/no answers pick the option that starts with that word, only when
   exactly one does, or exactly one is that bare word ("No" among "No, this
   is my first time ..." and "No, I applied ... more than 6 months ago" is
   ambiguous: blank and flag unless select_preference named one in step 4).
   Only options that add nothing to the answer are considered. A bare
   answer takes the bare word, or the word with echo words ("Yes, I do",
   "No, I do not"); "No, but I will require sponsorship" and "Yes, with
   sponsorship" are refused. An answer that carries more than the word
   ("Yes — up to 2 days per week in Philadelphia ...") takes the bare word, or
   a remainder whose every word is in the answer, with the answer's
   negations and in its order: "Yes, 5 days per week" is refused, and so is
   "No, I will require sponsorship" for "No, I will not require
   sponsorship". The legal_acknowledgement entry's "Yes" takes only an
   option that agrees: never "Yes, I opt out of arbitration" or "Yes, I
   used AI";
6. an acknowledgement (legal_acknowledgement) picks the one affirmative
   option that only agrees to or has read the document ("I understand and
   agree to ...", "I will read ..."), never one that negates, declines,
   opts out or states anything else ("I acknowledge I used AI");
7. safe fuzzy: one side's words contain all of the other's, the extra words
   are not a negation, a number that conflicts, or a different place, and
   exactly one option scores best."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

# ---- places --------------------------------------------------------------------

US_STATES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "PR": "Puerto Rico",
}


def norm(text: str) -> str:
    """Lowercase, NFKC, straight quotes, "&" -> "and", dots dropped ("U.S." ->
    "us"), other punctuation to spaces, whitespace collapsed."""
    s = unicodedata.normalize("NFKC", text).lower()
    s = s.replace("’", "'").replace("‘", "'").replace("`", "'")
    s = s.replace("&", " and ").replace(".", "")
    s = re.sub(r"[^\w\s']", " ", s)
    return re.sub(r"\s+", " ", s).strip(" '")


_STATE_BY_NAME = {norm(name): code for code, name in US_STATES.items()}
_US_NAMES = frozenset(
    {"united states", "united states of america", "the united states", "usa", "us", "america"}
)
_ZIP = re.compile(r"\s+\d{5}(?:-\d{4})?$")
_SEGMENT_SPLIT = re.compile(r"\s*(?:[,()|/]|\s[-–—]\s)\s*")


def _segments(text: str) -> list[str]:
    """'Bristol, PA 19007, USA' -> ['Bristol', 'PA', 'USA']."""
    out = []
    for seg in _SEGMENT_SPLIT.split(text.strip()):
        seg = _ZIP.sub("", seg.strip()).strip()
        if seg:
            out.append(seg)
    return out


def state_code(segment: str) -> str | None:
    """The state a segment names: a state name in any case, or an UPPERCASE
    2-letter code ("PA"; "or", "in", "me" are words, not states)."""
    seg = segment.strip()
    if re.fullmatch(r"[A-Z]{2}", seg) and seg in US_STATES:
        return seg
    return _STATE_BY_NAME.get(norm(seg))


def is_us(segment: str) -> bool:
    return norm(segment) in _US_NAMES


def states_in(text: str) -> frozenset[str]:
    """Every US state a text names as one of its segments."""
    return frozenset(c for c in (state_code(s) for s in _segments(text)) if c)


def canonical_place(text: str) -> str | None:
    """'state:PA' for "Pennsylvania", "PA", "Pennsylvania (PA)", "PA - Pennsylvania",
    "Pennsylvania, USA"; 'country:US' for the United States' spellings; None for
    anything that is not purely one of those places."""
    segs = _segments(text)
    if not segs:
        return None
    states: set[str] = set()
    for seg in segs:
        if is_us(seg):
            continue
        code = state_code(seg)
        if code is None:
            return None  # a segment that is not a place: "Bristol", "Remote"
        states.add(code)
    if len(states) > 1:
        return None
    return f"state:{states.pop()}" if states else "country:US"


def search_text(want: str) -> str:
    """What to type into a filterable dropdown to bring the answer into view:
    the full state name for a state, "United States" for the country."""
    place = canonical_place(want)
    if place and place.startswith("state:"):
        return US_STATES[place.split(":", 1)[1]]
    if place == "country:US":
        return "United States"
    return want.strip()[:60]


# ---- decline, yes/no, acknowledgement ------------------------------------------

_DECLINE_OBJECT = (
    r"(?:answer|say|disclose|identify|self ?identify|self ?disclose|respond|provide"
    r"|specify|share|state|reveal|indicate)"
)
# the decline wordings: "decline to self identify", "prefer not to say",
# "don't wish to answer", "rather not say"
_DECLINE_CLAUSE = (
    rf"declin(?:e|ed|es|ing) (?:to )?{_DECLINE_OBJECT}"
    rf"|(?:prefer|choose|chose|wish|want|elect|opt|rather) not (?:to )?{_DECLINE_OBJECT}"
    rf"|(?:do not|don't|does not|doesn't|not) (?:wish|want|care|like) to {_DECLINE_OBJECT}"
)
# anywhere in the text: the option or answer MENTIONS a decline
_DECLINE = re.compile(
    rf"^(?:i )?declin(?:e|ed)$|\b(?:{_DECLINE_CLAUSE})"
    r"|\bi'?d rather not\b|\bno answer\b|\bnot disclosed\b|\bundisclosed\b"
)
# at the start, after at most "I" / "I'd" / "I would": the text IS a decline.
# Review 2026-10-10: VEVRAA's "I am a protected veteran, but I choose not to
# self-identify the classifications to which I belong" and "Yes, I have a
# disability, but I prefer not to say which" state a status and decline only
# its detail. Neither is a decline: the stored EEO answer is a plain decline.
_PURE_DECLINE = re.compile(
    r"^(?:i |i'd |id |i would )?"
    rf"(?:declin(?:e|ed)$|rather not$|{_DECLINE_CLAUSE}"
    r"|no answer\b|not disclosed\b|undisclosed\b)"
)
# after the decline clause: a turn ("..., but I am a veteran") or a status
_DECLINE_TAIL_STATUS = re.compile(
    r"\b(?:but|however|although|though|yet|except|whereas)\b"
    r"|\b(?:yes|i am|i'm|i have|i've|i was|i identify|identify as|i served|i do have)\b"
)


def mentions_decline(text: str) -> bool:
    """The text carries decline wording anywhere, including an option that
    states a status and declines only its detail ("I am a protected veteran,
    but I choose not to self-identify ..."). Such an option is never chosen for
    a yes/no, an acknowledgement or a fuzzy answer."""
    return bool(_DECLINE.search(norm(text)))


def is_decline(text: str) -> bool:
    """A decline-to-answer option or answer and nothing else, in any of the
    wordings forms use: "I don't wish to answer", "Decline to self-identify",
    "I do not want to answer", "Prefer not to say", "I choose not to
    disclose", ... Not "No, I do not want to relocate", not "No, I do not have
    a disability", and not an option that states a status before or after
    its decline clause ("I am a protected veteran, but I choose not to
    self-identify ...", "Yes, I have a disability, but I prefer not to say
    which", "Prefer not to say, but I am a veteran")."""
    s = norm(text)
    m = _PURE_DECLINE.match(s)
    return m is not None and not _DECLINE_TAIL_STATUS.search(s[m.end() :])


_YES_NO = re.compile(r"^(yes|no)\b")
_AFFIRMATIVE_WANT = re.compile(
    r"^(?:yes|true|1|i agree|agreed?|i acknowledge|acknowledged?|i accept|accepted?|i consent)\b"
)
_AFFIRMATIVE_OPTION = re.compile(
    r"\b(?:yes|agree[sd]?|acknowledge[sd]?|understand|understood|accept(?:ed)?|consent"
    r"|confirm(?:ed)?|certify|authori[sz]e|i (?:have|will) read|read and)\b"
)
_NEGATIONS = frozenset(
    {
        "not",
        "no",
        "never",
        "none",
        "nor",
        "neither",
        "don't",
        "doesn't",
        "didn't",
        "won't",
        "can't",
        "cannot",
        "isn't",
        "aren't",
        "wasn't",
        "decline",
        "declined",
        "disagree",
        "refuse",
        "without",
        "non",
    }
)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "for",
        "and",
        "or",
        "i",
        "i'm",
        "im",
        "my",
        "me",
        "you",
        "your",
        "we",
        "our",
        "is",
        "am",
        "are",
        "be",
        "been",
        "this",
        "that",
        "with",
        "at",
        "by",
        "as",
        "it",
    }
)


def _tokens(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", norm(text))
    return frozenset(t for t in words if t not in _STOPWORDS)


# words after "Yes," / "No," that only echo the question ("Yes, I can", "No,
# I do", "Yes, that's correct") and say nothing the answer has to back
_ECHO = frozenset(
    {
        "do",
        "does",
        "can",
        "will",
        "would",
        "have",
        "has",
        "sure",
        "absolutely",
        "definitely",
        "certainly",
        "correct",
        "that's",
        "thats",
        "indeed",
    }
)


def _yes_no_qualifier(text: str, word: str) -> frozenset[str]:
    """The words of a yes/no text after its first word, minus stopwords and
    echo words: {"2", "days", "per", "week", ...} for "Yes — up to 2 days per
    week ...", empty for "Yes", "Yes, I do", "No."."""
    rest = norm(text)[len(word) :]
    return _tokens(rest) - _ECHO


def _remainder_seq(text: str, word: str) -> list[str]:
    """The words of a yes/no text after its first word, in order, minus
    stopwords and echo words."""
    words = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", norm(text)[len(word) :])
    return [t for t in words if t not in _STOPWORDS and t not in _ECHO]


def _in_order(part: Sequence[str], whole: Sequence[str]) -> bool:
    """Every word of `part` appears in `whole`, in the same order."""
    it = iter(whole)
    return all(any(w == x for x in it) for w in part)


def _consistent_yes_no(want: str, option: str, word: str) -> bool:
    """Step 5 for an answer that carries more than its yes/no word: the
    option says nothing the answer does not. Second review 2026-10-10: the
    bank's "Yes — up to 2 days per week in Philadelphia or Bucks County." took
    "Yes, 5 days per week" and "Yes, I can relocate to San Francisco", and
    "No, I do not require sponsorship" took "No, but I will require
    sponsorship in the future", because only the first word was compared.
    The option must be the bare word (or the word and echo words), or every
    word of its remainder must be in the answer, with no state the answer
    does not name. Anything else is blank and flag.

    Third review 2026-10-10: that check ran one way only, so an option that
    DROPS the answer's negation passed: "No, I will not require sponsorship"
    took "No, I will require sponsorship", and "Yes, I am authorized and do
    not need sponsorship" took "Yes, I am authorized and need sponsorship".
    The two remainders must hold the same negation words, and the option's
    words must come in the answer's order ("No, I will require sponsorship,
    not now" is not "No, I will not require sponsorship now")."""
    extra = _yes_no_qualifier(option, word)
    if not extra:
        return True
    if not extra <= _tokens(want):
        return False  # a different number, a negation, a place, a condition
    if (extra & _NEGATIONS) != (_yes_no_qualifier(want, word) & _NEGATIONS):
        return False  # the option drops (or adds) a "not", a "never", a "without"
    if not _in_order(_remainder_seq(option, word), _remainder_seq(want, word)):
        return False  # the same words, saying something else
    o_states = states_in(option)
    return not o_states or o_states <= states_in(want)


def _bare_yes_no(option: str, word: str) -> bool:
    """Step 5 for a bare "Yes" or "No": the option says nothing beyond its
    word. Third review 2026-10-10: a bare "No" took "No, but I will require
    sponsorship in the future" and a bare "Yes" took "Yes, with sponsorship"
    whenever that was the only option starting with the word; the option
    then states something the bank never did. Only the bare word, the word
    with echo words ("Yes, I do", "Yes, I can", "Yes, that's correct"), or a
    "No" with only echo words and its own negation ("No, I do not", "No, I
    have not", "No, never") carries a bare answer. Anything longer is blank
    and flag unless the entry's select_preference names it (step 4)."""
    extra = _yes_no_qualifier(option, word)
    if word == "no":
        extra = extra - _NEGATIONS
    elif extra & _NEGATIONS:
        return False  # "Yes, I do not"
    return not extra


# What an acknowledgement option may say besides its affirmative word (third
# review 2026-10-10): agreeing, having read or understood, and naming the
# document. "Yes, I opt out of arbitration" and "Yes, I used AI" start with
# the word the legal_acknowledgement entry holds, but say the opposite of the
# user's decision or state a fact; the entry's "Yes" means "I agree" only.
_ACK_OPTION_VERBS = frozenset(
    {
        "yes",
        "agree",
        "agreed",
        "agrees",
        "acknowledge",
        "acknowledged",
        "acknowledges",
        "understand",
        "understood",
        "accept",
        "accepted",
        "accepts",
        "consent",
        "consented",
        "confirm",
        "confirmed",
        "certify",
        "read",
        "reviewed",
        "received",
        "hereby",
        "fully",
        "carefully",
        "all",
        "ok",
        "okay",
    }
)
_ACK_OPTION_DOCUMENT = frozenset(
    {"agreement", "agreements", "policy", "policies", "terms", "conditions", "notice"}
    | {"disclosure", "guidelines"}
)
# words that may name WHICH document, next to one of the nouns above
_ACK_OPTION_DOCUMENT_WORDS = frozenset(
    {"arbitration", "arbitrate", "mutual", "ai", "use", "usage", "reference", "references"}
    | {"applicant", "application", "candidate", "candidates", "interview", "interviews"}
    | {"interviewer", "interviewers", "hiring", "above", "below", "set", "forth", "herein"}
    | {"foregoing", "stated", "described", "following", "its", "these", "those", "same"}
    | {"attached", "linked", "company"}
)
# opting out, waiving, withdrawing: the opposite of agreeing
_ACK_REVERSAL = re.compile(
    r"\bopt(?:s|ed|ing)?\s*out\b|\bwaiv\w*|\bwithdr[ae]w\w*|\brevok\w*|\brevocation\b"
    r"|\brescind\w*|\bobject\w*|\bexempt\w*|\breject\w*"
)


def _acknowledges_only(option: str) -> bool:
    """Steps 5 and 6 for the legal_acknowledgement entry: the option only
    agrees to, or says it has read, the document ("Yes", "I agree", "I
    understand and agree to the terms of the Agreement to Arbitrate set forth
    above.", "I will read the arbitration agreement below."). Never one with
    a negation, a decline, an opt-out or waiver, or any other word: "Yes, I
    opt out of arbitration", "Yes, I used AI", "I acknowledge I used AI"."""
    if _ACK_REVERSAL.search(norm(option)) or mentions_decline(option):
        return False
    if _CREATE_ROW.match(option.strip()):
        return False
    t = _tokens(option)
    if t & _NEGATIONS:
        return False
    rest = {w for w in t - _ECHO - _ACK_OPTION_VERBS if not w.endswith("'s")}
    if not rest:
        return not (t - _ECHO - _ACK_OPTION_VERBS)  # a possessive alone names nothing
    return bool(rest & _ACK_OPTION_DOCUMENT) and rest <= (
        _ACK_OPTION_DOCUMENT | _ACK_OPTION_DOCUMENT_WORDS
    )


# a creatable dropdown's 'Create "..."' row is not an option: choosing it
# would invent one
_CREATE_ROW = re.compile(r"^(?:create|add|use)\s*[\"'\u201c]", re.I)


def _match_exact_or_place(want: str, options: Sequence[str]) -> int | None:
    """Step 2 and 3: an exact normalized match, else a place equivalent."""
    w = norm(want)
    if not w:
        return None
    for i, opt in enumerate(options):
        if norm(opt) == w:
            return i
    place = canonical_place(want)
    if place:
        for i, opt in enumerate(options):
            if canonical_place(opt) == place:
                return i
    return None


def _fuzzy(want: str, options: Sequence[str]) -> int | None:
    """Step 7. Containment in either direction, scored by overlap; refused
    when the extra words carry a negation or a conflicting number or place, or
    when two options tie for best."""
    wt = _tokens(want)
    if not wt:
        return None
    w_states = states_in(want)
    w_digits = {t for t in wt if t.isdigit()}
    scored: list[tuple[float, int]] = []
    for i, opt in enumerate(options):
        if mentions_decline(opt) or _CREATE_ROW.match(opt.strip()):
            continue
        ot = _tokens(opt)
        if not ot:
            continue
        o_states = states_in(opt)
        if (o_states or w_states) and o_states != w_states:
            continue  # never a different state: North Dakota is not North Carolina
        if wt <= ot:
            extra = ot - wt
            score = 1.0 - len(extra) / (len(ot) + 1)  # the option restates the answer
        elif ot <= wt:
            extra = wt - ot
            score = len(ot) / len(wt)  # the answer restates the option
            if score < 0.5:
                continue  # "No" is not the answer "Open to relocating ... otherwise no ..."
        else:
            continue
        if extra & _NEGATIONS:
            continue  # "I am not a protected veteran" vs "I am a protected veteran"
        o_digits = {t for t in ot if t.isdigit()}
        if w_digits and o_digits and w_digits != o_digits:
            continue
        scored.append((score, i))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None  # ambiguous: the human picks
    return scored[0][1]


def choose_option(
    want: str,
    options: Sequence[str],
    *,
    prefer: Sequence[str] = (),
    acknowledgement: bool = False,
) -> int | None:
    """Index into `options` of the option that carries `want`, or None when no
    option safely does (leave blank and flag). `prefer` is the bank entry's
    select_preference; `acknowledgement` is set only for the bank's
    legal_acknowledgement entry. See the module docstring for the order."""
    w = want.strip()
    if not w or not options:
        return None
    # 1. decline: the form's decline option, in whatever words it uses, and
    # only an option that is purely a decline ("I am a protected veteran, but
    # I choose not to self-identify ..." is not one): the exact wording, else
    # the shortest. No pure decline on the list: blank and flag.
    if is_decline(w):
        declines = [i for i, o in enumerate(options) if is_decline(o)]
        exact = [i for i in declines if norm(options[i]) == norm(w)]
        if exact:
            return exact[0]
        return min(declines, key=lambda i: (len(norm(options[i])), i)) if declines else None
    if mentions_decline(w):
        # an answer that states something and declines a detail: only that
        # very option carries it
        same = [i for i, o in enumerate(options) if norm(o) == norm(w)]
        return same[0] if same else None
    # 2-3. exact, then known place equivalents; a place never goes further
    hit = _match_exact_or_place(w, options)
    if hit is not None or canonical_place(w):
        return hit
    # 4. the bank entry's own preference order
    for p in prefer:
        hit = _match_exact_or_place(str(p), options)
        if hit is not None:
            return hit
    affirmative = bool(_AFFIRMATIVE_WANT.match(norm(w)))
    # 5. yes/no: the option that starts with the same word, never a decline,
    # and only when that is unambiguous. Review 2026-10-10: a form's "Have you
    # applied before?" lists "No, this is my first time applying to this role"
    # and "No, I applied to this role more than 6 months ago"; a sponsorship
    # list holds "No, but I will require sponsorship in the future" before
    # "No, I do not require sponsorship". A bare "No" names neither, so the
    # first was a coin toss with a wrong-answer side. One candidate, or exactly
    # one that is the bare word itself, is safe; anything else is the human's
    # (select_preference, step 4, is how the owner names the right one).
    # Third review 2026-10-10: a candidate must also say nothing the answer
    # does not. A bare answer takes only a bare option ("No" never takes "No,
    # but I will require sponsorship in the future"), an acknowledgement only
    # one that agrees ("Yes" never takes "Yes, I opt out of arbitration").
    yn = _YES_NO.match(norm(w))
    if yn:
        word = yn.group(1)
        starts = [
            i
            for i, o in enumerate(options)
            if _YES_NO.match(norm(o))
            and norm(o).split()[0] == word
            and not mentions_decline(o)
            and not _CREATE_ROW.match(o.strip())
        ]
        if _yes_no_qualifier(w, word):
            # an answer with a qualifier ("Yes — up to 2 days per week ...")
            # takes only an option that says nothing the qualifier does not
            starts = [i for i in starts if _consistent_yes_no(w, options[i], word)]
        elif acknowledgement and affirmative:
            starts = [i for i in starts if _acknowledges_only(options[i])]
        else:
            starts = [i for i in starts if _bare_yes_no(options[i], word)]
        if len(starts) == 1:
            return starts[0]
        bare = [i for i in starts if norm(options[i]) == word]
        if len(bare) == 1:
            return bare[0]
        if not (acknowledgement and affirmative):
            return None
    # 6. an acknowledgement: the one affirmative option that only agrees
    if acknowledgement and affirmative:
        agree = [
            i
            for i, o in enumerate(options)
            if _AFFIRMATIVE_OPTION.search(norm(o)) and _acknowledges_only(o)
        ]
        if len(agree) == 1:
            return agree[0]
        if len(options) == 1 and _acknowledges_only(options[0]):
            return 0
        return None
    # 7. safe fuzzy
    return _fuzzy(w, options)


def pick_option(
    want: str,
    options: Sequence[str],
    *,
    prefer: Sequence[str] = (),
    acknowledgement: bool = False,
) -> str | None:
    """choose_option, returning the option text (stripped) instead of its index."""
    i = choose_option(want, options, prefer=prefer, acknowledgement=acknowledgement)
    return None if i is None else options[i].strip()


# ---- location pickers ------------------------------------------------------------


def parse_location(value: str) -> tuple[str | None, str] | None:
    """('Bristol', 'PA') for "Bristol, PA" or "Bristol, Pennsylvania, USA";
    (None, 'PA') for "Pennsylvania"; None when the value names no US state."""
    city: str | None = None
    state: str | None = None
    for seg in _segments(value):
        if is_us(seg):
            continue
        code = state_code(seg)
        if code:
            if state and code != state:
                return None  # two states: not one place
            state = code
        elif city is None:
            city = seg
    return (city, state) if state else None


def location_queries(city: str | None, state: str, value: str) -> list[tuple[str, bool]]:
    """What to type into a location autocomplete, in order, and whether a
    state-only suggestion is acceptable for that query: the city queries first
    (a city+state suggestion only), then the state name (city+state, or the
    state alone)."""
    name = US_STATES[state]
    out: list[tuple[str, bool]] = []
    if city:
        for q in (value.strip(), f"{city}, {state}", f"{city}, {name}"):
            if q and q not in [x for x, _ in out]:
                out.append((q, False))
    out.append((name, True))
    return out


def choose_location(
    city: str | None, state: str, suggestions: Sequence[str], *, state_only_ok: bool
) -> int | None:
    """Index of the suggestion for this place: the same city in the same state
    ("Bristol, Pennsylvania, United States", "Bristol, PA 19007, USA"), else,
    when state_only_ok, the state itself ("Pennsylvania, USA"). Never a
    suggestion that names another state, several states, or another city."""
    state_hit: int | None = None
    for i, text in enumerate(suggestions):
        segs = _segments(text)
        named = {c for c in (state_code(s) for s in segs) if c}
        if named != {state}:
            continue
        others = [s for s in segs if not state_code(s) and not is_us(s)]
        if city and others and norm(others[0]) == norm(city):
            return i
        if not others and state_hit is None:
            state_hit = i
    return state_hit if state_only_ok else None
