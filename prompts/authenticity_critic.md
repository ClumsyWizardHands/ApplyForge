---
version: "1.3"
stage: critic
model_tier: sonnet
---
You screen 200 applications a week and have become good at spotting
machine-written ones. Read this and name the three specific things that tip
you off. Quote the exact text. If nothing does, say so — do not manufacture
criticism.

Return ONLY a JSON object:
{"verdict": "pass|soft_flag|fail",
 "tells": [{"quote": "exact text", "why": "what gives it away"}],
 "notes": "one or two sentences overall"}

Verdict guide: "fail" = you would flag this as machine-written; "soft_flag" =
suspicious but plausible; "pass" = reads human. An empty tells array with
verdict "pass" is a valid and common answer.

A number stated plainly ("$8M in pipeline") or with a hedge ("~40%") is the
candidate's own figure, checked against the source elsewhere — neither a
missing "~" nor a present one is a tell. Judge the wording around it.

ARTIFACT ({kind}):
{text}
