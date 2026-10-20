"""`jobkit setup ...` — the setup interview's steps from the command line
(the MCP tools of onboarding/mcp_tools.py call the same functions).

Structured input (decisions, changes, answers, companies) is a path to a
YAML/JSON file, or the JSON itself."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from jobkit.onboarding import answer_bank_builder, corpus_confirm, ingest, intake, status, targets
from jobkit.onboarding.files import SetupError

setup_app = typer.Typer(
    no_args_is_help=True,
    help="The setup interview, step by step (ONBOARDING.md). In Claude, say "
    '"set up jobkit" instead: it asks the questions and calls these.',
)


def _input(arg: str) -> Any:
    """A YAML/JSON file's content, or inline JSON/YAML."""
    text = arg
    if not arg.lstrip().startswith(("{", "[")):
        try:
            p = Path(arg).expanduser()
            if p.is_file():
                text = p.read_text(encoding="utf-8")
        except OSError:
            pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        typer.echo(f"not valid YAML/JSON: {e}", err=True)
        raise typer.Exit(1) from e


def _run(fn: Any, *args: Any, **kwargs: Any) -> None:
    try:
        out = fn(*args, **kwargs)
    except SetupError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(out if isinstance(out, str) else out.render())


def show_status() -> None:
    """Which setup steps are done, in order, and the next action (read-only)."""
    report = status.setup_status()
    typer.echo(report.render())


@setup_app.command("status")
def status_cmd() -> None:
    """Which setup steps are done, in order, and the next action (read-only)."""
    show_status()


@setup_app.command("ingest")
def ingest_cmd(
    path: str = typer.Argument("", help="the resume: PDF, DOCX, HTML or plain text"),
    text_file: str = typer.Option("", "--text-file", help="a text file with the pasted resume"),
    replace_draft: bool = typer.Option(False, "--replace-draft", help="start the draft over"),
) -> None:
    """Read the resume; write the corpus draft (resume text only), the
    critique, and the extracted text."""
    text = Path(text_file).expanduser().read_text(encoding="utf-8") if text_file else None
    _run(ingest.ingest_resume, path or None, text=text, replace_draft=replace_draft)


@setup_app.command("draft")
def draft_cmd(batch: int = typer.Option(5, "--batch", help="bullets to show")) -> None:
    """The corpus draft: what is left to confirm, and the next bullets."""
    _run(corpus_confirm.draft_status, batch)


@setup_app.command("confirm")
def confirm_cmd(decisions: str = typer.Argument(..., help="file or JSON list")) -> None:
    """Apply bullet decisions: [{id, action: keep|edit|drop, metric_confidence,
    evidence, ...}]. metric_confidence and evidence are yours to set."""
    data = _input(decisions)
    if not isinstance(data, list):
        typer.echo("decisions must be a list", err=True)
        raise typer.Exit(1)
    _run(corpus_confirm.confirm_bullets, data)


@setup_app.command("update-draft")
def update_draft_cmd(changes: str = typer.Argument(..., help="file or JSON object")) -> None:
    """Identity, roles, education, positioning, skill_index, constraints,
    voice samples (see MCP update_corpus_draft)."""
    data = _input(changes)
    if not isinstance(data, dict):
        typer.echo("changes must be an object", err=True)
        raise typer.Exit(1)
    _run(corpus_confirm.update_draft, data)


@setup_app.command("promote")
def promote_cmd(
    replace_existing: bool = typer.Option(
        False, "--replace-existing", help="replace a corpus the interview did not write"
    ),
) -> None:
    """Write data/career_corpus.yaml from the confirmed draft."""
    _run(corpus_confirm.promote_corpus, replace_existing=replace_existing)


@setup_app.command("questions")
def questions_cmd() -> None:
    """The answer-bank questions, with what is already set."""
    _run(answer_bank_builder.questions_text)


@setup_app.command("answers")
def answers_cmd(
    answers: str = typer.Argument(..., help="file or JSON object {key: value}"),
    replace_existing: bool = typer.Option(
        False, "--replace-existing", help="replace an answer bank the interview did not write"
    ),
) -> None:
    """Save answer-bank answers and write data/answer_bank.yaml."""
    data = _input(answers)
    if not isinstance(data, dict):
        typer.echo("answers must be an object", err=True)
        raise typer.Exit(1)
    _run(answer_bank_builder.build_answer_bank, data, replace_existing=replace_existing)


@setup_app.command("intake")
def intake_cmd(
    answers: str = typer.Argument(..., help="file or JSON object (see MCP run_intake)"),
    replace_existing: bool = typer.Option(
        False, "--replace-existing", help="replace config the interview did not write"
    ),
) -> None:
    """Write config/filters.yaml and its companion files from interview answers."""
    data = _input(answers)
    if not isinstance(data, dict):
        typer.echo("answers must be an object", err=True)
        raise typer.Exit(1)
    _run(intake.run_intake, data, replace_existing=replace_existing)


@setup_app.command("targets")
def targets_cmd(
    names: Annotated[list[str] | None, typer.Argument(help="company names")] = None,
    file: str = typer.Option("", "--file", help="YAML/JSON list of {name, domain, url, tier}"),
    tier: int = typer.Option(2, "--tier", help="tier for the names given as arguments"),
    no_probe: bool = typer.Option(False, "--no-probe", help="registry and URLs only"),
    skip_not_found: bool = typer.Option(
        False, "--skip-not-found", help="do not record companies with no board found"
    ),
) -> None:
    """Find and verify public Greenhouse/Lever/Ashby boards; append to
    config/targets.yaml."""
    companies: list[Any] = [{"name": n, "tier": tier} for n in names or []]
    if file:
        data = _input(file)
        if not isinstance(data, list):
            typer.echo("--file must hold a list", err=True)
            raise typer.Exit(1)
        companies += data
    _run(
        targets.seed_targets,
        companies,
        probe=not no_probe,
        include_not_found=not skip_not_found,
    )
