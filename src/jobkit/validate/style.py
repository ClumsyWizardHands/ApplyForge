"""The `estimate_style` knob: how a figure's approximation mark is rendered.

One knob, one owner: config/banned_phrases.yaml `drift.estimate_style`. It
replaced `drift.enforce_hedging`, which no code read (2026-10-08). The
shipped template sets "hedged"; the setup interview asks which one you want.

- "hedged": an `estimate`-confidence number keeps a hedge right next to it
  ("~30%", "up to 15", "10+", a range), and a number its source only ever
  states hedged keeps that hedge. The behavior before 2026-10-08.
- "confident": an approximation mark ("~", "about", "approximately",
  "roughly", "around") may be dropped or kept. Everything else about the figure
  must match its source exactly: same value, same unit, no added precision, a
  range stays the same range, and "+", "up to", "under", "over", "or more"
  stay where the source has them (and are not added where it does not).

In both styles a rate keeps its source's period ("$2M/yr" is never "$2M/month",
"5–7 clients/month" never "5–7 clients"; provenance.period_mismatch).

`metric_confidence` in the corpus is unchanged either way.

Read by the provenance validator (resume bullets), the claims validator
(cover letters and drafted answers), the authenticity review (pass 3) and the
tailoring prompts. A missing key means "hedged" (the stricter rule); an
unknown value, or the retired `enforce_hedging` key, raises."""

from __future__ import annotations

from typing import Any

from jobkit.config import read_config_yaml

CONFIG_PATH = "config/banned_phrases.yaml"

CONFIDENT = "confident"
HEDGED = "hedged"
ESTIMATE_STYLES = (CONFIDENT, HEDGED)


def estimate_style_from(cfg: dict[str, Any] | None) -> str:
    """The style a loaded banned_phrases config sets (missing → "hedged")."""
    drift = (cfg or {}).get("drift") or {}
    if not isinstance(drift, dict):
        raise ValueError("banned_phrases.yaml: drift must be a mapping")
    if "enforce_hedging" in drift:
        raise ValueError(
            "banned_phrases.yaml: drift.enforce_hedging was replaced by drift.estimate_style "
            f"(one of {ESTIMATE_STYLES}); remove it"
        )
    return check_style(drift.get("estimate_style", HEDGED))


def check_style(style: object) -> str:
    """`style` if it is a known estimate_style; otherwise raise (fail loudly)."""
    s = str(style).strip().lower()
    if s not in ESTIMATE_STYLES:
        raise ValueError(f"estimate_style must be one of {ESTIMATE_STYLES}, got {style!r}")
    return s


def configured_estimate_style() -> str:
    """The style set in config/banned_phrases.yaml, read now. A missing file
    is a ConfigMissing error (config.read_config_yaml)."""
    cfg = read_config_yaml(CONFIG_PATH)
    return estimate_style_from(cfg if isinstance(cfg, dict) else {})


def resolve(style: str | None) -> str:
    """An explicit style, checked; None means the configured one."""
    return configured_estimate_style() if style is None else check_style(style)
