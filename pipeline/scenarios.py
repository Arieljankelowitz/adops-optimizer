"""Pure conditional-impact formulas over historical facts and test assumptions."""

from contracts.models import ScenarioRequest
from contracts.runtime import AssumptionRegistry, FactRegistry, ScenarioRegistry


def compute_scenarios(
    registry: FactRegistry,
    assumptions: AssumptionRegistry,
    requests: list[ScenarioRequest],
) -> ScenarioRegistry:
    """Compute scenario facts (kind ``scenario``) from code formulas; unknown impact stays null."""
    raise NotImplementedError
