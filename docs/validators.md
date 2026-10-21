# Fact checks

Everything an employer reads is checked against your career corpus
(`data/career_corpus.yaml`, the facts you confirmed during setup) before it is
used: resume bullets, the skills line, cover letters and drafted answers to
form questions. If any check fails, the draft is rejected.

A short example. Say your corpus has the bullet "Precepted 6 new graduate
nurses on a 32-bed cardiac step-down unit". A letter may say "I precepted 6
new graduate nurses on a 32-bed unit". It may not say "I precepted 8 new
graduate nurses" or "on a 40-bed unit": each number must be one a confirmed
fact states. A claim with no number, such as "I was the unit's charge nurse",
is read by the claim judge, which rejects it unless a confirmed fact supports
it. The same rule applies to a software engineer's "cut p95 latency from 800 ms to
120 ms", a teacher's "five sections of 28 students", or a salesperson's "131%
of annual quota".

The checks are in `src/jobkit/validate/provenance.py` (resume) and
`src/jobkit/validate/claims.py` (letters and answers), with a model-based
second check (the claim judge), a self-review, and an authenticity review. The
examples below use the fictional people and companies from the test suite
(Haulmark, ledgerline, Contoso, Fabrikam Payments, Aircall), and bullet ids
such as `b_061` from the test corpus.

## Numbers

- **Numbers keep their units.** `20%` does not match `~$20K`, and `#1` does
  not match `$75K+`. Number words ("team of twelve"), rank claims ("#1 rep",
  "top rep") and multipliers ("doubled", "tenfold") count as numeric claims
  and need a source.
- **Numbers are said as the corpus states them.** One setting decides how an
  approximation mark is treated: `drift.estimate_style` in
  `config/banned_phrases.yaml`. The template sets `hedged` (the stricter rule,
  and what a missing key means); the setup interview asks which you want.
  - `confident`: `~`, `about`, `approximately`, `roughly` and `around` may be
    dropped, even on an `estimate` figure (`~19%` may read `19%`). Everything
    else must match the source: the same value and unit, no added precision
    (never `19.4%`), a range stays the whole range (`6–10`, never `10`), and
    `+`, `up to`, `under` and `over` stay where the source has them and are
    never added (`up to $1.8M/yr`, never `$1.8M a year`). A bound written after
    the number counts the same: `$2.6M or more`, `$2.6M-plus` and `$2.6M and
    up` from a source that says `$2.6M` fail like `over $2.6M`. A peak is a
    qualifier of its own, before or after the number: `peaked at $110K`
    restates a source's `peaking at ~$110K`, while `up to $110K`, `topped out
    at $110K` and a bare `$110K` do not. A peak never hides another qualifier,
    so `~$110K at peak or more` and `over $110K at peak` fail as
    `added_qualifier`.
  - `hedged`: every number of an `estimate` figure keeps a hedge right next to
    it (`40%, over target` is unhedged; `~40%`, `up to 9`, `40+` and ranges
    like `$60–75K` are hedged), and a number its source only states hedged
    keeps that hedge.
  - Violations: `fabricated_number`, `collapsed_range`, `changed_range`,
    `dropped_qualifier`, `added_qualifier`.
- **A rate keeps its period**, in both styles. A figure's period is the one
  written next to it: `/day`, `/mo`, `a year`, `per month`, `each week`,
  `annually`, `monthly`, `run-rate`, `ARR`/`MRR`, `YoY`/`MoM`, including when
  hyphenated onto the figure (`$22K-a-month`); a hyphen after a word, as in
  `once-a-month`, is no figure's period. `$1.8M/yr` never becomes
  `$1.8M/month` (`changed_period`), `$22K` never becomes `$22K/month`
  (`added_period`), and `5–7 clients/month` never becomes `5–7 clients`
  (`dropped_period`). A figure with no period of its own may take a period the
  same text states elsewhere. Durations are not periods (`in under a year`).
- `metric_confidence` in the corpus means the same in both styles. The setting
  is read by the resume check, the letter and answer check, the authenticity
  review, and the selection, cover-letter and form-answer prompts
  (`FIGURE STYLE:` in each). An unknown value, or a leftover
  `enforce_hedging`, is an error.
- `jobkit figures` (tool `figures()`) prints the style, what it allows, and
  which corpus bullets pass word for word under it. `--bullet-id b_041
  --rendered "Ran 10 trial kickoffs/week."` checks one rendering. It makes no
  model call and opens no database.

## Never-claim lists

Optional keys in `data/career_corpus.yaml`; an absent key means an empty list.

```yaml
constraints:
  never_claim_titles: ["VP", "Nurse Manager", "Principal Engineer"]
  never_claim_phrases: ["treasury management sales"]
  never_claim_tools: []
```

- A title written as your own is a hard fail. Your own is the default:
  "Promoted to VP", "As VP of Sales", "I was effectively the General Manager"
  and "the general manager's duties were mine" all fail. An acronym also covers
  its S/E/A forms (VP covers SVP). A title counts as another person's only with
  an explicit person signal: "the general manager's office", "reported to the
  VP", "our VP of Sales asked". Even then it needs a source: in a resume bullet,
  the sourced title must keep the source's own words on each side (a paraphrase
  is a hard fail); in a letter or answer, a cited bullet (or, in a sentence about
  the employer, the job description) must mention the title.
- When the job you are applying for has a never-claim title, the letter may
  name the job ("the VP of Sales role at Oscorp", "your VP of Sales role") but
  never claim it: "I was the VP of Sales at Haulmark" still fails. A sentence
  that names one of your employers, or states one of your facts, gets this
  excuse only when the title is tied to the target ("…prepared me for the VP of
  Sales role at Oscorp"). Titles match on word boundaries ("CRO" doesn't fire
  inside "across").
- A never-claim phrase anywhere is a hard fail.

## The skills line

It may contain only `skill_index` keys. Its final value is written to
`provenance.json` (`skills_line`). When `skill_index` is empty, the skills line
is left off the resume and `tailor.skills_omitted` is logged.

## Cover letters and drafted answers

The letter model sees only the selected bullets, each labelled `employer ·
title · dates`, and must return `bullets_used`. Every number and proper noun
must appear in a cited bullet.

### Names

A capital at the start of a sentence makes a proper noun only when the word is
not a common English word (the one list, `src/jobkit/validate/common_words.txt`)
or it is a known name: the employer, one of your employers or schools, a
`skill_index` key, a tool or method from the questions' topic list, a company
in the database, a name the job description or the corpus uses, or a word the
letter itself capitalises mid-sentence.

- A name the description or the corpus uses is any word they capitalise
  mid-sentence, with two exceptions: a word after the first one in a
  title-cased run of common words ("Success" in "Customer Success"), and a
  capital they also write in lowercase that a lowercase "team", "org",
  "function" or "department" follows ("People teams"). So "Success shows up
  later." passes beside a description with "Customer Success", while
  "Brightwave hired me…", "Oracle was one of my accounts…" (beside "Oracle
  Cloud Infrastructure") and "Slack ran my team…" fail.
- A standard contraction at the start of a sentence ("Let's", "Don't",
  "We'll", "Here's") is never a name, even when the description capitalises it
  after an emoji or a bullet. "Brightwave's team hired me…" still fails.

### Sentences about the employer

A sentence about the company you are applying to may use numbers and names
from the job description, but only when the company is plainly its subject:

- It opens with the company's name ("Fabrikam Payments doubled its payment
  volume in 2025"). A sentence that says nothing happened may instead open with
  "you"/"your" or "the role" ("Your Account Associates book 35 walkthroughs a
  week"), but "Your team doubled…" fails.
- It contains no "I"/"my" and names none of your employers. It points back at
  or compares with nothing ("there", "then", "too", "the same", "like", "as the
  old team did", "that team", "they"), introduces no person and doesn't name
  you ("a leader who…"), and promises nothing ("will", "would", "can").
- So "Booking 35 walkthroughs a week with a team like your Account Associates
  was the job" doesn't borrow the description's "35", and "At Haulmark I saw
  revenue double" doesn't borrow its "doubled". Reactions such as "caught my
  attention" don't count as "I"/"my"; "means a lot to me" and "exciting to me"
  do, since "…, which means a lot to me" can close a claim.
- Durations are numbers: a plan's window ("the first sixty days", "within 30
  days", "a 30-60-90 day plan") needs a cited source, in a letter and in an
  answer. Results need a source in every tense ("I'll grow your ticket volume
  5x"), even when the description has the number.

### Plans and claims about you

A first-person sentence never uses a word from the job description unless a
cited bullet has it: "I'll coach your Account Associates" fails on
"Associates". A sentence makes no claim about you when it names none of your
employers, makes no past-tense claim ("coached", "I've led", "previously"), and
every "I"/"we" in it takes a future, modal or stance verb ("I'll", "I'd", "I
can", "I think"). "I'd" before a verb whose past participle is its base form
("I'd run", "I'd set up") might mean "I had", so it counts as a claim. Every
"my"/"our" in it is a plan ("my plan") or ends a short phrase ("around my
meetings"); "me" after a preposition and "myself" count the same as "my". Such
a sentence may name the job's title as the role sought, but still needs a cited
bullet for every number and every other proper noun.

**Experience claims** (`unsourced_experience_claim`). A first-person sentence
that claims experience, a skill, credentials or a background may use only words
of the bullets it cites (their employer and title included). Such a sentence
has a form of "bring", or "I offer/contribute/lend", or a word such as
experience, expertise, background, knowledge, track record, skills,
credentials, licensed, certified, degree or years, or "I am a/an/the". A modal
doesn't make it a plan: "I would bring years of field service operations
leadership", "I have deep expertise in equipment financing" and "I am a
licensed electrician" fail unless a cited bullet has every word. "Bring" and
its like need no source, but "years" and "expertise" do. "I'd bring the
onboarding program that cut time to first dispatch by ~40%" passes with the
bullet that says so cited. The employer's name supplies no word, so "…, and I'd
bring that to Fabrikam" fails. Drafted answers get the same rule.

Other wordings that count as such a claim:

- what you have, after a modal: "I'd have the trade chops your team needs",
  "I'll draw on my supplier network", "I could lean on…" ("I'd have to learn…"
  is not one), and a "my"/"our" that is not a plan in a sentence with a modal
  ("My field service leadership would serve your team");
- knowing or understanding after a modal ("I'd know exactly how equipment
  financing works", "I'd come in knowing…");
- needing no ramp ("on day one", "I'd hit the ground running", "Nobody would
  need to teach me…");
- ease, readiness, credibility or fit ("…is second nature to me", "I'd be
  productive immediately", "Contractors would trust me instantly", "that's
  me");
- an identity ("As a former electrician, I'd…", "I'd be the electrician in the
  room");
- a past claim inside a clause with a modal ("I'd be the rare Director of
  Revenue Operations who has run a plumbing shop", "I'd thrive leading
  technicians, as I always have");
- with no "I" at all, a hire the company would make ("You'd be hiring a
  licensed electrician");
- possession brought along ("I'd walk in with…", "armed with"), familiarity
  ("None of this would be new to me", "I've seen all of this before"), an
  ability ("I can navigate building-code compliance"; "I can help" is an offer
  and passes) and a skill verb after a modal ("I'd speak the technicians'
  language", "I'll train your reps…").

A claim that names nothing a cited bullet could source besides its own frame
("I can do this", "I'd bring that") fails whatever is cited. "I'd welcome the
chance to talk", "I'm excited about the role at Fabrikam Payments", "I'd love to
understand your priorities" and "As an Inside Sales Manager, I'd build
pipeline" pass. These markers are lists, so other wording of the same claim can
still get through; the critic and your own read of the filled form are the
check for that. Such a sentence is never one that "makes no claim about you",
so the job's title in it is checked as a seat claimed.

**Restating a change.** One kind of verb needs no cited word of its own: a
neutral change verb (climbed, grew, rose, increased, improved, went from) whose
clause restates a change a cited bullet states. The clause's subject must be
the change's own words in the bullet, all of them and in order (one leading verb
such as "improving" or "lifting" may be left out), with no negation or modal
before the verb, its figures the bullet's in the bullet's order, and nothing
after them before the next punctuation mark but a "+" or a period ("a month").
The sentence may name no year and no span of time other than a figure's own
rate. "…, and renewal rate climbed from 44% to 71%." passes on a bullet that
says "raising the renewal rate from ~44% to ~71%"; "…climbed from 71% to 44%",
"win rate climbed from 44% to 71%", "…climbed from 44% to 71% in a week" and
"I grew renewal rate from 44% to 71%" fail.

### The employer you are applying to (`fabricated_employer`)

The company you are applying to may be named as where you want to work ("I'd
love to join Contoso"), never as a past or current employer. Unless the corpus
lists it as one of your roles' companies, it fails as `fabricated_employer` (a
hard fail).

- **A seat cue fails in any sentence:** right before the name ("I previously
  worked at Contoso", "I've been with Contoso", "I joined Contoso", "while at
  Contoso", "ex-Contoso", "a former Contoso AE", "an alum of Contoso", "I'd love
  to rejoin / return to / be back at Contoso"); earlier in a list that runs on
  to it ("at both Haulmark and Contoso"); right after it ("Contoso gave me…",
  "Contoso and I go way back"); a seat after "I was/I'm/I've been" ("I was an
  early Contoso hire"); an employer label ("Current employer: Contoso"); a
  resume line ("Account Executive, Contoso", "Contoso, 2019–2021"); or a bare
  answer ("Yes, Contoso.").
- **In a sentence with any "I", "me", "my", "we" or "our"** (a present
  reaction such as "matters to me" aside), any mention of the company fails
  unless the sentence only aspires, names the job sought or the company's
  people ("I saw the Contoso posting"), compares ("a team Contoso's size"), or
  describes the company in the present ("Contoso is solving a problem I care
  about"). A sentence only aspires when it has a forward word ("would", "I'd",
  "I'll", "want", "excited"), no past tense, every "I" forward, no "my" beyond a
  plan, and no word of tenure ("again", "back", "before", "still", "stay",
  "continue", "keep", "home", "one more time"). "At Contoso I ran the SDR team"
  and "I work at Contoso" fail; "At Contoso, I'd focus on pipeline" passes.
- **"you"/"your" is the company.** After a seat cue it fails like the name: "I
  was on your sales team", "I previously sold your platform". In a sentence
  with any first person, any "you"/"your" fails unless the sentence only
  aspires, or it is "thank you", the job sought ("I read your job
  description"), or the company's people met or sought ("I enjoyed meeting your
  team", "For your reps, I'd…"). "I reported to your CEO" and "I have run your
  weekly revenue meeting" fail. The job's own title tied to the company is a
  seat claimed when its clause makes a past claim ("I was your Inside Sales
  Manager" fails as `claimed_job_title` too).
- An aspiration or description of the company has no "my" beyond a plan
  ("Contoso is on my resume" fails), no "me" as a verb's object ("Contoso will
  remember me"), no re-contact ("I'd love to reconnect with Contoso"), and no
  phrase that describes you as its customer or user ("I'd love to join Contoso
  as one of its earliest customers", "A longtime customer, I'd…"). "for", "to"
  or "into" name the job sought only after a word of wanting or applying: "I was
  promoted to the role at Contoso" fails. A present reaction with a word of
  tenure is an old one: "Contoso still matters to me" fails.
- A lowercase spelling counts ("I previously worked at contoso",
  "monday.com"). A name whose lowercase form is a word of the corpus or the
  description ("pilot" for Pilot) is judged in lowercase only after a seat cue.
- A client a cited bullet names stays a client ("accounts at Nakatomi", when
  applying to Nakatomi).
- **A drafted answer is checked with its question.** When the question asks
  whether you worked or work at the company ("Have you previously worked for
  Fabrikam Payments?", "Are you a current or former employee?"), only a plain
  "No" (or "N/A") passes unless the corpus lists the company; "Yes." fails and
  the field stays open for you.

### Tools (`unsourced_tool`)

A tool or method from the questions' topic list (Clari, Highspot, HubSpot,
MEDDPICC…) may be named in a letter or drafted answer only when a `skill_index`
key is that tool; otherwise the sentence fails as `unsourced_tool`. A cited
bullet that names the tool is not enough, because a bullet can say someone else
used it ("My manager ran Clari forecast calls at Haulmark"). The check doesn't
look at who used the tool, so a sentence about the employer ("Fabrikam
Payments runs its forecast on Clari") fails too. Add the tool to `skill_index`
(through its question, or by hand) if you did use it.

**When the company you apply to is the tool** (Aircall, Dialpad, Highspot,
Seismic…), its name may appear as that company: the job sought ("I'm applying
for the Director of Sales Enablement role at Aircall"), the object of an
aspiration in its own clause ("I'd like to join Aircall") in a sentence that
states none of your own facts, or a present-tense sentence about the company
that opens with its name, has no first person, and contains no number the
posting doesn't state and no fact of your cited bullets ("Aircall makes a cloud
phone and dialer…"). Each mention is judged on its own:

- a usage word before it or a product word after it ("using Aircall", "rolling
  Aircall out", "three years on Aircall", "Seismic playbooks") makes it the tool
  whatever else the sentence says;
- named as a tool you used, it fails ("I used Aircall daily at Haulmark",
  "Haulmark ran its floor on Aircall");
- every mention also fails when any sentence of the letter may point back at
  the product in a claim about you or your employers: a pronoun ("it", "its",
  "they", "their"), a demonstrative with no noun after it or "the same"
  ("That's what we ran"), a product word ("the product", "that dialer", "the
  platform") or a word of buying or holding it ("a customer", "users",
  "seats", "our contract", "the renewal") beside a first person that does more
  than aspire, an employer's name, a fact a cited bullet states, or "the team
  there";
- a phrase that describes you as the company's customer or user makes a
  sentence no aspiration ("I'd like to join Aircall, a power user of it since
  2022"), and a pronoun or product word that a usage word governs, or that sits
  inside an "as" phrase or appositive without being its head, still points back
  ("I'd love to join Aircall after using it").

"I'd like to join Aircall and help it grow", "…as its next Director of Sales
Enablement" and "Aircall, the dialer modern floors run on" pass. A hyphen
compound ("customer-facing") and a capitalised name your corpus writes
("Customer Success") are not words of buying. A drafted answer to a question
that names the tool or asks about tools, use or experience gets no exception;
any other drafted answer may name the employer only as the job sought or in a
sentence that only aspires.

### The job's title (`claimed_job_title`)

In a sentence that makes a claim about you, the job's own title is never
claimed as held. "I was the Inside Sales Manager at Haulmark" and "As an Inside
Sales Manager, I coach reps daily" fail. The title may name the role sought: "I
want to be an Inside Sales Manager", "As an Inside Sales Manager candidate,
I…", "As your Inside Sales Manager, I'd…" and "the Inside Sales Manager role at
Contoso" pass. So does a disclaimer that negates the seat itself: "I haven't
held the Inside Sales Manager title, but…". "I haven't just held the … title"
and "I wasn't officially the …" are still claims.

### Facts belong to their employer (`wrong_company`)

Each fact must belong to the employer named next to it, and a sentence that
mixes facts from two employers must name both. A sentence that refers back to
the one before ("It came from…", "…came from them") is checked together with
it. One employer's result may never be credited to another employer's work
("The ~$5.6M in Haulmark bookings came from usage-based pricing I launched at
ledgerline"), even when both are named. A phrase used only in one employer's
bullets counts as that employer's fact. Naming a company that is not the
employer you are applying to, one of your employers, or mentioned in the
description or the corpus is a hard fail (`wrong_company`).

### When a letter fails

The letter is written again up to twice with the violations fed back. If it
still fails:

- no `cover.pdf` or `cover.txt` is written, and `cover_rejected.md` holds the
  violations and the last draft, marked NOT FOR SENDING;
- the application stays at `approved` with no `cover_path`, so its form is not
  filled;
- a `cover_provenance_failed` event is recorded. The `review_artifacts` and
  `get_artifacts` tools, and `jobkit review <job_id>`, show it.

How a parked letter is retried is in
[How it works](how-it-works.md#tailoring).

## Drafted form answers

The same check runs against the labelled profile ids the answer cites. In an
answer, a window such as "within 30 days" usually describes your availability,
notice or relocation; like every number, it needs a cited source. Only you or
the answer bank may state those facts. A draft that fails twice is not stored:
the question stays open with `flag: unsourced-claim` and a `fact_check` list,
which `get_novel_questions` shows. A `novel_provenance_failed` event is
recorded, and `jobkit form-specs` prints `fact_check_failed=N`.

## Authenticity review

Resumes and cover letters also go through a four-pass review
(`src/jobkit/validate/authenticity.py`), cheapest first:

1. **Lexical.** Banned phrases from `config/banned_phrases.yaml`.
2. **Structural.** Uniform bullet lengths, repeated opening verbs, identical
   grammar, em-dash and semicolon density, uniform sentence length, stacked
   adjectives, and letter sentences that would be equally true of any company.
3. **Drift.** Each rendered bullet against its corpus source. A bullet the model
   rewrote rather than lightly reframed is flagged and put back to your wording,
   and figures are checked under `drift.estimate_style`.
4. **Critic** (`prompts/authenticity_critic.md`, run only when passes 1 to 3
   are clean). A missing, empty or unreadable verdict counts as `fail`, never
   `pass`. On resumes, em-dash and semicolon flags that come from your corpus's
   own punctuation are marked `corpus-inherited`; they still show, but don't
   stop the critic from running. The critic can only make the verdict worse.

## Claim judge

The rule-based check reads numbers, names, employers and lists of phrases. It
can't read meaning, so a claim in plain words gets past it: "I also led the
sales development team.", "I know trade licensing inside and out.", "Fabrikam
Payments was a great place to work." (which says you worked there). A model
claim judge reads every cover letter and every drafted form answer that the
rule-based check passes (`src/jobkit/validate/claim_judge.py`, prompt
`prompts/claim_judge.md`, on `claude-opus-5-5`).

- **What it reads.** The text; the bullets it cites, each with employer, title,
  dates and metric; every role; education; the skill index with its notes; the
  full labelled corpus; the employer and title; and, for an answer, the form
  question.
- **What counts as a claim.** Anything about your past, your skills or
  experience, your employers, titles, results, credentials or relationships,
  including a claim that is only implied. These are not claims: what you want to
  do in the role, opinions about the employer, facts about the employer, and
  greetings and thanks.
- **A denial is a claim.** "No." to "Have you ever been terminated?", "Have you
  ever been convicted of a felony?" or "Have you ever been placed on a PIP?"
  states a fact about your past that the corpus doesn't record, and so do "N/A"
  and "I have not". The only denial with a source is "I have not worked at" a
  company your role list doesn't include. The judge reports any other denial
  (terminations, discipline, convictions, non-competes, sponsorship or work
  status, licences), and the question stays open for you.
- **What supports a claim.** A cited bullet, the role list, education, or a
  skill index entry that states it. A fact stated only in a bullet the text
  didn't cite counts as unsupported, because the writer must cite what it uses.
- **It can only reject.** It runs only after the rule-based check passes, so it
  never sees, and can't rescue, a draft the rules rejected. A `fail` verdict, a
  `pass` that still lists a claim, and a reply that isn't the expected JSON all
  fail the draft.
- **A judge failure is a fact-check failure.** A letter is written again up to
  twice with the judge's reasons fed back. If it still fails, `cover_rejected.md`
  lists them as `claim_judge_unsupported` (or `claim_judge_unparseable`)
  violations with the sentence each is in, the application stays at
  `approved`, and a `cover_provenance_failed` event is recorded. A drafted
  answer gets one more try; if it still fails, the question stays open with
  `flag: unsourced-claim` and the judge's reasons in its `fact_check` list.
- **If the judge can't be reached** (every retry failed), nothing is written as
  final and no failure is recorded. The letter's job stays at `approved` and the
  next `jobkit tailor` or `run-all` tries again. A drafted answer stays open for
  you.
- **Cost and caching.** One strong-model call per draft that passes the rules,
  cached like every other call on the full prompt, so the same draft is judged
  once, and a changed draft, corpus or prompt version is judged again. A reply
  that doesn't parse is never cached. `provenance.json` records the judge's
  prompt version (`claim_judge_prompt`), and the letter's attempt key includes
  it, so a job parked by the judge is retried when the judge prompt changes.
- **It can't be turned off.** It runs inside `jobkit tailor` (tool `retailor`),
  `run-all` and `form-specs`, and on stored texts in `jobkit rejudge`. Its
  findings show where fact-check failures show: `jobkit review <job_id>`, the
  `review_artifacts`, `get_artifacts` and `get_novel_questions` tools.

### Checking stored letters and answers: `jobkit rejudge`

A letter or answer that has no record of passing the current judge prompt is
checked by `jobkit rejudge`. Run `jobkit rejudge --dry-run` (tool `rejudge()`)
to list them without a model call, then `jobkit rejudge` (tool
`rejudge(confirm=true)`, which runs in the background). `--job-ids 12,15` (tool
`rejudge(job_ids="12, 15")`) checks only those jobs. Each stored text gets the
check a new draft gets: the rule-based check against the current corpus, then
the judge.

- **What it checks.** Every job not submitted and not applied to, rejected and
  closed postings included, because `jobkit stage` and
  `stage_forms(job_ids=…)` can still fill them. For a letter: the application
  at `tailored` with its `cover.txt`, and the citations and resume bullets in
  its `provenance.json`. For answers: every stored answer in the form spec
  except the ones you gave through `answer_novel`, which are never judged. A
  stored answer keeps no citations, so it is checked against none: the role
  list, education and skill index can still support it, but a fact that only a
  bullet states fails it, and the question comes back to you. A letter missing
  its `cover.txt` or `provenance.json` can't be checked, so it fails.
- **What happens to a failure.** A letter is left as a letter that failed when
  drafted: `cover.pdf` and `cover.txt` are removed, `cover_rejected.md` holds
  the reasons and the letter marked NOT FOR SENDING, the application goes back
  to `approved` with no letter, and a `cover_provenance_failed` event is
  recorded. That event has no attempt key, so the next `jobkit tailor`,
  `run-all` or nightly run re-tailors an approved job once with the judge in the
  loop; if that letter fails too, the job is parked as usual. An answer is
  removed from the drafted answers and goes back to your open questions with
  `flag: unsourced-claim` and the reasons, with a `novel_provenance_failed`
  event.
- **What it skips.** A letter whose `provenance.json` names the current judge
  prompt, an answer whose form field records the current judge version (form
  capture stamps every draft the judge passed, as `claim_judge` in the form
  spec's fields), and anything an earlier `rejudge` passed under the current
  version. A text that changed while the judge was reading it is left alone.
- **If the judge can't be reached**, that text is left unjudged, the report
  lists it under NOT CHECKED, nothing is recorded, and the command exits 1. Run
  it again.
- **A letter is not filled into a form until it is judged.** A tailored letter
  with no record that the judge passed it (its `provenance.json` names no
  `claim_judge_prompt`, its fact check didn't pass, or the file is missing or
  not the letter's own) is not final: `jobkit stage` and the `stage_forms`,
  `restage` and `open_queue` tools stop the job as
  `cover-letter-needs-retailoring` before the browser opens the form, and the
  nightly run skips it and names it under "Needs you". It is marked once with a
  `cover_needs_retailor` event and listed in the digest's Staging section,
  `get_queue` and `schedule status` until it is re-tailored with the judge or a
  `rejudge` passes that letter (`src/jobkit/pipeline/letter_gate.py`).
- **By hand instead.** `retailor(n)` or `jobkit tailor --job-id N` re-tailors
  one letter with the judge. For one job's answers, run `jobkit rejudge
  --job-ids N`, or answer the question yourself with `answer_novel`.
- Each job checked gets a `claims_rejudged` event (judge version, the letter and
  whether it passed, the answers that passed and failed). Checking an unchanged
  text again costs nothing, because the judge's calls are cached.

## Self-review

Each drafted artifact is reviewed by the model before you see it: the resume
bullet selection, the cover letter and each drafted form answer.

- **What the review sees** (`prompts/self_review.md`, on `claude-opus-5-5`):
  the job's must-have, nice-to-have and implied requirements; the draft; the
  full labelled corpus (bullet ids, employer, title, dates, metrics,
  `metric_confidence`, positioning variants, `skill_index`, never-claim lists);
  the gap report; the role track; and any violations the draft failed. A cover
  letter's review is told it may cite only the bullets on the resume.
- **What it asks itself.** Which must-haves are unaddressed although the corpus
  supports them? Which strong facts are unused? Is the opening specific to this
  company and role? Is anything apologetic, generic, repetitive or weak? Does
  anything over-claim? It returns `verdict` (`good` or `revise`), a `changes`
  list (what, why, bullet ids) and `questions_for_human`.
- **Only a passing resume selection is revised.** A selection that passed its
  checks and got `revise` is rewritten once, with the change list added to its
  prompt (`prompts/self_review_revision.md`). Before the writer sees the list,
  each change whose `what` uses a word that is in none of the corpus, the
  passing selection and the job's title is dropped (`validate/revision.py`).
  The company's name supplies no word unless the corpus has it, and words of
  tense and tenure ("was", "were", "been", "had", "did", "before", "once",
  "ever", "ago", "previously", "formerly", "currently") need a source like any
  other word, so "Say I was at Fabrikam Payments before" is dropped. Editing
  words such as "add", "lead with" and "cut" don't count. The report lists
  dropped changes, and when every change is dropped nothing is revised.
- A revision may add, drop, reorder or swap corpus bullets and change the skills
  line, but it may not reword a bullet: every bullet it renders must be its
  named variant's corpus text, word for word, or the revision is discarded
  (`revision_reworded`). This is needed because the resume check reads only a
  bullet's numbers, proper nouns and never-claim terms, so an added sentence
  such as "I also managed the sales development team." would otherwise pass. A
  first draft may still trim a bullet lightly. The revision then runs the full
  check chain again (fact check, then authenticity). Its authenticity verdict
  may not be worse than the selection it replaces, and its drift pass may flag
  no new word. A revision that fails anything is discarded and the original
  is used. A fact the job needs and the corpus lacks belongs in the questions for
  you.
- **A cover letter or drafted answer is never revised.** Its review is reported
  only: the changes appear as "suggested (not applied)", and the letter or
  answer that passed the fact check (and, for a letter, the critic) is what
  is used. Rewritten prose can't be checked reliably against the corpus: moving a
  sentence can re-attribute a fact to another employer, and a new sentence can
  use only its bullets' words while saying something no bullet says.
- **A draft that failed its checks** has its violations passed to the review,
  and that review is reported only. The regeneration includes the check feedback
  and nothing from the review, inside the existing cap (resume 2 attempts,
  letter 3, answer 2), and is checked as before. A letter that fails the fact
  check is rejected (one that fails only the critic is rendered with its
  critique for you), a resume fails loudly, and an answer's field stays open for
  you with the reason.
- **One setting:** `self_review.max_revision_rounds` in `config/filters.yaml`
  (default 1) caps the revise rounds of a passing resume selection per run, and
  the reviews per artifact at max(1, setting). A failing draft's review counts
  as one. At 0 the review still runs and reports, but nothing is revised.
- **Cost.** Each review's cache key covers the artifact, description hash,
  corpus hash (profile hash for answers), prompt versions and the full rendered
  prompt, so tailoring the same job again reviews nothing twice. A discarded
  revision, or a reply without a valid verdict, is dropped from the cache. A
  review that fails or returns no valid verdict changes nothing: the checked
  draft is used and `self_review.failed` is logged.
- **Records.** `out/<job folder>/self_review.md` lists, per artifact, what the
  model asked itself, its verdict, the changes it asked for and why, the bullets
  added or dropped, why a revision was discarded, and the questions for you. It
  is rendered from `self_review.json` in the same folder. Each review is a
  `self_review` event (artifact, round, verdict, outcome
  `no_change|accepted|discarded|not_revised`, `original_failed` when the
  reviewed draft had failed its checks, changes kept and dropped, bullets added
  and dropped, why a revision was discarded). `provenance.json` records the
  outcomes too. When a discarded resume revision's authenticity run was
  recorded, the shipped selection's run is recorded again, so the newest
  `authenticity_runs` row is always the artifact that is used.
- **Questions for you.** The review asks only for facts that are absent from
  the corpus and would close a real gap. Questions about pay history or
  demographics are dropped. Each is stored once per job and key as a
  `question_open` event (`key`, `question`, `why`, `fact_needed`, `job_id`,
  `artifact`) and shown with the scorer's gaps (see
  [Questions for you](how-it-works.md#questions-for-you)).
- **Read it:** the `get_self_review(n | job_id, full)` tool or `jobkit
  self-review <job_id> [--full]`. Both show the latest outcome per artifact,
  the questions raised, the report path, and with `full` the report itself.
  Neither makes a model call.

## Known limits

The fact check can't see a claim that has no number, no proper noun and none of
the words it looks for: "I also led the sales development team", or "I ran the
merchant team for ~18 months" when every word comes from its cited bullets but
no bullet says it. Holding every first-person sentence to its cited bullets'
words would also reject honest sentences. The claim judge, the critic (for a
letter) and your own read of the filled form are the checks for those.

The rules also reject some true sentences. When that happens, the letter is
written again, or held for you to edit. Known cases:

- A past tense in a clause with a modal is a claim whatever its subject: "I'd
  rather show the numbers than claim the team had been bigger" needs cited
  words.
- Any mention of the employer you are applying to in a sentence with a first
  person that doesn't only aspire: "I led 9 AEs at Haulmark, and I'd love to
  join Contoso", "I'd love to build my career at Contoso", "The team at Contoso
  impressed me", "What drew me to Contoso is its mission", "I interviewed at
  Contoso last year", "I'd love to stay in touch with Contoso", and "No, I have
  never worked at Contoso" (a negation excuses nothing: "I never left Contoso"
  says you are still there). Answer "Have you worked for us before?" yourself.
- A comment on the employer joined to a claim about you: "At Haulmark I built
  the onboarding program that cut time to first dispatch by ~40%, which is what
  your team needs". Write the comment as its own sentence ("Your team needs
  that.").
- Sentences the employer-as-subject rule turns away: "For your Account
  Associates, that means notes on every recorded call, not only the calls that
  fit around my meetings", "Your team doubled its payment volume".
- A tool named in a sentence about the employer ("Fabrikam Payments runs its
  forecast on Clari") when your `skill_index` doesn't have it.
- In a letter to a company that is itself a tool: "I'd love to build on it",
  "…grow with it", and sentences such as "At Haulmark I grew the program and its
  revenue", "At Haulmark I sold to our customers" and "I learned that in
  enablement, adoption is everything", which may point back at the product.
  The letter is written again with each pointing sentence and its word in the
  reason.
- A self-review change such as "Say the General Manager was who I reported to"
  is dropped before the writer sees it.
- A stored answer checked by `rejudge` has no citations, so a fact only a bullet
  states fails it, and the question comes back to you.
- A claim the judge reads as unsupported because the fact sits in a bullet the
  text didn't cite costs one more draft.

Also not seen: a later sentence that points back at the employer without the
name ("I worked there before").
