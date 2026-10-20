#!/usr/bin/env python3
"""M0 — verify every data-source endpoint in SPEC.md §2.1/§2.2 against live boards.

Stdlib only (runs before `uv sync`). Probes candidate public board tokens per
ATS, records HTTP status, payload shape, whether parseable JD text is present,
and (Greenhouse) where the application-form `questions` array actually lives.

Writes JSON results to stdout; docs/endpoints_verified.md is authored from it.
Failures on individual candidate tokens are normal (SPEC §2.1 caveats) — an
ATS passes if ANY candidate returns a parseable board.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# your contact address for the polite User-Agent (as jobkit.contact does)
_CONTACT = os.environ.get("JOBKIT_CONTACT_EMAIL", "").strip()
UA = (
    f"jobkit/0.1 (job-search tool; contact: {_CONTACT})"
    if _CONTACT
    else "jobkit/0.1 (personal job-search tool)"
)
TIMEOUT = 20


def get(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, object | str | None]:
    req = urllib.request.Request(
        url, method=method, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=TIMEOUT) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw[:200].decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # noqa: BLE001 — record, don't crash the sweep
        return 0, f"{type(e).__name__}: {e}"


def shape(obj: object, depth: int = 0) -> object:
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in list(obj.items())[:12]} if depth < 2 else "dict"
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1), f"…{len(obj)} items"] if obj else []
    return type(obj).__name__


def probe_greenhouse(token: str) -> dict:
    out: dict = {"ats": "greenhouse", "token": token}
    st, payload = get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    out["list_status"] = st
    if st != 200 or not isinstance(payload, dict):
        return out
    jobs = payload.get("jobs", [])
    out["job_count"] = len(jobs)
    if jobs:
        j = jobs[0]
        out["sample_keys"] = sorted(j.keys())
        out["has_jd_content"] = bool(j.get("content")) and len(j.get("content", "")) > 200
        out["questions_in_list_payload"] = "questions" in j
        # Spec §2.3 claims questions arrive with ?content=true on the list.
        # Verify the per-job endpoint too, which is where GH documents them.
        st2, detail = get(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{j['id']}?questions=true"
        )
        if st2 == 200 and isinstance(detail, dict):
            qs = detail.get("questions")
            out["questions_via_job_detail"] = isinstance(qs, list)
            if isinstance(qs, list) and qs:
                out["question_sample"] = {
                    k: qs[0].get(k) for k in ("label", "required", "fields") if k in qs[0]
                }
    return out


def probe_lever(company: str) -> dict:
    st, payload = get(f"https://api.lever.co/v0/postings/{company}?mode=json")
    out: dict = {"ats": "lever", "token": company, "list_status": st}
    if st == 200 and isinstance(payload, list):
        out["job_count"] = len(payload)
        if payload:
            j = payload[0]
            out["sample_keys"] = sorted(j.keys())
            desc = j.get("descriptionPlain") or j.get("description") or ""
            out["has_jd_content"] = len(desc) > 200
            out["has_apply_url"] = "applyUrl" in j or "hostedUrl" in j
    return out


def probe_ashby(board: str) -> dict:
    st, payload = get(
        f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
    )
    out: dict = {"ats": "ashby", "token": board, "list_status": st}
    if st == 200 and isinstance(payload, dict):
        jobs = payload.get("jobs", [])
        out["job_count"] = len(jobs)
        if jobs:
            j = jobs[0]
            out["sample_keys"] = sorted(j.keys())
            out["has_jd_content"] = (
                len(j.get("descriptionPlain") or j.get("descriptionHtml") or "") > 200
            )
            out["has_compensation"] = "compensation" in j
    return out


def probe_workable(sub: str) -> dict:
    st, payload = get(f"https://apply.workable.com/api/v1/widget/accounts/{sub}?details=true")
    out: dict = {"ats": "workable", "token": sub, "list_status": st}
    if st == 200 and isinstance(payload, dict):
        jobs = payload.get("jobs", [])
        out["job_count"] = len(jobs)
        if jobs:
            out["sample_keys"] = sorted(jobs[0].keys())
            out["has_jd_content"] = len(jobs[0].get("description") or "") > 200
    return out


def probe_smartrecruiters(company: str) -> dict:
    st, payload = get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings")
    out: dict = {"ats": "smartrecruiters", "token": company, "list_status": st}
    if st == 200 and isinstance(payload, dict):
        content = payload.get("content", [])
        out["job_count"] = payload.get("totalFound", len(content))
        if content:
            j = content[0]
            out["sample_keys"] = sorted(j.keys())
            # JD text requires the posting-detail endpoint
            st2, detail = get(
                f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{j['id']}"
            )
            out["detail_status"] = st2
            if st2 == 200 and isinstance(detail, dict):
                sections = detail.get("jobAd", {}).get("sections", {})
                out["has_jd_content"] = bool(sections)
    return out


def probe_recruitee(company: str) -> dict:
    st, payload = get(f"https://{company}.recruitee.com/api/offers/")
    out: dict = {"ats": "recruitee", "token": company, "list_status": st}
    if st == 200 and isinstance(payload, dict):
        offers = payload.get("offers", [])
        out["job_count"] = len(offers)
        if offers:
            out["sample_keys"] = sorted(offers[0].keys())
            out["has_jd_content"] = len(offers[0].get("description") or "") > 200
    return out


def probe_rippling(token: str) -> dict:
    st, payload = get(f"https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs")
    out: dict = {"ats": "rippling", "token": token, "list_status": st}
    if st == 200 and isinstance(payload, list):
        out["job_count"] = len(payload)
        if payload:
            out["sample_keys"] = sorted(payload[0].keys())
    return out


def probe_aggregators() -> list[dict]:
    results = []
    for name, url, extract in [
        ("arbeitnow", "https://www.arbeitnow.com/api/job-board-api", lambda p: p.get("data", [])),
        ("remotive", "https://remotive.com/api/remote-jobs?limit=5", lambda p: p.get("jobs", [])),
        ("remoteok", "https://remoteok.com/api", lambda p: p[1:] if isinstance(p, list) else []),
        ("himalayas", "https://himalayas.app/jobs/api?limit=5", lambda p: p.get("jobs", [])),
    ]:
        st, payload = get(url)
        row: dict = {"source": name, "status": st}
        if st == 200 and payload is not None and not isinstance(payload, str):
            try:
                jobs = extract(payload)
                row["job_count"] = len(jobs)
                if jobs and isinstance(jobs[0], dict):
                    row["sample_keys"] = sorted(jobs[0].keys())[:15]
            except Exception as e:  # noqa: BLE001
                row["parse_error"] = str(e)
        results.append(row)
    return results


CANDIDATES = {
    "greenhouse": (probe_greenhouse, ["anthropic"]),
    "lever": (probe_lever, ["palantir", "plaid", "mistral", "attentive", "voleon"]),
    "ashby": (probe_ashby, ["openai", "ramp", "linear", "notion", "cursor"]),
    "workable": (probe_workable, ["huggingface", "hugging-face", "netdata", "bitpanda"]),
    "smartrecruiters": (
        probe_smartrecruiters,
        ["smartrecruiters", "BoschGroup", "Visa", "ServiceNowInc"],
    ),
    "recruitee": (probe_recruitee, ["recruitee", "framework", "sendcloud", "hypercare"]),
    "rippling": (probe_rippling, ["rippling", "rippling-careers"]),
}


def main() -> None:
    results: dict = {"ats": {}, "aggregators": []}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {}
        for ats, (fn, tokens) in CANDIDATES.items():
            futs[ats] = [ex.submit(fn, t) for t in tokens]
        agg_fut = ex.submit(probe_aggregators)
        for ats, fs in futs.items():
            results["ats"][ats] = [f.result() for f in fs]
        results["aggregators"] = agg_fut.result()
    json.dump(results, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
