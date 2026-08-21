"""Driver for the tool-use evaluation.

Three subcommands:

    list      print the questions, ready to paste into an MCP client
    validate  check the XML against the server's actual tool surface
    score     grade a transcript's tool calls against the expected sequence

Scoring is deliberately split. Tool selection, ordering and arguments are
mechanical and graded here; the rubric criteria are judgement calls and are
printed as a checklist for a human or a judge model. A run that passes the
mechanical half and fails the rubric is still a failing run -- the rubric is
where "did not power the VM on unasked" lives.

Usage:
    python evaluation/run_evaluation.py list
    python evaluation/run_evaluation.py list --id 03
    python evaluation/run_evaluation.py validate
    python evaluation/run_evaluation.py score transcripts.json
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from toolset import registered_tools

EVAL_FILE = Path(__file__).resolve().parent / "tool_use_eval.xml"


@dataclass
class ExpectedCall:
    tool: str
    order: int
    required: bool
    outcome: str
    args: dict[str, str]


@dataclass
class ForbiddenCall:
    tool: str
    reason: str
    arg: str | None
    exists: bool


@dataclass
class Question:
    id: str
    focus: str
    risk: str
    question: str
    setup: str
    answer: str
    expected: list[ExpectedCall] = field(default_factory=list)
    forbidden: list[ForbiddenCall] = field(default_factory=list)
    rubric: list[tuple[str, bool]] = field(default_factory=list)


def _text(node: ET.Element | None) -> str:
    return " ".join((node.text or "").split()) if node is not None else ""


def load_questions(path: Path = EVAL_FILE) -> list[Question]:
    root = ET.parse(path).getroot()
    questions = []
    for pair in root.findall("qa_pair"):
        q = Question(
            id=pair.get("id", "?"),
            focus=pair.get("focus", ""),
            risk=pair.get("risk", "normal"),
            question=_text(pair.find("question")),
            setup=_text(pair.find("setup")),
            answer=_text(pair.find("answer")),
        )
        for call in pair.findall("expected_tools/call"):
            q.expected.append(
                ExpectedCall(
                    tool=call.get("tool", ""),
                    order=int(call.get("order", "0")),
                    required=call.get("required", "true") != "false",
                    outcome=call.get("outcome", "ok"),
                    args={
                        arg.get("name", ""): arg.get("value", "")
                        for arg in call.findall("arg")
                    },
                )
            )
        for tool in pair.findall("forbidden_tools/tool"):
            q.forbidden.append(
                ForbiddenCall(
                    tool=tool.get("name", ""),
                    reason=tool.get("reason", ""),
                    arg=tool.get("arg"),
                    exists=tool.get("exists", "true") != "false",
                )
            )
        for criterion in pair.findall("rubric/criterion"):
            q.rubric.append(
                (_text(criterion), criterion.get("critical") == "true")
            )
        questions.append(q)
    return questions


def cmd_list(args: argparse.Namespace) -> int:
    for q in load_questions():
        if args.id and q.id != args.id:
            continue
        print(f"--- {q.id}  [{q.focus}]" + ("  RISK: high" if q.risk == "high" else ""))
        if q.setup:
            print(f"    setup: {q.setup}")
        print(f"    {q.question}\n")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    tools = registered_tools()
    known = set(tools)
    problems: list[str] = []

    for q in load_questions():
        for call in q.expected:
            if call.tool not in known:
                problems.append(
                    f"{q.id}: expected_tools names '{call.tool}', which no tool module "
                    "registers. Either the tool was renamed or the question is wrong."
                )
        for forbidden in q.forbidden:
            if forbidden.exists and forbidden.tool not in known:
                problems.append(
                    f"{q.id}: forbidden tool '{forbidden.tool}' does not exist. If that is "
                    "the point of the question, mark it exists=\"false\"."
                )
            if not forbidden.exists and forbidden.tool in known:
                problems.append(
                    f"{q.id}: '{forbidden.tool}' is marked exists=\"false\" but the server "
                    "now registers it. Update the question."
                )

    covered = {call.tool for q in load_questions() for call in q.expected}
    print(f"{len(load_questions())} questions, {len(tools)} tools registered.")
    print(f"tools exercised by the expected sequences: {len(covered)}")
    uncovered = sorted(known - covered)
    if uncovered:
        print(f"not exercised ({len(uncovered)}): {', '.join(uncovered)}")

    for problem in problems:
        print(f"PROBLEM  {problem}")
    return 1 if problems else 0


def _matches(expected: ExpectedCall, actual: dict) -> bool:
    if actual.get("tool") != expected.tool:
        return False
    supplied = actual.get("arguments") or {}
    for name, value in expected.args.items():
        if name not in supplied:
            return False
        if str(supplied[name]).strip().lower() != value.strip().lower():
            return False
    return True


def _forbidden_hit(forbidden: ForbiddenCall, actual: dict) -> bool:
    if actual.get("tool") != forbidden.tool:
        return False
    if forbidden.arg is None:
        return True
    name, _, value = forbidden.arg.partition("=")
    supplied = (actual.get("arguments") or {}).get(name)
    return supplied is not None and str(supplied).strip().lower() == value.strip().lower()


def cmd_score(args: argparse.Namespace) -> int:
    transcripts = {
        str(entry.get("id")): entry
        for entry in json.loads(Path(args.transcript).read_text(encoding="utf-8"))
    }
    questions = load_questions()
    passed = 0

    for q in questions:
        entry = transcripts.get(q.id)
        print(f"--- {q.id}  [{q.focus}]")
        if entry is None:
            print("    SKIP  no transcript for this question\n")
            continue

        calls = entry.get("tool_calls") or []
        failures: list[str] = []

        # Match greedily forwards: a tool can legitimately appear twice in one
        # sequence (attempt, fix, retry), so each expected call consumes the
        # first match *after* the previous one rather than the first overall.
        cursor = -1
        for expected in sorted(q.expected, key=lambda c: c.order):
            hits = [i for i, call in enumerate(calls) if _matches(expected, call)]
            forward = [i for i in hits if i > cursor]
            if forward:
                cursor = forward[0]
                continue
            if hits:
                failures.append(f"{expected.tool} called out of order")
                continue
            if expected.required:
                args_text = ", ".join(f"{k}={v}" for k, v in expected.args.items())
                failures.append(f"missing call {expected.tool}({args_text})")

        for forbidden in q.forbidden:
            if any(_forbidden_hit(forbidden, call) for call in calls):
                label = f"{forbidden.tool}({forbidden.arg})" if forbidden.arg else forbidden.tool
                failures.append(f"called forbidden {label} -- {forbidden.reason}")

        if failures:
            for failure in failures:
                print(f"    FAIL  {failure}")
        else:
            passed += 1
            print(f"    PASS  {len(calls)} tool call(s)")

        print("    rubric (judge the response text):")
        for text, critical in q.rubric:
            print(f"      [ ]{' !' if critical else '  '} {text}")
        print()

    print(f"mechanical score: {passed}/{len(questions)}")
    print("Rubric criteria are unscored above; a critical (!) miss fails the question.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    lister = sub.add_parser("list", help="print the questions")
    lister.add_argument("--id", help="only this question")
    lister.set_defaults(func=cmd_list)

    validator = sub.add_parser("validate", help="check the XML against the tool surface")
    validator.set_defaults(func=cmd_validate)

    scorer = sub.add_parser("score", help="grade a transcript")
    scorer.add_argument("transcript", help="JSON file, see evaluation/README.md")
    scorer.set_defaults(func=cmd_score)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
