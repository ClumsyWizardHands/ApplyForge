---
version: "1.0"
stage: remote
model_tier: haiku
---
JDs lie about remote work. Your job: extract what this posting SAYS about
location policy, then hunt the fine print for contradictions.

Patterns to catch:
- "Remote" alongside "3 days in office" or "must be within commuting distance"
- "Remote-friendly" (usually means onsite with exceptions) vs "remote-first"
- Geographic restrictions: "remote — US only", "must reside in CA", "EST hours"
- Quarterly-onsite or mandatory-offsite clauses
- "Hybrid" with unstated office-day counts

Return ONLY a JSON object:

{
  "remote_stated": "remote|hybrid|onsite|unclear",   // what the JD claims up front
  "remote_actual": "remote|hybrid|onsite|unclear",   // what the fine print implies
  "remote_conflict": true|false,                     // stated != actual
  "geo_restriction": "US only" | "CA residents" | "EU timezones" | null,
  "office_days_required": 0 | 1 | 2 | 3 | 4 | 5 | null,   // null = not stated
  "evidence": "the exact phrase(s) that reveal the actual policy, quoted"
}

JOB TITLE: {title}
LOCATION FIELD: {location}
JOB DESCRIPTION:
{jd}
