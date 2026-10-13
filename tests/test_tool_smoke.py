"""Tool-layer smoke tests — every tool, no model.

Two guarantees, both of which used to be checkable only by asking a model to
call the tool and seeing what happened:

1. **the contract holds** — all 125 tools expose a schema a provider will accept
   (name, description, typed parameters, a ``required`` list that matches the
   properties). A tool that breaks this is invisible until the model calls it,
   and then it fails as a schema error nobody can debug.
2. **the safe ones actually run** — the probes in
   :mod:`jarvis.tools.selftest` execute real tools (calculator, JSON, file
   reads against a scratch directory, memory lookups) and require either a
   result or a polite explanation; a leaked Python exception fails the test.

Nothing here touches the network, the desktop, the operator's files or a
provider: that is what makes it a *smoke* test rather than an integration one.
To run the same checks outside pytest — on your machine, with your browser and
vault configured — use ``python -m jarvis tools --smoke``.
"""

from __future__ import annotations

import pytest

from jarvis.tools import selftest as st
from tool_harness import assert_clean, run_tool, tool_registry


# ── the contract every tool has with the model ──────────────────────────────
def test_registry_loads_every_category(registry):
    assert len(registry) >= 100                     # a tool going missing is a bug
    assert set(registry.categories()) >= {"browser", "file", "memory", "utility"}


@pytest.mark.parametrize("name", sorted(tool_registry().names()))
def test_every_tool_contract_is_valid(name):
    """No schema errors anywhere — these are the ones providers reject."""
    tool_obj = tool_registry().get(name)
    assert tool_obj is not None, f"{name} disappeared from the registry"
    problems = st.contract_problems(tool_obj)
    assert not problems, f"{name}: " + "; ".join(problems)


def test_contract_warnings_are_known_and_few(registry):
    """Undocumented parameters are warnings: visible, but not failures. Yet.

    The ceiling is deliberate — if a new tool ships with five unexplained
    parameters, this test fails and someone has to write the docstring.
    """
    warnings = {name: issues for name, issues in st.check_contracts(registry).items()
                if any(issue.level == "warning" for issue in issues)}
    documented = set()
    for name, issues in warnings.items():
        for issue in issues:
            documented.add(f"{name}: {issue.message.split(' is ')[0]}")
    assert len(warnings) <= 12, (
        "too many thin contracts — document the parameters the model must fill:\n"
        + "\n".join(sorted(documented)))


def test_names_are_unique_and_callable(registry):
    names = [tool.name for tool in registry.get_all_tools()]
    assert len(names) == len(set(names)), "duplicate tool names — the brain binds by name"
    assert all(st.TOOL_NAME_RE.match(name) for name in names)


# ── probes: real calls, no model ────────────────────────────────────────────
@pytest.mark.parametrize("probe", st.PROBES, ids=lambda probe: f"{probe.tool}")
def test_safe_probe_runs_cleanly(probe, registry, tmp_path):
    """Either it works, or it explains itself — never a traceback."""
    workspace = st.prepare_workspace(tmp_path / "workspace")
    result = st.run_probe(registry, probe, workspace=workspace, timeout=30)
    assert result.status != "missing", f"{probe.tool} is not registered any more"
    assert result.status != "crashed", (
        f"{probe.tool} raised: {result.message}\n"
        "a tool must turn a failure into a message the model can act on")
    if probe.expect == "ok":
        assert result.status == "ok", (
            f"{probe.tool} should have succeeded ({probe.why}): {result.message}")


def test_probe_list_only_names_real_tools(registry):
    known = set(registry.names())
    missing = sorted({probe.tool for probe in st.PROBES} - known)
    assert not missing, f"probes point at tools that do not exist: {missing}"


def test_every_probe_declares_what_it_is_testing():
    for probe in st.PROBES:
        assert probe.why, f"{probe.tool}: say why this call is safe to run"
        assert probe.expect in ("ok", "clean"), f"{probe.tool}: bad expectation"


# ── the checker itself: it must catch the things providers reject ───────────
def _fake_tool(name="broken_tool", doc="A tool that does a thing and says so.",
               schema=None, category="utility", **kwargs):
    """The smallest possible tool — handy for testing the checker itself."""
    from jarvis.tools.base import Tool

    def fn(value: str = "") -> str:
        return doc

    overrides = dict(kwargs)
    return Tool(fn, name=overrides.pop("name", name),
                description=overrides.pop("description", doc),
                category=overrides.pop("category", category),
                parameters=schema, **overrides)


def test_contract_checker_catches_broken_schemas():
    cases = [
        ("no description to go on", dict(doc="hi"), "only 2 chars"),
        ("bad name", dict(name="Not A Name"), "snake_case"),
        ("required not a property",
         dict(schema={"type": "object", "properties": {"a": {"type": "string"}},
                      "required": ["b"]}), "not in properties"),
        ("parameter without a type",
         dict(schema={"type": "object", "properties": {"a": {}}}), "no type"),
        ("parameters not an object", dict(schema={"type": "array"}), "'object'"),
        ("no category", dict(category=""), "no category"),
        ("nonsense timeout", dict(timeout=0), "positive number"),
    ]
    for label, overrides, expected in cases:
        found = [issue.message for issue in st.contract_issues(_fake_tool(**overrides))]
        assert any(expected in message for message in found), \
            f"{label}: expected {expected!r} in {found}"


def test_contract_checker_accepts_a_well_documented_tool(registry):
    example = registry.get("calculate")
    assert example is not None
    assert st.contract_issues(example) == []


def test_an_undocumented_parameter_is_only_a_warning():
    made = _fake_tool(doc="Adds two numbers together, carefully and well.",
                      schema={"type": "object", "properties": {"value": {"type": "string"}}})
    issues = st.contract_issues(made)
    assert not [issue for issue in issues if issue.level == "error"]
    assert any("not documented" in issue.message for issue in issues)


# ── the CLI: the same checks without pytest ─────────────────────────────────
def test_cli_tools_smoke_runs_without_a_model(home):
    from click.testing import CliRunner

    from jarvis.config import load_config
    from jarvis.interfaces.cli import main

    load_config(refresh=True)
    outcome = CliRunner().invoke(main, ["tools", "--smoke"])
    assert outcome.exit_code == 0, outcome.output
    assert "no model involved" in outcome.output
    assert "✅" in outcome.output


def test_cli_still_lists_tools_by_category(home):
    from click.testing import CliRunner

    from jarvis.config import load_config
    from jarvis.interfaces.cli import main

    load_config(refresh=True)
    outcome = CliRunner().invoke(main, ["tools", "utility"])
    assert outcome.exit_code == 0, outcome.output
    assert "calculate" in outcome.output


# ── the helper the next tool test will use ──────────────────────────────────
def test_run_tool_calls_a_real_tool_without_a_model():
    result = run_tool("calculate", expression="40 + 2")
    assert result.ok and "42" in result.content

    missing = run_tool("read_file", path="/definitely/not/here.txt")
    assert_clean(missing)                       # a message, not a traceback


def test_assert_clean_rejects_a_leaked_exception():
    from jarvis.tools.base import ToolResult

    crashed = ToolResult(ok=False, tool="x", error="ValueError: bad input")
    with pytest.raises(AssertionError, match="crashed"):
        assert_clean(crashed)
    with pytest.raises(AssertionError, match="crashed"):     # a crash is always a crash
        assert_clean(crashed, expect_ok=True)

    explained = ToolResult(ok=False, tool="x", error="No Obsidian vault configured.")
    assert_clean(explained)                                  # a clean failure is fine…
    with pytest.raises(AssertionError, match="expected the tool to succeed"):
        assert_clean(explained, expect_ok=True)              # …unless we demanded success
    assert_clean(ToolResult(ok=True, tool="x", content="fine"), expect_ok=True)


def test_the_cli_report_is_printable_and_honest(registry):
    report = st.run_smoke(registry)
    assert report.tool_count == len(registry) and report.results
    text = report.text()
    assert "no model involved" in text
    assert report.ok, text                      # nothing broken anywhere
    assert ("✅" in text) if report.ok else ("❌" in text)
