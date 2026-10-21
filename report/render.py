"""Markdown/JSON rendering where every quantity comes from a registry object."""

from contracts.models import RenderedReport
from contracts.runtime import ValidatedRun


def render_report(run: ValidatedRun) -> RenderedReport:
    """Render warnings first, then summary and recommendations, with a span audit map."""
    raise NotImplementedError
