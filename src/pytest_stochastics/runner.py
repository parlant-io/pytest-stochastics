from logging import Logger
from typing import Generator, cast

import pytest
from _pytest.reports import TestReport

from pytest_stochastics.collector import StochasticFunctionCollector
from pytest_stochastics.runner_data import PlanId, RunnerStochasticsConfig, TestId, gen_fallback_lookup


class RunnerStochastics:
    """Stochastics Plugin Runner. Handles the relevant hooks."""

    def __init__(
        self,
        current_plan: str,
        runner_config: RunnerStochasticsConfig,
        logger: Logger,
    ) -> None:
        self.plan = current_plan
        self.runner_config = runner_config
        self.logger = logger

        self.lookup_test_thresholds = gen_fallback_lookup(runner_config, PlanId(current_plan))

        self._test_result_goals: dict[str, int] = {}
        self._stochastic_items: dict[str, pytest.Item] = {}
        self._flag_any_non_stochastic_test_failed = False

    def pytest_pycollect_makeitem(
        self,
        collector: pytest.Module | pytest.Class,
        name: str,
        obj: object,
    ) -> None | pytest.Item | pytest.Collector | list[pytest.Item | pytest.Collector]:
        """Interecpt `Function`s at collection time and try to match them with a strategy."""

        if not name.startswith("test_"):
            return None

        nodeid = TestId(f"{collector.nodeid}::{name}")
        if nodeid not in self.lookup_test_thresholds:
            self.logger.debug(f"No threshold found for {name}, not wrapping.")
            return None

        test_threshold = self.lookup_test_thresholds[nodeid]

        if test_threshold.out_of == 1 and test_threshold.at_least > 0:
            self.logger.debug(f"Redundant threshold found for {name}, not wrapping")
            return None

        self.logger.info(
            f"Wrapping stochastic function: `{name}` with threshold: `{test_threshold.threshold}`[{test_threshold.at_least}/{test_threshold.out_of}]"
        )

        return StochasticFunctionCollector.from_parent(
            collector,
            name=name,
            obj=obj,
            threshold=test_threshold,
        )

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_sessionstart(
        self,
        session: pytest.Session,
    ) -> Generator[None, None, None]:
        """Inject around session start to add some common-sense config prints."""

        yield None
        terminal_writer = session.config.get_terminal_writer()
        terminal_writer.write("stochastics plan: ")
        markup: dict[str, bool] = {}

        if len(self.lookup_test_thresholds) == 0:
            markup["red"] = True
        else:
            markup["green"] = True

        terminal_writer.write(f"{self.plan}", **markup)
        terminal_writer.line(f" [stochastic runs={len(self.lookup_test_thresholds)}]\n")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(
        self,
        item: pytest.Item,
        nextitem: pytest.Item | None,
    ) -> Generator[None, None, bool | None]:
        """Inject test groupings and summaries around the `pytest_runtest_protocol`."""

        if not isinstance(item.parent, StochasticFunctionCollector):
            yield None
            return None

        key = item.parent.nodeid
        threshold = item.parent.threshold
        if key not in self._test_result_goals:
            # Starting a new stochastics run
            self._test_result_goals[key] = threshold.at_least
            print(
                f"\n\r{"\033[94m"}StochasticSet:{"\033[0m"} {key} [{"\033[94m"}{threshold.threshold}: {threshold.at_least}/{threshold.out_of}{"\033[0m"}]",
                end="",
            )

        # skip to the end of the protocol
        yield None

        if nextitem is None or item.parent != nextitem.parent:
            # if we're the last item of this stochastics run
            penalty = self._test_result_goals[key]
            comment = f"{threshold.at_least - penalty} passed out of {threshold.out_of}"
            if penalty <= 0:
                outcome = f"{"\033[92m"}PASSED{"\033[0m"}"
            else:
                outcome = f"{"\033[91m"}FAILED{"\033[0m"}"
                comment += f", missing {penalty} passes"
                self._flag_any_non_stochastic_test_failed = True
            print(
                f"\n\r{"\033[94m"}StochasticSet:{"\033[0m"} {outcome} [{"\033[94m"}{comment}{"\033[0m"}]",
                end="",
            )
        return None

    def pytest_runtest_makereport(
        self,
        item: pytest.Item,
        call: pytest.CallInfo[None],
    ) -> None:
        """Called before each step's report is created."""

        if call.when == "call":
            if not isinstance(item.parent, StochasticFunctionCollector):
                return

            # before the actual test function is called, we store it with its parent nodeid.
            self._stochastic_items[item.nodeid] = item

    def pytest_runtest_logreport(
        self,
        report: TestReport,
    ) -> None:
        """Called after each step's test is run and a report has been generated."""

        if report.nodeid not in self._stochastic_items:
            self._flag_any_non_stochastic_test_failed |= not report.passed
            return

        testid = cast(pytest.Item, self._stochastic_items[report.nodeid].parent).nodeid
        if report.when == "call" and report.passed:
            self._test_result_goals[testid] -= 1

    def pytest_sessionfinish(
        self,
        session: pytest.Session,
        exitstatus: int,
    ) -> None:
        if len(self._stochastic_items) == 0:
            return

        if exitstatus != pytest.ExitCode.TESTS_FAILED:
            return

        if any(v > 0 for v in self._test_result_goals.values()) or self._flag_any_non_stochastic_test_failed:
            session.config.add_cleanup(
                lambda: print(f"{"\033[91m"}!!! Some tests failed. Check the details above !!!{"\033[0m"}\n")
            )
            return

        session.exitstatus = pytest.ExitCode.OK  # rectify the exit code
        session.config.add_cleanup(
            lambda: print(
                f"{"\033[92m"}!!! Testing finished successfully. Stochastic tests failed within acceptable margins. !!!{"\033[0m"}\n"
            )
        )
