---
name: jobkit-daily
description: The person's daily jobkit routine - what's in the queue, approve or reject by number, check the tailored resume and letter, open the filled application forms, log what they submitted, record employer responses, answer the questions jobkit has for them, and handle follow-ups. Use when the person says "what's in the queue", "jobkit", "approve 1, 3", "open my applications", "I submitted ...", "I heard back from ...", "any follow-ups?", "questions for me", or pastes a job link.
---

<!-- Maintainers: name jobkit MCP tools as calls, e.g. get_queue(), and CLI
commands in code spans, e.g. `uv run jobkit open-queue`. tests/test_claude_skills.py
checks that each one exists. -->

# jobkit daily routine

Overnight (or at the scheduled time) jobkit found postings, scored them,
tailored the jobs the person approved, and filled those forms. Now the person
decides. Their time should go to deciding and clicking submit, nothing else.

## How to talk

- Short messages. One decision per message when you need an answer.
- Numbers they can type back: "approve 2, 5" beats a paragraph.
- Lead with what waits on them. Skip what does not.
- Never decide for them. Offer, then wait.

## The gates (never skip one)

1. **The person approves.** Call approve() only with the numbers they gave.
   Rejections and snoozes are theirs too. "approve everything above 85"
   works (approve(ids=">85")) only when they say it.
2. **The person submits.** jobkit fills a form up to the last click and
   stops. They review it and click submit themselves. You never click
   submit, and never solve a CAPTCHA or bot check. jobkit has no submission
   step to turn on: there is no `src/jobkit/autofill/submit.py` and no
   `auto_submit` setting. If they ask jobkit to submit for them, say so. Do
   not build, enable or configure automatic submission: that is a change to
   Hard Rule 1 in `CLAUDE.md`, which only the owner of the jobkit GitHub
   repository makes.
3. **Facts only from the corpus, in their words.** Answers they give go in
   as they said them. Never reword, add a "yes", a tool or a company, or fill
   in evidence or metric_confidence for them.
4. **A corpus change needs their explicit yes** to that proposal:
   confirm_proposal() is the gate.
5. **They send follow-ups.** jobkit drafts; it never sends.

The jobkit MCP tools must be connected for approving and logging (approve,
reject, mark_submitted, record_outcome have no CLI twin). If they are not:
run `uv run jobkit doctor`, fix the MCP item, and start a new session in the
jobkit folder (approve the "jobkit" server).

## 1. What's in the queue

get_queue() lists jobs by number with score, comp, remote verdict, form
friction and gaps. Numbers are pinned until the next get_queue(). Show:

- The banner's ⚠ lines first (stale data, failed nightly stages, forms a
  staging run stopped on: CAPTCHA, ID or payment field, assessment).
- The top 10, one line each. Say how many more.
- Today's questions for them (at the end of the output): count only, and
  offer to go through them (section 6).

Then ask: "Approve, reject or skip any? (e.g. approve 1, 4 · reject 2)"

Details on request: get_job(n) for one job (JD summary, requirements, comp
analysis, flags).

## 2. Decide

- approve(ids="1,4"): records the decision and starts tailoring in the
  background (resume and cover letter from the corpus only).
- reject(ids="2", reason="..."): ask for a short reason; it tunes filters and
  calibration. "no reason" is fine.
- snooze(ids="3", days=7): back in the queue later.

## 3. Check what was written

A few minutes after approving:

- get_artifacts(n): paths to the resume PDF, cover letter and gap report.
  Offer to open the PDF. If the letter was rejected by the fact check, say so
  and why (review_artifacts(n)).
- get_self_review(n): what the self-review changed and what it asked.
- retailor(n) after a corpus change, only on their request.
- background_status() shows whether tailoring is still running.

## 4. Open the filled forms

- Forms already filled overnight: open_queue() opens them as tabs in one
  browser, fully filled first (CLI: `uv run jobkit open-queue`).
- A job approved today: stage_forms(ids="1,4"), or by job id
  stage_forms(job_ids="412") (CLI: `uv run jobkit stage --job-id 412 --keep-open`).
  The form is filled from the answer bank and the drafts, and left open.
- Only one browser can use the jobkit profile at a time. "a browser holds
  the staging profile": they quit that browser (Cmd+Q), then try again.
- Questions the form asks that jobkit could not answer: get_novel_questions(n).
  Ask the person each one; record their words with
  answer_novel(n, field_identity, answer). EEO fields come only from the
  answer bank, and salary history is never answered: the tool refuses both.
- Then they review every field and click submit themselves.

## 5. Log what happened

- **Submitted**: "I submitted Acme and Beta" → mark_submitted(job_ids="412, 418")
  (job ids are in the open_queue() and staging output; queue numbers work
  only from the current get_queue()). Back-date with submitted_on="YYYY-MM-DD".
  This schedules the follow-ups. Applied somewhere by hand? Same call.
- **Employer response**: record_outcome(job_id, outcome, note) with outcome
  `reply`, `rejection`, `interview` or `offer`, and their note (who wrote,
  what they said). It stops that job's pending follow-ups.

## 6. Questions for them

get_questions() lists facts the corpus lacks that would close gaps in open
jobs (at most a few a day), each with why it matters. One per message:

1. Ask the question as written. Record the answer in their exact words:
   answer_question(key, answer). "No" or "never used it" is a valid answer
   (recorded as a decline); so is answer_question(key, decline=true).
2. If the tool refuses the wording, tell them why and ask them to restate.
   Never rephrase it for them.
3. The answer becomes a proposal. Ask: where could you show this (evidence),
   and how solid is the number (verified / estimate / none)? For a bullet,
   which role it belongs to.
4. On their explicit yes: confirm_proposal(proposal_id, evidence,
   metric_confidence, role_id). That writes the corpus and re-scores the
   jobs it helps.

undecline_question(key) re-opens a question they declined, only when they ask.
CLI twins: `uv run jobkit questions`, `uv run jobkit answer <key> "<text>"`,
`uv run jobkit confirm-proposal <id>`.

## 7. Follow-ups

get_followups() shows the follow-ups due with their drafts (it starts drafting
any that have none; ask again in a few minutes). For each: show the draft,
they edit and send it themselves, then mark_followup_sent(followup_ids="12, 13").

## 8. Anything else

- A job link from anywhere: add_job_url(url) (CLI: `uv run jobkit add-url "<url>"`).
- How it is going: get_stats() (funnel and follow-ups due), get_digest().
- Did last night's run work? schedule_status() (the nightly run stage by
  stage, and jobs it stopped on), background_status().
- Something broken: doctor().

## Weekly (offer, don't push)

- calibrate(): numbered proposals from their decisions. Each one is applied
  only if they accept that item; they or you edit the config only then.
- tune_filters(): top rejection reasons. After a filter change,
  refilter() shows the dry run; refilter(confirm=true) only on their yes.
- corpus_health(): how thin the corpus is.

## Close

End a session with one line: what is still waiting on them (forms to submit,
questions, follow-ups), or "All clear for today."
