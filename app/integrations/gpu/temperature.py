"""GPU temperature via `nvidia-smi`.

`nvidia-smi` ships with the NVIDIA driver, so this needs no extra package. A
missing tool, a timeout or an unreadable reply all return None: the cooldown is
a courtesy to the hardware, and not being able to read the sensor must never
stop the queue.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional

from app.logging_setup import log

QUERY = ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"]


def parse_temperature(output: str) -> Optional[int]:
    """The hottest GPU in nvidia-smi's output (one line per GPU), or None."""
    readings = []
    for line in output.splitlines():
        try:
            readings.append(int(line.strip()))
        except ValueError:
            continue
    return max(readings) if readings else None


def gpu_temperature() -> Optional[int]:
    """Current GPU temperature in Celsius, or None if it cannot be read."""
    if shutil.which(QUERY[0]) is None:
        log.debug("nvidia-smi not found; GPU temperature unavailable")
        return None
    try:
        completed = subprocess.run(QUERY, capture_output=True, text=True, timeout=10, check=True)
    except (subprocess.SubprocessError, OSError) as error:
        log.warning("could not read the GPU temperature: %s", error)
        return None
    return parse_temperature(completed.stdout)
