"""The model check compares real TLC reports with the known list, in both directions."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "check_models.py"
spec = importlib.util.spec_from_file_location("check_models", SCRIPT)
check_models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_models)

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


def test_known_list_skips_comments():
    assert check_models.known_breaks("# note\ndemo Lost a reason\n") == {("demo", "Lost")}
