"""Answer Bank matching: form fields → stored answers. Aliases first (cheap),
embeddings second (fastembed, local), below threshold → novel question.
EEO/compliance fields match ONLY the eeo block — never drafted, never guessed.

Identity (name, email, phone, links, address) is the highest-trust fill on a
form: it is typed verbatim and outlined as answer-bank. So identity patterns
only ever apply to a field-like label (at most 4 content words, no "?") as a
WHOLE-label match, or to an ATS system-field id. A question that merely
mentions "email", "country" or "location" ("…please include their email",
"…sponsorship to work in the country…", "open to relocation?") is a screening
question or a novel one, never an identity field (audit 2026-10-08 §2.4).
The "?" check sees the whole label, and only required-markers and format
hints are dropped from parentheses: "Email (the referrer's)" or "Location
(would you relocate?)" is not a field name (review 2026-10-08)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from typing import TYPE_CHECKING, Any, NamedTuple

import yaml

from jobkit import paths
from jobkit.log import log

if TYPE_CHECKING:
    from jobkit.config import Filters

BANK_PATH = str(paths.HOME / "data" / "answer_bank.yaml")  # project root (paths.py)

# ATS system-field ids → bank targets. The id is the ATS's own name for a
# standard field, so it is authoritative whatever the visible label says (Ashby
# labels _systemfield_name plain "Name"). Keys are lowercased.
_SYSTEM_FIELDS: dict[str, str] = {
    # Greenhouse
    "first_name": "identity.first_name",
    "last_name": "identity.last_name",
    "email": "identity.email",
    "phone": "identity.phone",
    "resume": "attach.resume",
    "resume_text": "attach.resume",
    "cover_letter": "attach.cover_letter",
    "cover_letter_text": "attach.cover_letter",
    # Lever
    "name": "identity.name",
    "location": "identity.address",
    "urls[linkedin]": "identity.linkedin",
    "urls[github]": "identity.github",
    "urls[portfolio]": "identity.portfolio",
    "urls[twitter]": "identity.twitter",
    "urls[other]": "flag.other-url-needs-human",
    # Ashby
    "_systemfield_name": "identity.name",
    "_systemfield_email": "identity.email",
    "_systemfield_phone": "identity.phone",
    "_systemfield_location": "identity.address",
    "_systemfield_resume": "attach.resume",
}

_LINK = r"(?:link to (?:your )?)?"
_URL = r"(?: url| link| profile url)?"
# Whole-label patterns (re.fullmatch) over _field_label(label). Each one
# describes the complete field name, so order does not matter and a label that
# only CONTAINS an identity word never matches.
_IDENTITY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p), target)
    for p, target in (
        (
            r"(?:full |legal |full legal |candidate |applicant )?name"
            r"|(?:full |legal )?(?:first and last|first & last|first/last|first, last) names?",
            "identity.name",
        ),
        (r"(?:legal |preferred )?(?:first|given) name", "identity.first_name"),
        (r"(?:legal |preferred )?(?:last|family) name|surname", "identity.last_name"),
        (r"(?:personal |contact |primary )?e-?mail(?: address)?", "identity.email"),
        (
            r"(?:(?:mobile|cell|contact|primary|home) )?(?:phone|telephone|mobile|cell)"
            r"(?: phone)?(?: number| no\.?| #)?",
            "identity.phone",
        ),
        (_LINK + r"linkedin(?: profile)?" + _URL, "identity.linkedin"),
        (_LINK + r"github(?: profile| account)?" + _URL, "identity.github"),
        (
            r"(?:portfolio|website|personal website)/github"
            + _URL
            + r"|github/(?:portfolio|website|personal website)"
            + _URL,
            "identity.portfolio_or_github",
        ),
        (
            _LINK
            + r"(?:personal )?(?:portfolio|website|web site|portfolio website)"
            + _URL
            + r"|(?:personal )?website/portfolio"
            + _URL
            + r"|portfolio/(?:personal )?website"
            + _URL,
            "identity.portfolio",
        ),
        (_LINK + r"(?:twitter|x|twitter/x)(?: profile| handle)?" + _URL, "identity.twitter"),
        (
            r"(?:other|additional) (?:website|url|link|profile)s?" + _URL,
            "flag.other-url-needs-human",
        ),
        (
            r"(?:upload )?(?:resume|résumé|cv|curriculum vitae)(?:/(?:resume|cv))?"
            r"(?: upload| file| attachment)?",
            "attach.resume",
        ),
        (r"(?:upload )?cover letter(?: upload| file| attachment)?", "attach.cover_letter"),
        (
            r"(?:zip|postal|post)(?: code)?(?:/(?:zip|postal)(?: code)?)?|postcode",
            "identity.postal_code",
        ),
        (r"(?:current )?country(?: of residence)?|country/region", "identity.country"),
        (
            r"(?:current )?(?:state|province|state/province|state or province)"
            r"(?: of residence)?",
            "identity.state",
        ),
        (r"(?:current )?(?:city|town|city/town)(?: of residence)?", "identity.city"),
        (
            r"(?:home |street |mailing |residential |current )?address(?: line 1)?"
            r"|(?:current |working |current working |home )?\blocation\b"
            r"|city, state|city and state|city/state",
            "identity.address",
        ),
    )
]

_FIELD_FILLER = frozenset({"and", "or", "&", "your", "the", "a", "an"})

# A parenthetical (innermost first) and the ones that only say whether the
# field is required or restate its format. Matched against the WHOLE inner
# text: "(if any)" is harmless, "(if referred, the referrer's)" is not. Any
# other parenthetical is a qualifier and stays part of the label, so an
# identity value never lands in a field about someone else (review 2026-10-08).
_PAREN = re.compile(r"\(([^()]*)\)")
_HARMLESS_PAREN = re.compile(
    r"optional|required|not required|mandatory"
    r"|if (?:any|applicable|available|you have one)"
    r"|(?:first|given)(?: and | & |, |,|/)(?:last|family|surname)(?: names?)?"
    r"|city(?:(?:, |,| and | or |/)(?:state|province|state/province|region|country))+"
    r"|(?:with|including|incl\.?) (?:your )?(?:country|area) code"
    r"|(?:(?:e\.g\.|eg|ex\.?|example|i\.e\.)[:,]? ?)?"
    r"(?:(?:https?://|www\.)\S+|[\w-]+(?:\.[\w-]+)+/\S*|[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"
    r"|(?:pdf|docx?|txt|rtf)(?:(?:, |,| or |/)(?:pdf|docx?|txt|rtf))*(?: (?:only|preferred))?"
)
_LEADING_ASK = re.compile(
    r"^(?:please\s+)?(?:(?:enter|provide|add|list|share|select|choose)\s+)?(?:your\s+)?"
)

_EEO_PATTERN = re.compile(
    r"gender|race|ethnic|hispanic|latino|veteran|disabilit|demographic|lgbt|lbgt|glbt|orientation"
    r"|pronoun|sexuality|queer|lesbian|bisexual|asexual|intersex",
    re.I,
)
# _eeo_key helpers (labels arrive lowercased). Membership of the LGBTQ+
# community has no eeo key; neither does the sex assigned at birth.
_EEO_COMMUNITY = re.compile(
    r"\b(?:lgbt|lbgt|glbt)\w*"
    r"|\b(?:lesbian|gay|bisexual|transgender|queer|intersex|asexual)\b.*\bcommunity\b"
    r"|\bcommunity\b.*\b(?:lesbian|gay|bisexual|transgender|queer|intersex|asexual)\b"
)
_EEO_ASSIGNED_AT_BIRTH = re.compile(r"\bassigned\b[^.?]{0,25}\bat birth\b")
# asks whether the candidate is transgender — and nothing else alongside it
_EEO_TRANSGENDER_QUESTION = re.compile(
    r"(?:\b(?:identify|self-identify|consider yourself|describe yourself) (?:as |to be )?(?:a )?"
    r"|\bare you (?:a )?)transgender\b(?!\s*(?:,|/|&|\band\b|\bor\b))"
    r"|\btransgender (?:identity|status|experience|history)\b"
    r"|^\W*transgender\W*$"
)
# an example list: "(e.g. woman, man, transgender)", "such as …", "including …"
_EXAMPLE_TAIL = re.compile(
    r"\b(?:e\.g\.?|eg|ex\.|for example|for instance|such as|including)(?=\W)[^?.;]*"
)


def _without_examples(label: str) -> str:
    """The label with parentheticals, bracketed notes and example lists
    removed: what it asks, not what it offers as examples."""
    s = label
    while True:  # innermost first
        s, n = _PAREN.subn(" ", s)
        if not n:
            break
    s = _EXAMPLE_TAIL.sub(" ", re.sub(r"\[[^\[\]]*\]", " ", s))
    return re.sub(r"\s+", " ", s).strip()


# Residence questions ("What is your current country of residence?", "What is
# your state of residence?") go to the bank's country_of_residence /
# state_of_residence entries.
# Sponsorship, eligibility and relocation questions that mention "the country
# where the job is located" are never residence questions.
_RESIDES = r"(?:currently |presently |now )?(?:live|living|reside|residing|located|based)"
_RESIDENCE_OF = r"of (?:current |primary |legal |permanent )?(?:residence|residency|domicile)"
_RESIDENCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "country": re.compile(
        rf"\bcountry {_RESIDENCE_OF}\b"
        rf"|\bcountry (?:in which|where|that) you (?:are )?{_RESIDES}\b"
        rf"|\b(?:which|what) country (?:do |are )?you (?:are )?{_RESIDES}\b"
        rf"|\bcountry you (?:are )?{_RESIDES}\b"
    ),
    "state": re.compile(
        rf"\bstate {_RESIDENCE_OF}\b"
        rf"|\b(?:the|your) state (?:in which|where|that) you (?:are )?{_RESIDES}\b"
        rf"|\b(?:which|what) state (?:do |are )?you (?:are )?{_RESIDES}\b"
        rf"|\b(?:the|your) state you (?:are )?{_RESIDES}\b"
    ),
}
_NOT_RESIDENCE = re.compile(
    r"sponsor|visa|authori[sz]|eligib|relocat|willing|permit|citizen|licen[sc]|register"
)

# Legal acknowledgements on the application (arbitration agreements, AI-use
# policies, reference-check consent). The user may auto-agree to them through
# the bank's legal_acknowledgement entry; without it, or for any other
# acknowledgement or consent, the field is left blank and flagged. Never
# drafted: an AI must not agree to legal terms on its own.
#
# A topic alone is not an acknowledgement (review 2026-10-10: "Arbitration
# experience" and "Familiarity with AI policy frameworks" were answered "Yes"
# from the entry). A label is covered only when it is SHAPED like one: the
# candidate's own statement ("I understand and agree…", "By submitting, I
# acknowledge…"), an instruction to read the agreement or policy ("Please read
# the arbitration agreement below"), a consent question ("Do you consent to us
# contacting your references?") or one of a few known titles ("Agreement to
# Arbitrate", "AI Policy for Application"). A label that asks about the
# candidate's experience or skill is never covered, whatever its shape.
_ACK_FIRST_PERSON = re.compile(
    r"(?:^|[.;:!,]\s*)i (?:have read|hereby|acknowledge|agree|understand|consent|accept"
    r"|certify|authori[sz]e|confirm)\b"
)
_ACK_IMPERATIVE = re.compile(
    r"(?:^|[.;:!]\s*)please (?:carefully )?(?:read|review)\b[^.?]{0,80}"
    r"\b(?:agreement|polic(?:y|ies)|terms|notice|disclosure)\b"
    r"|(?:^|[.;:!]\s*)please (?:acknowledge|accept|agree|confirm|consent|sign)\b"
)
_ACK_CONSENT_QUESTION = re.compile(
    r"(?:^|[.;:!,]\s*)(?:do|will|would) you (?:agree|consent) to\b"
    r"|(?:^|[.;:!,]\s*)do you (?:acknowledge|accept|authori[sz]e)\b"
)
# whole-label titles, matched against _field_label() (required-markers dropped)
_ACK_TITLE = re.compile(
    r"(?:mutual )?(?:arbitration agreement|agreement to arbitrate)"
    r"|ai(?:[- ]use| usage)? polic(?:y|ies)"
    r"(?: for (?:the )?(?:applications?|applicants?|candidates?|interviewers?|interviews?))?"
)
# a question about the candidate, not an acknowledgement
_ACK_NOT_ACK = re.compile(
    r"\b(?:experience[ds]?|familiar\w*|proficien\w*|expertise|skills?|knowledge|background"
    r"|worked|describe|explain)\b"
    r"|\bhave you\b|\bdo you have\b|\bdid you\b|\btell us\b|\bhow (?:many|much|often|do|did)\b"
)
_ARBITRATION = re.compile(r"\barbitrat\w*")
_AI_POLICY = re.compile(
    r"\b(?:ai|artificial intelligence)\b[^.?]{0,30}\b(?:polic(?:y|ies)|guidelines?)\b"
    r"|\bpolic(?:y|ies)\b[^.?]{0,40}\b(?:ai|artificial intelligence)\b"
)
_REFERENCE_CHECK = re.compile(
    r"\breferences?\b[^.?]{0,80}\b(?:contact\w*|check\w*|verif\w*|reach\w*)"
    r"|\b(?:contact\w*|check\w*|verif\w*|reach\w* out to)\b[^.?]{0,40}\breferences?\b"
)
_ANY_ACK = re.compile(
    r"\bi (?:have read|acknowledge|agree|understand|consent|certify|authori[sz]e|accept)\b"
    r"|\bdo you (?:consent|agree|authori[sz]e|acknowledge|accept)\b|\bconsent to\b"
    r"|\bhave you (?:read|reviewed)\b[^?]{0,60}"
    r"\b(?:agreement|polic(?:y|ies)|terms|notice|disclosure)\b"
    r"|\bterms (?:and|&) conditions\b|\bterms of (?:use|service)\b"
    r"|\bprivacy (?:notice|policy|statement)\b|\backnowledg\w*"
)

# Anything that reads like terms, consent, a waiver or a signature is never
# drafted by the AI, recognised kind or not ("Class action waiver",
# "Electronic signature", "Candidate consent"): it goes to the human
# (review 2026-10-10). Screening entries the user wrote still answer first.
_LEGALISH = re.compile(
    r"\bwaive\w*|\bsignature\b|\bsign (?:here|below)\b|\bconsent\w*|\bterms\b"
    r"|\bagree\w*|\bauthori[sz]\w*|\battest\w*|\barbitrat\w*|\bdisclosure\b"
    r"|\bpolicy\b|\bpolicies\b|\bnotice\b|\bcertify\b|\bcertification of\b",
    re.I,
)


# What marks text as asking something besides a closing "?". Any
# interrogative auxiliary + "you" asks, wherever it stands and with or without
# a "?" (second review 2026-10-10: "Please read our AI policy below. Do you
# currently use AI tools at work" and "Do you agree to arbitrate claims, and
# do you currently have any pending claims ...?" were covered and answered
# "Yes"). The consent question itself ("Do you agree to", "Would you consent
# to") is cut out of its sentence before this runs, so any other "do you",
# "can you", "are you" ... in the label is a question.
_ACK_ASK = re.compile(
    r"\b(?:do|does|did|can|could|have|has|had|is|are|was|were|will|would|should|shall"
    r"|may|might|must)\s+you\b"
    r"|\blet us know\b|\bif you\b|\bwhether\b|\binterested\b"
)
# ":" ends a sentence too ("Please read the AI policy above: Do you use AI at work")
_ACK_SENTENCE = re.compile(r"[^.?!;:]+[.?!;:]*")
# the clauses of a sentence, and the words that only lead into one
_ACK_CLAUSE = re.compile(r",|\s[-–—]\s|\b(?:and|or|but|then|so)\b")
_ACK_CLAUSE_LEAD = re.compile(r"^(?:(?:and|or|but|then|so|also|now|please|kindly)\s+)+")
# A clause that asks for information: an instruction to name, pick or state
# something ("... and select which AI tools you used"), a wh-question put to
# the candidate ("what tools do you use", "which AI tools you used"), or an
# interrogative auxiliary that opens a question without "you" ("Is any part of
# this application ...").
_ACK_INFO_REQUEST = re.compile(
    r"(?:list|select|choose|pick|indicate|tell|describe|provide|specify|explain|enter|name"
    r"|share|disclose|identify|detail)\b"
    r"|(?:which|what|how|when|where|who|whom|whose|why)\b(?=.*\byou)"
    r"|(?:is|are|was|were|does|did|has|had|will|would|should|can|could|may|might|shall)\s+"
    r"(?:there|any\w*|your|this|that|it|the|a|an|ai|we|they|someone|somebody)\b"
)
# "Select 'I agree'", "indicate your agreement": an instruction to agree
_ACK_AGREEMENT_WORD = re.compile(
    r"\b(?:agree\w*|acknowledg\w*|consent\w*|accept\w*|understand\w*|understood)\b"
)


def _clause_asks(text: str) -> bool:
    """The text asks the candidate something: an _ACK_ASK cue anywhere, or a
    clause that opens with an information request."""
    if _ACK_ASK.search(text):
        return True
    for clause in _ACK_CLAUSE.split(text):
        c = _ACK_CLAUSE_LEAD.sub("", clause.strip(" .?!;:\"'"), count=1)
        if _ACK_INFO_REQUEST.match(c) and not _ACK_AGREEMENT_WORD.search(c):
            return True
    return False


def _asks_beyond_consent(low: str) -> bool:
    """A sentence of the label asks the candidate something that is not
    itself the agree/consent question. Review 2026-10-10: "Please review our
    AI policy. Will you use AI assistance during your interviews?" was
    covered by its first sentence and answered "Yes" from the entry; so were
    "Please read the arbitration agreement below. Are you currently a party to
    any lawsuit or arbitration ...?" and "Please read the AI policy above and
    let us know if you used AI to prepare this application". What the label
    ASKS decides, not what it tells the candidate to read. A consent question
    that also asks something else ("Do you agree to arbitrate, and are you a
    party to ...?") asks beyond consent too.

    Second review 2026-10-10: a question needs no "?" and no particular verb.
    "... Do you currently use AI tools at work", "Please review our AI
    policy; do you use Copilot", "Please read the AI policy above: Do you use
    AI at work", "... Can you confirm you have no pending claims ..." and
    "Please review our AI policy and select which AI tools you used" all ask
    (see _ACK_ASK, _ACK_INFO_REQUEST)."""
    for m in _ACK_SENTENCE.finditer(low):
        sentence = m.group(0).strip()
        if not sentence:
            continue
        consent = _ACK_CONSENT_QUESTION.search(sentence)
        if consent:
            # the consent question's own "?" is not a second question
            rest = sentence[: consent.start()] + " , " + sentence[consent.end() :]
            if _clause_asks(rest):
                return True
            continue
        if sentence.endswith("?") or _clause_asks(sentence):
            return True
    return False


# Confirm / certify / attest ... said BY THE CANDIDATE: an instruction
# ("Please confirm ...", "... and confirm ...") or a first-person statement
# ("I certify ..."). A verb governed by a modal, "to", "we" or "they" is the
# employer's ("... may contact my references to confirm my employment"), and
# "verify" is the candidate's only right after "please", "kindly" or "I".
_ACK_ATTEST = re.compile(
    r"\b(?P<verb>confirm(?:s|ed|ing)?|certif(?:y|ies|ied|ying)|attest(?:s|ed|ing)?"
    r"|affirm(?:s|ed|ing)?|verif(?:y|ies|ied|ying)|represent(?:s|ing)?|declare[sd]?|declaring"
    r"|warrant(?:s|ing)?|swear(?:s|ing)?)\b(?P<obj>[^.;:!?]*)"
)
_ACK_NOT_CANDIDATE = frozenset(
    {"to", "may", "might", "will", "would", "can", "could", "shall", "should", "must"}
    | {"we", "they", "us", "it", "who", "which"}
)
# a first-person statement: "I am ...", "I have never ...", "I did not ...",
# "I've read ..."; what follows is checked like an attestation's object
_ACK_I_STATEMENT = re.compile(
    r"\bi(?:'m|'ve|'d|(?:\s+(?:hereby|also|further|fully))*\s+(?:am|was|have|had|did|do|does"
    r"|will|would|can|cannot|could|shall|don't|didn't|haven't|hadn't|won't|wasn't|can't))\b"
    r"(?P<obj>[^.;:!?,]*)"
)
_ACK_PART = re.compile(r",|\b(?:and|or|but)\b")
# subject, auxiliaries and complementizer before the predicate of a part
_ACK_PART_LEAD = re.compile(
    r"^(?:(?:that|to|having|i|i've|i'm|you|you've|you're|we|hereby|also|fully|both|have|has"
    r"|had|am|are|is|do|does|did|will|been)\s+)*"
)
# what may be confirmed or certified: having read, understood or agreed
_ACK_AGREED = re.compile(
    r"(?:read|re-?read|reviewed|review|received|receipt|understand|understood|agree[ds]?"
    r"|accept(?:ed|s)?|acknowledg\w*|consent(?:ed|s)?|aware)\b"
    r"|your (?:agreement|acceptance|consent|acknowledg\w*|understanding)\b"
)
# ... or the document itself ("confirm the arbitration agreement")
_ACK_DOCUMENT = re.compile(
    r"(?:(?:the|our|this|these|its|their|that)\s+)?(?:[\w'-]+\s+){0,4}"
    r"(?:agreement|polic(?:y|ies)|terms|notice|disclosure|conditions)(?:\s+(?:above|below))?"
)
_ACK_FILLER = re.compile(r"(?:below|above|here|yes)?")


def _states_a_fact(obj: str) -> bool:
    """The object of a candidate's confirm/certify, or the rest of an "I ..."
    statement, holds anything beyond having read, understood or agreed to the
    document: "you did not use AI", "I have never been terminated for cause",
    "you are not currently party to any lawsuit"."""
    for part in _ACK_PART.split(obj):
        p = _ACK_PART_LEAD.sub("", part.strip(" \"'"), count=1).strip()
        if not p or _ACK_FILLER.fullmatch(p):
            continue
        if _ACK_AGREED.match(p) or _ACK_DOCUMENT.fullmatch(p):
            continue
        return True
    return False


# ... and the same statement with its subject left out after "and", "but" or
# "or": "I acknowledge the AI policy and have not used AI" (third review
# 2026-10-10: the "I" is not repeated, so _ACK_I_STATEMENT never saw it)
_ACK_ELIDED_I_STATEMENT = re.compile(
    r"\b(?:and|but|or)\s+(?:(?:also|further|hereby|fully)\s+)*"
    r"(?:am|was|have|had|did|do|does|will|would|can|cannot|could|shall|don't|didn't|haven't"
    r"|hadn't|won't|wasn't|can't|never)\b"
    r"(?P<obj>[^.;:!?,]*)"
)


def _attests_fact(low: str) -> bool:
    """The candidate is asked to confirm, certify or state a fact about
    themselves, not only agreement to the document. Second review 2026-10-10:
    "Please review our AI policy and confirm that you did not use AI to
    complete this application", "I acknowledge the AI policy and certify that
    I did not use AI to prepare my application" and "By checking this box, I
    agree to the arbitration agreement and confirm I have never been
    terminated for cause" were covered, and the entry's "Yes" attested to the
    fact. "Confirm you have read and agree to the policy" stays covered.
    Third review 2026-10-10: so does a statement whose "I" is left out ("I
    acknowledge the AI policy and have not used AI")."""
    s = low.replace("’", "'")
    for m in _ACK_ATTEST.finditer(s):
        before = s[: m.start()].split()[-2:]
        if any(w in _ACK_NOT_CANDIDATE for w in before):
            continue  # the employer confirms or verifies
        if m.group("verb").startswith("verif") and not (
            before and before[-1] in ("please", "kindly", "i", "hereby")
        ):
            continue
        if _states_a_fact(m.group("obj")):
            return True
    if any(_states_a_fact(m.group("obj")) for m in _ACK_I_STATEMENT.finditer(s)):
        return True
    return any(_states_a_fact(m.group("obj")) for m in _ACK_ELIDED_I_STATEMENT.finditer(s))


# Third review 2026-10-10: the checks above look for what a label ASKS, by
# cue, so a fact written as a fragment slipped past them. "Please read the
# arbitration agreement. Currently party to a lawsuit against an employer",
# "Please review our AI policy. AI used in this application (yes/no)" and
# "Please read the arbitration agreement below. Opt out of arbitration" were
# covered, and the entry's "Yes" stated the fact or opted out. Covered now
# needs EVERY sentence of the label to be acknowledgement-shaped, or to do
# nothing but describe the document; any other sentence or fragment sends the
# label to the human. A line break ends a sentence, as forms render it.
_ACK_LEAD_IN = r"(?:(?:by|before|to|upon|when)\s[^,]{1,80},\s*)?"
_ACK_SHAPED_SENTENCE = re.compile(
    # the candidate's own statement: "By submitting, I acknowledge ..."
    rf"^{_ACK_LEAD_IN}i\s+(?:hereby\s+)?(?:have (?:carefully )?(?:read|reviewed)|acknowledge"
    r"|agree|understand|consent|accept|certify|authori[sz]e|confirm)\b"
    # an instruction to confirm or consent (what is confirmed: _attests_fact)
    r"|^(?:please|kindly)\s+(?:confirm|consent|agree to arbitrate)\b"
    # a consent question
    rf"|^{_ACK_LEAD_IN}(?:do|will|would) you (?:agree|consent) to\b"
    rf"|^{_ACK_LEAD_IN}do you (?:acknowledge|accept|authori[sz]e)\b"
)
_ACK_DOC_NOUN = r"(?:agreements?|polic(?:y|ies)|terms|notice|disclosure|guidelines)"
# an instruction to read the document or to agree to it ("Please read the
# arbitration agreement below") ...
_ACK_READ_INSTRUCTION = re.compile(
    r"^(?:please|kindly)\s+(?:carefully\s+)?(?:read|review|acknowledge|accept|agree to|sign)\b"
    rf"[^.?]{{0,80}}\b{_ACK_DOC_NOUN}\b"
)
# ... and all that may follow the document's name in it: where it is, when to
# read it, what it covers, what to select. A run-on fragment ("Please read the
# arbitration agreement below currently party to a lawsuit") is not shaped.
_ACK_READ_TAIL = re.compile(
    r"(?:\s*,?\s*(?:below|above|carefully|here|in full|in its entirety|attached|linked|link"
    r"|at link|on this page|first|for (?:the |this |our )?(?:applications?|applicants?"
    r"|candidates?|interviewers?|interviews?|hiring process)))*"
    r"\s*,?\s*(?:"
    r"|(?:which|that) (?:explains|describes|covers|applies|governs|outlines|sets out)\b.*"
    r"|(?:and|then)\s+(?:then\s+)?(?:select|check|tick|click|choose|mark)\s+['\"]?"
    r"(?:i agree|i acknowledge|i understand|yes|agree|acknowledge)['\"]?"
    r"|(?:and|then)\s+(?:then\s+)?(?:agree|acknowledge|accept|confirm|consent|sign)\b.*"
    r"|before (?:applying|continuing|proceeding|you (?:apply|continue|proceed))\b.*"
    r")"
)
# a sentence about the document itself: "The arbitration agreement is below.",
# "Our AI policy explains how we use AI in hiring."
_ACK_DOCUMENT_SENTENCE = re.compile(
    rf"^(?:the|our|this|these|its|their)\s+(?:[\w'-]+\s+){{0,3}}{_ACK_DOC_NOUN}"
    r"(?:\s+(?:below|above))?"
    r"(?:$|\s+(?:is|are|explains|describes|covers|applies|governs|outlines|sets out"
    r"|can be found|appears|follows)\b)"
)
_ACK_FILLER_SENTENCE = frozenset(
    {"", "required", "optional", "mandatory", "link", "see link", "below", "above"}
    | {"see below", "see above"}
)
_ACK_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)+/\S*")
# dots that do not end a sentence: "U.S.", "e.g.", "Acme Inc."
_ACK_ABBREV = re.compile(r"\b(?:[a-z]\.){2,}|\b(?:inc|llc|ltd|corp|co|etc|vs|dr|mr|mrs|ms|st)\.")
# what the user's "Yes" must never say: opting out, waiving, withdrawing,
# declining, or anything negated ("I acknowledge ... and have not used AI",
# "(yes/no)"). Conservative: such a label goes to the human.
_ACK_REVERSAL = re.compile(
    r"\bopt(?:s|ed|ing)?[\s-]*out\b|\bwaiv\w*|\bwithdr[ae]w\w*|\brevok\w*|\brevocation\b"
    r"|\brescind\w*|\bdeclin\w*|\bdisagree\w*|\brefus\w*|\breject\w*|\bobject(?:s|ed|ing|ion)?\b"
    r"|\bexempt\w*"
)
_ACK_NEGATION = re.compile(r"\b(?:not|no|never|none|nor|neither|without|cannot)\b|n't\b")


def _ack_sentences(label: str) -> list[str]:
    """The label's sentences, lowercased: split at . ? ! ; :, at line breaks
    and at a spaced dash, with URLs and abbreviation dots taken out first."""
    s = _ACK_URL.sub(" link ", label.lower())
    s = _ACK_ABBREV.sub(lambda m: m.group(0).replace(".", ""), s)
    out: list[str] = []
    for line in re.split(r"[\r\n]+|\s[-–—]\s", s):
        for m in _ACK_SENTENCE.finditer(_norm_text(line)):
            out.append(m.group(0).strip(" .?!;:,\"'"))
    return out


def _every_sentence_acknowledges(label: str) -> bool:
    """Each sentence of the label is a known title, the candidate's own
    agree/acknowledge statement, an instruction to read or agree to the
    document, a consent question, or a sentence about the document itself;
    and at least one is not merely about the document."""
    shaped = False
    for sentence in _ack_sentences(label):
        field = _field_label(sentence)
        if field in _ACK_FILLER_SENTENCE:
            continue
        read = _ACK_READ_INSTRUCTION.match(sentence)
        if read and _ACK_READ_TAIL.fullmatch(sentence[read.end() :]):
            shaped = True
            continue
        if _ACK_TITLE.fullmatch(field) or _ACK_SHAPED_SENTENCE.match(sentence):
            shaped = True
            continue
        if _ACK_DOCUMENT_SENTENCE.match(sentence):
            continue
        return False  # a fragment or a question: "Currently party to a lawsuit ..."
    return shaped


def _ack_shaped(label: str, low: str) -> bool:
    return bool(
        _ACK_TITLE.fullmatch(_field_label(label))
        or _ACK_FIRST_PERSON.search(low)
        or _ACK_IMPERATIVE.search(low)
        or _ACK_CONSENT_QUESTION.search(low)
    )


def legal_acknowledgement(label: str) -> str | None:
    """'covered' for the kinds of legal acknowledgement the user chose to
    auto-agree to (arbitration, an AI-use policy, consent to contact or check
    references), 'other' for any other acknowledgement, consent or terms
    field, None for everything else.

    Covered needs the topic AND an acknowledgement shape (see _ACK_* above).
    A label about the candidate's experience or skill ("Arbitration
    experience", "Have you ever been party to an arbitration?") is a question,
    not an acknowledgement: it is never covered. An arbitration or AI-policy
    label with neither shape nor such a question ("Arbitration", "May we
    contact your references?") goes to the human as 'other' — never to the
    entry, never to the drafter. So does a label that tells the candidate to
    read the policy and then asks something other than agreement or consent
    ("Please review our AI policy. Will you use AI assistance during your
    interviews?"): see _asks_beyond_consent. So does one that has the
    candidate confirm or state a fact about themselves ("Please read the
    arbitration agreement below and confirm you are not currently party to
    any lawsuit ..."): see _attests_fact. A question about the candidate
    that is also acknowledgement-shaped ("Please read our AI policy. Did you
    use AI ...?") goes to the human as 'other' too.

    Third review 2026-10-10: covered needs EVERY sentence of the label to be
    acknowledgement-shaped or about the document (_every_sentence_acknowledges):
    a fragment ("Please read the arbitration agreement. Currently party to a
    lawsuit against an employer") is 'other'. So is any label with opt-out,
    waiver, withdrawal or decline wording, or a negation ("I acknowledge the
    AI policy and have not used AI")."""
    low = _norm_text(label)
    topic = _ARBITRATION.search(low) or _AI_POLICY.search(low) or _REFERENCE_CHECK.search(low)
    if topic and not _ACK_NOT_ACK.search(low):
        if _asks_beyond_consent(low) or _attests_fact(low):
            return "other"
        if _ACK_REVERSAL.search(low) or _ACK_NEGATION.search(low.replace("’", "'")):
            return "other"
        return "covered" if _every_sentence_acknowledges(label) else "other"
    if topic and _ack_shaped(label, low):
        return "other"
    if _ANY_ACK.search(low):
        return "other"
    return None


def residence_question(label: str) -> str | None:
    """'country' or 'state' when the label asks where the candidate lives
    ("Please choose the country in which you are located.", "What is your
    state of residence?"), else None."""
    low = _norm_text(label)
    if _NOT_RESIDENCE.search(low):
        return None
    for kind, pat in _RESIDENCE_PATTERNS.items():
        if pat.search(low):
            return kind
    return None


# Comp HISTORY (hard rule 5): what the candidate earns or earned. Never
# answered, never drafted — blank + flag.
_COMP_HISTORY = re.compile(
    r"salary history|compensation history|pay history"
    r"|\b(?:current|present|previous|prior|past|last|most recent)\b.{0,40}"
    r"\b(?:salary|compensation|comp|pay|ote|base|earnings|bonus|package|wage)\b"
    r"|\b(?:salary|compensation|pay|ote)\b.{0,30}\b(?:current|previous|prior|last)\b"
    r"|\b(?:do|did) you (?:currently )?earn\b|\bcurrently earn\b"
    r"|\bhow much do you (?:currently )?(?:earn|make)\b"
    r"|(?:current|salary|compensation).*(?:salary|history|\bearn)",
    re.I,
)
# Comp EXPECTATION: what the candidate is asking for. Routed to the bank's
# comp_expectation entry (or blank + flag) — never to the novel-question drafter.
_COMP_EXPECTATION = re.compile(
    r"\b(?:salary|compensation|comp|pay|ote|on.target earnings|total comp\w*)\b"
    r".{0,40}\b(?:expectations?|expected|requirements?|range|target|desired|ask)\b"
    r"|\b(?:expected|desired|target|requested)\b.{0,25}"
    r"\b(?:salary|compensation|comp|pay|ote|earnings)\b",
    re.I,
)

# Default for BankMatcher.match(comp_expectation_block=...): a caller that has
# not established the job's role track gets no salary-expectation answer.
NO_JOB_CONTEXT = "comp-expectation-needs-human:no-job-context"


@lru_cache(maxsize=1)
def load_bank() -> dict[str, Any]:
    with open(BANK_PATH) as f:
        bank: dict[str, Any] = yaml.safe_load(f)
    return bank


def _alias_entries(bank: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(entry_id, kind, alias_text) rows for screening + scaffolds."""
    out = []
    for s in bank.get("screening", []):
        for a in s.get("aliases", []):
            out.append((s["id"], "screening", a))
    for s in bank.get("scaffolds", []):
        for a in s.get("aliases", []):
            out.append((s["id"], "scaffold", a))
    return out


def _norm_text(text: str) -> str:
    s = text.lower().replace("\xa0", " ").replace("✱", " ").replace("*", " ")
    return re.sub(r"\s+", " ", s).strip()


def _paren_repl(m: re.Match[str]) -> str:
    inner = m.group(1).strip(" .:;,")
    return " " if not inner or _HARMLESS_PAREN.fullmatch(inner) else f" {inner} "


def _field_label(label: str) -> str:
    """A label reduced to its field name: lowercased, required-markers and
    format-hint parentheticals dropped ("Name (first and last)" → "name",
    "Location (City, State)" → "location"), slashes tight, a leading "please
    enter your" removed.

    Any other parenthetical is a qualifier ("Email (the referrer's)", "Phone
    (referrer)") and only its brackets go: its words stay in the label, count
    toward is_field_like's 4-word limit and defeat the whole-label identity
    match (review 2026-10-08)."""
    s = re.sub(r"\s*/\s*", "/", _norm_text(label))
    while True:  # innermost first, so nested qualifiers are seen too
        s, n = _PAREN.subn(_paren_repl, s)
        if not n:
            break
    s = re.sub(r"\s+", " ", s).strip(" :.-–—")
    return _LEADING_ASK.sub("", s, count=1).strip()


def is_field_like(label: str) -> bool:
    """A short field name ("Email", "LinkedIn Profile URL", "Legal First and
    Last Name"), not a question: no "?" anywhere in the label (parentheticals
    included) and at most 4 content words, a qualifier's words counted."""
    if "?" in _norm_text(label):
        return False
    norm = _field_label(label)
    if not norm or "?" in norm:
        return False
    words = [w for w in re.split(r"[\s/,]+", norm) if w and w not in _FIELD_FILLER]
    return 0 < len(words) <= 4


def _alias_hit(low: str, alias: str) -> bool:
    """The alias appears in the label, or the label is a truncated form of the
    alias. The truncated direction needs at least 3 words: a one-word field
    like "Country" must not hit "…right to work in the country of employment?"."""
    al = _norm_text(alias)
    if not al:
        return False
    if al in low:
        return True
    return low in al and len(low.split()) >= 3


@lru_cache(maxsize=1)
def _embedder() -> Any:
    from fastembed import TextEmbedding

    return TextEmbedding("BAAI/bge-small-en-v1.5")


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=True))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    return float(num / (da * db)) if da and db else 0.0


def _target(target: str) -> dict[str, Any]:
    kind, entry = target.split(".", 1)
    if kind == "flag":
        return {"kind": "blank_flag", "reason": entry}
    return {"kind": kind, "entry_id": entry, "source": "answer_bank"}


def screening_entry(bank: dict[str, Any], entry_id: str) -> dict[str, Any] | None:
    """The bank's screening entry with this id, or None."""
    for s in bank.get("screening", []) or []:
        if s.get("id") == entry_id:
            return dict(s)
    return None


def _ack_label_key(label: str) -> str:
    """A label reduced for exact comparison: lowercase, no required/optional
    markers, no punctuation, single spaces."""
    s = _norm_text(label)
    s = re.sub(r"\((?:required|optional)\)", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def approved_acknowledgement(bank: dict[str, Any], label: str) -> bool:
    """True only when the label is, word for word, one the user approved in
    the legal_acknowledgement entry's `approved_labels`. Classifying free-text
    acknowledgements kept letting compound labels through ("agree to the
    arbitration agreement and to relocate"), so the auto-agree is an
    allowlist: a new or reworded label goes to the human (fail closed)."""
    entry = screening_entry(bank, "legal_acknowledgement")
    if not entry:
        return False
    approved = {_ack_label_key(a) for a in entry.get("approved_labels") or [] if str(a).strip()}
    return bool(approved) and _ack_label_key(label) in approved


def _comp_entry(bank: dict[str, Any]) -> dict[str, Any] | None:
    return screening_entry(bank, "comp_expectation")


def comp_expectation_block(
    title: str, comp_max: int | None, bank: dict[str, Any], filters: Filters | None
) -> str | None:
    """Why the bank's single salary-expectation figure must NOT go on this
    job's form, or None when it may.

    The bank holds one negotiation figure (comp_expectation.floor). It is set
    against the default comp floor (filters.comp.floor), not per role track.
    A job whose track has its own lower floor (in the shipped template
    sdr_bdr, sdr_bdr_manager and account_executive can sit under the default)
    would be priced out by it, so the field is left blank and
    flagged: the number is the human's call. Also blocked when the posting's
    own ceiling is under the bank figure. Fail closed: no config → blocked."""
    if filters is None:
        return "comp-expectation-needs-human:no-filters-config"
    cf = filters.comp_floor_for(title or "")
    default = filters.comp_floor
    if cf.track is not None and (not default or cf.floor < default):
        return f"comp-expectation-needs-human:track-{cf.track}-floor-{cf.floor}-below-{default}"
    entry = _comp_entry(bank) or {}
    try:
        bank_floor = int(entry.get("floor") or 0)
    except (TypeError, ValueError):
        bank_floor = 0
    if comp_max and bank_floor and int(comp_max) < bank_floor:
        return f"comp-expectation-needs-human:posted-max-{comp_max}-below-bank-figure"
    return None


def comp_expectation_block_for(
    title: str, comp_max: int | None, bank: dict[str, Any]
) -> str | None:
    """comp_expectation_block with filters loaded from config; a config that
    will not load blocks (fail closed)."""
    from jobkit.config import load_filters

    try:
        filters = load_filters()
    except Exception as e:  # noqa: BLE001 — unreadable config means no comp answer
        log("answer_bank.filters_unavailable", error=str(e)[:120])
        filters = None
    return comp_expectation_block(title, comp_max, bank, filters)


class BankMatcher:
    """Matches form-field labels against the answer bank. Embeddings are
    computed lazily so alias-only matching stays dependency-free (and testable)."""

    def __init__(self, bank: dict[str, Any] | None = None, use_embeddings: bool = True) -> None:
        self.bank = bank or load_bank()
        self.threshold = float(self.bank.get("match_threshold", 0.82))
        self.aliases = _alias_entries(self.bank)
        self.use_embeddings = use_embeddings
        self._alias_vecs: list[list[float]] | None = None

    def _embed_aliases(self) -> list[list[float]]:
        if self._alias_vecs is None:
            texts = [a for _, _, a in self.aliases]
            self._alias_vecs = [list(v) for v in _embedder().embed(texts)]
        return self._alias_vecs

    def _exact_alias(self, entry_id: str, label: str) -> bool:
        """The label IS one of this entry's own aliases, word for word
        (case, punctuation and required/optional markers aside), or, for the
        legal_acknowledgement entry, one of the user's approved labels."""
        key = _ack_label_key(label)
        if not key or not entry_id:
            return False
        if any(eid == entry_id and _ack_label_key(a) == key for eid, _, a in self.aliases):
            return True
        return entry_id == "legal_acknowledgement" and approved_acknowledgement(self.bank, label)

    def work_status_refusal(self, label: str, verdict: Mapping[str, Any]) -> str | None:
        """Why this verdict must not answer this label, as a blank_flag
        reason, or None when it may.

        1. work_auth ("Yes") never answers a label that asks whether the
           candidate needs sponsorship or a visa, or REQUIRES work
           authorization or a work permit, and sponsorship ("No") never
           answers one that asks whether they are authorized to work: not by
           alias, not by embedding, not by an alias that is the label word
           for word. The two answer with opposite polarity (2026-10-12:
           "Do you now, or will you ever, require employment sponsorship to
           work in the country where this job is located?" took work_auth's
           "Yes" at similarity 0.839).
        2. A label that mentions sponsorship, a visa or immigration
           (mentions_sponsorship) is answered only by the sponsorship entry,
           and only when the label is recognised as asking whether the
           candidate needs sponsorship (by embedding, only a label that IS
           the plain question, "you" the subject of require/need:
           _ASKS_NEEDS_SPONSORSHIP; and never with a word that turns it
           around: _REVERSED; and never one that asks something besides:
           _asks_besides, "Do you require visa sponsorship? Are you a U.S.
           citizen?"; by embedding, only allowlisted words from require/need
           to the question mark: _plain_ending, not "... to work in the
           UK?", not "require UK visa sponsorship?"), or by an entry whose
           own alias IS the label.
           Anything else (another entry, an identity or EEO value, the
           drafter) leaves it blank, flagged sponsorship-needs-human.
        3. Otherwise the work-status line of _work_status_crossed holds for
           any screening or scaffold verdict that is not the label word for
           word (work-status-needs-human)."""
        kind = str(verdict.get("kind") or "")
        if kind in ("blank_flag", "attach", "human"):
            return None
        low = _norm_text(label)
        text = _undot(low)
        entry_id = str(verdict.get("entry_id") or "") if kind in ("screening", "scaffold") else ""
        mentions = bool(_MENTIONS_SPONSORSHIP.search(text))
        prefix = SPONSORSHIP_NEEDS_HUMAN if mentions else "work-status-needs-human"
        exact = self._exact_alias(entry_id, label)
        if entry_id and not exact:
            crossed = _work_status_crossed(low, entry_id, by_embedding="similarity" in verdict)
            if crossed:
                return f"{prefix}:{crossed}"
        r = _reading(text)
        if entry_id == "work_auth" and r.sponsor:
            return f"{prefix}:sponsorship-question-took-work_auth"
        if entry_id == "work_auth" and r.requires:
            return f"{prefix}:requires-authorization-question-took-work_auth"
        if entry_id == "sponsorship" and r.authorized:
            return f"{prefix}:authorization-question-took-sponsorship"
        if exact or not mentions:
            return None
        if entry_id == "sponsorship":
            # by embedding, only a label that IS the plain question
            # (_ASKS_NEEDS_SPONSORSHIP: "you" the subject of require/need);
            # never a bare "visa sponsorship" nor a reversed wording
            asks = r.needs_sponsor if "similarity" in verdict else r.sponsor
            if asks and not r.reversed:
                return None
            return f"{prefix}:not-recognised-as-a-sponsorship-question"
        if entry_id:
            return f"{prefix}:only-the-sponsorship-entry-answers-not-{entry_id}"
        what = kind + (f".{verdict['entry_id']}" if verdict.get("entry_id") else "")
        return f"{prefix}:only-the-sponsorship-entry-answers-not-{what or 'unknown'}"

    def match(
        self,
        label: str,
        compliance_type: str | None = None,
        *,
        identity: str | None = None,
        comp_expectation_block: str | None = NO_JOB_CONTEXT,
    ) -> dict[str, Any]:
        """Returns {kind, entry_id?, source} — kind in
        identity | attach | screening | scaffold | eeo | novel | blank_flag.

        identity: the ATS field id (Greenhouse name, Lever name, Ashby path).
        comp_expectation_block: from comp_expectation_block() for this job;
        None allows the bank's comp_expectation entry, a reason string blanks
        and flags the field. The default blocks: no job context, no number.

        Order: EEO → comp history → ATS system id → comp expectation →
        screening/scaffold aliases → legal acknowledgements → residence
        questions → identity (field-like labels only) → embeddings → novel.
        Whatever path answered, the work-status gate (work_status_refusal)
        has the last word: a sponsorship, visa or immigration label that the
        sponsorship entry does not answer is blank + flagged, never drafted."""
        verdict = self._match(
            label,
            compliance_type,
            identity=identity,
            comp_expectation_block=comp_expectation_block,
        )
        refused = self.work_status_refusal(label.strip(), verdict)
        if refused:
            log("answer_bank.work_status_refused", label=label[:120], reason=refused)
            return {"kind": "blank_flag", "reason": refused}
        return verdict

    def _match(
        self,
        label: str,
        compliance_type: str | None = None,
        *,
        identity: str | None = None,
        comp_expectation_block: str | None = NO_JOB_CONTEXT,
    ) -> dict[str, Any]:
        """BankMatcher.match before the final work-status gate."""
        lab = label.strip()
        low = _norm_text(lab)

        # EEO: only the eeo block may answer; absent entry -> blank + flag
        if compliance_type == "eeo" or _EEO_PATTERN.search(low):
            eeo = self.bank.get("eeo", {}) or {}
            key = self._eeo_key(low)
            if key and eeo.get(key):
                return {"kind": "eeo", "entry_id": key, "answer": eeo[key], "source": "answer_bank"}
            return {"kind": "blank_flag", "reason": "eeo-no-stored-choice"}

        # salary history: never answer (hard rule 5)
        if _COMP_HISTORY.search(low):
            return {"kind": "blank_flag", "reason": "salary-history-never-answer"}

        # the ATS's own id for a standard field outranks its label
        sys_target = _SYSTEM_FIELDS.get((identity or "").strip().lower())
        if sys_target:
            return _target(sys_target)

        # salary expectation: the bank's entry or the human — never the drafter
        if _COMP_EXPECTATION.search(low):
            if comp_expectation_block:
                return {"kind": "blank_flag", "reason": comp_expectation_block}
            if _comp_entry(self.bank) is None:
                return {"kind": "blank_flag", "reason": "comp-expectation-not-in-bank"}
            return {"kind": "screening", "entry_id": "comp_expectation", "source": "answer_bank"}

        ack = legal_acknowledgement(lab)
        # a label the user approved word for word is answered by the
        # legal_acknowledgement entry before any other entry can claim it
        if approved_acknowledgement(self.bank, lab):
            return {
                "kind": "screening",
                "entry_id": "legal_acknowledgement",
                "source": "answer_bank",
            }

        def gated(verdict: dict[str, Any]) -> dict[str, Any]:
            # a comp_expectation hit by alias or embedding passes the same gate
            if verdict.get("entry_id") == "comp_expectation" and comp_expectation_block:
                return {"kind": "blank_flag", "reason": comp_expectation_block}
            # the user's auto-agree covers only acknowledgement-shaped labels
            # of the covered kinds, however the label reached the entry: a
            # broad alias ("I understand and agree that") or a near embedding
            # must not agree to a privacy notice or answer a skill question
            # the user's auto-agree answers ONLY labels on its exact allowlist
            # (2026-10-11: the allowlist is the single criterion; the shape
            # classifier no longer vetoes a label the user approved word for
            # word, e.g. Greenhouse's "I certify that the information ...")
            if verdict.get("entry_id") == "legal_acknowledgement" and not approved_acknowledgement(
                self.bank, lab
            ):
                why = (
                    "not-an-approved-label"
                    if ack == "covered"
                    else (ack or "not-an-acknowledgement")
                )
                return {"kind": "blank_flag", "reason": f"legal-acknowledgement-needs-human:{why}"}
            # a label that is (or contains) a legal acknowledgement must not
            # borrow ANOTHER entry's answer: "I agree to the arbitration
            # agreement and confirm I am at least 18" would tick the box with
            # age_18's "Yes" and agree to the terms with it (review 2026-10-10).
            # The one exception is a label that IS that entry's alias, word for
            # word ("Do you consent to a background check?").
            # work authorization and sponsorship answer with opposite
            # polarity ("Yes, authorized" / "No, no sponsorship"), so one
            # entry's answer on the other's question is a false statement
            # (2026-10-12: "...require employment sponsorship ...?" took
            # work_auth's "Yes" at similarity 0.839). Across that
            # line, or on a label asking both, only an alias that IS the
            # label word for word answers. By embedding, the label must also
            # be recognised as asking that entry's side, and no other entry
            # answers a work-status question (review 2026-10-12).
            refused = self.work_status_refusal(lab, verdict)
            if refused:
                return {"kind": "blank_flag", "reason": refused}
            other = verdict.get("entry_id")
            if ack and other and other != "legal_acknowledgement":
                entry = screening_entry(self.bank, str(other)) or {}
                exact = {_ack_label_key(a) for a in entry.get("aliases") or []}
                if _ack_label_key(lab) not in exact:
                    return {
                        "kind": "blank_flag",
                        "reason": f"legal-acknowledgement-needs-human:{ack}",
                    }
            return verdict

        # screening/scaffold aliases BEFORE identity: questions are answered by
        # the screening bank, not by whichever identity word they mention
        for entry_id, kind, alias in self.aliases:
            if _alias_hit(low, alias):
                return gated({"kind": kind, "entry_id": entry_id, "source": "answer_bank"})

        # legal acknowledgements: the user's legal_acknowledgement entry for
        # the kinds it covers, otherwise the human; never drafted
        if ack == "covered" and approved_acknowledgement(self.bank, lab):
            return {
                "kind": "screening",
                "entry_id": "legal_acknowledgement",
                "source": "answer_bank",
            }
        if ack == "covered" and screening_entry(self.bank, "legal_acknowledgement"):
            # a covered kind, but not word for word on the user's allowlist
            return {
                "kind": "blank_flag",
                "reason": "legal-acknowledgement-needs-human:not-an-approved-label",
            }
        if ack:
            return {"kind": "blank_flag", "reason": f"legal-acknowledgement-needs-human:{ack}"}

        # where the candidate lives: the bank's residence entries, phrased any
        # way ("country in which you are located", "country you live in");
        # with no entry, the label goes on to the identity patterns
        place = residence_question(lab)
        if place and screening_entry(self.bank, f"{place}_of_residence"):
            entry = f"{place}_of_residence"
            return {"kind": "screening", "entry_id": entry, "source": "answer_bank"}

        # identity: whole-label match on a short field name only
        if is_field_like(lab):
            norm = _field_label(lab)
            for pat, target in _IDENTITY_PATTERNS:
                if pat.fullmatch(norm):
                    return _target(target)

        # embedding similarity
        if self.use_embeddings and self.aliases:
            try:
                qv = list(next(iter(_embedder().embed([lab]))))
                vecs = self._embed_aliases()
            except Exception as e:  # noqa: BLE001 — no embedder: a novel question, never a guess
                log("answer_bank.embed_failed", error=str(e)[:120])
                self.use_embeddings = False
                return {"kind": "novel"}
            best_i, best_s = -1, 0.0
            for i, av in enumerate(vecs):
                s = _cosine(qv, av)
                if s > best_s:
                    best_i, best_s = i, s
            if best_i >= 0 and best_s >= self.threshold:
                entry_id, kind, _ = self.aliases[best_i]
                return gated(
                    {
                        "kind": kind,
                        "entry_id": entry_id,
                        "source": "answer_bank",
                        "similarity": round(float(best_s), 3),
                    }
                )

        return {"kind": "novel"}

    @staticmethod
    def _eeo_key(label: str) -> str | None:
        """The eeo-block key a demographic label asks for, or None (blank +
        flag). Word-bounded where a substring would mislead: "transgender"
        contains "gender", "embrace" contains "race".

        Mentioning a word is not asking the question (review 2026-10-10):
        A form's "Do you identify as part of the Lesbian, Bisexual, Gay,
        Transgender, … (LBGTQIA+) community?" asks about the whole community
        and the bank has no entry for it, so it stays blank. Transgender is
        the key only when the label asks it ("Do you identify as
        transgender?"); a gender-identity label that lists it as an example
        ("Gender identity (e.g. woman, man, transgender, non-binary)") asks
        for gender. Any other mention of it, or a question about the sex
        assigned at birth (the transgender question with its polarity
        reversed), stays blank."""
        if "hispanic" in label or "latino" in label:
            return "hispanic_latino"
        if "pronoun" in label:
            return None  # no stored pronoun choice — blank + flag, never inherited
        asked = _without_examples(label)
        if _EEO_COMMUNITY.search(label) or _EEO_ASSIGNED_AT_BIRTH.search(asked):
            return None
        if "transgender" in label:
            if _EEO_TRANSGENDER_QUESTION.search(asked):
                return "transgender"
            if "transgender" in asked:
                return None  # mentioned, not asked
            # only an example in a list: the rest of the label decides
        if re.search(r"\bsexual (?:orientation|identity)\b|\bsexuality\b|^orientation\b", label):
            return "sexual_orientation"
        if re.search(r"\bgender\b", label):
            return "gender"
        if re.search(r"\brac(?:e|ial)\b|\bethnic", label):
            return "race_ethnicity"
        if "veteran" in label:
            return "veteran_status"
        if "disab" in label:
            return "disability_status"
        return None


# Dotted abbreviations lose their dots before the work-status checks, so the
# period in "U.S." does not read as the end of a clause: "need U.S.
# sponsorship" → "need us sponsorship", "u.s.a." → "usa", "e.g." → "eg".
_DOTTED_ABBREV = re.compile(r"\b[a-z](?:\.[a-z]\b)+\.?")


def _undot(low: str) -> str:
    return _DOTTED_ABBREV.sub(lambda m: m.group(0).replace(".", ""), low)


# What a work-status question asks: about needing sponsorship, or about being
# authorized to work. "sponsor" alone is sales vocabulary (executive sponsor),
# so sponsorship needs the require/need wording, a visa, or "you". The windows
# stop at a question mark only: a period inside the question ("U.S.", "the
# U.S. government's sponsorship") must not hide it (review 2026-10-12).
_ASKS_SPONSORSHIP = re.compile(
    r"\b(?:require|need)\w*\b[^?]{0,60}(?<!executive )(?<!exec )\bsponsor(?:ship|ed|ing)?\b"
    r"|\bsponsor (?:you|your)\b|\bvisas?\s+sponsor\w*"
    r"|\b(?:require|need)\w*\b[^?]{0,40}\b(?:visas?|h-?1-?b)\b"
)
# What a verdict reached by embedding similarity needs before the
# sponsorship entry's "No" answers: the label must BE the plain question, in
# one of the shapes below. A bare "visa sponsorship" is a mention, not the
# question, and reads either way: "Are you exempt from visa sponsorship
# requirements?" and "Is visa sponsorship unnecessary in your case?" matched
# _ASKS_SPONSORSHIP and took the "No" (review 2026-10-15, round 3). The
# embedder does not read polarity, so the label's own words must ask whether
# the candidate needs sponsorship.
#
# An allowlist of shapes, not a list of reversing words (review 2026-10-15,
# round 4): with require/need anywhere before the sponsor word, "Are you
# excused from needing visa sponsorship?", "Can you rule out needing visa
# sponsorship?", "Is it true that you require zero sponsorship?" and "Is the
# need to sponsor you nonexistent?" took the "No"; a candidate who needs none
# answers each "Yes". No word list can name every way to turn the question
# around, so the label must open with the question itself: "will/do/would/
# does you", only connector words ("now", "or", "in the future", "ever",
# ",", "will you"...), then require/need, then (within 60 characters, no
# quantity word in between) the sponsor or visa word: Greenhouse's "Do you now, or
# will you ever, require employment sponsorship...", "Will you now or in the
# future require Initech to sponsor an immigration case...". Or someone else
# needs to sponsor you: "Will we need to sponsor you?". _REVERSED and the
# negation checks still veto. Anything else is blank + flag (a false flag
# sends the question to the human; a wrong polarity sends a false statement).
_SPONSOR_CONNECTOR = (
    r"(?:now|currently|presently|or|and|either|in|the|near|future|ever|at|any|some|point"
    r"|time|later|also|still|be|will|would|do|does|you)"
)
# words between the verb and the sponsor word that change how much is needed
# ("require zero sponsorship", "need nothing but sponsorship")
_SPONSOR_SPAN = (
    r"(?:(?!\b(?:no|zero|nothing|none|nobody|no-?one|neither|nor|never|not|less|fewer|minus"
    r"|without|except|beyond|instead|rather|other|but|avoid\w*|free|exempt\w*)\b)[^?.;])"
)
_ASKS_NEEDS_SPONSORSHIP = re.compile(
    r"^\W*(?:will|would|do|does)\s+you"
    rf"(?:\s*,?\s*{_SPONSOR_CONNECTOR}\b)*\s*,?\s+(?P<verb>requir(?:e|es|ing)|need(?:s|ing)?)\b"
    rf"(?:{_SPONSOR_SPAN}{{0,60}}?(?<!executive )(?<!exec )\bsponsor(?:ship|ed|ing)?\b"
    rf"|{_SPONSOR_SPAN}{{0,40}}?\b(?:visas?|h-?1-?b)\b)"
    r"|^\W*(?:will|would|do|does)\s+(?!(?:no|nobody|no-?one|none|neither)\b)"
    r"(?:we|(?:the|our|this)\s+[\w&'’-]+|[\w&'’-]+)"
    rf"(?:\s*,?\s*{_SPONSOR_CONNECTOR}\b)*\s*,?\s+(?:need|require|have)\s+to\s+sponsor\s+you\b"
)
# "Are you able to work in the U.S.?" and "Can you work in the US ...?" ask
# about authorization when a country follows; "able to work in our NYC
# office", "...in US time zones" and "can you work weekends" do not.
_WORK_COUNTRY = (
    r"(?:the )?(?:us|usa|united states|america|canada|uk|united kingdom"
    r"|(?:this|that|the) country|country (?:where|in which|of))\b"
    r"(?!\s*(?:-\s*)?(?:based\s+)?(?:hours|time|timezones?|business|working hours|shifts?"
    r"|eastern|pacific|central|mountain|est|pst|cst|mst|et|pt|ct)\b)"
)
_ASKS_AUTHORIZATION = re.compile(
    r"\b(?:authori[sz]ed|eligible|permitted|entitled) to (?:work|be employed)\b"
    r"|\b(?:legally|lawfully) (?:(?:able|allowed) to )?(?:work|be employed)\b"
    r"|\bable to (?:legally|lawfully) work\b"
    r"|\b(?:work|employment) (?:authori[sz]ation|permits?|eligibility)\b"
    r"|\bauthori[sz]ation to work\b|\bright to work\b"
    rf"|\b(?:able to|can you|could you) work (?:\w+ ){{0,2}}?in {_WORK_COUNTRY}"
)
# A label that asks the sponsorship question in reverse: "Can you work in
# the US WITHOUT visa sponsorship?" is answered Yes by a candidate who needs
# none, so the sponsorship entry's "No" would say the opposite (review
# 2026-10-12). Neither side's entry answers it unless an alias is the label
# word for word. "Without executive sponsorship" is sales vocabulary.
_SPONSORSHIP_NEGATED = re.compile(
    r"\b(?:without|no (?:need|requirement) (?:for|of))\b[^?]{0,40}"
    r"(?<!executive )(?<!exec )\b(?:visas?|sponsor\w*|h-?1-?b)\b"
)
# A label asking whether the candidate REQUIRES work authorization or a work
# permit asks the sponsorship question in other words: "Yes" says the
# candidate needs one (review 2026-10-15: with the real embedder "Do you
# require work authorization to be employed in the United States?" matched
# work_auth at 0.91 and "Will you require a work permit to work in the
# United States?" at 0.877; work_auth's "Yes" says the candidate needs a
# permit). work_auth never answers it; the sponsorship entry only by an alias
# that is the label word for word, or when the label also asks about
# sponsorship. Active ("do you require a work permit") and passive ("is a
# work permit required"; the passive window stops at a comma or a
# conjunction: "Do you have work authorization, or is it required that we
# sponsor you?" is not one). The active window stops at a sponsor word (then
# sponsorship is what is required, "require visa sponsorship to maintain
# that work authorization"), at the end of a question or sentence, and at a
# second question ("... relocation, and do you have a work permit?").
_PERMIT = (
    r"(?:(?:work|employment) (?:authori[sz]ation|permits?)"
    r"|authori[sz]ation to (?:work|be employed)|permits? to work)"
)
_NEW_QUESTION = (
    r"\b(?:and|or|but)\s+(?:are|do|does|did|have|has|will|would|can|could|is|were|was)\s+you\b"
)
_REQUIRES_AUTHORIZATION = re.compile(
    rf"\b(?:require|need)\w*\b(?:(?!\bsponsor|{_NEW_QUESTION})[^?.;]){{0,50}}?\b{_PERMIT}\b"
    rf"|\b{_PERMIT}\b(?:(?!\b(?:and|or|but)\b)[^?.;,]){{0,25}}?\b(?:required|needed|necessary)\b"
)
# A negation near the work-status words turns the question around: "Can you
# confirm that you will NOT require sponsorship?" and "Is it correct that
# you WON'T need visa sponsorship?" are answered "Yes" by a candidate who
# needs none, so the sponsorship entry's "No" would say the opposite (review
# 2026-10-15: the real embedder put both at 0.877 and 0.934 to sponsorship).
# Neither side's entry answers such a label unless an alias is the label
# word for word. "no" alone counts only right before a visa or sponsorship
# ("no sponsorship required"): "(Yes/No) Will you require..." must not.
_NEGATION = (
    r"(?:not|never|no longer|cannot|neither|nor"
    r"|(?:can|won|don|doesn|didn|isn|aren|wasn|weren|wouldn|shouldn|haven|hasn)['’]?t)"
)
_STATUS_WORD = (
    r"(?:requir\w*|need\w*|sponsor\w*|authori[sz]\w*|eligib\w*|permit\w*|entitled"
    r"|legal(?:ly)?|lawful(?:ly)?|visas?|h-?1-?b|right to work"
    r"|able to (?:legally |lawfully )?work)"
)
_NO_SPONSORSHIP = (
    r"\bno\s+(?:(?:visa|immigration|employment|employer|work)\s+)?(?:sponsor\w*|visas?)\b"
)
_NEGATED_NEAR = re.compile(
    rf"\b{_NEGATION}(?:[^\w?]+\w+){{0,3}}?[^\w?]+{_STATUS_WORD}\b|{_NO_SPONSORSHIP}"
)
# any negation at all: a verdict reached by embedding similarity alone does
# not answer a work-status label that has one anywhere (the embedder does not
# read polarity: the 0.839 case above and the four labels above)
_ANY_NEGATION = re.compile(rf"\b{_NEGATION}\b|{_NO_SPONSORSHIP}")
# Words that turn a work-status question around with no negation word
# (review 2026-10-15, round 3): "Are you exempt from visa sponsorship
# requirements?", "Is visa sponsorship unnecessary in your case?", "…, free
# of any visa sponsorship requirement?", "(i.e., visa sponsorship is
# irrelevant to you)". A candidate who needs no sponsorship answers each
# "Yes", so the sponsorship entry's "No" would say the opposite. Anywhere in
# the label, whatever matched it short of an alias that is the label word for
# word: neither side's entry answers it (blank + flag). A false flag sends
# the question to the human; a wrong polarity sends a false statement.
_REVERSED = re.compile(
    r"\b(?:exempt\w*|unnecessary|needless|irrelevant|inapplicable|not\s+applicable|n/a"
    r"|free\s+(?:of|from)|waive[ds]?|waivers?|moot|(?:do|does|did)\s+not\s+need\w*"
    r"|(?:don|doesn|didn)['’]?t\s+need\w*)(?!\w)"
)
# the bank entries on each side of that line (data/answer_bank.example.yaml ids)
_WORK_STATUS_SIDES = {"work_auth": "authorization", "sponsorship": "sponsorship"}

# Any mention of sponsorship, a visa or immigration (2026-10-15, after a
# sponsorship question was staged with work_auth's "Yes").
# Such a label is answered only by the bank's sponsorship entry, or by an
# entry whose alias IS the label (BankMatcher.work_status_refusal). Sales
# vocabulary ("executive sponsor") and Visa the company are caught too, on
# purpose: a false flag sends a question to the human, a wrong polarity sends
# a false statement to the employer.
_MENTIONS_SPONSORSHIP = re.compile(r"\bsponsor\w*|\bvisas?\b|\bimmigra\w*|\bh-?1-?b\b")
# the blank_flag reason prefix for such a label
SPONSORSHIP_NEEDS_HUMAN = "sponsorship-needs-human"


def mentions_sponsorship(label: str) -> bool:
    """The label mentions sponsorship, a visa, an H-1B or immigration."""
    return bool(_MENTIONS_SPONSORSHIP.search(_undot(_norm_text(label))))


def _asks_work_status(low: str) -> bool:
    """The label asks about sponsorship, work authorization, visas,
    citizenship or immigration status (never_draft's work-status kind)."""
    text = _undot(low)
    return bool(
        _WORK_STATUS.search(text)
        or _ASKS_SPONSORSHIP.search(text)
        or _ASKS_AUTHORIZATION.search(text)
        or _SPONSORSHIP_NEGATED.search(text)
        or _REQUIRES_AUTHORIZATION.search(text)
    )


class _Reading(NamedTuple):
    """What a work-status label asks, from the patterns above (undotted text)."""

    sponsor: bool  # whether the candidate needs sponsorship or a visa
    needs_sponsor: bool  # ...as the plain question: "you" the subject of require/need
    authorized: bool  # whether the candidate is authorized to work
    without: bool  # the sponsorship question in reverse ("... without sponsorship?")
    requires: bool  # whether the candidate REQUIRES work authorization or a permit
    negation: bool  # a negation near the work-status words
    any_negation: bool  # a negation anywhere
    reversed: bool  # a word that turns the question around ("exempt from")
    besides: bool  # it asks something besides the sponsorship question (_asks_besides)
    # the plain question's opening, with words between require/need and the
    # sponsor or visa word that are not on the allowlist (_unlisted_words)
    unlisted_before: bool
    # ... or with such words after the sponsor or visa word
    unlisted_ending: bool


# A label that asks something besides the sponsorship question (review
# 2026-10-20): "Do you require visa sponsorship? Are you a U.S. citizen or
# green card holder?" took the sponsorship entry's one "No", by embedding and
# by alias, and a citizen answers the second question "Yes". _NEW_QUESTION
# finds only "…, or are you…"; any other sentence or question, before or
# after the one asking about sponsorship, counts too. Only required/optional
# markers and a Yes/No hint may stand beside it ("Do you require visa
# sponsorship? (Yes/No) *"). A false flag sends the question to the human; a
# second question answered "No" sends a false statement.
_SENTENCE_END = re.compile(r"[?!;]|\.(?!\w)")
_MARKERS_ONLY = re.compile(r"(?:[\W\d_]|\b(?:required|optional|mandatory|yes|no|or|and|y|n)\b)*")


def _asks_besides(text: str, m: re.Match[str]) -> bool:
    """Does the label (undotted) ask anything besides the question that `m`,
    a match of one of the sponsorship patterns, is in: a second question
    after it in the same sentence (_NEW_QUESTION), or another sentence or
    question with words in it before or after (_SENTENCE_END,
    _MARKERS_ONLY)?"""
    if re.search(_NEW_QUESTION, text[m.end() :]):
        return True
    ends = [e.end() for e in _SENTENCE_END.finditer(text)]
    start = max((e for e in ends if e <= m.start()), default=0)
    stop = next((e for e in ends if e >= m.end()), len(text))
    return _MARKERS_ONLY.fullmatch(f"{text[:start]} {text[stop:]}") is None


# What may follow the sponsor or visa word of the plain question
# (_ASKS_NEEDS_SPONSORSHIP) before its "?" when the verdict was reached by
# embedding (review 2026-10-20, round 3). _ASKS_NEEDS_SPONSORSHIP checks the
# words up to the sponsor or visa word and took any after it: "Do you require
# visa sponsorship or relocation assistance?" (the "No" also answers the
# relocation question), "... to work in the UK?" and "... to work in Canada?"
# (a U.S. candidate needs sponsorship there), "... for any family members?"
# and "Will you need a visa to travel to China for this role?" took the
# sponsorship entry's "No". An allowlist again, not a list of bad endings:
# only these pieces, in any order, separated by spaces or commas —
#   a time:        "now or in the future", "now, or will you ever", "currently"
#   a purpose:     "for this role/position/job", "for employment (visa status)",
#                  "for a visa", "(sponsorship) of an employment visa"
#   the country:   "(to work) in the US / U.S. / United States", "(to work) in
#                  the country where this job/position is located",
#                  "in the country in which you are applying"
#   examples:      a parenthesis of visa types "(e.g., H-1B, TN, etc.)", or of
#                  markers "(Yes/No)", "(required)"
#   and, from labels seen on real application forms: "to remain in your
#   current location", "(in order) to employ you", "if you join <Company>",
#   and right after the word: what is sponsored ("an immigration case",
#   "you", "your visa") or what the visa is ("an H-1B transfer"), and the
#   closing quote of a "commence (“sponsor”)" label.
# Anything else ("or"/"and" plus another thing, "for <a person>", "to work in
# <another country>", travel) is blank + flag: a false flag sends the
# question to the human; a "No" to a second thing sends a false statement.
_TAIL_US = r"(?:the\s+)?(?:us|usa|united\s+states(?:\s+of\s+america)?|america)"
_TAIL_JOB = r"(?:job|position|role|opening|opportunity)"
_TAIL_APPLYING = r"(?:you\s+are|you['’]re)\s+applying(?:\s+for)?"
_TAIL_THIS_COUNTRY = (
    r"(?:the\s+)?country\s+(?:where|in\s+which)\s+"
    rf"(?:(?:this|the)\s+{_TAIL_JOB}(?:\s+{_TAIL_APPLYING})?\s+is\s+(?:located|based)"
    rf"|{_TAIL_APPLYING})"
)
_TAIL_TIME = (
    r"(?:(?:either\s+)?(?:now|currently)\s*,?\s*(?:or|and)\s*,?\s*(?:will\s+you\s+)?"
    r"(?:(?:at\s+)?(?:any|some)\s+(?:point|time)\s+)?(?:in\s+the\s+(?:near\s+)?future|ever)"
    r"|(?:at\s+(?:any|some)\s+(?:point|time)\s+)?in\s+the\s+(?:near\s+)?future"
    r"|at\s+(?:any|some)\s+(?:point|time)|now|currently|ever)"
)
_TAIL_WORDS = (
    rf"{_TAIL_TIME}"
    rf"|for\s+(?:(?:this|the)\s+{_TAIL_JOB}|employment(?:\s+visa(?:\s+status)?)?)"
    r"|(?:for|of)\s+(?:an?\s+|your\s+)?(?:(?:employment|work|h-?1-?b)\s+)?visa(?:\s+status)?"
    rf"|(?:(?:in\s+order\s+)?to\s+work\s+)?in\s+(?:{_TAIL_US}|{_TAIL_THIS_COUNTRY})"
    r"|to\s+remain\s+in\s+your\s+current\s+location"
    r"|(?:in\s+order\s+)?to\s+employ\s+you"
    r"|if\s+(?:you\s+join\s+[\w&'’.-]+|(?:you\s+are\s+)?(?:hired|selected))"
)
# a visa type in an examples parenthesis: H-1B, TN, E-3, F-1, O-1, STEM OPT...
_TAIL_VISA = (
    r"(?:h-?1-?b\d?|[a-z]-?\d+[a-z]?|tn|stem|opt|cpt|ead|green\s+card|etc\.?|visas?|status)"
)
_TAIL_PAREN = (
    r"\(\s*(?:(?:eg|ie|such\s+as|including|for\s+example)?[\s,:]*"
    rf"{_TAIL_VISA}(?:[\s,/]+(?:(?:or|and)\s+)?{_TAIL_VISA})*[\s,.]*"
    r"|(?:required|optional|mandatory|yes|no|y|n|or|[\s/])+)\)"
)
_SPONSOR_TAIL = re.compile(
    r"[”\"’')\]]*"  # "(“sponsor”)": the word's own closing quote and bracket
    # right after the word: what is sponsored, or what the visa is
    r"(?:\s+(?:you|your\s+(?:(?:work|employment|h-?1-?b)\s+)?visa"
    r"|an?\s+(?:immigration\s+case|(?:(?:work|employment|h-?1-?b)\s+)?visa)"
    r"|(?:visa\s+)?(?:transfer|status|petition)|visa)(?![\w-]))?"
    rf"(?:[\s,]*(?:(?:{_TAIL_WORDS})(?![\w-])|{_TAIL_PAREN}))*"
    r"[\s,.]*"
)
# What may stand between require/need and the sponsor or visa word of the
# plain question (review 2026-10-20, round 4). _ASKS_NEEDS_SPONSORSHIP takes up
# to 60 characters there, and the tail check began where its match ended: at
# the LAST sponsor word when a visa word came first, so "Do you require a visa
# or relocation sponsorship?" and "Will you need a visa to travel to China,
# or sponsorship?" passed with everything between unchecked; and any noun
# before the word went unread: "Do you require relocation assistance or visa
# sponsorship?", "... UK visa sponsorship?", "... family visa sponsorship?",
# "Will you need a Canadian work visa?" took the sponsorship entry's "No".
# The allowlist now runs from the verb: only these words before the FIRST
# sponsor, visa or H-1B word, then that word, then the tail allowlist above —
#   an article and what kind of sponsorship or visa: "a", "an", "the",
#     "employment", "employer", "work", "immigration", "visa", "H-1B"
#     ("require employer sponsorship", "require employment visa sponsorship",
#     "need an H-1B transfer"), and the one country the tail allows, the US
#     ("need U.S. sponsorship", "require the U.S. government's sponsorship");
#   who sponsors: one word (the company, "us") or "the company", then "to",
#     and a "commence (“" before the sponsor word: "require Globex to sponsor
#     an immigration case", "require Vandelay to commence (“sponsor”) ...".
# Anything else is blank + flag, as after the word.
_SPONSOR_BEFORE = (
    r"\s*(?:"
    r"(?:the\s+)?(?!(?:no|zero|nothing|none|nobody|no-?one|neither|nor|never|not)\b)"
    r"[\w&'’-]+\s+to\s+(?:commence\s*\(\s*[“\"‘']?\s*)?"
    r"|(?:(?:an?|the|employment|employer|work|immigration|visas?|h-?1-?b"
    r"|(?:us|usa|united\s+states)(?:\s+government['’]?s)?)\s+)*"
    r")"
)
_SPONSOR_WORD = r"(?:sponsor(?:ship|ed|ing)?|visas?|h-?1-?b)(?![\w-])"
# from the verb to the sponsor or visa word (where "before" ends)
_SPONSOR_LEAD = re.compile(_SPONSOR_BEFORE + _SPONSOR_WORD)
# from the verb to the question mark
_SPONSOR_PLAIN = re.compile(_SPONSOR_BEFORE + _SPONSOR_WORD + _SPONSOR_TAIL.pattern)


def _unlisted_words(text: str, m: re.Match[str]) -> str | None:
    """Where the plain question that `m` (a match of _ASKS_NEEDS_SPONSORSHIP)
    opens leaves the allowlist, up to its question mark: "before" (a word
    between require/need and the sponsor or visa word that _SPONSOR_BEFORE
    does not name), "after" (a word after it that _SPONSOR_TAIL does not
    name), or None when every word is on it. What follows the question mark
    is _asks_besides's to judge."""
    verb = m.end("verb")
    if verb < 0:  # "Will we need to sponsor you": the match ends at the word
        tail = text[m.end() :].split("?", 1)[0]
        return None if _SPONSOR_TAIL.fullmatch(tail) else "after"
    rest = text[verb:].split("?", 1)[0]
    if _SPONSOR_PLAIN.fullmatch(rest):
        return None
    return "after" if _SPONSOR_LEAD.match(rest) else "before"


def _plain_ending(text: str, m: re.Match[str]) -> bool:
    """Is everything from require/need (or from "sponsor you") to the question
    mark of the plain question that `m` (a match of _ASKS_NEEDS_SPONSORSHIP)
    opens on the allowlist (_unlisted_words: _SPONSOR_BEFORE, the sponsor or
    visa word, _SPONSOR_TAIL)?"""
    return _unlisted_words(text, m) is None


def _asks_needs_sponsorship(text: str) -> bool:
    """The label IS the plain needs-sponsorship question (_ASKS_NEEDS_SPONSORSHIP),
    asks nothing besides (_asks_besides: "Do you require sponsorship, or
    are you able to start now?", "Do you require visa sponsorship? Are you a
    U.S. citizen?": one "No" would answer both), and has only allowlisted
    words from require/need to its question mark (_plain_ending: not "... or
    relocation assistance?", "... to work in the UK?", "... for any family
    members?", "require relocation assistance or visa sponsorship?", "require
    a visa or relocation sponsorship?", "UK visa sponsorship?")."""
    m = _ASKS_NEEDS_SPONSORSHIP.search(text)
    return m is not None and not _asks_besides(text, m) and _plain_ending(text, m)


def _reading(text: str) -> _Reading:
    requires = bool(_REQUIRES_AUTHORIZATION.search(text))
    q = _ASKS_NEEDS_SPONSORSHIP.search(text)
    unlisted = _unlisted_words(text, q) if q else None
    # "do you require a work permit" is not an authorization question: the
    # permit is what is required. Whatever else the label says still counts
    # ("are you authorized to work, or do you need a work permit?").
    rest = _REQUIRES_AUTHORIZATION.sub(" ", text) if requires else text
    return _Reading(
        sponsor=bool(_ASKS_SPONSORSHIP.search(text)),
        needs_sponsor=_asks_needs_sponsorship(text),
        authorized=bool(_ASKS_AUTHORIZATION.search(rest)),
        without=bool(_SPONSORSHIP_NEGATED.search(text)),
        requires=requires,
        negation=bool(_NEGATED_NEAR.search(text)),
        any_negation=bool(_ANY_NEGATION.search(text)),
        reversed=bool(_REVERSED.search(text)),
        besides=bool(
            (m := _ASKS_SPONSORSHIP.search(text) or _ASKS_NEEDS_SPONSORSHIP.search(text))
            and _asks_besides(text, m)
        ),
        unlisted_before=unlisted == "before",
        unlisted_ending=unlisted == "after",
    )


def _work_status_crossed(low: str, entry_id: str, *, by_embedding: bool = False) -> str | None:
    """Why this entry must not answer this label, or None.

    work_auth and sponsorship answer with opposite polarity, so neither may
    answer the other side's question, nor a label asking both. A verdict
    reached by embedding similarity rather than an alias needs more: the
    label must be recognised as asking that entry's own side, or a wording
    the patterns miss would take whichever entry the embedder put nearest
    (0.839 in the case above). Any other entry may not answer a sponsorship or
    authorization question ("Are you at least 18 and will you require
    sponsorship?" must not take age_18's "Yes"), nor, by embedding, any
    work-status question ("Are you a U.S. citizen?" must not take
    based_in_us's "Yes").

    A label asking it in reverse ("...work in the US without visa
    sponsorship?") goes to neither side: sponsorship's "No" would say the
    candidate cannot work without it, and work_auth's "Yes" says nothing
    about sponsorship (review 2026-10-12).

    Review 2026-10-15: a label asking whether the candidate REQUIRES work
    authorization or a permit is a needs-sponsorship question that work_auth
    never answers and sponsorship answers only when the label also asks
    about sponsorship; a label with a negation near its work-status words
    ("will not require", "won't need") goes to neither side; and a verdict
    reached by embedding does not answer a label with any negation.

    Round 3: a label with a word that turns it around ("exempt from",
    "unnecessary", "irrelevant", "free of", "not applicable"; _REVERSED)
    goes to neither side, and by embedding the sponsorship entry answers
    only a label whose own words ask whether the candidate needs
    sponsorship (_ASKS_NEEDS_SPONSORSHIP): a bare "visa sponsorship" is
    not enough.

    Round 4: by embedding, that is an allowlist of question shapes ("you"
    the subject of require/need, only connector words between), not
    require/need anywhere: "Can you rule out needing visa sponsorship?"
    and "Is it true that you require zero sponsorship?" are blank + flag.

    Review 2026-10-20: the sponsorship entry, by alias or by embedding,
    never answers a label that asks something besides (_asks_besides): "Do
    you require visa sponsorship? Are you a U.S. citizen or green card
    holder?" is blank + flag. Round 3: by embedding, only allowlisted words
    may follow the sponsor or visa word (_plain_ending): "... or relocation
    assistance?", "... to work in the UK?", "... for any family members?"
    are blank + flag. Round 4: and only allowlisted words may stand between
    require/need and the FIRST sponsor or visa word (_SPONSOR_BEFORE): "Do
    you require relocation assistance or visa sponsorship?", "... UK visa
    sponsorship?", "Do you require a visa or relocation sponsorship?" are
    blank + flag. Round 5: the ending allowlist also holds for a verdict
    reached by an alias that is not the label word for word: only
    allowlisted words may follow the sponsor or visa word, since an alias
    is matched as a substring ("Do you now, or will you ever, require
    employment sponsorship to work in Canada?" contains the user's alias).
    The words before it are already the alias's own."""
    r = _reading(_undot(low))
    side = _WORK_STATUS_SIDES.get(entry_id)
    if side is None:
        if (
            r.sponsor
            or r.authorized
            or r.without
            or r.requires
            or (by_embedding and _WORK_STATUS.search(_undot(low)))
        ):
            return f"work-status-question-took-{entry_id or 'another-entry'}"
        return None
    if r.sponsor and r.authorized:
        return "asks-authorization-and-sponsorship"
    if side == "authorization" and r.sponsor:
        return "sponsorship-question-took-work_auth"
    if side == "authorization" and r.requires:
        return "requires-authorization-question-took-work_auth"
    if side == "sponsorship" and r.authorized:
        return "authorization-question-took-sponsorship"
    if r.without:
        return "asks-without-sponsorship"
    if r.negation:
        return "negated-work-status-question"
    if r.reversed:
        return "reversed-work-status-question"
    if side == "sponsorship" and r.requires and not r.sponsor:
        return "asks-whether-work-authorization-is-required"
    if side == "sponsorship" and r.besides:
        return "asks-a-second-question"
    if by_embedding and r.any_negation:
        return "embedding-negated-work-status-question"
    if by_embedding and side == "authorization" and not r.authorized:
        return "embedding-not-an-authorization-question"
    if by_embedding and side == "sponsorship" and r.unlisted_before:
        return "embedding-unlisted-words-before-sponsorship"
    if side == "sponsorship" and r.unlisted_ending:
        return f"{'embedding' if by_embedding else 'alias'}-unlisted-words-after-sponsorship"
    if by_embedding and side == "sponsorship" and not r.needs_sponsor:
        return "embedding-not-a-sponsorship-question"
    return None


def human_answer_barred(label: str, compliance_type: str | None = None) -> str | None:
    """Why an answer the human typed (MCP answer_novel) must still stay off the
    form, or None when it may go on. A human answer is not a draft, so the
    never_draft rules and the salary-expectation gate do not apply to it. Two
    kinds of field stay blank whoever answered: EEO/demographic fields, which
    only the Answer Bank's eeo block may select (hard rule 4), and compensation
    history, which is never answered (hard rule 5). Same checks, same order as
    BankMatcher.match."""
    low = _norm_text(label)
    if compliance_type == "eeo" or _EEO_PATTERN.search(low):
        return "eeo-answer-bank-only"
    if _COMP_HISTORY.search(low):
        return "salary-history-never-answer"
    return None


# contact-detail: any mention of email asks for an address (the candidate's, a
# referrer's, a former employee's login), as it did before 2026-10-09 — unless
# EVERY mention is email as a sales channel and nothing else in the label asks
# how to reach someone. A form's "Have you independently managed full outbound
# prospecting sequences (calls, email, and social) ...?" was flagged
# never-draft:contact-detail on 2026-10-09; "What email
# should we use to contact you?" must stay blocked (review 2026-10-10). The
# exemption is the narrow side: a missed channel is a false rejection (the
# human answers), a missed ask would send a contact detail to the drafter.
_EMAIL_WORD = re.compile(r"\be-?mails?\b")
_CONTACT_INFO = re.compile(r"\bcontact (?:info|information|details)\b")
_CHANNEL = (
    r"(?:cold )?(?:calls?|calling|phone(?: calls?)?|dials?|dial(?:l)?ing"
    r"|social(?: media| selling)?|linkedin(?: inmails?)?|inmails?|sms|texts?|texting"
    r"|video(?: messages?)?|voicemails?|direct mail|events?|chat|whatsapp)"
)
_LIST_JOIN = r"\s*(?:,|/|&|\+|\band\b|\bor\b)\s*(?:(?:and|or)\s+)?"
# "(calls, email, and social)", "phone/email", "email or LinkedIn"
_CHANNEL_BEFORE = re.compile(rf"\b{_CHANNEL}{_LIST_JOIN}$")
_CHANNEL_AFTER = re.compile(rf"{_LIST_JOIN}{_CHANNEL}\b")
# "email sequences", "email outreach", "the email channel"
_CHANNEL_NOUN = (
    r"(?:campaigns?|sequences?|sequencing|outreach|marketing|cadences?|channels?|prospecting"
    r"|templates?|copy|copywriting|automation|tools?|platforms?|software|newsletters?"
    r"|nurtur\w*|deliverability|(?:open|reply|response) rates?|blasts?|programs?"
    r"|strateg(?:y|ies)|volume|performance|metrics|engagement|touch(?:es|points?)?)\b"
)
_CHANNEL_NOUN_AFTER = re.compile(rf"\s+{_CHANNEL_NOUN}")
# "cold email", "outbound emails", "how many emails"
_CHANNEL_MODIFIER_BEFORE = re.compile(
    r"\b(?:cold|outbound|inbound|mass|bulk|personali[sz]ed|automated|marketing|sales"
    r"|prospecting|follow-up|how many)\s+$"
)
# asks how to reach someone, whatever channel words the label also uses:
# "What's your phone, email, LinkedIn?", "best phone or email to reach you"
_ASKS_TO_REACH = re.compile(
    r"\b(?:your|their|his|her|whose|personal|work|contact|alternate|alternative|preferred"
    r"|best|good|primary|secondary|backup|referrer'?s?|employee'?s?)\s+(?:[\w-]+\s+)?"
    rf"(?:e-?mails?|phone)\b(?!\s+{_CHANNEL_NOUN})"
    r"|\baddress(?:es)?\b|\be-?mails?\s+id\b"
    r"|\b(?:reach|contact|email|call|text|message) you\b|\bget in touch\b|\bschedul\w*"
    r"|\bcalendly\b|\bon file\b|\bsign(?:ed|ing)? in\b|\blog(?:ged|ging)? in\b|\blogin\b"
    r"|\bwhen you applied\b"
)


# The exemption needs a label about DOING sales work (review 2026-10-10):
# 'Email / Phone', 'Phone or email' and 'LinkedIn or email' list contact
# channels, and a channel list alone made them "email as a channel" and sent
# them to the drafter. A label is about sales work only when it asks about the
# candidate's experience or activity AND uses sales-work words; a field-like
# label that mentions email is a contact field, whatever else it says.
_WORK_QUESTION = re.compile(
    r"\b(?:have you|did you|how (?:many|much|often)|experience[ds]?|familiar\w*|proficien\w*"
    r"|worked with|tell us about|describe)\b"
)
_SALES_WORK = re.compile(
    r"\b(?:prospect\w*|outreach|outbound|inbound|cold|sequenc\w*|cadences?|campaigns?"
    r"|pipeline|quota|leads?|sdrs?|bdrs?|sales|selling|marketing|nurtur\w*|deliverability"
    r"|touch(?:es|points?)|channels?|copywriting|templates?|blasts?|newsletters?"
    r"|(?:open|reply|response) rates?|meetings?|send|sent|sending)\b"
)


def _about_sales_work(label: str, low: str) -> bool:
    return (
        not is_field_like(label)
        and bool(_WORK_QUESTION.search(low))
        and bool(_SALES_WORK.search(low))
    )


def _email_is_channel(low: str, m: re.Match[str]) -> bool:
    before, after = low[max(0, m.start() - 80) : m.start()], low[m.end() :]
    return bool(
        _CHANNEL_BEFORE.search(before)
        or _CHANNEL_AFTER.match(after)
        or _CHANNEL_NOUN_AFTER.match(after)
        or _CHANNEL_MODIFIER_BEFORE.search(before)
    )


def asks_for_contact_detail(label: str) -> bool:
    """True when the label asks for an email address or contact details.
    Any mention of email counts unless the label is a question about doing
    sales work (see _about_sales_work: never a field-like label such as
    "Email / Phone"), every mention is email as a sales channel ("(calls,
    email, and social)", "cold email outreach", "email sequences") and the
    label does not otherwise ask how to reach someone."""
    low = _norm_text(label)
    if _CONTACT_INFO.search(low):
        return True
    mentions = list(_EMAIL_WORD.finditer(low))
    if not mentions:
        return False
    if not _about_sales_work(label, low):
        return True
    if not all(_email_is_channel(low, m) for m in mentions):
        return True
    return bool(_ASKS_TO_REACH.search(low))


# Questions whose every answer, a plain "No." included, asserts a fact about
# the candidate that the corpus never records (claim-judge review 2026-10-10:
# "No." to "Have you ever been terminated or asked to resign?" passed the
# rule-based check, and the judge prompt then exempted plain denials). The
# judge now reports such a denial too; these keep the drafter from being asked
# at all. Fail closed: a false hit leaves the field to the human. The bank's
# own screening entries (sponsorship, non_compete, work_auth...) are matched
# before never_draft runs, so they still fill from the bank.
_EMPLOYMENT_RECORD = re.compile(
    r"\b(?:been|were|was|got|gotten|being)\s+(?:\w+\s+){0,2}"
    r"(?:terminated|fired|dismissed|discharged|let go|laid off)\b"
    r"|\b(?:asked|forced|required|pressured) to resign\b|\bresign\w* in lieu\b"
    r"|\b(?:terminated|fired|dismissed|discharged) (?:from|by)\b"
    r"|\binvoluntar\w* (?:terminat|separat|discharg|dismiss|resign)\w*"
    r"|\btermination (?:of|from) (?:your |any )?(?:employment|position|job|role)\b"
    r"|\bperformance improvement plans?\b|\bpips?\b"
    r"|\bdisciplinary\b|\bbeen disciplined\b|\bsubject to discipline\b"
    r"|\b(?:written|final) warnings?\b"
)
_CRIMINAL_RECORD = re.compile(
    r"\bconvicted\b|\b(?:criminal|felony|misdemeanou?r|prior|any) convictions?\b"
    r"|\bconvictions? (?:of|for)\b|\bfelon(?:y|ies)\b|\bmisdemeanou?rs?\b"
    r"|\bcriminal (?:record|history|offen[cs]es?|charges?|background|conduct|case)s?\b"
    r"|\barrested\b|\b(?:pleaded|pled) (?:guilty|no contest|nolo contendere)\b"
    r"|\bpending (?:criminal )?charges?\b"
)
_RESTRICTIVE_COVENANT = re.compile(
    r"\bnon[\s-]?compet\w*|\bnon[\s-]?solicit\w*|\brestrictive covenants?\b"
    r"|\bpost[\s-]employment (?:restrictions?|obligations?|covenants?)\b"
    r"|\bagreements? that (?:would |could |might |may )?(?:restrict|prevent|prohibit|limit)\w*"
)
# "sponsor" alone is sales vocabulary ("executive sponsor", "event
# sponsorships"), and Visa is an employer: both need an immigration context.
_WORK_STATUS = re.compile(
    r"\b(?:work|employment|h-?1-?b|student|tn|o-?1) visas?\b"
    r"|\bvisas?\s+(?:sponsor\w*|status|holder|support|transfer|requirements?)\b"
    r"|\bon an? (?:\w+\s+)?visa\b|\b(?:require|need)\w*\b[^?]{0,40}\bvisas?\b"
    r"|\bimmigration\b|\bwork (?:authori[sz]ation|permits?|eligibility)\b"
    r"|\b(?:legally )?(?:authori[sz]ed|eligible|permitted|entitled) to (?:work|be employed)\b"
    r"|\bright to work\b|\bgreen card\b|\bh-?1-?b\b|\bcitizenship\b"
    r"|\b(?:u\.?s\.?|american) citizen\b|\bcitizen of\b|\bpermanent resident\b"
    r"|\b(?:require|need)\w*\b[^?]{0,60}(?<!executive )(?<!exec )\bsponsorship\b"
    r"|\bsponsor (?:you|your)\b"
)


def never_draft(label: str) -> str | None:
    """The answer_bank.yaml never_draft_if rules, as deterministic checks."""
    low = label.lower()
    if (
        re.search(r"salary|compensation|current pay|\bearn", low)
        or _COMP_HISTORY.search(low)
        or _COMP_EXPECTATION.search(low)
        or re.search(r"\bote\b|on.target earnings", low)
    ):
        return "compensation-related"
    if _EEO_PATTERN.search(low):
        return "demographic"
    if legal_acknowledgement(label):
        return "legal-acknowledgement"  # an AI never agrees to terms
    if re.search(r"certif|clearance|license|credential|degree required", low):
        return "credential"
    # an email address, or who referred you, is a fact the corpus never holds
    if asks_for_contact_detail(label):
        return "contact-detail"
    if re.search(
        r"\breferr(?:ed|er|al)\b.{0,60}\b(?:who|name|email|their)\b|\bwho referred\b"
        r"|\b(?:name|e-?mail|phone|contact)\b.{0,60}\breferr(?:ed|er|al)\b",
        low,
    ):
        return "referral"
    # facts about the candidate's record and status that no corpus holds: any
    # drafted answer, a plain "No." included, would be unsupported (hard rule 8)
    if _EMPLOYMENT_RECORD.search(low):
        return "employment-record"
    if _CRIMINAL_RECORD.search(low):
        return "criminal-record"
    if _RESTRICTIVE_COVENANT.search(low):
        return "restrictive-covenant"
    if _asks_work_status(low):
        return "work-status"
    if _LEGALISH.search(low):
        return "legal-acknowledgement"  # terms/consent/waiver/signature of any kind
    return None
