"""The model check compares real TLC reports with the known list, in both directions."""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import check_models  # noqa: E402
import verify_mutants  # noqa: E402

MUTANTS = [mutant for area in verify_mutants.AREAS.values() for mutant in area]

REPORT = """# demo

## Lost breaks
The shortest path has 1 step:

## Rules that hold
Kept, NoDeadlock
"""


def test_each_model_has_a_config_and_a_view():
    for spec in check_models.MODELS.glob("*.tla"):
        assert spec.with_suffix(".cfg").exists(), spec.name
        assert spec.with_name(f"{spec.stem}.view.mjs").exists(), spec.name


def test_each_known_break_names_a_rule_of_its_model():
    for model, rule in check_models.known_breaks(check_models.KNOWN.read_text()):
        assert rule in (check_models.MODELS / f"{model}.cfg").read_text(), (model, rule)


def test_a_new_break_names_the_rule_and_the_list():
    found = check_models.report_breaks("demo", REPORT)
    [problem] = check_models.compare(found, set())
    assert "demo: Lost breaks" in problem
    assert "known-breaks.txt" in problem


def test_a_rule_that_holds_now_must_leave_the_list():
    [problem] = check_models.compare(set(), {("demo", "Fixed")})
    assert "demo: Fixed holds now" in problem


@pytest.mark.parametrize("mutant", MUTANTS, ids=lambda mutant: f"{mutant.rule}-{mutant.why}")
def test_each_mutant_text_is_in_its_file_1_time(mutant):
    """A change to verified code must move its mutant too, or the mutant breaks nothing."""
    if isinstance(mutant, verify_mutants.Model):
        path = check_models.MODELS / f"{mutant.model}.tla"
    else:
        path = check_models.ROOT / mutant.file
    text = path.read_text()
    for old, _ in mutant.edits:
        assert text.count(old) == 1, (
            f"{path.name} does not hold the mutant text of {mutant.rule} 1 time. Copy the new "
            f"line into scripts/verify_mutants.py, and run the verify skill of the area: {old!r}"
        )


def test_each_mutant_rule_is_checked():
    for mutant in MUTANTS:
        if isinstance(mutant, verify_mutants.Model):
            assert mutant.rule in (check_models.MODELS / f"{mutant.model}.cfg").read_text()


def test_known_list_skips_comments():
    assert check_models.known_breaks("# note\ndemo Lost a reason\n") == {("demo", "Lost")}


def fake_statechart(tmp_path, monkeypatch, code: int) -> None:
    """Point STATECHART at a command that exits with code and writes nothing."""
    command = tmp_path / "statechart"
    command.write_text(f"#!/bin/sh\nexit {code}\n")
    command.chmod(0o755)
    monkeypatch.setenv(check_models.COMMAND_ENV, str(command))


def test_a_refused_run_never_reads_the_report_of_another_run(tmp_path, monkeypatch):
    """A shared --out holds the report of another run, and statechart exits 2 on it."""
    (tmp_path / "out" / "demo").mkdir(parents=True)
    (tmp_path / "out" / "demo" / "report.md").write_text(REPORT)
    fake_statechart(tmp_path, monkeypatch, 2)
    with pytest.raises(check_models.RunFailed, match="demo: statechart exited 2"):
        check_models.run_model("demo", tmp_path / "out", tmp_path)


def test_a_rule_that_breaks_gives_its_report(tmp_path, monkeypatch):
    (tmp_path / "out" / "demo").mkdir(parents=True)
    (tmp_path / "out" / "demo" / "report.md").write_text(REPORT)
    fake_statechart(tmp_path, monkeypatch, 1)
    assert check_models.run_model("demo", tmp_path / "out", tmp_path) == REPORT


def test_an_output_folder_that_is_not_empty_is_refused(tmp_path, monkeypatch, capsys):
    (tmp_path / "old").mkdir()
    fake_statechart(tmp_path, monkeypatch, 0)
    assert check_models.main([str(tmp_path)]) == 2
    assert f"{tmp_path} is not empty" in capsys.readouterr().err


def test_each_failed_run_is_a_problem(tmp_path, monkeypatch, capsys):
    fake_statechart(tmp_path, monkeypatch, 2)
    assert check_models.main([str(tmp_path / "out")]) == 1
    lines = capsys.readouterr().out.splitlines()
    for spec in check_models.MODELS.glob("*.tla"):
        assert (
            f"{spec.stem}: statechart exited 2 and wrote no report. See its error above." in lines
        )
    assert not [line for line in lines if "holds now" in line]
