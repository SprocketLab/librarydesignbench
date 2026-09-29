"""Command line interface for lib-design-bench."""

from __future__ import annotations

import logging
from typing import Annotated

import typer

from lib_design_bench.cli.eval import eval_app
from lib_design_bench.cli.run import run_experiment_command
from lib_design_bench.cli.saved import recalculate_command
from lib_design_bench.cli.saved import resume_command
from lib_design_bench.cli.verify import app as verify_app
from lib_design_bench.cli.verify import static_command
from lib_design_bench.logging import configure_logging

app = typer.Typer(
    help="Benchmark agent-authored library designs through Design and Evaluation Runs.",
    no_args_is_help=True,
)


@app.callback()
def configure_cli_logging(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show debug-level diagnostics."),
    ] = False,
) -> None:
    """Install readable logging before every command executes."""
    configure_logging(console_level=logging.DEBUG if verbose else logging.INFO)


app.command(name="static")(static_command)


app.command(name="recalculate")(recalculate_command)


app.command(name="run")(run_experiment_command)


app.add_typer(eval_app, name="eval")


app.command(name="resume")(resume_command)


app.add_typer(verify_app, name="verify")


def main() -> None:
    """Console script entrypoint."""
    app()
