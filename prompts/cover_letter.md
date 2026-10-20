---
version: "1.16"
stage: cover
model_tier: opus
---
Write a cover letter: three paragraphs, ~250 words total, in the candidate's own
voice (samples below — match their cadence and directness; the letter itself is
confident, not hedging).

Structure (SPEC §7 stage 10):
- P1: the role, plus ONE concrete non-generic fact about the company drawn from
  the job description — not their mission statement.
- P2: two corpus bullets retold as narrative, with their numbers intact.
- P3: close on ONE more concrete win from a cited bullet — not one already used
  in P2 — chosen because this role needs exactly that result, and say in one
  sentence what it means for the role. Then a direct close: ask for the
  conversation. The letter ends on strength.

Gaps: never confess, apologize for, or volunteer a gap, and never mention
overqualification. Mention a gap ONLY when it is a stated hard requirement —
it appears in JOB MUST-HAVES below — that the candidate does not plainly meet.
Then frame it as how the candidate covers it, with a cited fact as the evidence
("Your posting asks for X. At <employer> I <cited fact>.", with X in plain
words, not the posting's numbers or names), in P2 or P3 — never as the
letter's last word.

Hard bans: "excited to apply", "passionate about", "fast-paced environment",
"align with your mission", "leverage my skills", "proven track record",
"perfect fit", "I believe I would be", "as you can see from my resume",
"I would love the opportunity", "thank you for your time and consideration".
Also banned, in any wording: a contrast construction (what the candidate
would rather do, set against something else); announcing a gap as a gap;
prefacing a weakness with a promise of candor; confessing that the candidate
has never held some title; and an apologetic close that concedes the gap
might rule the candidate out. Every letter is compared with the candidate's
earlier letters, and a phrase reused across them fails.
Vary sentence length — a four-word sentence beside a thirty-word one.

FACT RULES — the letter is machine-checked against the bullets you cite and is
thrown away if any rule is broken:
- Every fact about the candidate comes from CANDIDATE FACTS below. Nothing else.
- List EVERY bullet id you drew on in "bullets_used".
- FIGURE STYLE: {estimate_style}. Copy each number from its bullet by the
  rule for that style:
  - confident: say it plainly. You may drop "~", "about", "approximately" or
    "roughly", even on an "estimate" bullet ("~$5.6M" may read "$5.6M"; "$8M"
    stays "$8M"). Keep everything else exactly as the bullet writes it: the
    same value and unit, no added precision, a range kept whole ("6–10",
    never "10"), and "+", "up to", "under", "over" exactly where the bullet
    has them ("up to 9" stays "up to 9"; never add one).
  - hedged: the same unit and the same hedge ("~$5.6M" stays "~$5.6M", "up to
    9" stays "up to 9").
  In both styles a rate keeps its period ("$1.8M a year" never "$1.8M a month",
  "5–7 deals a month" never "5–7 deals", "~$5.6M in annual bookings" never
  "$5.6M book"), and "or more", "-plus", "and up" are never added.
  No number that is not in a cited bullet — not in digits, not in words
  ("two", "twelve", "doubled", "#1"). A company fact may use the job
  description's numbers and names only in a sentence that opens with
  {company}'s name followed by words copied in order from ONE sentence of
  the job description, and nothing else ("{company} doubled its payment
  volume in 2025" for "…and it doubled its payment volume in 2025"; not "I
  noticed {company} doubled…", "Your team doubled…", "{company}'s growth
  doubled…" or "Booking 35 walkthroughs a week, like your team, was the job"). That sentence has no "I"/"my"/"me" (not even "caught
  my eye"), none of the candidate's employers, no "who",
  "will"/"would"/"can", no word that points back or compares ("there",
  "then", "too", "the same", "like", "they") and no word of someone's
  history ("previous", "experience", "used to"). Say what the fact means in
  the next sentence, without the posting's numbers or names. Every other
  sentence, including every sentence with "I", uses the job description's
  words only for {company} and the {title} role; anything else in it needs
  a cited bullet ("I'll coach your Account Associates" is thrown away unless a
  cited bullet says "Account Associates").
- Each bullet is labeled with the employer and title it belongs to. Attach a
  fact only to that employer. Never let one employer's result explain
  another's ("most of that revenue came from…", "it came from…", "the engine
  behind it was…"), even with both employers named. If one sentence uses facts
  from two employers, name both employers in it — or split the sentence. A
  sentence that starts "It…", "This…" or "The <noun>…" is read together with
  the sentence before it.
- Name no company except {company} and the candidate's own employers.
- Name {company} only as where the candidate wants to work ("I'd like to
  join {company}", "the {title} role at {company}") or in the company-fact
  sentence: never as a past or current employer, and never in a sentence
  that says what the candidate did, has or knows ("I led {company}'s team",
  "{company} taught me…", "I was on your team").
- A sentence about what the candidate knows, is, would bring or would do
  without ramp ("I'd know…", "as a former…", "on day one", "second
  nature") uses only words of the bullets it cites.
- Never present a title from NEVER CLAIM as the candidate's own, and never use
  a NEVER CLAIM phrase. Mention another person's title only as a bullet words it.
  If the job's own title is on that list, name it only as the job ("the {title}
  role at {company}", "your {title} role") — never "As {title}", "I was…" or
  "my {title} …". In a sentence that also names one of the candidate's
  employers, always tie it to {company} ("the {title} role at {company}").

Return ONLY a JSON object:
{"letter": "the full letter text, paragraphs separated by blank lines",
 "bullets_used": ["b_00X", "b_00Y"],
 "company_fact": "the concrete fact you anchored P1 on"}

JOB: {title} at {company}
JOB DESCRIPTION (for the company fact):
{jd}

JOB MUST-HAVES (the only requirements a gap may be about): {must_haves}

POSSIBLE GAPS FROM FIT SCORING (context only — not a topic for the letter unless
one is a JOB MUST-HAVE above): {gaps}

CANDIDATE FACTS (the only facts you may state; each labeled employer · title · dates):
{bullets}

CONFIRMED SKILLS (may be named as the candidate's own): {skills}

NEVER CLAIM: {never_claim}

VOICE SAMPLES (the candidate's real prose — match this voice):
{voice}
{feedback}
