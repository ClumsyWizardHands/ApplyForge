---
version: "1.0"
stage: extract
model_tier: haiku
---
You are extracting structured requirements from a job description for a matching
system. The job can be in any field. Read the JD below and return ONLY a JSON
object, no other text:

{
  "must_have": ["..."],       // explicit hard requirements (skills, years, licenses, credentials)
  "nice_to_have": ["..."],    // explicitly optional or "preferred"
  "implied": ["..."],         // strongly implied but unstated (e.g. "lead the unit on nights" => charge nurse experience)
  "disqualifier": ["..."]     // anything that would disqualify a candidate outright
}

Rules:
- Short noun phrases, not sentences. "3+ years acute care nursing", "active state
  teaching license", "5+ years backend development" — not "The candidate should
  have...".
- Keep a license, certification or clearance as written ("BLS certification",
  "Secret clearance").
- Do not invent requirements that are not in the text.
- Ignore EEO statements, benefits, and company boilerplate.
- Empty arrays are valid.

JOB TITLE: {title}
JOB DESCRIPTION:
{jd}
