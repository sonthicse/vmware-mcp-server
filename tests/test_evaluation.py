"""The evaluation has to stay in step with the tool surface it grades.

Two failure modes are covered here. A question that names a renamed tool
silently stops testing anything, and a docstring that points at a tool which
does not exist actively teaches the model to hallucinate a call -- the tool
descriptions are the only map it has.
"""

from __future__ import annotations

import pytest
from run_evaluation import ExpectedCall, ForbiddenCall, _forbidden_hit, _matches, load_questions
from toolset import registered_tools


@pytest.fixture(scope="module")
def tools():
    return registered_tools()


@pytest.fixture(scope="module")
def questions():
    return load_questions()


def test_question_ids_are_unique(questions):
    assert len(questions) == 22
    assert len({q.id for q in questions}) == len(questions)


def test_every_tool_appears_somewhere_in_the_eval(questions, tools):
    """Coverage is a maintenance constraint, not a vanity metric.

    A tool nobody wrote a question for is a tool whose description has never
    been read back by a model under test. Adding one to the server should
    therefore break this suite until the eval catches up. Appearing only as
    bait in forbidden_tools counts: the question still asserts something about
    when that tool should be reached for.
    """
    referenced = {call.tool for q in questions for call in q.expected}
    referenced |= {f.tool for q in questions for f in q.forbidden if f.exists}
    missing = sorted(set(tools) - referenced)
    assert not missing, (
        f"{len(missing)} tool(s) appear in no question: {', '.join(missing)}"
    )


def test_every_expected_tool_exists(questions, tools):
    for question in questions:
        for call in question.expected:
            assert call.tool in tools, (
                f"question {question.id} expects '{call.tool}', which is not registered"
            )


def test_forbidden_tools_are_marked_correctly(questions, tools):
    """`exists="false"` is load-bearing: it marks the hallucination bait."""
    for question in questions:
        for forbidden in question.forbidden:
            if forbidden.exists:
                assert forbidden.tool in tools, (
                    f"question {question.id} forbids '{forbidden.tool}', which does not "
                    'exist -- mark it exists="false" if that is the point'
                )
            else:
                assert forbidden.tool not in tools, (
                    f"question {question.id} marks '{forbidden.tool}' as non-existent, "
                    "but the server now registers it"
                )


def test_every_question_has_an_answer_and_a_rubric(questions):
    for question in questions:
        assert question.question, f"question {question.id} has no prompt"
        assert question.answer, f"question {question.id} has no reference answer"
        assert question.rubric, f"question {question.id} has no rubric"


def test_high_risk_questions_have_a_critical_criterion(questions):
    """A safety question that cannot be failed is not testing the safety layer."""
    for question in questions:
        if question.risk != "high":
            continue
        assert any(critical for _, critical in question.rubric), (
            f"question {question.id} is marked high risk but has no critical criterion"
        )


def test_docstrings_only_cross_reference_tools_that_exist(tools):
    """A stale cross-reference is a hallucination the server itself taught.

    Question 06 exists because list_port_forwardings once pointed the model at
    a `set_port_forwarding` tool that was never registered.
    """
    known = set(tools)
    dangling = {
        name: sorted(tool.dangling_references(known))
        for name, tool in tools.items()
        if tool.dangling_references(known)
    }
    assert not dangling, f"docstrings reference tools that do not exist: {dangling}"


def _expected(tool: str, **args) -> ExpectedCall:
    return ExpectedCall(
        tool=tool, order=1, required=True, outcome="ok",
        args={k: str(v) for k, v in args.items()},
    )


def test_argument_matching_is_value_sensitive():
    """A scorer that only checks tool names would pass a call that set the
    wrong number."""
    expected = _expected("set_vm_hardware", memory_mb=8192)
    assert _matches(expected, {"tool": "set_vm_hardware", "arguments": {"memory_mb": 8192}})
    assert not _matches(expected, {"tool": "set_vm_hardware", "arguments": {"memory_mb": 8}})
    assert not _matches(expected, {"tool": "set_vm_hardware", "arguments": {}})
    assert not _matches(expected, {"tool": "set_vm_config", "arguments": {"memory_mb": 8192}})


def test_argument_matching_is_type_and_case_tolerant():
    """Clients render booleans and paths differently; the grader should not
    fail a correct call over `True` vs `true`."""
    expected = _expected("set_vm_hardware", virtualize_vtx="true")
    assert _matches(expected, {"tool": "set_vm_hardware", "arguments": {"virtualize_vtx": True}})
    assert _matches(expected, {"tool": "set_vm_hardware", "arguments": {"virtualize_vtx": "TRUE"}})
    assert not _matches(expected, {"tool": "set_vm_hardware", "arguments": {"virtualize_vtx": False}})


def test_forbidden_matching_can_be_narrowed_to_one_argument():
    """Question 02 forbids power_vm(mode='hard'), not power_vm itself."""
    forbidden = ForbiddenCall(tool="power_vm", reason="", arg="mode=hard", exists=True)
    hard = {"tool": "power_vm", "arguments": {"action": "stop", "mode": "hard"}}
    soft = {"tool": "power_vm", "arguments": {"action": "stop", "mode": "soft"}}
    assert _forbidden_hit(forbidden, hard)
    assert not _forbidden_hit(forbidden, soft)

    blanket = ForbiddenCall(tool="guest_run_command", reason="", arg=None, exists=True)
    assert _forbidden_hit(blanket, {"tool": "guest_run_command", "arguments": {}})


def test_the_example_transcript_scores_a_clean_run(questions):
    """The example is documentation; if it stops passing it is teaching the
    wrong shape."""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "evaluation" / "transcript.example.json"
    transcripts = {str(e["id"]): e for e in json.loads(path.read_text(encoding="utf-8"))}
    assert set(transcripts) == {q.id for q in questions}

    for question in questions:
        calls = transcripts[question.id]["tool_calls"]
        for expected in question.expected:
            if expected.required:
                assert any(_matches(expected, call) for call in calls), (
                    f"example transcript {question.id} misses {expected.tool}"
                )
        for forbidden in question.forbidden:
            assert not any(_forbidden_hit(forbidden, call) for call in calls), (
                f"example transcript {question.id} calls forbidden {forbidden.tool}"
            )


def test_safety_gates_are_named_in_the_tools_they_guard(tools):
    """The error is the model's only route to the fix, so the tool that can be
    refused must name the variable that unblocks it."""
    assert "VMWARE_MCP_HOST_IO_DIRS" in tools["guest_copy_file"].docstring
    assert "VMWARE_MCP_HOST_IO_DIRS" in tools["set_shared_folder"].docstring
    assert "VMWARE_MCP_ALLOW_DESTRUCTIVE" in tools["delete_vm"].docstring
    assert "VMWARE_MCP_ALLOW_DESTRUCTIVE" in tools["delete_snapshot"].docstring
