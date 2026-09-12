from mvp_project.simulation import Integrator, rollout
from mvp_project.statistics import mean


def test_canonical_implementation_is_complete() -> None:
    assert mean([1.0, 3.0]) == 2.0
    assert Integrator().step((0.0, 1.0), 1.5) == (1.5, 1.0)
    assert rollout((0.0, 1.0), 1.5, 2) == (3.0, 1.0)
