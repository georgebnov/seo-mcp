"""Run each TLA+ model in models/, and compare the rules that break with the known list.

A run needs the statechart command, which installs TLC and Java, so `just check` and CI do not
run it. Run `just models` after a change to a flow that a model describes.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
KNOWN = MODELS / "known-breaks.txt"
COMMAND_ENV = "STATECHART"
COMMAND = "~/.claude/skills/statechart/scripts/statechart"
BREAK = re.compile(r"^## (\w+) breaks$", re.M)
NOT_CHECKED = "## Rules not checked"
HOLDS = re.compile(r"^## Rules that hold\n(.+)$", re.M)


def known_breaks(text: str) -> set[tuple[str, str]]:
    lines = [line.split() for line in text.splitlines() if line and not line.startswith("#")]
    return {(parts[0], parts[1]) for parts in lines}


def report_breaks(model: str, report: str) -> set[tuple[str, str]]:
    return {(model, rule) for rule in BREAK.findall(report)}


def compare(found: set[tuple[str, str]], known: set[tuple[str, str]]) -> list[str]:
    new = [
        f"{model}: {rule} breaks, and {KNOWN.name} does not list it. Fix the code or the model."
        for model, rule in sorted(found - known)
    ]
    old = [
        f"{model}: {rule} holds now. Remove its line from {KNOWN.name}."
        for model, rule in sorted(known - found)
    ]
    return new + old


class RunFailed(Exception):
    """The statechart command wrote no report of its own."""


def run_model(model: str, out: Path, folder: Path = MODELS) -> str:
    """Run 1 model of folder with its config and view, and give its report.

    Only exit 0 (each rule holds) and exit 1 (a rule breaks) write a report. On each other exit,
    a report in the folder is from another run, for example a run that shares the folder.
    """
    command = os.environ.get(COMMAND_ENV) or str(Path(COMMAND).expanduser())
    arguments = [f"{model}.tla", "--config", f"{model}.cfg", "--view", f"{model}.view.mjs"]
    done = subprocess.run(  # noqa: S603
        [command, *arguments, "--out", str(out / model)],
        cwd=folder,
        stdout=subprocess.DEVNULL,
        check=False,
    )
    if done.returncode not in (0, 1):
        raise RunFailed(
            f"{model}: statechart exited {done.returncode} and wrote no report. "
            "See its error above."
        )
    return (out / model / "report.md").read_text(encoding="utf-8")


def main(args: list[str]) -> int:
    out = Path(args[0]).resolve() if args else Path(tempfile.mkdtemp())
    if out.is_dir() and any(out.iterdir()):
        print(f"{out} is not empty. Give a new or empty folder.", file=sys.stderr)
        return 2
    known = known_breaks(KNOWN.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()
    problems: list[str] = []
    for spec in sorted(MODELS.glob("*.tla")):
        try:
            report = run_model(spec.stem, out)
        except RunFailed as error:
            problems.append(str(error))
            known = {(model, rule) for model, rule in known if model != spec.stem}
            continue
        breaks = report_breaks(spec.stem, report)
        found |= breaks
        holds = HOLDS.search(report)
        print(f"{spec.stem}: holds {holds[1] if holds else 'none'}")
        for _, rule in sorted(breaks):
            print(f"{spec.stem}: breaks {rule}")
        if NOT_CHECKED in report:
            problems.append(f"{spec.stem}: TLC did not check each rule. See {out / spec.stem}.")
    problems += compare(found, known)
    print(f"Charts and reports are in {out}.")
    print("\n".join(problems) or "Each break is in the known list, and each other rule holds.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
