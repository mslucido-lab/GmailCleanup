"""Narrow local-process bridge; the browser never invokes Gmail work directly."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path


def invoke(*arguments: str) -> None:
    script = Path(__file__).parents[1] / "execute" / "run.py"
    if not script.exists():
        raise RuntimeError("The execute component has not been installed yet")
    subprocess.Popen([sys.executable, str(script), *arguments], cwd=script.parents[1])
