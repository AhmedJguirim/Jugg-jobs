"""Unit tests for the command audit tool.

The audit trail is only worth having if its risk classification is trustworthy,
so the destructive-command rules are tested explicitly.
"""

from __future__ import annotations

import json

import pytest

from tools import cmdlog


class TestClassify:
    @pytest.mark.parametrize(
        "command",
        [
            "ls -la",
            "cat main.py",
            "grep -n foo bar.py",
            "git status",
            "psql -c 'select count(*) from companies'",
        ],
    )
    def test_read_only_commands_are_none(self, command: str):
        assert cmdlog.classify(command)[0] == "none"

    @pytest.mark.parametrize(
        "command",
        [
            "pip install sqlalchemy",
            "alembic upgrade head",
            "git commit -m 'x'",
            "psql -c 'CREATE TABLE companies (id int)'",
        ],
    )
    def test_state_changing_commands_are_medium(self, command: str):
        assert cmdlog.classify(command)[0] == "medium"

    @pytest.mark.parametrize(
        "command",
        [
            "psql -c 'DELETE FROM companies'",
            "alembic downgrade -1",
            "git reset --hard origin/main",
            "rm -rf build",
            "taskkill /PID 123 /F",
        ],
    )
    def test_data_losing_commands_are_high(self, command: str):
        assert cmdlog.classify(command)[0] == "high"

    @pytest.mark.parametrize(
        "command",
        [
            "psql -c 'DROP DATABASE nljobs'",
            "psql -c 'DROP TABLE companies'",
            "psql -c 'TRUNCATE companies'",
            "git push --force origin main",
            "dropdb nljobs",
        ],
    )
    def test_irreversible_commands_are_severe(self, command: str):
        assert cmdlog.classify(command)[0] == "severe"

    def test_drop_beats_the_generic_psql_rule(self):
        """Ordering matters: the destructive rule must win."""
        assert cmdlog.classify("psql -U postgres -c 'DROP DATABASE nljobs'")[0] == "severe"

    def test_unknown_command_defaults_to_low_not_none(self):
        """An unrecognised command must never be assumed harmless."""
        risk, reason = cmdlog.classify("frobnicate --wildly")

        assert risk == "low"
        assert "defaulting" in reason

    def test_classification_is_case_insensitive(self):
        assert cmdlog.classify("DROP DATABASE nljobs")[0] == "severe"
        assert cmdlog.classify("drop database nljobs")[0] == "severe"


class TestRecording:
    @pytest.fixture(autouse=True)
    def isolated_logs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cmdlog, "LOG_DIR", str(tmp_path))
        monkeypatch.setattr(cmdlog, "TEXT_LOG", str(tmp_path / "audit.log"))
        monkeypatch.setattr(cmdlog, "JSON_LOG", str(tmp_path / "audit.jsonl"))
        return tmp_path

    def _entries(self, tmp_path) -> list[dict]:
        path = tmp_path / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_log_writes_both_files(self, isolated_logs):
        cmdlog.main(["log", "--note", "listing", "--", "ls", "-la"])

        assert (isolated_logs / "audit.log").exists()
        assert (isolated_logs / "audit.jsonl").exists()

    def test_entry_captures_command_note_and_risk(self, isolated_logs):
        cmdlog.main(["log", "--note", "listing", "--", "ls", "-la"])

        entry = self._entries(isolated_logs)[0]
        assert entry["command"] == "ls -la"
        assert entry["note"] == "listing"
        assert entry["risk"] == "none"

    def test_log_is_append_only(self, isolated_logs):
        cmdlog.main(["log", "--", "ls"])
        cmdlog.main(["log", "--", "pwd"])

        assert len(self._entries(isolated_logs)) == 2

    def test_explicit_risk_overrides_heuristic_and_is_flagged(self, isolated_logs):
        cmdlog.main(["log", "--risk", "severe", "--", "ls"])

        entry = self._entries(isolated_logs)[0]
        assert entry["risk"] == "severe"
        assert entry["heuristic_risk"] == "none"
        assert entry["risk_override"] is True

    def test_run_executes_and_records_exit_code(self, isolated_logs):
        # Quoted as one token: `run` joins argv with spaces before shelling out.
        code = cmdlog.main(["run", "--note", "ok", "--", 'python -c "import sys; sys.exit(3)"'])

        entry = self._entries(isolated_logs)[0]
        assert code == 3
        assert entry["exit_code"] == 3
        assert entry["duration_s"] is not None

    def test_verify_flags_understated_risk(self, isolated_logs, capsys):
        cmdlog.main(["log", "--risk", "none", "--", "psql -c 'DROP DATABASE nljobs'"])

        exit_code = cmdlog.main(["verify"])

        assert exit_code == 1
        assert "DROP DATABASE" in capsys.readouterr().out

    def test_verify_passes_on_honest_log(self, isolated_logs):
        cmdlog.main(["log", "--", "ls -la"])

        assert cmdlog.main(["verify"]) == 0

    def test_report_summarises_entries(self, isolated_logs, capsys):
        cmdlog.main(["log", "--", "ls"])
        cmdlog.main(["log", "--risk", "severe", "--note", "x", "--", "dropdb nljobs"])

        cmdlog.main(["report"])

        out = capsys.readouterr().out
        assert "2 command(s) recorded" in out
        assert "severe=1" in out

    def test_report_can_filter_by_minimum_risk(self, isolated_logs, capsys):
        cmdlog.main(["log", "--", "ls"])
        cmdlog.main(["log", "--", "dropdb nljobs"])

        cmdlog.main(["report", "--min-risk", "high"])

        out = capsys.readouterr().out
        assert "dropdb nljobs" in out
        assert "  ls" not in out

    def test_missing_command_is_an_error(self, isolated_logs):
        assert cmdlog.main(["log", "--note", "nothing"]) == 2
