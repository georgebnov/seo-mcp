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


def _run(model: str, out: Path) -> str:
    command = os.environ.get(COMMAND_ENV) or str(Path(COMMAND).expanduser())
    arguments = [f"{model}.tla", "--config", f"{model}.cfg", "--view", f"{model}.view.mjs"]
    subprocess.run(  # noqa: S603
        [command, *arguments, "--out", str(out / model)],
        cwd=MODELS,
        stdout=subprocess.DEVNULL,
        check=False,
    )
    return (out / model / "report.md").read_text(encoding="utf-8")


def main() -> int:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
    known = known_breaks(KNOWN.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()
    problems: list[str] = []
    for spec in sorted(MODELS.glob("*.tla")):
        report = _run(spec.stem, out)
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
    sys.exit(main())
