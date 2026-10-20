---
version: "1.5"
stage: score
model_tier: haiku
---
Score how well THIS candidate fits THIS job, 0-100. Return ONLY a JSON object:

{
  "score": 0-100,
  "rationale": "exactly two sentences",
  "gaps": ["unmet must-haves, most important first; an overqualification gap goes LAST and starts with 'overqualified:'"]
}

CANDIDATE PROFILE (the only facts you may use — do not invent):
{profile}

WHAT THE SCORE MEANS: how likely this candidate is to be invited to interview,
judged the way a recruiter screens — must-haves first, then title history and
dates. Work through it in this order:

1. Must-have coverage. For each must-have, find the profile fact that meets
   it. A must-have with no supporting fact is a gap. Never assume a skill,
   tool or domain the profile does not state.
2. Years and level. Every role in the profile has its title, dates and years
   in role. Compute tenure from them — never write "tenure not documented",
   "not explicitly confirmed" or "inferred from career arc". When the job asks
   for N+ years at a level (closing AE, people manager, director), count only
   the years in roles at that level; total years in sales count only where the
   job asks for total years. A shortfall is a gap that costs points in
   proportion to its size.
3. Level fit. One level above the highest title held is a stretch. Two or more
   levels above rarely gets an interview, however strong the metrics.
4. Overqualification is NOT disqualifying. The candidate may deliberately
   target roles below their highest title. Do not score such a role near zero:
   score it on must-have coverage and interview likelihood, and list the
   overqualification as the LAST gap, starting "overqualified:" and phrased as
   what the application must address (for example "overqualified: 5 yrs
   leading a sales team — the application must explain the move to an
   individual-contributor seat").

WORKED ANCHORS — five role examples, each scored against one profile shape:
an invented example candidate, NOT the candidate above, with about 4 yrs as
an account manager at an office-equipment dealer (renewals and upsell across
a book of small-business accounts), then about 2.5 yrs, to date, as a
mid-market account executive at a software company (full-cycle new business,
at or above quota in both full years, deals mostly $20-40K ACV). No role
involved managing people, and the example's NEVER CLAIM list rules out
manager, director and VP titles. The anchors teach the method, not the
answer: apply the same reasoning to the CANDIDATE PROFILE above, and where it
differs from this shape, the profile's facts win.

ANCHOR 90 — "Account Executive, Mid-Market SaaS. Must-haves: 2+ yrs closing
new business as a quota-carrying AE, full-cycle sales, $15-50K ACV deals,
Salesforce." → 90. Every must-have is met at the right level: about 2.5 yrs of
full-cycle closing at or above quota, deals inside the band, Salesforce in the
profile. Gap: no experience in the posting's industry. Score 85-92 when the
level matches and every must-have is met.

ANCHOR 80 — "Account Manager, SMB. Must-haves: 2+ yrs managing a book of
existing customers, renewals and upsell, CRM hygiene." → 80. Every must-have
is met by the account-manager years; the closing years strengthen the case
rather than weaken it. Gaps: the account-management role ended over two years
ago; "overqualified: 2.5 yrs closing new business as an AE — the application
must explain the move back to an account-management seat". Score 76-86 for a
full must-have match one or more levels below the highest title held.

ANCHOR 66 — "Senior Account Executive, Mid-Market. Must-haves: 4+ yrs closing
as a quota-carrying AE, full-cycle sales, $40-80K ACV deals, MEDDIC." → 66.
Full-cycle closing at quota is met, and the account-management years are
adjacent evidence of working a book; but direct closing tenure is about 2.5
yrs against 4+, the deals were mostly below the $40-80K band, and MEDDIC is
not in the profile. Score 60-72 when the sales motion matches but direct
tenure at the level is short.

ANCHOR 45 — "Sales Manager, Mid-Market. Must-haves: 2+ yrs managing a team of
AEs, hiring and coaching, forecasting a team number." → 45. The candidate's own
quota record is strong, but the role is one level above the highest title held
and its core must-have is unmet: no role in the profile involved managing
people. Score 40-55 when a role one level up rests on a must-have the profile
does not meet.

ANCHOR 25 — "VP of Sales. Must-haves: 10+ yrs in sales, 5+ yrs leading
managers, scaled a sales org through a major ARR milestone, board-level
forecasting." → 25. The highest sales title held is a mid-market account
executive, total sales tenure is under 7 yrs, and the example's NEVER CLAIM
list rules out manager, director and VP titles. Score below 35 when the role
is two or more levels above the highest title held, however strong the
metrics.

Distribute honestly across the full range. A 75 must be meaningfully better than
a 65. Do not cluster at 70-85.

JOB TITLE: {title}
COMPANY: {company}
REQUIREMENTS (extracted):
{requirements}
