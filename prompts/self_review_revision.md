---
version: "1.5"
stage: self_review
# Not sent on its own. pipeline/self_review.py appends this block to the
# WRITER's own prompt for the one revision call a self-review asks for. Only a
# resume bullet selection that passed its checks is ever revised (stage
# tailor, whose model tier applies); a cover letter or drafted answer is
# reviewed and reported only. It belongs to the self-review step, so it names
# that stage and carries no model_tier. Its text is part of the writer call's
# rendered prompt: an edit misses the cache.
---


SELF-REVIEW OF YOUR PREVIOUS DRAFT. You reviewed the draft below against this
job and the candidate's corpus, and decided it should change. Write a complete
new version, in exactly the JSON format asked for above, making these changes:
{changes}

Every rule above still applies, and one more replaces "light trim": in this
new version every "rendered" is the chosen variant's corpus text copied word
for word, exactly as its long or short line above has it, for every bullet,
the ones you keep included (even where the draft below trimmed one). You may
add, drop or reorder bullets, switch a bullet between its long and short
variant, and change the positioning and the skills line. You may not trim,
merge, extend or reword a bullet. Skip any change that would need a fact,
number, title, tool, team size or result the corpus does not state, or a
bullet worded any other way. The new version is machine-checked: one bullet
that is not its variant's text word for word discards the whole new version.

YOUR PREVIOUS DRAFT:
{draft}
