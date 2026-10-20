---
version: "1.2"
stage: comp
model_tier: haiku
---
Assess how demanding this job is, so its scope can be compared against its
posted pay. Return ONLY a JSON object:

{
  "demand_score": 0-100,        // how much the job asks for
  "demand_factors": ["..."],    // what drives the score
  "breadth_roles": 1-4,         // how many distinct jobs this posting really is
  "management_scope": true|false,
  "on_call": true|false,
  "travel_pct": 0-100 | null,
  "unusual_credentials": ["..."],
  "comp_basis": "base|ote|unclear",      // for sales roles: is the posted number base salary or OTE?
  "ote_realism": "realistic|inflated|unclear|n/a",
  "ote_rationale": "one sentence"
}

OTE realism (sales roles only — otherwise "n/a"): judge whether the on-target
earnings are attainable. Signals of a REALISTIC OTE: stated base/variable split
near 50/50, defined territory or book, ramp guarantee, established product with
named customers. Signals of an INFLATED OTE: "uncapped commission" doing all the
work, no split stated, seed-stage company with no customer evidence, quota that
implies unrealistic deal velocity, "top reps earn $X" framing.

Demand score guide:
- 20: single-function IC role, reasonable requirements
- 50: senior IC or first-line manager, focused scope
- 75: management + hands-on + cross-functional ownership
- 90+: multiple jobs in one posting (e.g. "sales director + AE + SDR + ops"),
  heavy management, on-call, or credential-stacking

Count breadth honestly: a posting that asks one person to lead the sales team,
close deals and prospect at once is breadth_roles=3, however it is framed. That
pattern specifically must be caught.

JOB TITLE: {title}
POSTED COMP: {comp}
JOB DESCRIPTION:
{jd}
