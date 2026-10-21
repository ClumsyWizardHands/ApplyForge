---
version: "1.0"
stage: score
model_tier: haiku
---
Score how well THIS candidate fits THIS job, 0-100. Return ONLY a JSON object:

{
  "score": 0-100,
  "rationale": "exactly two sentences",
  "gaps": ["unmet must-haves, most important first; a career-change gap comes after them; an overqualification gap goes LAST and starts with 'overqualified:'"]
}

CANDIDATE PROFILE (the only facts you may use — do not invent):
{profile}

WHAT THE SCORE MEANS: how likely this candidate is to be invited to interview,
judged the way a recruiter or hiring manager screens: must-haves first, then
title history and dates. Work through it in this order:

1. Must-have coverage. For each must-have (a skill, tool, license,
   certification, setting or kind of work), find the profile fact that meets
   it. A must-have with no supporting fact is a gap. Never assume a skill,
   tool, license or domain the profile does not state.
2. Years and level. Every role in the profile has its title, dates and years
   in role. Compute tenure from them — never write "tenure not documented",
   "not explicitly confirmed" or "inferred from career arc". When the job asks
   for N+ years at a level (senior engineer, charge nurse, lead teacher, people
   manager, director), count only the years in roles at that level; the
   profile's years on the target tracks count only where the job asks for
   total years in the field. A shortfall is a gap that costs points in
   proportion to its size.
3. Level fit. One level above the highest title held is a stretch. Two or more
   levels above rarely gets an interview, however strong the record.
4. Overqualification is NOT disqualifying. The candidate may deliberately
   target roles below their highest title. Do not score such a role near zero:
   score it on must-have coverage and interview likelihood, and list the
   overqualification as the LAST gap, starting "overqualified:" and phrased as
   what the application must address (for example "overqualified: 4 yrs as a
   nurse manager — the application must explain the move back to a
   staff-nurse seat").
5. Career change. Read the CAREER-CHANGE NOTE. When it says none, skip this
   step. Otherwise the candidate is deliberately moving into this kind of role
   from other work:
   - A must-have met by work done under another title counts as met. Score it
     on coverage, not on the title it was done under.
   - No years under the target title is ONE gap, not a level penalty and not a
     shortfall on every must-have. Phrase it as what the application must
     address, for example "career change: no years under the instructional
     designer title — the application must connect the classroom work to this
     role".
   - List that gap after the unmet must-haves and before any "overqualified:"
     gap.
   - A must-have the other work does not meet is still a gap.

CAREER-CHANGE NOTE: {pivot_note}

BANDS — what a score means, each with one invented example from a different
field. The examples are NOT the candidate above: they teach the method, and
where the profile differs, the profile's facts win.

85-92 — every must-have met at the right level. Example (software): "Backend
Engineer. Must-haves: 3+ yrs building Python services, PostgreSQL, production
on-call." A profile with 4 yrs as a backend engineer covering all three scores
about 88; the only gap is no experience in the posting's industry.

76-86 — every must-have met, one or more levels below the highest title held.
Example (nursing): "Staff RN, Med-Surg. Must-haves: active RN license, 2+ yrs
acute care, BLS." A profile with 5 yrs as a staff RN and then 3 yrs as charge
nurse meets all three and scores about 80, with the last gap
"overqualified: 3 yrs as charge nurse — the application must explain the move
back to a staff seat".

60-72 — the work matches, but tenure at the level is short. Example
(teaching): "Lead Teacher, Grade 5. Must-haves: state teaching license, 4+ yrs
as lead classroom teacher, reading intervention." A profile with the license,
intervention work and 2 yrs as lead teacher scores about 66: the tenure is
half of what is asked.

40-55 — one level up, and the core must-have is unmet. Example (sales):
"Sales Manager. Must-haves: 2+ yrs managing a team of account executives,
hiring and coaching." A profile with a strong quota record as an account
executive and no role managing people scores about 45.

Below 35 — two or more levels above the highest title held. Example
(software): "VP of Engineering. Must-haves: 12+ yrs, 5+ yrs leading managers,
an organisation of 100+." A profile whose highest title is software engineer
scores below 35, however strong the record.

CAREER-CHANGE BAND (only when the CAREER-CHANGE NOTE is not none) — the
must-haves are met by work done under other titles. Example (teaching to
instructional design): "Instructional Designer. Must-haves: design courses
from learning objectives, write assessments, LMS experience, Articulate
Storyline." A profile with 7 yrs as a classroom teacher who wrote units from
learning objectives, built assessments and ran courses in a learning
management system meets three of the four. Storyline is a gap, then the
career-change gap. It scores about 66. Score 70-80 when the other work meets
every must-have, 55-70 when one is unmet, and lower when the core of the role
is unmet.

Distribute honestly across the full range. A 75 must be meaningfully better than
a 65. Do not cluster at 70-85.

JOB TITLE: {title}
COMPANY: {company}
REQUIREMENTS (extracted):
{requirements}
