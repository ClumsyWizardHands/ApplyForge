"""Common English words: the claims validator's one list for telling a word
capitalized only because it starts a sentence from a name (validate/claims.py).
Without it, a letter whose sentence opened with an ordinary word ("Success
…", "People …", "Pick …") was rejected: the first word was read as an
unsourced proper noun.

The list itself is common_words.txt next to this module, one lowercase word
per line ("#" starts a comment line); edit it there and nowhere else. It ships
inside the package, so no dependency is needed (SPEC §10 lists none for it)."""

from __future__ import annotations

from functools import cache
from importlib.resources import files

# inflections tried when the word itself is not listed: (suffix, replacement)
_INFLECTIONS = (
    ("ies", "y"),
    ("ied", "y"),
    ("es", ""),
    ("s", ""),
    ("ed", ""),
    ("ed", "e"),
    ("d", ""),
    ("ing", ""),
    ("ing", "e"),
    ("ly", ""),
)


@cache
def common_words() -> frozenset[str]:
    """Every word on the bundled list, lowercase."""
    text = files("jobkit.validate").joinpath("common_words.txt").read_text(encoding="utf-8")
    return frozenset(
        w for w in (line.strip() for line in text.splitlines()) if w and not w.startswith("#")
    )


def is_common_word(word: str) -> bool:
    """True when `word` (any case) is a common English word: on the list
    itself, or an inflection of a listed word ("Calls", "Climbed",
    "Booking"), or a contraction of one ("Isn't", "Don't", "Can't"). A
    possessive is the caller's to strip."""
    w = word.lower().replace("’", "'")
    if "'" in w:
        head = w.split("'", 1)[0]
        # "isn't" → "is", "don't" → "do", "can't" → "can", "we're" → "we"
        return is_common_word(head) or (head.endswith("n") and is_common_word(head[:-1]))
    if not w.isalpha():
        return False
    words = common_words()
    if w in words:
        return True
    for suffix, repl in _INFLECTIONS:
        if not w.endswith(suffix) or len(w) - len(suffix) < 3:
            continue
        base = w[: -len(suffix)] + repl
        # "planned" → "plan", "running" → "run"
        undoubled = base[:-1] if len(base) >= 4 and base[-1] == base[-2] and not repl else ""
        if base in words or (undoubled and undoubled in words):
            return True
    return False
