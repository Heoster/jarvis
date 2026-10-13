"""Unit tests for _extract_openai_tool_calls covering every content-embedded pattern."""
from jarvis.core.providers import _extract_openai_tool_calls


def _check(msg, expected_name, test_name):
    calls = _extract_openai_tool_calls(msg)
    assert calls, f"{test_name}: no calls extracted"
    assert calls[0]["name"] == expected_name, f"{test_name}: got {calls[0]['name']!r}"
    print(f"  OK  {test_name}: {calls[0]}")


def test_standard_tool_calls_field():
    msg = {"tool_calls": [{"id": "c1", "function": {
        "name": "calculate", "arguments": '{"expression": "12*12"}'}}]}
    _check(msg, "calculate", "standard tool_calls field")


def test_hermes_flat_content():
    msg = {"content": '{"arguments": {"expression": "12*12"}, "name": "calculate"}'}
    _check(msg, "calculate", "hermes flat content")


def test_hermes_wrapped_function():
    msg = {"content": '{"name": "function", "type": "function", "function": {"name": "calculate", "arguments": {"expression": "12*12"}}}'}
    _check(msg, "calculate", "hermes wrapped function")


def test_tool_calls_in_content_wrapper():
    msg = {"content": '{"tool_calls": [{"function": {"name": "calculate", "arguments": {"expression": "12*12"}}}]}'}
    _check(msg, "calculate", "tool_calls in content wrapper")


def test_xml_tag_style():
    msg = {"content": '<tool_call>{"name": "calculate", "arguments": {"expression": "12*12"}}</tool_call>'}
    _check(msg, "calculate", "XML tag style")


def test_empty_returns_empty():
    assert _extract_openai_tool_calls({}) == []
    assert _extract_openai_tool_calls({"content": "just some text"}) == []
    assert _extract_openai_tool_calls({"content": ""}) == []
    print("  OK  empty inputs return []")


if __name__ == "__main__":
    test_standard_tool_calls_field()
    test_hermes_flat_content()
    test_hermes_wrapped_function()
    test_tool_calls_in_content_wrapper()
    test_xml_tag_style()
    test_empty_returns_empty()
    print("All extraction tests passed.")
