"""`jobkit doctor` (CLI) and MCP `doctor`: is this checkout ready to run?

Read-only. Each check reports ok or a problem, and every problem comes with
the exact fix. Nothing is installed, created, moved or changed here.

Checks, in order:

* Python 3.12 and `uv` on PATH;
* Playwright's Chromium installed for the installed Playwright version
  (staging opens a headed Chromium);
* an LLM backend: ANTHROPIC_API_KEY, or the `claude` CLI on PATH (the
  fallback that runs on a Claude subscription);
* the checkout is not under ~/Desktop, ~/Documents, ~/Downloads or iCloud
  Drive (iCloud sets the macOS hidden flag on files, Python then skips the
  hidden .pth file and `import jobkit` fails; launchd cannot read there
  either), and no .pth file in the venv carries the hidden flag;
* every personal config file (config.PERSONAL_CONFIG_FILES) is present and
  loads. A missing one is fixed by the interview or its template, never from
  git history: history holds another person's config, and doctor cannot tell
  a checkout's own old copy from someone else's;
* every allowed place has a commute-table entry (a warning only: a hybrid
  posting in a place without one is rejected as sanity:commute:unknown);
* the scoring prompt set: a warning when `prompt_set` is not set and no
  track is a sales seat (jobs would be scored against sales examples);
* how many days without a reply mark an application ghosted, and which
  setting gave that number (information, never a warning);
* role tracks still holding the template placeholder (a warning);
* standing rules whose `title_class` names no track (a warning: such a rule
  never matches);
* an enabled keyed feed (Adzuna, USAJOBS) without its keys, and LinkedIn
  discovery left on the built-in sales keywords for a search with no sales
  track (warnings);
* the career corpus and answer bank exist (data/, gitignored);
* the database is initialized (`jobkit init`);
* the MCP server is registered for this checkout (.mcp.json);
* a contact address for the polite User-Agent (a warning only).

The setup interview (say "set up jobkit" in Claude) writes the config and data
files; doctor is how it, and you, confirm the result."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat as statmod
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobkit import config, contact, paths
from jobkit import db as q
from jobkit.normalize import location_bucket, names_no_specific_place
from jobkit.schedule import PROTECTED_DIRS

INTERVIEW = 'run the jobkit setup interview (say "set up jobkit" in Claude)'
DATA_FILES = (
    ("career_corpus.yaml", "your career corpus: every fact a resume or letter may use"),
    ("answer_bank.yaml", "your answer bank: the questions every application form asks"),
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True  # False: a warning, never a failure

    def render(self) -> list[str]:
        mark = "ok  " if self.ok else ("FAIL" if self.required else "warn")
        lines = [f"[{mark}] {self.name}: {self.detail}"]
        if not self.ok and self.fix:
            first, *rest = self.fix.splitlines()
            lines += [f"       fix: {first}", *(f"            {line}" for line in rest)]
        return lines


@dataclass
class Report:
    root: Path
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok or not c.required for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.required]

    def render(self) -> str:
        lines = [f"jobkit doctor — checkout {self.root}"]
        for c in self.checks:
            lines += c.render()
        failed = len(self.failures)
        warned = sum(1 for c in self.checks if not c.ok and not c.required)
        if failed:
            lines.append(
                f"\n{failed} problem(s) to fix" + (f", {warned} warning(s)." if warned else ".")
            )
        else:
            lines.append("\nReady." + (f" {warned} warning(s) above." if warned else ""))
        return "\n".join(lines)


@dataclass(frozen=True)
class Env:
    """What the checks read from the machine; the tests replace it."""

    root: Path
    home: Path
    platform: str
    environ: Mapping[str, str]
    which: Callable[[str], str | None]
    python: tuple[int, int]
    site_packages: Path | None


def _site_packages() -> Path | None:
    v = f"python{sys.version_info.major}.{sys.version_info.minor}"
    sp = Path(sys.prefix) / "lib" / v / "site-packages"
    return sp if sp.is_dir() else None


def current_env(root: Path | None = None) -> Env:
    return Env(
        root=(root or paths.resolve_home()).resolve(),
        home=Path.home(),
        platform=sys.platform,
        environ=os.environ,
        which=shutil.which,
        python=(sys.version_info.major, sys.version_info.minor),
        site_packages=_site_packages(),
    )


# ---- toolchain ----


def check_python(env: Env) -> Check:
    have = f"{env.python[0]}.{env.python[1]}"
    if env.python == (3, 12):
        return Check("python", True, f"Python {have}")
    return Check(
        "python",
        False,
        f"Python {have}; jobkit needs 3.12",
        "uv python install 3.12 && uv sync   (uv picks 3.12 from pyproject.toml)",
    )


def check_uv(env: Env) -> Check:
    found = env.which("uv")
    if found:
        return Check("uv", True, found)
    return Check(
        "uv",
        False,
        "`uv` is not on PATH",
        "curl -LsSf https://astral.sh/uv/install.sh | sh   (or: brew install uv), "
        "then open a new terminal",
    )


def _playwright_browsers_dir(env: Env) -> Path | None:
    """Where Playwright keeps its browsers (PLAYWRIGHT_BROWSERS_PATH, else the
    per-platform default cache)."""
    custom = env.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if custom == "0":
        if env.site_packages is None:
            return None
        return env.site_packages / "playwright" / "driver" / "package" / ".local-browsers"
    if custom:
        return Path(custom).expanduser()
    if env.platform == "darwin":
        return env.home / "Library" / "Caches" / "ms-playwright"
    if env.platform.startswith("win"):
        local = env.environ.get("LOCALAPPDATA") or str(env.home / "AppData" / "Local")
        return Path(local) / "ms-playwright"
    xdg = env.environ.get("XDG_CACHE_HOME") or str(env.home / ".cache")
    return Path(xdg) / "ms-playwright"


def _chromium_revisions(env: Env) -> set[str]:
    """The Chromium revisions the installed Playwright accepts (its
    browsers.json); empty when that cannot be read."""
    if env.site_packages is None:
        return set()
    spec = env.site_packages / "playwright" / "driver" / "package" / "browsers.json"
    try:
        browsers = json.loads(spec.read_text()).get("browsers", [])
    except (OSError, ValueError):
        return set()
    out: set[str] = set()
    for b in browsers:
        if isinstance(b, dict) and b.get("name") == "chromium":
            out.add(str(b.get("revision", "")))
            out.update(str(v) for v in (b.get("revisionOverrides") or {}).values())
    return {r for r in out if r}


def check_playwright(env: Env) -> Check:
    fix = "uv run playwright install chromium"
    where = _playwright_browsers_dir(env)
    if where is None or not where.is_dir():
        return Check("playwright chromium", False, "no Playwright browsers installed", fix)
    installed = sorted(p.name for p in where.glob("chromium-*") if p.is_dir())
    if not installed:
        return Check("playwright chromium", False, f"no Chromium under {where}", fix)
    wanted = _chromium_revisions(env)
    if wanted and not any(f"chromium-{r}" in installed for r in wanted):
        return Check(
            "playwright chromium",
            False,
            f"Chromium under {where} ({', '.join(installed)}) is not the revision this "
            f"Playwright needs (chromium-{'/'.join(sorted(wanted))})",
            fix,
        )
    return Check("playwright chromium", True, f"{installed[-1]} under {where}")


def check_llm(env: Env) -> Check:
    if env.environ.get("ANTHROPIC_API_KEY", "").strip():
        return Check("llm backend", True, "ANTHROPIC_API_KEY is set (Anthropic SDK)")
    found = env.which("claude")
    if found:
        return Check("llm backend", True, f"`claude` CLI at {found} (your Claude subscription)")
    return Check(
        "llm backend",
        False,
        "no ANTHROPIC_API_KEY and no `claude` CLI on PATH: every LLM stage would fail",
        "install Claude Code (https://docs.anthropic.com/en/docs/claude-code) and log in, "
        "so `claude` is on PATH\n"
        "or put ANTHROPIC_API_KEY=... in .env (see .env.example)",
    )


# ---- where the checkout lives ----


def protected_location(path: Path, home: Path) -> str | None:
    """The synced/protected folder `path` resolves into, or None."""
    resolved = path.resolve()
    for name in PROTECTED_DIRS:
        base = home / name
        for candidate in {base, base.resolve()}:
            if resolved.is_relative_to(candidate):
                return str(base)
    return None


def check_location(env: Env) -> Check:
    where = protected_location(env.root, env.home)
    if where is None:
        return Check("checkout location", True, f"{env.root} (not in a synced folder)")
    return Check(
        "checkout location",
        False,
        f"{env.root} is under {where}. iCloud sets the macOS hidden flag on files there; "
        "Python skips a hidden .pth file, so `import jobkit` fails, and launchd cannot "
        "read the folder for the nightly run",
        f"mkdir -p ~/code && mv {shlex.quote(str(env.root))} ~/code/jobkit && cd ~/code/jobkit && "
        "chflags -R nohidden . && uv sync",
    )


def check_hidden_pth(env: Env) -> Check | None:
    """macOS only: a hidden .pth file in the venv breaks `import jobkit`."""
    if env.platform != "darwin" or env.site_packages is None:
        return None
    hidden_flag = getattr(statmod, "UF_HIDDEN", 0)
    hidden = []
    for pth in env.site_packages.glob("*.pth"):
        try:
            if getattr(os.lstat(pth), "st_flags", 0) & hidden_flag:
                hidden.append(pth.name)
        except OSError:
            continue
    if not hidden:
        return Check("venv .pth files", True, "none hidden")
    return Check(
        "venv .pth files",
        False,
        f"hidden: {', '.join(sorted(hidden))} — Python skips them, so imports fail",
        f"cd {shlex.quote(str(env.root))} && chflags -R nohidden . && uv sync",
    )


# ---- personal config and data ----


def _config_problem(name: str, path: Path) -> str | None:
    """Why a present config file would not load, or None."""
    try:
        if name == "filters.yaml":
            config.load_filters(path)
        elif name == "targets.yaml":
            config.load_targets(path)
        else:
            data = config.read_config_yaml(path)
            if not isinstance(data, dict):
                return "is empty or not a YAML mapping"
    except yaml.YAMLError as e:
        return f"does not parse: {' '.join(str(e).split())[:160]}"
    except Exception as e:  # noqa: BLE001 — validation errors of any kind
        return f"does not load: {' '.join(str(e).split())[:200]}"
    return None


def check_config_files(env: Env) -> list[Check]:
    out: list[Check] = []
    for name in config.PERSONAL_CONFIG_FILES:
        rel = f"config/{name}"
        path = env.root / rel
        stem = name.removesuffix(".yaml")
        if not path.is_file():
            out.append(
                Check(
                    rel,
                    False,
                    "not found",
                    f"{INTERVIEW}\nor: cp config/{stem}.example.yaml {rel}  (then edit it)",
                )
            )
            continue
        problem = _config_problem(name, path)
        if problem:
            out.append(
                Check(rel, False, problem, f"edit {rel}; config/{stem}.example.yaml documents it")
            )
        else:
            out.append(Check(rel, True, "present, loads"))
    return out


def check_commute_coverage(env: Env) -> Check | None:
    """Every place in `filters.locations.allowed` (not a relocation city) has
    a commute time. Without one, the remote check rejects a hybrid posting
    there as `sanity:commute:unknown`. A warning: the config may mean it.
    Skipped while either file is missing or broken (reported above)."""
    from jobkit.pipeline.triage import commute_minutes

    filters_path = env.root / "config" / "filters.yaml"
    table_path = env.root / "config" / "commute_table.yaml"
    if not (filters_path.is_file() and table_path.is_file()):
        return None
    try:
        filters = config.load_filters(filters_path)
        table = config.read_config_yaml(table_path) or {}
        relocation = filters.relocation_buckets
        places = filters.allowed_locations
    except Exception:  # noqa: BLE001 — check_config_files reports it
        return None
    if not isinstance(table, dict):
        return None
    missing: dict[str, list[str]] = {}
    for place in places:
        bucket = location_bucket(place, False)
        if (
            bucket.startswith("remote-")
            or bucket in relocation
            or names_no_specific_place(place)
            or commute_minutes(table, bucket) is not None
        ):
            continue
        missing.setdefault(bucket, []).append(place)
    if not missing:
        return Check("commute table", True, "every allowed place has a commute time")
    named = "; ".join(f"{', '.join(ps)} ({b})" for b, ps in sorted(missing.items()))
    return Check(
        "commute table",
        False,
        f"no commute time for allowed place(s): {named}. A hybrid posting there "
        "is rejected as sanity:commute:unknown",
        "add under `buckets:` in config/commute_table.yaml, with your door-to-door minutes:\n"
        + "\n".join(f"  {b}: <minutes>" for b in sorted(missing))
        + "\nor drop the place from filters.locations.allowed in config/filters.yaml",
        required=False,
    )


# ---- targeting: prompt set, tracks, standing rules, feeds ----

PLACEHOLDER_TRACK_ID = "my_role"
PLACEHOLDER_REGEX_TEXT = "REPLACE ME"
FEED_KEYS = {
    "adzuna": ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"),
    "usajobs": ("USAJOBS_API_KEY", "USAJOBS_USER_AGENT_EMAIL"),
}


def _filters(env: Env) -> config.Filters | None:
    """The checkout's filters.yaml, or None when it is missing or does not
    load (check_config_files reports that)."""
    path = env.root / "config" / "filters.yaml"
    if not path.is_file():
        return None
    try:
        return config.load_filters(path)
    except Exception:  # noqa: BLE001 — check_config_files reports it
        return None


def check_prompt_set(env: Env) -> Check | None:
    """Which scoring prompts score jobs. No warning for a sales search without
    prompt_set: that is the original setup and its scores must not move."""
    filters = _filters(env)
    if filters is None:
        return None
    steers = [t.id for t in filters.tracks if t.pivot or t.emphasis]
    if filters.prompt_set == "sales" and steers:
        return Check(
            "prompt set",
            False,
            f"track(s) {', '.join(steers)} set pivot or emphasis, but the sales prompts "
            "ignore both; only prompt_set: general reads them",
            "put this line at the top of config/filters.yaml:\nprompt_set: general\n"
            "then run `uv run jobkit rescore` so queued jobs are scored again",
            required=False,
        )
    if filters.prompt_set_explicit:
        return Check("prompt set", True, f"prompt_set: {filters.prompt_set}")
    if filters.has_sales_track:
        return Check(
            "prompt set", True, "sales (prompt_set is not set; your tracks are sales roles)"
        )
    return Check(
        "prompt set",
        False,
        "prompt_set is not set, so jobs are scored against examples from sales careers",
        "add this line at the top of config/filters.yaml:\nprompt_set: general\n"
        "then run `uv run jobkit rescore` so queued jobs are scored again with it",
        required=False,
    )


def check_ghost_after(env: Env) -> Check | None:
    """Information only: the days without a reply before a submitted
    application is marked ghosted, and where the number comes from."""
    filters = _filters(env)
    if filters is None:
        return None
    ghost = filters.ghost_after
    where = {
        "tracking": "tracking.ghost_after_days",
        "followups": "the close_as_ghosted entry of the followups block in config/filters.yaml",
        "default": "the default",
    }[ghost.source]
    return Check(
        "no-reply rule",
        True,
        f"a submitted application with no reply after {ghost.days} days is marked "
        f"ghosted ({where})",
    )


def check_tracks(env: Env) -> Check | None:
    """A track still holding the template placeholder matches no real title."""
    filters = _filters(env)
    if filters is None:
        return None
    placeholders = [
        t.id
        for t in filters.tracks
        if t.id == PLACEHOLDER_TRACK_ID or PLACEHOLDER_REGEX_TEXT in t.title_regex
    ]
    if not placeholders:
        return Check("role tracks", True, ", ".join(filters.track_ids) or "none defined")
    return Check(
        "role tracks",
        False,
        f"track(s) {', '.join(placeholders)} still hold the template placeholder",
        f"{INTERVIEW}\nor edit filters.tracks in config/filters.yaml: give each track an id "
        "and a title_regex for the titles you want",
        required=False,
    )


def check_title_classes(env: Env) -> Check | None:
    """Every standing rule's title_class names a track id."""
    from jobkit.rules import title_classes_in, unknown_title_classes

    filters = _filters(env)
    path = env.root / "config" / "autoapprove.yaml"
    if filters is None or not path.is_file():
        return None
    try:
        rules = config.read_config_yaml(path)
    except Exception:  # noqa: BLE001 — check_config_files reports it
        return None
    if not isinstance(rules, dict):
        return None
    unknown = unknown_title_classes(rules, filters)
    if not unknown:
        named = title_classes_in(rules)
        return Check(
            "standing rules",
            True,
            "title_class values name tracks: " + ", ".join(named) if named else "no title_class",
        )
    return Check(
        "standing rules",
        False,
        f"title_class {', '.join(unknown)} in config/autoapprove.yaml names no track, so "
        "those rules never match",
        f"use a track id from config/filters.yaml ({', '.join(filters.track_ids) or 'none'}) "
        "or remove the rule",
        required=False,
    )


def check_feeds(env: Env) -> list[Check]:
    """Enabled keyed feeds have their keys; LinkedIn discovery is not left on
    the built-in sales keywords for a search that has no sales track."""
    filters = _filters(env)
    if filters is None:
        return []
    feeds = filters.raw.get("feeds") or {}
    if not isinstance(feeds, dict):
        return []
    out: list[Check] = []
    for feed, keys in FEED_KEYS.items():
        cfg = feeds.get(feed)
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            continue
        missing = [k for k in keys if not str(env.environ.get(k, "")).strip()]
        if missing:
            out.append(
                Check(
                    f"{feed} feed",
                    False,
                    f"enabled, but {' and '.join(missing)} not set: the feed is skipped",
                    "put " + " and ".join(f"{k}=..." for k in missing) + " in .env "
                    f"(see .env.example), or set feeds.{feed}.enabled: false",
                    required=False,
                )
            )
        else:
            out.append(Check(f"{feed} feed", True, "enabled, keys set"))
    li = feeds.get("linkedin_guest")
    if (
        isinstance(li, dict)
        and li.get("enabled") is True
        and "keywords" not in li
        and not filters.has_sales_track
    ):
        out.append(
            Check(
                "linkedin discovery",
                False,
                "enabled with no keywords, so it searches the built-in sales keywords",
                "add feeds.linkedin_guest.keywords to config/filters.yaml: up to "
                f"{config.LINKEDIN_MAX_KEYWORDS} job titles you want",
                required=False,
            )
        )
    return out


def check_data_files(env: Env) -> list[Check]:
    out: list[Check] = []
    for name, what in DATA_FILES:
        rel = f"data/{name}"
        if (env.root / rel).is_file():
            out.append(Check(rel, True, "present"))
            continue
        out.append(
            Check(
                rel,
                False,
                f"not found ({what})",
                f"{INTERVIEW}: it builds this from your resume, and you confirm every entry",
            )
        )
    return out


def check_db(env: Env) -> Check:
    path = Path(q.DB_PATH)
    if path.is_file():
        return Check("database", True, str(path))
    return Check("database", False, f"no database at {path}", "uv run jobkit init")


def check_mcp(env: Env) -> Check:
    path = env.root / ".mcp.json"
    server = {
        "mcpServers": {
            "jobkit": {
                "command": "uv",
                "args": ["run", "--directory", str(env.root), "jobkit-mcp"],
            }
        }
    }
    fix = (
        f"write {path} with:\n{json.dumps(server)}\n"
        f"or: claude mcp add --scope project jobkit -- uv run --directory {env.root} jobkit-mcp"
    )
    if not path.is_file():
        return Check("mcp server", False, ".mcp.json not found: Claude cannot see jobkit", fix)
    try:
        cfg: Any = json.loads(path.read_text())
        entry = cfg["mcpServers"]["jobkit"]
        args = [str(a) for a in entry.get("args", [])]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return Check("mcp server", False, ".mcp.json has no mcpServers.jobkit entry", fix)
    if "--directory" not in args or args.index("--directory") + 1 >= len(args):
        return Check("mcp server", False, ".mcp.json jobkit entry has no --directory", fix)
    target = Path(args[args.index("--directory") + 1]).expanduser()
    try:
        same = target.resolve() == env.root
    except OSError:
        same = False
    if not same:
        return Check(
            "mcp server",
            False,
            f".mcp.json points jobkit at {target}, not this checkout",
            fix,
        )
    return Check("mcp server", True, f".mcp.json runs jobkit-mcp from {env.root}")


def check_contact(env: Env) -> Check:
    email = contact.contact_email(env.environ)
    if email:
        return Check("contact email", True, f"User-Agent names {email}", required=False)
    return Check(
        "contact email",
        False,
        "job boards are polled with a generic User-Agent (no contact address)",
        "put JOBKIT_CONTACT_EMAIL=you@yourdomain in .env "
        "(or your email in data/answer_bank.yaml identity.email)",
        required=False,
    )


def run_checks(env: Env | None = None) -> Report:
    """Every check; none raises (a check that crashes reports the crash)."""
    env = env or current_env()
    report = Report(root=env.root)
    steps: list[Callable[[Env], Check | list[Check] | None]] = [
        check_python,
        check_uv,
        check_playwright,
        check_llm,
        check_location,
        check_hidden_pth,
        check_config_files,
        check_commute_coverage,
        check_prompt_set,
        check_ghost_after,
        check_tracks,
        check_title_classes,
        check_feeds,
        check_data_files,
        check_db,
        check_mcp,
        check_contact,
    ]
    for step in steps:
        try:
            result = step(env)
        except Exception as e:  # noqa: BLE001 — a diagnostic reports, it never stops
            name = step.__name__.removeprefix("check_").replace("_", " ")
            result = Check(name, False, f"check crashed: {e}", "report this as a jobkit bug")
        if result is None:
            continue
        report.checks += result if isinstance(result, list) else [result]
    return report
