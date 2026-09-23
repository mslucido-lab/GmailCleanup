from __future__ import annotations

import importlib.util
import threading
import time
import unittest
from pathlib import Path


EXECUTOR_PATH = Path(__file__).parents[1] / "review-ui" / "executor.py"
SPEC = importlib.util.spec_from_file_location("review_ui_executor_dispatch", EXECUTOR_PATH)
dispatch = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(dispatch)


class ExecutorDispatchTests(unittest.TestCase):
    def test_invoke_serializes_subprocesses_in_fifo_order(self) -> None:
        started: list[tuple[str, ...]] = []
        release_first = threading.Event()
        second_started = threading.Event()
        original_popen = dispatch.subprocess.Popen

        class FakeProcess:
            def __init__(self, arguments: list[str]) -> None:
                self.arguments = arguments

            def wait(self) -> int:
                if self.arguments[-1] == "first":
                    release_first.wait(timeout=2)
                else:
                    second_started.set()
                return 0

        def fake_popen(arguments, **_kwargs):
            started.append(tuple(arguments[-2:]))
            return FakeProcess(arguments)

        dispatch.subprocess.Popen = fake_popen
        try:
            dispatch.invoke("--archive", "first")
            dispatch.invoke("--archive", "second")
            self.assertTrue(_wait_for(lambda: started == [("--archive", "first")]))
            self.assertFalse(second_started.is_set())
            release_first.set()
            self.assertTrue(_wait_for(second_started.is_set))
        finally:
            dispatch.subprocess.Popen = original_popen


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


if __name__ == "__main__":
    unittest.main()
