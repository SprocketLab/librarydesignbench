"""Harbor agents LDB runs: the workspace setup wrapper and the artifact replay."""

from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import suppress
from pathlib import Path
from shlex import quote
from tempfile import TemporaryDirectory
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.agents.factory import AgentFactory
from harbor.environments.base import BaseEnvironment
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext
from harbor.models.agent.name import AgentName
from harbor.models.task.artifacts import normalize_artifact_entries
from harbor.models.task.task import Task
from harbor.models.trial.config import AgentConfig
from harbor.models.trial.paths import EnvironmentPaths
from harbor.models.trial.paths import TrialPaths
from harbor.trial.artifact_handler import ArtifactHandler

from lib_design_bench.models.task import LIBRARY_INSTALL
from lib_design_bench.models.task import WORKSPACE_LOCATION
from lib_design_bench.runs.outcomes import AGENT_LOG_NAME
from lib_design_bench.runs.outcomes import STALL_WINDOW_LINES
from lib_design_bench.runs.outcomes import network_stalled
from lib_design_bench.runs.store import REPLAY_AGENT_IMPORT_PATH
from lib_design_bench.runs.store import workspace_artifact

STAGED_STARTER_DIR = "/tmp/ldb-replay-staged-starter"


def preserve_starter_command(workspace: str, staged: str) -> str:
    """Move every workspace entry aside so artifacts restore onto an empty tree."""
    workspace_path = quote(workspace)
    staged_path = quote(staged)
    return (
        "set -e\n"
        f"rm -rf {staged_path}\n"
        f"mkdir -p {staged_path}\n"
        f"find {workspace_path} -mindepth 1 -maxdepth 1 -exec sh -c "
        f'\'staged=$1; shift; for entry; do mv "$entry" "$staged" || exit; done\' '
        f"_ {staged_path} {{}} +\n"
    )


def restore_starter_command(workspace: str, staged: str) -> str:
    """Lay the staged starter back under the artifact, which wins every name."""
    workspace_path = quote(workspace)
    staged_path = quote(staged)
    return (
        "set -e\n"
        f"find {staged_path} -mindepth 1 -maxdepth 1 -exec sh -c "
        f"'workspace=$1; shift; for entry; do\n"
        '  restored="$workspace/${entry##*/}"\n'
        '  [ -e "$restored" ] || [ -L "$restored" ] || mv "$entry" "$restored" || exit\n'
        f"done' _ {workspace_path} {{}} +\n"
        f"rm -rf {staged_path}\n"
    )


class ReplayArtifactsAgent(BaseAgent):
    """Harbor agent that restores saved artifacts instead of calling an LLM."""

    def __init__(
        self,
        *args: Any,
        artifacts_dir: str,
        task_dir: str,
        replay_install_cmd: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self._artifacts_dir = Path(artifacts_dir)
        self._task_dir = Path(task_dir)
        self._replay_install_cmd = replay_install_cmd

    @staticmethod
    def name() -> str:
        """Return the agent name persisted in Harbor results."""
        return "ldb-replay-artifacts"

    def version(self) -> str | None:
        """Return no package version for this internal replay shim."""
        return None

    async def setup(self, environment: BaseEnvironment) -> None:
        """Restore the saved artifact over the starter the workspace staged."""
        workspace_artifact(self._artifacts_dir.parent)
        await _run_workspace_command(
            environment,
            preserve_starter_command(WORKSPACE_LOCATION, STAGED_STARTER_DIR),
        )
        env_paths = EnvironmentPaths.for_os(environment.os)
        task_artifacts = normalize_artifact_entries(
            Task(self._task_dir).config.artifacts
        )
        handler = ArtifactHandler(artifacts=task_artifacts, logger=self.logger)
        await handler.upload_artifacts(
            environment,
            artifacts_dir=self._artifacts_dir,
            source_artifacts_dir=env_paths.artifacts_dir,
            target_artifacts_dir=env_paths.artifacts_dir,
            artifacts=task_artifacts,
        )
        await _run_workspace_command(
            environment, restore_starter_command(WORKSPACE_LOCATION, STAGED_STARTER_DIR)
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Reinstall excluded dependencies after the artifact is restored."""
        if self._replay_install_cmd is not None and self._replay_install_cmd.strip():
            result = await environment.exec(self._replay_install_cmd)
            if result.return_code != 0:
                output = result.stderr or result.stdout or "no output"
                raise RuntimeError(
                    "Replay install command failed with code "
                    f"{result.return_code}: {output}"
                )
        context.metadata = {
            "replay_artifacts_dir": self._artifacts_dir.as_posix(),
        }


async def _run_workspace_command(environment: BaseEnvironment, command: str) -> None:
    result = await environment.exec(command, user="root")
    if result.return_code != 0:
        output = result.stderr or result.stdout or "no output"
        raise RuntimeError(
            f"Replay workspace preparation failed with code {result.return_code}: "
            f"{output}"
        )


class AgentNetworkStalledError(RuntimeError):
    """The agent stopped reaching its provider and would idle until timeout."""


_SETUP_BUNDLE_PATH = "/tmp/ldb-workspace-setup"


_STARTER_BUNDLE_PATH = "/tmp/ldb-workspace-starter"


_SETUP_FILES = frozenset(
    (
        "setup.sh",
        "ldb-library-setup.sh",
        "ldb-problem-setup.sh",
    )
)


STALL_POLL_SEC = 300.0
"""How often the adapter reads the agent log for a provider network stall."""


AGENT_ARTIFACT_FAILURE_EXIT_CODE = 86


AGENT_ARTIFACT_FAILURE_MARKER = "/tmp/ldb-agent-artifact-failed"


class WorkspaceSetupError(RuntimeError):
    """Raised when staging an authored library prevents agent execution."""


def workspace_setup_agent_config(
    agent: AgentConfig,
    *,
    task_dir: Path,
    trial_dir: Path,
    library_source: Path | None = None,
) -> AgentConfig:
    """Wrap one Harbor agent with LDB's pre-agent workspace setup."""
    inner = agent.model_dump(
        mode="json",
        include={
            "name",
            "import_path",
            "model_name",
            "kwargs",
            "env",
            "override_setup_timeout_sec",
        },
    )
    return agent.model_copy(
        deep=True,
        update={
            "name": None,
            "import_path": WORKSPACE_SETUP_AGENT_IMPORT_PATH,
            "kwargs": {
                "inner_agent": inner,
                "task_dir": task_dir.as_posix(),
                "trial_dir": trial_dir.as_posix(),
                "library_source": library_source.as_posix() if library_source else "",
            },
        },
    )


class WorkspaceSetupAgent(BaseAgent):
    """Stage and prepare `/workspace`, then delegate to the selected agent."""

    def __init__(
        self,
        logs_dir: Path,
        inner_agent: dict[str, Any],
        task_dir: str,
        trial_dir: str,
        library_source: str = "",
        model_name: str | None = None,
        logger: logging.Logger | None = None,
        extra_env: dict[str, str] | None = None,
        mcp_servers: list[Any] | None = None,
        skills_dir: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            logger=logger,
            extra_env=extra_env,
            mcp_servers=mcp_servers,
            skills_dir=skills_dir,
            **kwargs,
        )
        self._task_dir = Path(task_dir)
        self._library_source = library_source
        config = AgentConfig.model_validate(inner_agent)
        factory_kwargs: dict[str, Any] = {"logger": self.logger}
        if mcp_servers is not None:
            factory_kwargs["mcp_servers"] = mcp_servers
        if skills_dir is not None:
            factory_kwargs["skills_dir"] = skills_dir
        if config.name == AgentName.ORACLE.value:
            factory_kwargs.update(
                task_dir=self._task_dir,
                trial_paths=TrialPaths(Path(trial_dir)),
            )
        elif config.import_path == REPLAY_AGENT_IMPORT_PATH:
            factory_kwargs["task_dir"] = self._task_dir.as_posix()
        self._inner = AgentFactory.create_agent_from_config(
            config,
            logs_dir=self.logs_dir,
            **factory_kwargs,
        )

    @staticmethod
    def name() -> str:
        """Return the internal adapter name."""
        return "ldb-workspace-setup"

    def version(self) -> str | None:
        """Return the selected agent's version."""
        return self._inner.version()

    def to_agent_info(self):
        """Persist the selected agent's identity, not the internal adapter."""
        return self._inner.to_agent_info()

    async def setup(self, environment: BaseEnvironment) -> None:
        """Upload starter files, run setup, and initialize the selected agent."""
        if self._library_source:
            await environment.upload_dir(Path(self._library_source), LIBRARY_INSTALL)
            result = await environment.exec(
                f"mount --bind {LIBRARY_INSTALL} {LIBRARY_INSTALL} "
                f"&& mount -o remount,bind,ro {LIBRARY_INSTALL}",
                user="root",
            )
            if result.return_code != 0:
                raise RuntimeError(
                    "Execution environment cannot mount the uploaded authored library "
                    f"read-only at {LIBRARY_INSTALL}: {result.stderr or result.stdout}"
                )
        workspace = self._task_dir / "workspace"
        with TemporaryDirectory(prefix="ldb-workspace-setup-") as temporary_dir:
            temporary_root = Path(temporary_dir)
            setup_bundle = temporary_root / "setup"
            starter = temporary_root / "starter"
            _copy_workspace_parts(workspace, starter, setup_bundle)
            result = await _stage_workspace(environment, starter, setup_bundle)
            _raise_for_setup_result(result, self.logger)
        self._inner.session_id = self.session_id
        self._inner.context_id = self.context_id
        await self._inner.setup(environment)

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Delegate execution to the selected agent, aborting a network stall.

        The agent runs as one sandbox command Harbor waits on, so a stall is
        only visible in the log it streams. Polling that log lets the trial
        fail with a retryable error instead of idling to the agent timeout.
        """
        agent = asyncio.ensure_future(
            self._inner.run(instruction, environment, context)
        )
        watchdog = asyncio.ensure_future(_watch_for_network_stall(environment))
        try:
            await asyncio.wait({agent, watchdog}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (agent, watchdog):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        if watchdog.done() and not watchdog.cancelled():
            watchdog.result()
        agent.result()

    async def resume(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Delegate native resume to the selected agent."""
        await self._inner.resume(instruction, environment, context)

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Delegate optional post-run context population."""
        self._inner.populate_context_post_run(context)


WORKSPACE_SETUP_AGENT_IMPORT_PATH = f"{__name__}:{WorkspaceSetupAgent.__name__}"


async def _watch_for_network_stall(environment: BaseEnvironment) -> None:
    log = (EnvironmentPaths.agent_dir / AGENT_LOG_NAME).as_posix()
    while True:
        await asyncio.sleep(STALL_POLL_SEC)
        result = await environment.exec(
            f"tail -n {STALL_WINDOW_LINES} {log} 2>/dev/null || true"
        )
        lines = (result.stdout or "").splitlines()
        if network_stalled(lines):
            raise AgentNetworkStalledError(
                "The agent stopped reaching its provider; its log ends in "
                f"reconnect failures: {lines[-1]!r}"
            )


def _copy_workspace_parts(workspace: Path, starter: Path, setup_bundle: Path) -> None:
    if not workspace.is_dir():
        return
    shutil.copytree(
        workspace,
        starter,
        ignore=lambda _directory, names: [
            name for name in names if name in _SETUP_FILES
        ],
    )
    setup_files = tuple(name for name in _SETUP_FILES if (workspace / name).is_file())
    if not setup_files:
        return
    setup_bundle.mkdir()
    for name in setup_files:
        shutil.copy2(workspace / name, setup_bundle / name)


async def _stage_workspace(
    environment: BaseEnvironment,
    starter: Path,
    setup_bundle: Path,
) -> ExecResult | None:
    commands = ["status=0"]
    if starter.is_dir():
        await environment.upload_dir(starter, _STARTER_BUNDLE_PATH)
        commands.append(
            f"mkdir -p {WORKSPACE_LOCATION} "
            f"&& cp -a {_STARTER_BUNDLE_PATH}/. {WORKSPACE_LOCATION}/"
            " || status=$?"
        )
    if (setup_bundle / "setup.sh").is_file():
        await environment.upload_dir(setup_bundle, _SETUP_BUNDLE_PATH)
        commands.append(
            f'if [ "$status" -eq 0 ]; then bash {_SETUP_BUNDLE_PATH}/setup.sh; '
            "status=$?; fi"
        )
    if len(commands) == 1:
        return None
    commands.extend(
        (
            f"rm -rf {_STARTER_BUNDLE_PATH} {_SETUP_BUNDLE_PATH}",
            'exit "$status"',
        )
    )
    return await environment.exec("; ".join(commands), user="root")


def _raise_for_setup_result(
    result: ExecResult | None,
    logger: logging.Logger,
) -> None:
    """Raise durable setup evidence instead of publishing an unrun agent as complete."""
    if result is None or result.return_code == 0:
        return
    output = _setup_output(result)
    if result.return_code == AGENT_ARTIFACT_FAILURE_EXIT_CODE:
        logger.warning("Authored-library setup failed: %s", output)
        raise WorkspaceSetupError(
            "Authored-library setup failed before the selected agent ran: " + output
        )
    raise RuntimeError(
        f"Evaluation Phase workspace setup failed with code {result.return_code}: {output}"
    )


def _setup_output(result: ExecResult) -> str:
    """Preserve both setup streams in the exception Harbor persists."""
    parts = [
        f"stdout: {result.stdout.strip()}" if result.stdout else "",
        f"stderr: {result.stderr.strip()}" if result.stderr else "",
    ]
    return "\n".join(part for part in parts if part) or "no output"
