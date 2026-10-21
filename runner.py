"""End-to-end orchestration used by the app and the CLI."""

from collections.abc import Callable
from typing import BinaryIO

from contracts.config import RunConfig
from contracts.models import ProgressEvent, RunArtifact


def run_pipeline(
    source: BinaryIO, config: RunConfig, progress: Callable[[ProgressEvent], None]
) -> RunArtifact:
    """load -> initial quality -> aggregate -> candidates -> refined quality -> evidence -> model -> validate -> render."""
    raise NotImplementedError
