"""The pause between vacancies when the GPU runs hot."""

from __future__ import annotations

import subprocess

import pytest
from dbos import DBOS

from app.integrations.gpu import temperature as gpu
from app.workflows import llm_queue as flow


class TestParseTemperature:
    def test_one_gpu(self):
        assert gpu.parse_temperature("53\n") == 53

    def test_the_hottest_of_several(self):
        assert gpu.parse_temperature("61\n84\n70\n") == 84

    def test_unreadable_output(self):
        assert gpu.parse_temperature("[N/A]\n") is None

    def test_empty_output(self):
        assert gpu.parse_temperature("") is None


class TestReadingTheSensor:
    def test_no_nvidia_smi_means_no_reading(self, monkeypatch):
        """Not being able to read the sensor must never stop the queue."""
        monkeypatch.setattr(gpu.shutil, "which", lambda name: None)

        assert gpu.gpu_temperature() is None

    def test_a_failing_nvidia_smi_means_no_reading(self, monkeypatch):
        monkeypatch.setattr(gpu.shutil, "which", lambda name: "nvidia-smi")

        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=10)

        monkeypatch.setattr(gpu.subprocess, "run", boom)

        assert gpu.gpu_temperature() is None

    def test_reads_nvidia_smi(self, monkeypatch):
        monkeypatch.setattr(gpu.shutil, "which", lambda name: "nvidia-smi")
        monkeypatch.setattr(
            gpu.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="77\n", stderr=""),
        )

        assert gpu.gpu_temperature() == 77


class TestHotOrNot:
    @pytest.mark.parametrize("reading, hot", [(79, False), (80, False), (81, True), (None, False)])
    def test_above_the_limit_is_hot(self, monkeypatch, reading, hot):
        """Above 80, not at it - and an unreadable sensor never counts as hot."""
        monkeypatch.setattr(flow, "gpu_temperature", lambda: reading)

        assert flow.read_gpu_temperature()["hot"] is hot

    def test_defaults_are_80_degrees_and_3_minutes(self):
        settings = flow.get_settings()

        assert settings.gpu_max_temp_c == 80
        assert settings.gpu_cooldown_seconds == 180


class TestCoolDown:
    @pytest.fixture
    def sensor(self, monkeypatch):
        """Feed a sequence of temperatures; record sleeps and stage messages."""
        state = {"readings": [], "sleeps": [], "stages": []}

        def read():
            temperature = state["readings"].pop(0)
            return {
                "temperature": temperature,
                "hot": temperature > 80,
                "max_temp": 80,
                "cooldown_seconds": 180,
                "max_cooldowns": 5,
            }

        monkeypatch.setattr(flow, "read_gpu_temperature", read)
        monkeypatch.setattr(DBOS, "sleep", lambda seconds: state["sleeps"].append(seconds))
        monkeypatch.setattr(DBOS, "set_event", lambda key, value: state["stages"].append(value))
        return state

    def test_a_cool_gpu_does_not_wait(self, sensor):
        sensor["readings"] = [65]

        assert flow.cool_down_gpu() == 0
        assert sensor["sleeps"] == []

    def test_a_hot_gpu_pauses_three_minutes(self, sensor):
        sensor["readings"] = [85, 72]

        assert flow.cool_down_gpu() == 1
        assert sensor["sleeps"] == [180]

    def test_it_rechecks_after_each_pause(self, sensor):
        sensor["readings"] = [88, 83, 79]

        assert flow.cool_down_gpu() == 2
        assert sensor["sleeps"] == [180, 180]

    def test_it_gives_up_after_five_pauses(self, sensor):
        """A stuck sensor must not stall the queue for good."""
        sensor["readings"] = [90] * 6

        assert flow.cool_down_gpu() == 5
        assert len(sensor["sleeps"]) == 5

    def test_the_progress_line_says_why_it_is_waiting(self, sensor):
        sensor["readings"] = [85, 70]

        flow.cool_down_gpu()

        assert sensor["stages"] == ["GPU at 85°C - cooling down for 3 min"]

    def test_a_pause_shorter_than_a_minute_reads_in_seconds(self, sensor, monkeypatch):
        sensor["readings"] = [85, 70]
        original = flow.read_gpu_temperature
        monkeypatch.setattr(flow, "read_gpu_temperature", lambda: {**original(), "cooldown_seconds": 10})

        flow.cool_down_gpu()

        assert sensor["stages"] == ["GPU at 85°C - cooling down for 10 s"]
