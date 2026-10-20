# M0 — Endpoints verified against live boards

**Date:** 2026-08-18 · **Method:** `scripts/verify_endpoints.py` (stdlib, no deps) · every claim below observed live, not assumed.

## Direct ATS board endpoints (SPEC §2.1)

| ATS | Verified | Live board tested | Jobs | JD text | Notes |
| :-- | :-- | :-- | --: | :-- | :-- |
| Greenhouse | ✅ | `anthropic` | 462 | ✅ `content` (HTML) | **Spec correction — see below.** List payload also carries `ai_disclaimer`, `application_deadline`, `data_compliance`, `metadata`, `offices`. |
| Lever | ✅ | `palantir` | 308 | ✅ `descriptionPlain` + `descriptionBodyPlain` | `plaid`, `mistral`, `voleon` return `200 []` — a valid endpoint with zero public postings is indistinguishable from a wrong token. Treat `200 []` as "unverified token", not "no openings". `workplaceType` present. |
| Ashby | ✅ | `openai`, `ramp`, `linear`, `notion`, `cursor` | 32–729 | ✅ `descriptionPlain` | `?includeCompensation=true` works — `compensation` present on all five boards. `isRemote`, `workplaceType`, `applyUrl` in list payload. |
| Workable | ✅ | `huggingface` | 7 | ✅ `description` | Widget endpoint live. `telecommuting` is the remote flag; `application_url` present. |
| SmartRecruiters | ✅ | `BoschGroup` (4800), `smartrecruiters`, `Visa` | 2–4800 | ✅ via detail | List payload has **no JD text** — requires `GET /postings/{id}` per job (`jobAd.sections`). Tier-dependent visibility confirmed: `ServiceNowInc` returns `200` with 0 postings. |
| Recruitee | ⚠️ unverified | — | — | — | Endpoint shape untested: all four candidate tokens 404'd. Shape is probably still `{company}.recruitee.com/api/offers/` but we have no live board. Re-verify when the first real Recruitee target appears; do not build the adapter until then. |
| Rippling | ⚠️ partial | `rippling` | 695 | ❌ not in list | `GET /platform/api/ats/v1/board/{token}/jobs` works, but the list payload is shallow: `{department, name, url, uuid, workLocation}` only. JD text requires a per-job follow-up fetch (job `url`). Confirmed newer/less rich, as SPEC warned. |

### Spec correction (Greenhouse form spec, SPEC §2.3)

SPEC v1.4 claimed the `questions` array arrives in the jobs list via `?content=true`.
**Observed: it does not.** The list payload (`/v1/boards/{token}/jobs?content=true`) has no
`questions` key. The form spec lives on the per-job detail endpoint:

```
GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true
```

Verified live: returns `questions[]` with `label`, `required`, and `fields[]` — each field
carrying `name` (e.g. `first_name`), `type` (e.g. `input_text`), and `values` (options).
Consequence for M2b: Greenhouse form-spec capture is **one extra request per surviving job**,
not free with the poll. SPEC.md §2.3 table updated to match.

## Aggregator feeds (SPEC §2.2)

| Source | Verified | Jobs returned | Notes |
| :-- | :-- | --: | :-- |
| Arbeitnow | ✅ | 176 | No key. `remote` boolean, `tags`, `location` present. |
| Remotive | ✅ | 17 (limit=5 ignored?) | No key. `candidate_required_location`, `salary` (free text) present. |
| RemoteOK | ✅ | 100 | No key, but requires a real `User-Agent`. First array element is a legal notice, not a job — skip index 0. `salary_min`/`salary_max` numeric. |
| Himalayas | ✅ | 5 (limit respected) | No key. `locationRestrictions`, `minSalary`/`maxSalary` numeric. |
| Adzuna | ⏸ untested | — | Needs `app_id` + `app_key` (free tier). Verify at M1b once keys exist in `.env`. |
| USAJOBS | ⏸ untested | — | Needs free API key + `User-Agent` email header. Disabled in config until federal roles are in scope. |

## Key payload shapes (as observed 2026-08-18)

- **Greenhouse job (list):** `id, internal_job_id, title, content, absolute_url, location, offices, departments, metadata, updated_at, first_published, requisition_id, company_name, language, application_deadline, ai_disclaimer, ai_opt_out_request_url, include_ai_disclaimer, data_compliance`
- **Greenhouse question:** `{label, required, fields: [{name, type, values}]}`
- **Lever posting:** `id, text, hostedUrl, applyUrl, categories, country, workplaceType, description, descriptionPlain, descriptionBody, descriptionBodyPlain, additional, additionalPlain, lists, opening, openingPlain, createdAt`
- **Ashby job:** `id, title, department, team, employmentType, location, secondaryLocations, address, isRemote, isListed, workplaceType, publishedAt, jobUrl, applyUrl, descriptionHtml, descriptionPlain, compensation, shouldDisplayCompensationOnJobPostings`
- **Workable job:** `title, shortcode, code, url, application_url, shortlink, department, function, industry, telecommuting, employment_type, experience, education, description, city, state, country, locations, created_at, published_on`
- **SmartRecruiters posting (list):** `id, uuid, name, refNumber, ref, company, location, department, function, industry, experienceLevel, typeOfEmployment, releasedDate, language, customField, visibility, jobAdId, defaultJobAd` — JD via `GET /postings/{id}` → `jobAd.sections`
- **Rippling job (list):** `uuid, name, url, department, workLocation` — shallow; JD needs follow-up fetch
- Full raw results: run `python3 scripts/verify_endpoints.py` (re-probes live).

## M0 acceptance

- [x] Greenhouse returns parseable JD text for a known posting (`anthropic`, 462 jobs)
- [x] Lever returns parseable JD text for a known posting (`palantir`, 308 jobs)
- [x] Ashby returns parseable JD text for a known posting (5 boards, all with compensation)
- [x] Greenhouse `questions` array captured (via per-job `?questions=true` — spec corrected)
- [x] All endpoint shapes recorded above with date
