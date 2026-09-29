"""Tests for what a run's `run.log` keeps."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from lib_design_bench.logging import run_logging


def test_run_log_keeps_orchestration_and_warnings_only(tmp_path: Path) -> None:
    """Trial-scoped Harbor records and library chatter stay out below WARNING."""
    trial = logging.getLogger(
        "harbor.utils.logger.harbor.trial.trial.task-a1__abc.harbor.agents.base"
    )
    root_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        with run_logging(tmp_path):
            logging.getLogger("lib_design_bench.pipeline.run").debug("kept")
            trial.debug("Running command: cat > /tmp/runtime.py")
            trial.warning("trial warning")
            logging.getLogger("LiteLLM").debug("Using AiohttpTransport...")
            logging.getLogger("asyncio").debug("Using selector: EpollSelector")
            logging.getLogger("harbor.utils.logger").debug("harbor, not a trial")
    finally:
        logging.getLogger().setLevel(root_level)

    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "run.log").read_text().splitlines()
    ]
    assert events == ["kept", "trial warning", "harbor, not a trial"]
