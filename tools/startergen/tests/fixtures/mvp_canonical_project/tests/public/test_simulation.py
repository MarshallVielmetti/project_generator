from mvp_project.simulation import Integrator, rollout


def test_integrator_step() -> None:
    assert Integrator().step((1.0, 0.5), 2.0) == (3.0, 0.5)


def test_rollout() -> None:
    assert rollout((0.0, 0.5), 2.0, 3) == (6.0, 0.5), (
        "rollout must apply every integration step"
    )
