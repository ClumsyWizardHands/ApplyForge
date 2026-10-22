# Form filling

For each approved job (approved by you, or by the bar you set), ApplyForge
(the `jobkit` command) opens the
application form in a visible browser window, attaches the tailored resume and
cover letter, fills what it can from your answer bank, and stops. You check the
form, fill what was left blank, and click submit. [Why you click
submit](../README.md#why-you-click-submit) explains that choice.

## Which forms

Form filling works on Greenhouse, Lever and Ashby application forms. Those are
the systems whose forms ApplyForge reads ahead of time (overnight) and knows how to
fill. A job whose apply link is anywhere else (Workday, iCIMS, Taleo,
SmartRecruiters, an employer's own portal) is not filled. ApplyForge has no form
spec for it, so the run stops it as `no-form-spec-captured` (or
`apply-url-not-allowlisted` when the link's host is neither a known job system
nor the employer's own domain) and leaves it for you.

For a single application on another system, Claude in Chrome can help you
through it in your own browser, one form at a time. Many of those systems
require you to create an account with the employer, which ApplyForge never does
for you.

## Commands

`jobkit stage` (tools `stage_forms`, `restage`) fills approved forms;
`jobkit open-queue` (tool `open_queue`) reopens filled forms for you. The
nightly run fills every approved, tailored job that hasn't been filled since it
was tailored (see [the nightly run](how-it-works.md#the-nightly-run)).

- **One process.** `stage_forms(ids="", job_ids="")` runs all the jobs in one
  `jobkit stage --job-ids … --keep-open` process. The browser profile admits
  one process, so `stage_forms`, `restage` and `open_queue` refuse to start
  while another filling run is going or a browser holds the profile, and say
  which pid.
- **More than 8 jobs.** At most 8 tabs are open at once; a longer list runs in
  batches of 8. With `--keep-open` (and `open-queue`), the next batch opens
  after you close every tab of the current one. Closing the tabs ends the wait,
  so the browser releases its profile. A job that can't be filled is returned
  as `deferred` with a reason, for example when another browser still holds the
  profile (the reason names the pid to quit).
- **`open-queue`** opens jobs whose latest run is `staged`, or `needs_human`
  with at least one field filled (fully filled first, then cheapest friction),
  skipping jobs already submitted and runs that stopped before filling
  anything. The `open_queue` tool lists the jobs it opens, or says there is
  nothing to open.
- **Outlines.** Each filled field is outlined by where its value came from:
  your answer bank (plain or green), a drafted answer (amber), or a field that
  needs you (red).

## What gets filled

- **Fields are matched again when the form is filled**, against the current
  answer bank, so a bank edit reaches jobs already in the queue. A changed
  match is logged as `stage.rematched`.
- **Identity values go only into identity fields.** Name, email, phone, links
  and address fill a field only when its label is a short field name (at most 4
  words, no "?", matched as a whole label) or the form system's own field id
  (`_systemfield_name`, `first_name`, `urls[GitHub]`, …). A question that
  mentions "email", "country" or "location" goes to the screening answers or to
  you. Screening aliases are checked before identity.
- **Dropdowns, radios and checkboxes** (`src/jobkit/autofill/options.py`).
  Every option is read (in an alphabetical state list, North Dakota is option
  35). A long list that shows only part of itself is filtered by typing the
  answer, never with Enter. The answer must land on one of these, in order: the
  form's decline option, for a decline answer; an exact match; a known
  equivalent ("PA" = "Pennsylvania" = "Pennsylvania (PA)"; "United States" =
  "United States of America" = "USA" = "US" = "U.S."); the bank entry's
  `select_preference`; the option starting with the same Yes or No, only when
  exactly one does or exactly one is that bare word; or a safe partial match. A
  bare "No" among "No, this is my first time applying to this role" and "No, I
  applied to this role more than 6 months ago" is left blank unless
  `select_preference` names the option. A partial match is refused when the
  extra words are a negation, a conflicting number or a different state, or
  when two options fit equally. A state or country answer never gets a partial
  match, so North Dakota never becomes North Carolina. With no safe option the
  field stays blank as `fill-failed`, and `stage.no_safe_option` logs the
  answer and the options it saw.
- **Where you live.** Country- and state-of-residence questions, however they
  are worded ("Please choose the country in which you are located", "Which
  state do you live in?"), use the bank's `country_of_residence` and
  `state_of_residence` entries. A sponsorship, eligibility or relocation
  question that mentions "the country where the job is located" is not a
  residence question.
- **Location pickers.** An autocomplete location field (Ashby's Location, a
  form's "working location" picker) gets the bank's place typed in: "Bristol,
  PA", then "Bristol, Pennsylvania", then the state name. It takes the
  suggestion for the same city in the same state; failing that, once the state
  name is typed, the state itself. A suggestion that names another state, two
  states or another city is never taken. If nothing fits, the box is cleared
  and left to you. A plain text location box is typed into.
- **Links.** GitHub, portfolio and Twitter fields use `identity.github`,
  `identity.portfolio` and `identity.twitter` in `data/answer_bank.yaml`. An
  unset key leaves the field blank and flagged (`not-in-answer-bank:github`);
  the LinkedIn URL is never used instead. "Other website" is always left to
  you.
- **Your answers to open questions.** An answer you give through the
  `answer_novel(n, field_identity, answer)` tool is filled and marked
  `human_typed`, including on fields drafts never fill (a referrer's email, a
  salary expectation for a low-floor track) and over a bank value for that
  field. `answer_novel` refuses EEO and demographic fields (only the `eeo` block
  of the answer bank fills those) and pay-history questions (never answered),
  and names the form's fields when the identity is unknown.
  `get_novel_questions` lists your answers separately from drafts. Filling
  tells the two apart by the `novel_answered` event, so an answer written
  straight into `form_specs.drafted_answers` is treated as a draft. When a rule
  keeps a stored answer off the form, the run's unfilled entry says so
  (`"withheld": "ai_drafted"` or `"human_typed"`) and the log records
  `stage.answer_withheld`.

## What is left for you

- **EEO and voluntary self-identification** is filled only from the `eeo` block
  of `data/answer_bank.yaml`. Its keys are `gender`, `race_ethnicity`,
  `hispanic_latino`, `veteran_status`, `disability_status`,
  `sexual_orientation` and `transgender`. Pronouns are never filled, and
  neither is a question with no key of its own (membership of the LGBTQIA+
  community, or the sex assigned at birth). `transgender` answers only a
  question about being transgender; a gender question that lists it as an
  example ("Gender identity (e.g. woman, man, transgender, non-binary)") takes
  `gender`. When the stored answer declines ("I don't wish to answer"), the
  form's own decline option is chosen, whatever its wording ("Decline to
  self-identify", "Prefer not to say"). Only an option that is purely a decline
  counts: one that states a status and declines only its detail ("I am a
  protected veteran, but I choose not to self-identify the classifications to
  which I belong") is never chosen. Among pure decline options the exact
  wording wins, then the shortest. In a "mark all that apply" group only that
  box is ticked. With no pure decline option, or no `eeo` entry, the field stays
  blank and flagged.
- **Legal acknowledgements.** Arbitration agreements, AI-use policies and
  consent to contact or check references are filled from the bank's
  `legal_acknowledgement` entry, only when that entry exists and the label is
  worded as an acknowledgement: your own statement ("I understand and agree
  …"), an instruction ("Please read the arbitration agreement below"), a
  consent question ("Do you consent to us contacting your references?") or a
  known title ("Agreement to Arbitrate"). A question about you ("Arbitration
  experience") never takes the entry, and neither does a label that tells you
  to read the policy and then asks something else ("Please review our AI
  policy. Will you use AI assistance during your interviews?"): it goes to you
  as `legal-acknowledgement-needs-human:other`. The filler picks the one
  affirmative option, or ticks a single consent checkbox. Without the entry
  these fields are left blank as `legal-acknowledgement-needs-human:covered`.
  Any other acknowledgement or consent (a privacy notice, consent to recording
  an interview, terms and conditions, "Have you read the arbitration
  agreement?") goes to you as `legal-acknowledgement-needs-human:other`. The
  entry's `aliases` can't widen it, and no answer is drafted for one.
- **Contact details** (`never-draft:contact-detail`). Any question that
  mentions email is treated as asking for an address (yours, a referrer's, a
  former employee's) and is never drafted. The exception is a question about
  sales outreach work: it asks about your experience or activity ("Have you
  …", "How many …", "experience") in outreach words ("prospecting",
  "sequences", "outreach", "campaigns", …), every mention of email is a channel
  in a list ("outbound sequences (calls, email, and social)") or before a
  channel word ("email sequences"), and it doesn't also ask how to reach
  someone. Those are answered from the bank or drafted as usual. A field name
  that lists contact channels ("Email / Phone", "LinkedIn or email") is always a
  contact detail.
- **Your record and status** are never drafted, because any answer, a plain
  "No." included, states a fact the corpus doesn't hold. A question the bank
  doesn't answer is left blank and flagged as `never-draft:employment-record`
  (terminated, fired, let go, asked to resign, a performance improvement plan,
  disciplinary action), `never-draft:criminal-record` (convictions, felonies,
  misdemeanours, pending charges), `never-draft:restrictive-covenant`
  (non-compete, non-solicitation) or `never-draft:work-status` (visa
  sponsorship, work authorization, citizenship, green card, H-1B). The bank's
  own `sponsorship`, `non_compete` and `work_auth` entries still answer the
  wordings they match, and an answer you give through `answer_novel` is filled.
- **Salary.** The bank's one `comp_expectation` figure is not used for a job
  whose track has a floor below the default `filters.comp.floor`, or when the
  posting's own maximum is below it; those fields are left blank and flagged.
  An hourly salary expectation is your own wording and sets no floor.
  Pay-history questions are always left blank and flagged.

### Work authorization and sponsorship

`work_auth` ("Are you authorized to work…?", answer usually Yes) and
`sponsorship` ("Will you require sponsorship?", answer usually No) answer with
opposite polarity, so neither answers the other's question, nor a label that
asks both ("authorized to work … and will you require sponsorship?"), unless one
of its aliases is the label word for word. Such a field is left blank as
`work-status-needs-human:<why>`.

- **A match by embedding similarity** (not by an alias) is used only when the
  label is recognised as asking that entry's own question: "need U.S.
  sponsorship", "require a visa", "need a work visa" or "sponsor an immigration
  case" for `sponsorship`; "authorized/eligible to work", "legally able to
  work", "can you legally work", "right to work", or "able to work" / "can you
  work" followed by a country ("in the U.S.") for `work_auth`. "Able to work in
  our NYC office", "in US time zones" and "can you work weekends" are not
  work-status questions. Any other wording ("Are you a U.S. citizen?", "Visa
  status") is left blank as
  `work-status-needs-human:embedding-not-an-authorization-question` or
  `…embedding-not-a-sponsorship-question`.
- **A sponsorship question asked in reverse** ("Can you work in the US without
  visa sponsorship?") is answered by neither entry, because `sponsorship`'s "No"
  would say you can't work without it. It is left blank as
  `work-status-needs-human:asks-without-sponsorship` (or
  `…asks-authorization-and-sponsorship` when it also asks about working there),
  unless an alias is the label word for word.
- **No other bank entry** answers a sponsorship or authorization question ("Are
  you at least 18 and will you require sponsorship?" doesn't take `age_18`'s
  "Yes"), and none answers a work-status question by embedding ("Are you a U.S.
  citizen?" doesn't take `based_in_us`'s "Yes"):
  `work-status-needs-human:work-status-question-took-<entry>`.
- **Any label that mentions sponsor or sponsorship, a visa, an H-1B or
  immigration** is answered only by the `sponsorship` entry, and only when the
  label is recognised as asking whether you need sponsorship
  (`work_status_refusal` in `autofill/answer_bank.py`), or by a bank entry one
  of whose aliases is the label word for word. Anything else (another entry's
  alias inside the label, an embedding match, an identity or EEO value, a
  draft) leaves it blank, flagged `sponsorship-needs-human:<why>`. `work_auth`
  never answers a label asking about sponsorship or a visa, and `sponsorship`
  never answers one asking whether you are authorized to work, not even through
  a word-for-word alias (`…:sponsorship-question-took-work_auth`,
  `…:authorization-question-took-sponsorship`). This also catches sales
  vocabulary and the company Visa ("Do your deals require executive
  sponsorship?", "Why Visa?"): those go to you.
- **A label asking whether you require work authorization or a work permit**
  ("Do you require work authorization to be employed in the United States?",
  "Will you require a work permit …?") asks the sponsorship question in other
  words: `work_auth` never answers it
  (`…:requires-authorization-question-took-work_auth`), and `sponsorship`
  answers it only by a word-for-word alias, or when the label also asks about
  sponsorship (`…:asks-whether-work-authorization-is-required`).
- **A negation** within three words of the work-status words ("Can you confirm
  you will not require sponsorship?", "We do not sponsor visas. Will you …?")
  means neither entry answers unless an alias is the label word for word
  (`…:negated-work-status-question`), and an embedding match doesn't answer a
  work-status label with a negation anywhere in it
  (`…:embedding-negated-work-status-question`).
- To have a wording filled, add it as an alias to the right entry. For example,
  "Do you now, or will you ever, require employment sponsorship …?" belongs in
  `sponsorship`'s aliases. A blank work-status field you answer through
  `answer_novel` is filled. Every stored field is matched again at filling time,
  so these rules also apply to form specs captured earlier.

## How it fills

- **Uploads go first, named for the recruiter.** The resume and cover letter
  are uploaded before anything is typed, as copies named
  `First_Last_Resume.pdf` and `First_Last_Cover_Letter.pdf` from the answer
  bank's `identity.first_name` and `last_name` (accents dropped, other
  characters made `_`). The copies live in `out/uploads/job-<id>/`, because the
  browser reads an uploaded file again when you send the form; the tailored
  `resume.pdf` and `cover.pdf` are not changed. With no first or last name in
  the bank, the original file is uploaded and `stage.upload_name_unavailable` is
  logged. A file goes only into a real file input: the one with the form
  system's field id (`resume`, `_systemfield_resume`), else the one input on the
  form known as a resume (or a cover letter) by its own label, name or id, then
  by its field's text. An employer-hosted copy of a Greenhouse form can name its
  inputs its own way ("Resume", not "Resume/CV"). Two inputs claiming the same
  file get nothing. Every failure is logged as `stage.attach_failed` and
  recorded in the run's unfilled entry with its cause (`no-file`,
  `no-file-input-for-resume`, `2-file-inputs-claim-resume`, `upload-failed:…`).
  Greenhouse's "or paste it" box (`resume_text`, `cover_letter_text`) is never
  typed into; it counts as answered once its file is uploaded.
- **Waiting for the resume parser.** Some forms read the uploaded resume and
  write their own reading into name, email and phone. After an upload, the
  filler waits until the form's values stop changing (1.5 s with no change
  after a change, or 3 s with none at all), at most 8 s, then fills. Logged as
  `stage.parser_wait`.
- **Read-back.** After the fill, every text, select, radio, checkbox, dropdown,
  location and Yes/No value is read back from the page and compared with what
  was put in. Reformatting the page does on its own doesn't count: case and
  spacing, phone punctuation and country code, a URL's `https://`, `www.` and
  trailing slash. A field showing something else is filled once more; if it
  still differs it leaves the filled list as `overwritten-after-fill` (with what
  was filled and what the form shows), is outlined red, and the run is
  `needs_human` whether or not the field is required. A widget that can't be
  read, or shows nothing at all, is counted as unverified, not as a mismatch. A
  custom dropdown or location box is read from what the widget itself shows as
  its value (its input's value, the element showing the chosen option, a
  button-style control's own text), never from the text around it, and the
  value must be the answer (a whole line of it), not contain it. A placeholder
  shown in place of a value is a mismatch. Each run writes a `readback` event
  (the run id, fields checked, changed, put right by the refill, still changed,
  unverified); `jobkit stage` prints the count, and `get_job` shows the latest
  run and any field the form changed.
- **The right box.** When a field has no id or name the filler can use, its
  control is found from its label in the page (`question_control` in
  `autofill/browser.py`, over a read-only tree of the page): the control a
  `<label for>` names or a `<label>` wraps, the control whose
  `aria-labelledby` names the label, or else the nearest element above the
  label text that holds a control, which must hold one question's controls (one
  control, or one radio or checkbox group).
  - A wrapper holding two questions' controls is never narrowed to its first
    one, a control with its own label of other text is another question's, and
    a label found in two places leading to two controls is ambiguous: the field
    is left blank as `control-ambiguous:<why>` (log `stage.control_ambiguous`).
  - The nearest element holding a control must hold nothing else of another
    question: no shown text with letters outside the label text, the control
    and its own labels, and no other widget (a button, a `role=button`
    dropdown, an editable box). Otherwise it is
    `control-ambiguous:container-holds-several-questions`.
  - A yes/no or pick-one answer is never typed into a text box (log
    `stage.choice_not_typed`): a boolean goes on to the Yes/No buttons,
    anything else is left blank.
  - A label lookup that matches several controls of more than one question
    ("Location" inside "Preferred work location") is decided by the exact
    label, or refused the same way.
  - Ashby's Yes/No buttons are found the same way. A Yes/No answer is clicked
    only when that container holds exactly one button (or radio) with the
    answer's name, and an in-page check finds it and the opposite answer form
    one question's pair: the nearest element holding both must lead to the
    label without passing another question's text, and the question may hold no
    other Yes/No pair and no other shown field. Anything less clear clicks
    nothing, logs `stage.yes_no_unclear` with the reason, and the field goes to
    you as `fill-failed`.
- **No form, no run.** The filler waits up to 15 seconds for the form,
  including a form inside an embedded iframe (a Greenhouse board inside an
  employer's careers page). If none appears, the job stops as
  `posting-not-found`, with the signal (`http-404`, `page-says:…`,
  `no-captured-field-rendered`) and the final URL in the log. An Ashby posting
  URL is sent to its `/application` tab.

## When it stops

- **Clicks never land on a send control.** Besides the static scan of
  `src/jobkit/autofill/`, every click or check the filler makes (dropdowns,
  options, radios, checkboxes, location suggestions, Ashby Yes/No buttons) is
  preceded by an in-page probe of that element. It is refused when it is a
  submit or image control, sits inside one, is a `<button>` with no type inside
  the form (which would send it), or has submit, apply or send in its own text.
  A refusal is logged as `stage.click_guard_refused` and the field goes to you
  as `fill-failed`; nothing is clicked. `tests/test_click_guard.py` reads the
  code and fails if any click, check or script click in `src/jobkit/autofill/`
  is not preceded by this probe on the same element in the same function.
- **CAPTCHA.** Filling stops for a challenge that is shown: a visible reCAPTCHA
  checkbox or challenge frame, hCaptcha, or Turnstile. The invisible-reCAPTCHA
  badge in the corner doesn't stop it. That is one reason you click submit
  yourself. The widget that triggered a stop is logged as
  `stage.captcha_detected`. Nothing interacts with a challenge.
- **A CAPTCHA the fill brings up.** Some forms show a challenge only after a
  scripted click (Lever shows hCaptcha after checkbox and radio clicks). So the
  check runs again after every click the filler makes, after any fill that
  failed with an error (Playwright's check() clicks first and then fails with
  "Clicking the checkbox did not change its state" when a challenge took the
  click), once before the read-back, and once more when the fill is done. If a
  challenge appears, filling stops at once with
  `captcha-provoked-by-fill:<field>` (the field whose click brought it up, or
  the last field filled when it appeared at the end), takes a screenshot, logs
  `stage.captcha_provoked` with the field's label and the widget, and doesn't
  touch the challenge. The job is listed with the other stops in the digest,
  `get_queue` and `schedule status`, and the nightly run doesn't retry it.
- **Other stops.** A government-ID or payment field, an embedded assessment, no
  form on the page, no captured form spec, and an apply link off the allowlist
  stop the job as `needs_human` with the reason. How the nightly run reports
  and retries them is in [the nightly run](how-it-works.md#the-nightly-run).

## Dead postings

`jobkit form-specs` (and the nightly `form-specs` stages, tool `run_nightly`)
fetches each queued or approved job's form from its system. When the system
says the posting doesn't exist (Greenhouse's per-job API answers 404 or 410;
Lever's apply page fails and its postings API answers 404 or
`{"ok":false,"error":"Document not found"}`; Ashby's GraphQL answers
`jobPosting: null` and the board's public job list,
`api.ashbyhq.com/posting-api/job-board/<token>`, answers without the posting),
the job is closed (status `closed`, `closed_at`, event `posting_gone` with the
signal, log `form_spec.posting_gone`) and gets no form spec, so it is never
tailored or filled. Any other failure (5xx, 403, a timeout, a page that won't
parse) keeps a `dom_scrape` placeholder, and the job goes on. Ashby's form
endpoint is one GraphQL POST shared by every posting, so its HTTP status never
closes a job. `jobkit form-specs` prints `posting_gone=N`; `get_job` says why a
job closed. Greenhouse's `location_questions` (a required Location box some
employers add) are captured too, without their hidden latitude and longitude
fields.
