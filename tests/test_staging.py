"""M5 staging logic — offline. Abort scanning, per-field provenance sources,
EEO/salary-history guarantees at fill time. The submit-guard static scan in
test_form_specs.py already covers browser.py (it scans all of autofill/)."""

from __future__ import annotations

from jobkit.autofill.browser import _identity_value, _value_for, scan_abort_html

BANK = {
    "identity": {
        "first_name": "Jordan",
        "last_name": "Rivera",
        "email": "a@x.com",
        "phone": "+1 555",
        "city": "Bristol",
        "state": "PA",
        "linkedin": "li.com/x",
    },
    "screening": [
        {"id": "work_auth", "answer": "Yes"},
        {"id": "salary_history", "answer": "", "action": "leave_blank_and_flag"},
    ],
}


def test_abort_on_captcha_turnstile_assessment_and_ids() -> None:
    assert scan_abort_html('<div class="g-recaptcha">') == "captcha-or-turnstile-detected"
    assert scan_abort_html("<script src=turnstile.js>") == "captcha-or-turnstile-detected"
    assert scan_abort_html("Enter your Social Security Number") == (
        "requests-government-id-or-payment"
    )
    assert scan_abort_html("complete this timed test to continue") == (
        "embedded-assessment-detected"
    )
    assert scan_abort_html("<form><input name=email></form>") is None


def test_value_sources_are_correct() -> None:
    v, src = _value_for({"kind": "identity", "entry_id": "email"}, BANK, {}, "email")
    assert v == "a@x.com" and src == "answer_bank"
    v, src = _value_for({"kind": "screening", "entry_id": "work_auth"}, BANK, {}, "wa")
    assert v == "Yes" and src == "answer_bank"
    v, src = _value_for({"kind": "eeo", "entry_id": "gender", "answer": "Male"}, BANK, {}, "g")
    assert v == "Male" and src == "answer_bank"
    v, src = _value_for({"kind": "novel"}, BANK, {"q1": "drafted answer"}, "q1")
    assert v == "drafted answer" and src == "ai_drafted"
    v, src = _value_for({"kind": "novel"}, BANK, {}, "q2")
    assert v is None and src == "unfilled"


def test_salary_history_never_filled() -> None:
    v, _ = _value_for({"kind": "screening", "entry_id": "salary_history"}, BANK, {}, "sh")
    assert v is None, "leave_blank_and_flag answers must never produce a value"


def test_identity_full_name_and_address() -> None:
    assert _identity_value("name", BANK) == "Jordan Rivera"
    assert _identity_value("address", BANK) == "Bristol, PA"
    assert _identity_value("nonexistent", BANK) is None
