# pytest · py3.10 · unguarded · 28cf5274c0548a68a2425d8473bebd605f11982d

- runner: Linux
- python: Python 3.10.22
- job: tests

## installed

```
anyio==4.15.1
backports.asyncio.runner==1.2.0
certifi==2026.7.22
click==8.5.0
exceptiongroup==1.3.1
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.20
iniconfig==2.3.1
-e git+https://github.com/heostertech-cyber/jarvis@28cf5274c0548a68a2425d8473bebd605f11982d#egg=jarvis_operator
markdown-it-py==4.2.0
mdurl==0.1.2
packaging==26.3
pluggy==1.6.0
Pygments==2.21.0
pytest==9.1.1
pytest-asyncio==1.4.0
PyYAML==6.0.3
rich==15.0.0
ruff==0.17.0
tomli==2.5.0
typing_extensions==4.16.0
```

## output (tail)

```
........................F............................................... [ 40%]
..........................................................F............. [ 80%]
.........................F.F....F.F                                      [100%]
=================================== FAILURES ===================================
_____________ test_harness_launch_fails_gracefully_without_chrome ______________

    def test_harness_launch_fails_gracefully_without_chrome():
        harness = BrowserHarness({"browser": {"engine": "auto"}})
        outcome = harness.launch(url="about:blank")
>       assert outcome["ok"] is False
E       assert True is False

tests/test_browser_screen.py:126: AssertionError
----------------------------- Captured stderr call -----------------------------
08:29:53 INFO    browser.launch launching google-chrome (port 9222, headless)
08:29:58 INFO    browser.launch chrome ready: pid=2496 port=9222
08:29:58 INFO    browser.harness Chrome launched from terminal: pid=2496 port=9222
------------------------------ Captured log call -------------------------------
INFO     jarvis.browser.launch:terminal_launcher.py:230 launching google-chrome (port 9222, headless)
INFO     jarvis.browser.launch:terminal_launcher.py:265 chrome ready: pid=2496 port=9222
INFO     jarvis.browser.harness:harness.py:1325 Chrome launched from terminal: pid=2496 port=9222
_____________________ test_prebuilt_status_and_description _____________________

vault = PosixPath('/tmp/pytest-of-runner/pytest-1/test_prebuilt_status_and_descr0/Vault')
tmp_path = PosixPath('/tmp/pytest-of-runner/pytest-1/test_prebuilt_status_and_descr0')

    def test_prebuilt_status_and_description(vault, tmp_path):
        from jarvis.tools.obsidian import prebuilt as prebuilt_module
    
        vendor = tmp_path / "prebuilt"
        _fake_bundle(vendor / "dataview", "dataview")
        prebuilt_module.install_prebuilt(vault, [prebuilt_module.find_prebuilt("dataview")],
                                         vendored_root_path=vendor)
        rows = prebuilt_module.prebuilt_status(vault, vendored_root_path=vendor)
        dataview = next(row for row in rows if row["id"] == "dataview")
        assert dataview["installed"] and dataview["enabled"] and dataview["vendored"]
>       assert dataview["version"] == "9.9.9"
E       AssertionError: assert '0.5.68' == '9.9.9'
E         
E         - 9.9.9
E         + 0.5.68

tests/test_obsidian.py:502: AssertionError
----------------------------- Captured stderr call -----------------------------
08:30:10 INFO    tools.obsidian.prebuilt dataview: main.js 2322 KiB ok
08:30:10 INFO    tools.obsidian.prebuilt dataview: styles.css 3 KiB ok
------------------------------ Captured log call -------------------------------
INFO     jarvis.tools.obsidian.prebuilt:prebuilt.py:347 dataview: main.js 2322 KiB ok
INFO     jarvis.tools.obsidian.prebuilt:prebuilt.py:347 dataview: styles.css 3 KiB ok
____________ test_search_reports_missing_providers_without_hanging _____________

config = {'jarvis': {'version': '1.0.0', 'name': 'JARVIS', 'operator': 'Heoster', 'company': 'CODEEX-AI', ...}, 'llm': {'provid... {'max_budget': 14000, 'system_prompt_budget': 2000, 'tool_definitions_budget': 2500, 'memory_budget': 3500, ...}, ...}
registry = <jarvis.tools.registry.ToolRegistry object at 0x7fdeda9ff7f0>
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x7fdedaa40e20>

    def test_search_reports_missing_providers_without_hanging(config, registry, monkeypatch):
        """With no keys, no SearXNG and no network, search must explain itself."""
        monkeypatch.setattr("jarvis.config.get_config", lambda: config, raising=False)
        result = registry.execute_sync("search_web", {"query": "anything"})
        message = f"{result.content} {result.error}".lower()
>       assert "provider" in message or "no search" in message
E       AssertionError: assert ('provider' in '**search results for “anything”** _(via ddg)_\n1. **anything - ai app builder**\n   https://www.createanything.com/\n...avily.com); serper: no api key (free at serper.dev); searxng: searxng request failed: [errno 111] connection refused_ ' or 'no search' in '**search results for “anything”** _(via ddg)_\n1. **anything - ai app builder**\n   https://www.createanything.com/\n...avily.com); serper: no api key (free at serper.dev); searxng: searxng request failed: [errno 111] connection refused_ ')

tests/test_tools.py:249: AssertionError
---------------------------- Captured stderr setup -----------------------------
08:30:13 INFO    tools.registry loaded 123 tools across 10 categories: {'browser': 22, 'code': 8, 'communication': 8, 'crawl': 8, 'file': 11, 'memory': 5, 'obsidian': 18, 'screen': 19, 'search': 8, 'utility': 16}
------------------------------ Captured log setup ------------------------------
INFO     jarvis.tools.registry:registry.py:194 loaded 123 tools across 10 categories: {'browser': 22, 'code': 8, 'communication': 8, 'crawl': 8, 'file': 11, 'memory': 5, 'obsidian': 18, 'screen': 19, 'search': 8, 'utility': 16}
____________________ test_read_url_reports_failure_cleanly _____________________

config = {'jarvis': {'version': '1.0.0', 'name': 'JARVIS', 'operator': 'Heoster', 'company': 'CODEEX-AI', ...}, 'llm': {'provid... {'max_budget': 14000, 'system_prompt_budget': 2000, 'tool_definitions_budget': 2500, 'memory_budget': 3500, ...}, ...}

    def test_read_url_reports_failure_cleanly(config):
        """No network → the crawl chain raises a clear error, not a hang."""
        from jarvis.tools.crawl.reader import read_url_text
        from jarvis.tools.search.base import SearchError
    
        try:
            outcome = read_url_text("https://example.com")
        except SearchError as exc:
            assert "example.com" in str(exc) or "could not" in str(exc).lower()
        else:
>           assert outcome["provider"] == "" or not outcome["text"]
E           AssertionError: assert ('jina' == ''
E             
E             + jina or not 'Title: Example Domain\n\nURL Source: https://example.com/\n\nPublished Time: Fri, 09 Oct 2026 20:18:28 GMT\n\nWarning...n examples without needing permission. This is not a service; avoid relying on it for testing and monitoring purposes.')

tests/test_tools.py:281: AssertionError
____________ test_headless_browser_tools_report_failure_not_success ____________

registry = <jarvis.tools.registry.ToolRegistry object at 0x7fdeda88ff40>

    def test_headless_browser_tools_report_failure_not_success(registry):
        for name, args in [("browse_open", {"url": "example.com"}),
                           ("page_read", {}), ("page_click", {"target": "1"}),
                           ("page_type", {"target": "Search", "text": "x"}),
                           ("page_press", {"key": "Enter"}), ("page_scroll", {}),
                           ("page_back", {}), ("page_forward", {}),
                           ("page_tabs", {}), ("page_screenshot", {}),
                           ("browser_launch", {}), ("screenshot_page", {})]:
            result = registry.execute_sync(name, args)
>           assert result.ok is False, f"{name} claimed success: {result.content[:80]}"
E           AssertionError: browser_launch claimed success: ✅ Chrome launched from terminal:
E             {
E               "pid": 2858,
E               "port": 9222,
E               "running": t
E           assert True is False
E            +  where True = ToolResult(ok=True, content='✅ Chrome launched from terminal:\n{\n  "pid": 2858,\n  "port": 9222,\n  "running": true,\...0.0,\n  "tabs": 1,\n  "cdp": "http://127.0.0.1:9222"\n}', error='', elapsed_ms=22, tool='browser_launch', artifacts=[]).ok

tests/test_tools.py:352: AssertionError
---------------------------- Captured stderr setup -----------------------------
08:30:16 INFO    tools.registry loaded 123 tools across 10 categories: {'browser': 22, 'code': 8, 'communication': 8, 'crawl': 8, 'file': 11, 'memory': 5, 'obsidian': 18, 'screen': 19, 'search': 8, 'utility': 16}
------------------------------ Captured log setup ------------------------------
INFO     jarvis.tools.registry:registry.py:194 loaded 123 tools across 10 categories: {'browser': 22, 'code': 8, 'communication': 8, 'crawl': 8, 'file': 11, 'memory': 5, 'obsidian': 18, 'screen': 19, 'search': 8, 'utility': 16}
----------------------------- Captured stderr call -----------------------------
08:30:16 INFO    browser.harness browser engine: cdp (navigation + tabs only (pip install websockets for DOM control))
08:30:16 WARNING browser.harness engine cdp failed (cannot read DOM: raw CDP needs `pip install websockets` to read the DOM); degrading
08:30:16 INFO    browser.launch launching google-chrome (port 9222, headless)
08:30:16 INFO    browser.launch chrome ready: pid=2858 port=9222
------------------------------ Captured log call -------------------------------
INFO     jarvis.browser.harness:harness.py:963 browser engine: cdp (navigation + tabs only (pip install websockets for DOM control))
WARNING  jarvis.browser.harness:harness.py:978 engine cdp failed (cannot read DOM: raw CDP needs `pip install websockets` to read the DOM); degrading
INFO     jarvis.browser.launch:terminal_launcher.py:230 launching google-chrome (port 9222, headless)
INFO     jarvis.browser.launch:terminal_launcher.py:265 chrome ready: pid=2858 port=9222
__________________ test_search_and_crawl_fail_loudly_offline ___________________

registry = <jarvis.tools.registry.ToolRegistry object at 0x7fdeda87d870>

    def test_search_and_crawl_fail_loudly_offline(registry):
        """With no network and no keys, search/crawl must not look successful."""
        search = registry.execute_sync("search_web", {"query": "anything at all"})
>       assert search.ok is False and search.error
E       AssertionError: assert (True is False)
E        +  where True = ToolResult(ok=True, content='**Search results for “anything at all”** _(via ddg)_\n1. **Anything at All - YouTube**\n ...g: searxng request failed: [Errno 111] Connection refused_', error='', elapsed_ms=603, tool='search_web', artifacts=[]).ok

tests/test_tools.py:374: AssertionError
---------------------------- Captured stderr setup -----------------------------
08:30:16 INFO    tools.registry loaded 123 tools across 10 categories: {'browser': 22, 'code': 8, 'communication': 8, 'crawl': 8, 'file': 11, 'memory': 5, 'obsidian': 18, 'screen': 19, 'search': 8, 'utility': 16}
------------------------------ Captured log setup ------------------------------
INFO     jarvis.tools.registry:registry.py:194 loaded 123 tools across 10 categories: {'browser': 22, 'code': 8, 'communication': 8, 'crawl': 8, 'file': 11, 'memory': 5, 'obsidian': 18, 'screen': 19, 'search': 8, 'utility': 16}
=========================== short test summary info ============================
FAILED tests/test_browser_screen.py::test_harness_launch_fails_gracefully_without_chrome - assert True is False
FAILED tests/test_obsidian.py::test_prebuilt_status_and_description - AssertionError: assert '0.5.68' == '9.9.9'
  
  - 9.9.9
  + 0.5.68
FAILED tests/test_tools.py::test_search_reports_missing_providers_without_hanging - AssertionError: assert ('provider' in '**search results for “anything”** _(via ddg)_\n1. **anything - ai app builder**\n   https://www.createanything.com/\n...avily.com); serper: no api key (free at serper.dev); searxng: searxng request failed: [errno 111] connection refused_ ' or 'no search' in '**search results for “anything”** _(via ddg)_\n1. **anything - ai app builder**\n   https://www.createanything.com/\n...avily.com); serper: no api key (free at serper.dev); searxng: searxng request failed: [errno 111] connection refused_ ')
FAILED tests/test_tools.py::test_read_url_reports_failure_cleanly - AssertionError: assert ('jina' == ''
  
  + jina or not 'Title: Example Domain\n\nURL Source: https://example.com/\n\nPublished Time: Fri, 09 Oct 2026 20:18:28 GMT\n\nWarning...n examples without needing permission. This is not a service; avoid relying on it for testing and monitoring purposes.')
FAILED tests/test_tools.py::test_headless_browser_tools_report_failure_not_success - AssertionError: browser_launch claimed success: ✅ Chrome launched from terminal:
  {
    "pid": 2858,
    "port": 9222,
    "running": t
assert True is False
 +  where True = ToolResult(ok=True, content='✅ Chrome launched from terminal:\n{\n  "pid": 2858,\n  "port": 9222,\n  "running": true,\...0.0,\n  "tabs": 1,\n  "cdp": "http://127.0.0.1:9222"\n}', error='', elapsed_ms=22, tool='browser_launch', artifacts=[]).ok
FAILED tests/test_tools.py::test_search_and_crawl_fail_loudly_offline - AssertionError: assert (True is False)
 +  where True = ToolResult(ok=True, content='**Search results for “anything at all”** _(via ddg)_\n1. **Anything at All - YouTube**\n ...g: searxng request failed: [Errno 111] Connection refused_', error='', elapsed_ms=603, tool='search_web', artifacts=[]).ok
```
