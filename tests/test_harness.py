"""Agentic harness: guardrails, approvals, verification, breaker and audit log."""
from __future__ import annotations

import json

import pytest

from jarvis.core.harness import (ApprovalRequest, AuditLog, FailureBreaker, Harness,
                                 classify, verify_result)
from test_tool_calling import _orchestrator, _run  # same helpers the tool-calling tests use


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


def _harness(tmp_path, mode="ask", approver=None, **extra):
    cfg = {"harness": {"approval": mode, "workspace_roots": [str(tmp_path)], **extra}}
    return Harness(cfg, approver=approver or (lambda request: False),
                   audit=AuditLog(tmp_path / "audit.jsonl"))


# ── guardrails: risk classification and decisions ────────────────────────
def test_read_tools_never_ask(workspace):
    h = _harness(workspace, mode="strict")
    assert h.decide("read_file", {"path": "x"}).action == "allow"


def test_write_inside_workspace_is_allowed_in_ask_mode(workspace):
    h = _harness(workspace)
    target = str(workspace / "notes.txt")
    decision = h.decide("write_file", {"path": target, "content": "hi"})
    assert decision.risk == "write" and decision.action == "allow"


def test_write_outside_workspace_needs_approval(workspace):
    h = _harness(workspace)
    decision = h.decide("write_file", {"path": "/etc/hosts-not-here", "content": "x"})
    assert decision.risk == "external" and decision.action == "approve"


def test_destructive_and_system_tools_ask_in_ask_mode(workspace):
    h = _harness(workspace)
    assert h.decide("delete_path", {"path": "old"}).action == "approve"
    assert h.decide("run_bash", {"command": "ls"}).action == "approve"
    assert h.decide("send_email", {"to": "a@b.c"}).action == "approve"


def test_strict_mode_asks_for_every_write(workspace):
    h = _harness(workspace, mode="strict")
    target = str(workspace / "notes.txt")
    assert h.decide("write_file", {"path": target, "content": "x"}).action == "approve"


def test_auto_mode_never_asks(workspace):
    h = _harness(workspace, mode="auto")
    assert h.decide("delete_path", {"path": "old"}).action == "allow"


def test_deny_list_wins_over_everything(workspace):
    h = _harness(workspace, mode="auto", deny_tools=["run_bash"])
    assert h.decide("run_bash", {"command": "ls"}).action == "deny"


def test_run_pipeline_is_allowed_by_default(workspace):
    # the user's own "search X" is the authorization for the routed web search
    h = _harness(workspace)
    assert h.decide("run_pipeline", {"name": "web_search", "query": "x"}).action == "allow"


def test_no_terminal_means_no(workspace, monkeypatch):
    from jarvis.core import harness as module

    monkeypatch.setattr(module.sys, "stdin", None)
    request = ApprovalRequest("run_bash", "system", {"command": "ls"}, "shell")
    assert module.console_approver(request) is False


def test_broken_approver_denies(workspace):
    def broken(request):
        raise RuntimeError("boom")

    h = _harness(workspace, approver=broken)
    assert h.ask(ApprovalRequest("run_bash", "system", {}, "")) is False


def test_classify_unknown_tool_takes_category_risk():
    assert classify("mystery", {}, category="communication")["risk"] == "external"
    assert classify("mystery", {}, category="search")["risk"] == "read"


# ── feedback loop: verification ──────────────────────────────────────────
def test_write_file_is_read_back(workspace):
    target = workspace / "a.txt"
    target.write_text("hello\n", encoding="utf-8")
    assert verify_result("write_file", {"path": str(target), "content": "hello\n"}).startswith(
        "verified")
    assert verify_result("write_file", {"path": str(target), "content": "other"}).startswith(
        "VERIFICATION FAILED")


def test_delete_is_verified_gone(workspace):
    target = workspace / "gone.txt"
    assert verify_result("delete_path", {"path": str(target)}).startswith("verified")
    target.write_text("still here", encoding="utf-8")
    assert verify_result("delete_path", {"path": str(target)}).startswith(
        "VERIFICATION FAILED")


def test_unverifiable_tool_has_no_verdict():
    assert verify_result("search_web", {"query": "x"}) is None


# ── loop breaker ─────────────────────────────────────────────────────────
def test_breaker_blocks_after_limit():
    breaker = FailureBreaker(limit=2)
    breaker.note("media_play", False, "no network")
    assert not breaker.blocked("media_play")
    breaker.note("media_play", False, "no network")
    assert breaker.blocked("media_play")
    assert "Stopped" in breaker.message("media_play")


def test_success_resets_the_breaker():
    breaker = FailureBreaker(limit=2)
    breaker.note("t", False)
    breaker.note("t", True)
    breaker.note("t", False)
    assert not breaker.blocked("t")


# ── observability: audit log ─────────────────────────────────────────────
def test_audit_log_is_jsonl_and_redacts_secrets(workspace):
    audit = AuditLog(workspace / "audit.jsonl")
    audit.record({"event": "tool", "tool": "http_request",
                  "args": {"token": "sk-ABCDEFGHIJKLMNOPQRSTUV1234"}})
    rows = [json.loads(line) for line in (workspace / "audit.jsonl").read_text().splitlines()]
    assert rows[0]["tool"] == "http_request"
    assert "ABCDEFGHIJKLMNOPQRSTUV1234" not in json.dumps(rows[0])


def test_audit_none_path_is_silent():
    AuditLog(None).record({"event": "tool"})  # must not raise


# ── orchestrator integration ─────────────────────────────────────────────
def test_denied_tool_is_never_executed(config, workspace):
    orchestrator, calls_run = _orchestrator(config, [
        {"tool_calls": [{"id": "1", "name": "run_bash", "args": {"command": "rm -rf x"}}]},
        {"content": "I could not run that without approval."},
    ], (True, "ran"))
    orchestrator.harness.approver = lambda request: False
    events = _run(orchestrator, "clean up the project folder")
    assert calls_run == []
    assert any(e.type == "error" and "not run" in e.text for e in events)
    orchestrator.shutdown()


def test_approved_tool_runs_and_is_audited(config, workspace):
    orchestrator, calls_run = _orchestrator(config, [
        {"tool_calls": [{"id": "1", "name": "run_bash", "args": {"command": "ls"}}]},
        {"content": "Listed the files."},
    ], (True, "a.txt"))
    orchestrator.harness.approver = lambda request: True
    orchestrator.harness.audit = AuditLog(workspace / "audit.jsonl")
    _run(orchestrator, "list the files")
    assert [c["name"] for c in calls_run] == ["run_bash"]
    text = (workspace / "audit.jsonl").read_text()
    assert '"event": "approval"' in text and '"approved": true' in text
    orchestrator.shutdown()


def test_repeated_failing_tool_is_blocked(config):
    failing = {"tool_calls": [{"id": str(i), "name": "run_bash",
                               "args": {"command": f"try {i}"}} for i in range(1)]}
    orchestrator, calls_run = _orchestrator(config, [
        failing, failing, failing, failing, {"content": "It keeps failing."},
    ], (False, "permission denied"))
    orchestrator.harness.approver = lambda request: True
    _run(orchestrator, "run the script")
    assert len(calls_run) == 2  # third and fourth attempts are stopped by the breaker
    orchestrator.shutdown()
