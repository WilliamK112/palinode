"""The suite's own isolation guarantees — the third way a green run can lie.

Two failure modes live here, both siblings of the "exit 134 after everything
passed":

1. **Global config leaking across tests.** ``palinode.core.config.config`` is a
   process-wide singleton that ~30 fixtures mutate. Yield-fixture teardown runs
   after test-body failures, but is not registered if setup raises before its
   ``yield``. ``tests/conftest.py::_isolate_global_config`` starts first and
   restores config in a ``finally``, covering that setup-failure window.

2. **Colour-forcing environment.** ``rich`` colourises when ``FORCE_COLOR`` or
   ``CLICOLOR_FORCE`` is merely *present*, so a developer with either exported
   sees CLI tests fail on embedded ANSI while CI stays green. Tests that only
   fail on your machine teach you to ignore local failures.
"""
from __future__ import annotations

import os

import pytest

from palinode.core.config import config

pytest_plugins = ("pytester",)


class TestGlobalConfigIsolation:
    """Ordered pair: the first test corrupts config, the second must not see it.

    pytest runs tests in file order, so ``test_2_...`` observes whatever
    ``test_1_...`` left behind. Marked xfail rather than failing, because the
    point is that the *corruption* does not survive.
    """

    @pytest.mark.xfail(reason="deliberately fails after mutating global config", strict=True)
    def test_1_failing_test_that_mutates_global_config(self) -> None:
        config.memory_dir = "/nonexistent/leaked-from-a-failing-test"
        config.git.auto_commit = not config.git.auto_commit
        raise AssertionError("deliberate failure with global config mutated")

    def test_2_next_test_sees_pristine_config(self) -> None:
        assert config.memory_dir != "/nonexistent/leaked-from-a-failing-test", (
            "global config leaked out of a failing test — one red test will now "
            "cascade into unrelated ones"
        )


def test_nested_config_objects_keep_their_identity() -> None:
    """Restoration must not swap nested config objects for copies.

    Modules hold ``from palinode.core.config import config`` and reach through
    it (``config.services.api.port``); a restore that rebound ``config.services``
    to a clone would leave any captured reference stale.
    """
    assert config.services is config.services
    assert config.git is config.git


def test_colour_forcing_env_is_neutralised() -> None:
    assert "FORCE_COLOR" not in os.environ
    assert "CLICOLOR_FORCE" not in os.environ
    assert os.environ.get("NO_COLOR")


def test_prearmed_finalizer_runs_after_fixture_setup_failure(pytester) -> None:
    """Cleanup registered before mutation survives an exception before yield."""
    pytester.makepyfile(
        """
        import pytest

        state = []

        @pytest.fixture
        def mutates_then_fails(request):
            request.addfinalizer(state.clear)
            state.append("leaked")
            raise RuntimeError("fixture setup failed before yield")
            yield

        @pytest.mark.xfail(raises=RuntimeError, strict=True)
        def test_1_setup_failure(mutates_then_fails):
            pass

        def test_2_following_test_sees_clean_state():
            assert state == []
        """
    )

    result = pytester.runpytest("-q")
    result.assert_outcomes(passed=1, xfailed=1)
