"""Serialized local-process bridge; the browser never invokes Gmail work directly."""
from __future__ import annotations

import logging
import queue
import subprocess
import sys
import threading
from pathlib import Path


LOGGER = logging.getLogger(__name__)
_WORK: queue.Queue[tuple[Path, tuple[str, ...]]] = queue.Queue()


def _run_queued_work() -> None:
    """Run one Gmail-writing executor at a time for this local account."""
    while True:
        script, arguments = _WORK.get()
        try:
            subprocess.Popen([sys.executable, str(script), *arguments], cwd=script.parents[1]).wait()
        except Exception:
            # The executor itself records Gmail failures durably. This catches
            # only process-launch failures while keeping the queue alive.
            LOGGER.exception("Unable to launch queued executor work")
        finally:
            _WORK.task_done()


_WORKER = threading.Thread(target=_run_queued_work, name="gmail-cleanup-executor", daemon=True)
_WORKER.start()


def invoke(*arguments: str) -> None:
    """Queue work immediately; the single worker serializes Gmail API access."""
    script = Path(__file__).parents[1] / "execute" / "run.py"
    if not script.exists():
        raise RuntimeError("The execute component has not been installed yet")
    _WORK.put((script, tuple(arguments)))
