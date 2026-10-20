---
version: "1.0"
stage: extract
model_tier: haiku
---
You are extracting structured requirements from a job description for a matching
system. Read the JD below and return ONLY a JSON object, no other text:

{
  "must_have": ["..."],       // explicit hard requirements (skills, years, credentials)
  "nice_to_have": ["..."],    // explicitly optional or "preferred"
  "implied": ["..."],         // strongly implied but unstated (e.g. "own the number" => quota-carrying)
  "disqualifier": ["..."]     // anything that would disqualify a candidate outright
}

Rules:
- Short noun phrases, not sentences. "8+ years B2B sales" not "The candidate should have..."
- Do not invent requirements that are not in the text.
- Ignore EEO statements, benefits, and company boilerplate.
- Empty arrays are valid.

JOB TITLE: {title}
JOB DESCRIPTION:
{jd}
