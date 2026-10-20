---
version: "1.3"
stage: cover
model_tier: opus
---
Draft a follow-up email for a job application. Short — 60 to 110 words. The
candidate's voice: direct, concrete, zero fluff. SENDING IS ALWAYS MANUAL —
this is a draft for the human to review.

Kind-specific shape:
- status_check (+7d): one line re-affirming interest with ONE concrete reason
  drawn from the corpus bullets below, one line asking about timeline. No apology
  for following up.
- hiring_manager_note (+14d): to a named hiring manager; one specific thing the
  candidate would do in the role's first month, grounded in a corpus bullet.

Hard bans: "just following up", "touching base", "circling back", "excited",
"passionate", "I hope this finds you well".

Figures (ESTIMATE STYLE below): copy every number's value, unit, range and
"up to"/"+" exactly as the bullet has it; never add precision or round.
- confident: drop approximation marks ("~", "about", "roughly") and state the
  figure plainly ("cut ramp time by ~40%" becomes "cut ramp time by 40%").
- hedged: keep the bullet's approximation marks.

Sign off with the candidate's name exactly as given below, nothing else.

Return ONLY JSON: {"subject": "...", "body": "..."}

KIND: {kind}
JOB: {title} at {company}
CANDIDATE NAME: {name}
ESTIMATE STYLE: {estimate_style}
CORPUS BULLETS (only allowed facts):
{bullets}
