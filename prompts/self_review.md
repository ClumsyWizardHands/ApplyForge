---
version: "1.2"
stage: self_review
model_tier: opus
---
You just drafted the {artifact_kind} below for ONE job application. Before any
human sees it, review your own draft against the job, the gap report and the
candidate's full corpus, and decide whether it should change.

Ask yourself each of these questions explicitly, and answer each one:
1. Which of the job's must-haves does the draft leave unaddressed, although the
   corpus supports them? Cite the bullet ids that support each one.
2. Which of the strongest corpus facts relevant to this job does the draft
   leave unused? Cite their bullet ids.
3. Is the opening specific to this company and this role, or could it open an
   application to anyone? (For a resume: does each role lead with what this
   job needs most, and does the positioning line fit this role?)
4. Is anything apologetic, generic, repetitive or weak?
5. Does anything over-claim relative to the corpus: a number, title, tool,
   scope, team or result the cited bullets do not state exactly?
6. Is there anything I should update or change to give the best answer here?

Rules for the changes you ask for:
- Every change must be possible with corpus facts alone, and must name the
  bullet ids it draws on. Never ask for a fact, number, title, tool, team size
  or result the corpus does not state. The revision is machine-checked against
  the corpus and thrown away if it adds anything the corpus lacks.
- Write each change's "what" as the edit itself (add, cut, lead with, reword,
  re-pick), in the words of the corpus and the draft, naming the bullet ids.
  Put the requirement it answers in "why". A change whose "what" uses a word
  that the corpus, the draft and the job's name do not contain is dropped
  before the writer sees it. If the job needs a fact the corpus lacks, ask for
  it in questions_for_human instead.
- {scope}
- A resume change may only add, drop, reorder or re-pick (long/short) corpus
  bullets, or change the positioning id or the skills line (SKILL INDEX keys
  only). Bullet wording stays the corpus's own words.
- If VALIDATOR VIOLATIONS are listed below, the draft failed those checks: your
  first changes must fix every one of them, by removing or correcting what was
  flagged, never by restating it.
- Do not manufacture criticism. If the draft is already the best this corpus
  supports for this job, the verdict is "good" with no changes. "good" is a
  common and valid answer.

Questions for the candidate ("questions_for_human"): ask ONLY for a fact that
is ABSENT from the corpus and that would close a real gap for this job (a
must-have the corpus cannot support). Never ask what the corpus already
answers, never ask the candidate to confirm something it states, and never ask
about compensation history or demographic information. Give each question a
short, stable snake_case key that names the fact (the same fact gets the same
key on every job, e.g. "crm_admin_experience"). An empty list is the usual
answer.

Return ONLY a JSON object:
{"self_check": [{"question": "the question as asked above", "answer": "your answer, citing bullet ids"}],
 "verdict": "good|revise",
 "changes": [{"what": "the change to make", "why": "the question it answers or the violation it fixes", "bullet_ids": ["b_00X"]}],
 "questions_for_human": [{"key": "snake_case_fact_key", "question": "what to ask the candidate", "why": "the gap it would close for this job", "fact_needed": "the specific fact missing from the corpus"}]}
"changes" is empty when the verdict is "good".

JOB: {title} at {company}
ROLE TRACK: {track}

REQUIREMENTS (must_have / nice_to_have / implied):
{requirements}

GAP REPORT (from fit scoring):
{gaps}

CANDIDATE CORPUS (the only facts that exist):
{corpus}

VALIDATOR VIOLATIONS FROM THE LAST ATTEMPT:
{violations}

YOUR DRAFT ({artifact_kind}):
{draft}
