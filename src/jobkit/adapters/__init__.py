"""Adapter registry. Adding an ATS is one file implementing BoardAdapter."""

from __future__ import annotations

from jobkit.adapters.ats.ashby import AshbyAdapter
from jobkit.adapters.ats.base import BoardAdapter
from jobkit.adapters.ats.greenhouse import GreenhouseAdapter
from jobkit.adapters.ats.lever import LeverAdapter

ADAPTERS: dict[str, BoardAdapter] = {
    a.ats_name: a for a in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter())
}
