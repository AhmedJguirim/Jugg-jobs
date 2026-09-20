import os
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest

BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_up(url: str, timeout: float = 1.0) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (URLError, OSError):
        return False


@pytest.fixture(scope="session")
def app_server() -> str:
    """Reuse a running app if there is one, otherwise start main.py for the session."""
    if _is_up(BASE_URL):
        yield BASE_URL
        return

    process = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if _is_up(BASE_URL):
                break
            if process.poll() is not None:
                raise RuntimeError("app exited before it started serving")
            time.sleep(0.5)
        else:
            raise RuntimeError(f"app did not come up at {BASE_URL} within 60s")
        yield BASE_URL
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
