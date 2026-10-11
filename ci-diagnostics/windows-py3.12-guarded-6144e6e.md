# pytest Â· windows-py3.12 Â· guarded Â· 6144e6ede1657477b18cd4b7c73983ed1399795a

- runner: Windows
- python: Python 3.12.10
- job: tests-windows

## installed

```
anyio==4.15.1
argcomplete==3.7.2
certifi==2026.7.22
click==8.5.0
colorama==0.4.6
filelock==4.0.3
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.20
iniconfig==2.3.1
-e git+https://github.com/heostertech-cyber/jarvis@6144e6ede1657477b18cd4b7c73983ed1399795a#egg=jarvis_operator
markdown-it-py==4.2.0
mdurl==0.1.2
packaging==26.3
pipx==1.17.6
platformdirs==4.11.14
pluggy==1.6.0
Pygments==2.21.0
pytest==9.1.1
pytest-asyncio==1.4.0
PyYAML==6.0.3
rich==15.0.0
ruff==0.17.0
typing_extensions==4.16.0
userpath==1.9.2
```

## output (tail)

```
........................................F.F.F........................... [ 40%]
.................................F.F....F............................... [ 80%]
......................F........F...                                      [100%]
================================== FAILURES ===================================
_________ test_write_brief_is_idempotent_and_lands_in_the_daily_note __________

reviewed_vault = WindowsPath('C:/Users/runneradmin/AppData/Local/Temp/pytest-of-runneradmin/pytest-0/test_write_brief_is_idempotent0/Vault')
cfg = {'obsidian': {'vault_path': 'C:\\Users\\runneradmin\\AppData\\Local\\Temp\\pytest-of-runneradmin\\pytest-0\\test_write...ily_notes': {'folder': '02 Daily Notes', 'format': '%Y-%m-%d', 'template': ''}, 'study': {'session_minutes': 45}, ...}}

    def test_write_brief_is_idempotent_and_lands_in_the_daily_note(reviewed_vault, cfg):
        first = coach_module.write_brief(reviewed_vault, cfg, day=TODAY)
        second = coach_module.write_brief(reviewed_vault, cfg, day=TODAY)
        note = Path(second["path"]).read_text(encoding="utf-8")
    
>       assert first["note"] == f"02 Daily Notes/{TODAY.isoformat()}.md"
E       AssertionError: assert '02 Daily Not...2026-10-11.md' == '02 Daily Notes/2026-10-11.md'
E         
E         - 02 Daily Notes/2026-10-11.md
E         ?               ^
E         + 02 Daily Notes\2026-10-11.md
E         ?               ^

tests\test_coach.py:176: AssertionError
___________________________ test_weekly_review_note ___________________________

reviewed_vault = WindowsPath('C:/Users/runneradmin/AppData/Local/Temp/pytest-of-runneradmin/pytest-0/test_weekly_review_note0/Vault')
cfg = {'obsidian': {'vault_path': 'C:\\Users\\runneradmin\\AppData\\Local\\Temp\\pytest-of-runneradmin\\pytest-0\\test_weekl...ily_notes': {'folder': '02 Daily Notes', 'format': '%Y-%m-%d', 'template': ''}, 'study': {'session_minutes': 45}, ...}}

    def test_weekly_review_note(reviewed_vault, cfg):
        result = coach_module.weekly_review(reviewed_vault, cfg, day=TODAY)
        text = Path(result["path"]).read_text(encoding="utf-8")
>       assert result["note"].startswith("05 Revision/Weekly review — ")
E       AssertionError: assert False
E        +  where False = <built-in method startswith of str object at 0x000001A3CF81D980>('05 Revision/Weekly review — ')
E        +    where <built-in method startswith of str object at 0x000001A3CF81D980> = '05 Revision\\Weekly review — 2026-10-11.md'.startswith

tests\test_coach.py:194: AssertionError
______________ test_run_once_writes_the_weekly_review_on_sundays ______________

reviewed_vault = WindowsPath('C:/Users/runneradmin/AppData/Local/Temp/pytest-of-runneradmin/pytest-0/test_run_once_writes_the_weekl0/Vault')
cfg = {'obsidian': {'vault_path': 'C:\\Users\\runneradmin\\AppData\\Local\\Temp\\pytest-of-runneradmin\\pytest-0\\test_run_o...ily_notes': {'folder': '02 Daily Notes', 'format': '%Y-%m-%d', 'template': ''}, 'study': {'session_minutes': 45}, ...}}

    def test_run_once_writes_the_weekly_review_on_sundays(reviewed_vault, cfg):
        sunday = next(day for day in (TODAY + timedelta(days=offset) for offset in range(7))
                      if day.weekday() == 6)
        run = coach_module.run_once(reviewed_vault, cfg, day=sunday, weekly=True)
>       assert run.weekly.startswith("05 Revision/Weekly review")
E       AssertionError: assert False
E        +  where False = <built-in method startswith of str object at 0x000001A3CF81D860>('05 Revision/Weekly review')
E        +    where <built-in method startswith of str object at 0x000001A3CF81D860> = '05 Revision\\Weekly review — 2026-10-11.md'.startswith
E        +      where '05 Revision\\Weekly review — 2026-10-11.md' = CoachRun(day='2026-10-11', headline='3 card(s) due · 1 task(s) overdue — weakest: Physics / Optics (43% retention) · B...es\\2026-10-11.md', notified='', spoken='', weekly='05 Revision\\Weekly review — 2026-10-11.md', at='2026-10-11T08:23').weekly

tests\test_coach.py:215: AssertionError
---------------------------- Captured stderr call -----------------------------
08:23:28 INFO    tools.obsidian.coach brief written to 02 Daily Notes\2026-10-11.md
08:23:28 INFO    tools.obsidian.coach weekly review written to 05 Revision\Weekly review — 2026-10-11.md
------------------------------ Captured log call ------------------------------
INFO     jarvis.tools.obsidian.coach:coach.py:538 brief written to 02 Daily Notes\2026-10-11.md
INFO     jarvis.tools.obsidian.coach:coach.py:543 weekly review written to 05 Revision\Weekly review — 2026-10-11.md
__________ test_note_parsing_reads_frontmatter_tags_links_and_title ___________

vault = WindowsPath('C:/Users/runneradmin/AppData/Local/Temp/pytest-of-runneradmin/pytest-0/test_note_parsing_reads_frontm0/Vault')

    def test_note_parsing_reads_frontmatter_tags_links_and_title(vault):
        note = vault_module.read_note(vault, "Newton's Laws")
        assert note.title == "Newton's Laws of Motion"
>       assert note.rel == "01 Subjects/Physics/Newton's Laws.md"
E       assert "01 Subjects\...ton's Laws.md" == "01 Subjects/...ton's Laws.md"
E         
E         - 01 Subjects/Physics/Newton's Laws.md
E         ?            ^       ^
E         + 01 Subjects\Physics\Newton's Laws.md
E         ?            ^       ^

tests\test_obsidian.py:80: AssertionError
_____________________ test_write_read_append_and_prepend ______________________

vault = WindowsPath('C:/Users/runneradmin/AppData/Local/Temp/pytest-of-runneradmin/pytest-0/test_write_read_append_and_pre0/Vault')

    def test_write_read_append_and_prepend(vault):
        first = vault_module.write_note(vault, "01 Subjects/Physics/Unit 1", "Line A",
                                        tags=["physics"])
>       assert first.rel == "01 Subjects/Physics/Unit 1.md"       # no folder doubling
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E       AssertionError: assert '01 Subjects\...cs\\Unit 1.md' == '01 Subjects/...ics/Unit 1.md'
E         
E         - 01 Subjects/Physics/Unit 1.md
E         ?            ^       ^
E         + 01 Subjects\Physics\Unit 1.md
E         ?            ^       ^

tests\test_obsidian.py:98: AssertionError
_____________________ test_daily_note_creation_and_append _____________________

vault = WindowsPath('C:/Users/runneradmin/AppData/Local/Temp/pytest-of-runneradmin/pytest-0/test_daily_note_creation_and_a0/Vault')
config = {'jarvis': {'version': '1.0.0', 'name': 'JARVIS', 'operator': 'Heoster', 'company': 'CODEEX-AI', ...}, 'llm': {'provid... {'max_budget': 14000, 'system_prompt_budget': 2000, 'tool_definitions_budget': 2500, 'memory_budget': 3500, ...}, ...}

    def test_daily_note_creation_and_append(vault, config):
        note = vault_module.daily_note(vault, day=date(2026, 1, 5), config=config)
>       assert note.rel == "02 Daily Notes/2026-01-05.md"
E       AssertionError: assert '02 Daily Not...2026-01-05.md' == '02 Daily Notes/2026-01-05.md'
E         
E         - 02 Daily Notes/2026-01-05.md
E         ?               ^
E         + 02 Daily Notes\2026-01-05.md
E         ?               ^

tests\test_obsidian.py:149: AssertionError
______________________ test_datetime_uses_ist_by_default ______________________

config = {'jarvis': {'version': '1.0.0', 'name': 'JARVIS', 'operator': 'Heoster', 'company': 'CODEEX-AI', ...}, 'llm': {'provid... {'max_budget': 14000, 'system_prompt_budget': 2000, 'tool_definitions_budget': 2500, 'memory_budget': 3500, ...}, ...}
registry = <jarvis.tools.registry.ToolRegistry object at 0x000001A3CF830E90>

    def test_datetime_uses_ist_by_default(config, registry):
        result = registry.execute_sync("get_current_datetime", {})
>       assert result.ok
E       assert False
E        +  where False = ToolResult(ok=False, content='', data=None, error="ZoneInfoNotFoundError: 'No time zone found with key UTC'", elapsed_ms=0, tool='get_current_datetime', artifacts=[]).ok

tests\test_tools.py:225: AssertionError
---------------------------- Captured stderr setup ----------------------------
08:24:04 INFO    tools.registry loaded 123 tools across 10 categories: {'browser': 22, 'code': 8, 'communication': 8, 'crawl': 8, 'file': 11, 'memory': 5, 'obsidian': 18, 'screen': 19, 'search': 8, 'utility': 16}
----------------------------- Captured log setup ------------------------------
INFO     jarvis.tools.registry:registry.py:194 loaded 123 tools across 10 categories: {'browser': 22, 'code': 8, 'communication': 8, 'crawl': 8, 'file': 11, 'memory': 5, 'obsidian': 18, 'screen': 19, 'search': 8, 'utility': 16}
---------------------------- Captured stderr call -----------------------------
08:24:04 WARNING tools tool get_current_datetime failed: ZoneInfoNotFoundError: 'No time zone found with key UTC'
------------------------------ Captured log call ------------------------------
WARNING  jarvis.tools:base.py:218 tool get_current_datetime failed: ZoneInfoNotFoundError: 'No time zone found with key UTC'
____________ test_headless_screen_tools_report_failure_not_success ____________

registry = <jarvis.tools.registry.ToolRegistry object at 0x000001A3CF8D4BC0>

    def test_headless_screen_tools_report_failure_not_success(registry):
        """On a box with no display, screen tools must say ok=False, not "\u2705"."""
        for name, args in [("take_desktop_screenshot", {}),
                           ("list_screen_monitors", {}),
                           ("read_screen_text", {}),
                           ("find_on_screen", {"text": "Save"}),
                           ("screen_click_text", {"text": "Save"}),
                           ("cursor_move", {"x": 5, "y": 5}),
                           ("cursor_click", {"x": 5, "y": 5}),
                           ("keyboard_type", {"text": "hi"}),
                           ("keyboard_press", {"key": "enter"}),
                           ("screen_scroll", {}),
                           ("screen_list_windows", {}),
                           ("screen_focus_window", {"title": "nothing-here"}),
                           ("screen_active_window", {}),
                           ("launch_application", {"name": "definitely-not-installed"})]:
            result = registry.execute_sync(name, args)
>           assert result.ok is False, f"{name} claimed success: {result.content[:80]}"
E           AssertionError: list_screen_monitors claimed success: all monitors: 1024×768 at (0, 0)
E             monitor 1: 1024×768 at (0, 0)
E           assert True is False
E            +  where True = ToolResult(ok=True, content='all monitors: 1024×768 at (0, 0)\nmonitor 1: 1024×768 at (0, 0)', data='all monitors: 1024×768 at (0, 0)\nmonitor 1: 1024×768 at (0, 0)', error='', elapsed_ms=591, tool='list_screen_monitors', artifacts=[]).ok

tests\test_tools.py:329: AssertionError
---------------------------- Captured stderr setup ----------------------------
08:24:17 INFO    tools.registry loaded 123 tools across 10 categories: {'browser': 22, 'code': 8, 'communication': 8, 'crawl': 8, 'file': 11, 'memory': 5, 'obsidian': 18, 'screen': 19, 'search': 8, 'utility': 16}
----------------------------- Captured log setup ------------------------------
INFO     jarvis.tools.registry:registry.py:194 loaded 123 tools across 10 categories: {'browser': 22, 'code': 8, 'communication': 8, 'crawl': 8, 'file': 11, 'memory': 5, 'obsidian': 18, 'screen': 19, 'search': 8, 'utility': 16}
=========================== short test summary info ===========================
FAILED tests/test_coach.py::test_write_brief_is_idempotent_and_lands_in_the_daily_note - AssertionError: assert '02 Daily Not...2026-10-11.md' == '02 Daily Notes/2026-10-11.md'
  
  - 02 Daily Notes/2026-10-11.md
  ?               ^
  + 02 Daily Notes\2026-10-11.md
  ?               ^
FAILED tests/test_coach.py::test_weekly_review_note - AssertionError: assert False
 +  where False = <built-in method startswith of str object at 0x000001A3CF81D980>('05 Revision/Weekly review — ')
 +    where <built-in method startswith of str object at 0x000001A3CF81D980> = '05 Revision\\Weekly review — 2026-10-11.md'.startswith
FAILED tests/test_coach.py::test_run_once_writes_the_weekly_review_on_sundays - AssertionError: assert False
 +  where False = <built-in method startswith of str object at 0x000001A3CF81D860>('05 Revision/Weekly review')
 +    where <built-in method startswith of str object at 0x000001A3CF81D860> = '05 Revision\\Weekly review — 2026-10-11.md'.startswith
 +      where '05 Revision\\Weekly review — 2026-10-11.md' = CoachRun(day='2026-10-11', headline='3 card(s) due · 1 task(s) overdue — weakest: Physics / Optics (43% retention) · B...es\\2026-10-11.md', notified='', spoken='', weekly='05 Revision\\Weekly review — 2026-10-11.md', at='2026-10-11T08:23').weekly
FAILED tests/test_obsidian.py::test_note_parsing_reads_frontmatter_tags_links_and_title - assert "01 Subjects\...ton's Laws.md" == "01 Subjects/...ton's Laws.md"
  
  - 01 Subjects/Physics/Newton's Laws.md
  ?            ^       ^
  + 01 Subjects\Physics\Newton's Laws.md
  ?            ^       ^
FAILED tests/test_obsidian.py::test_write_read_append_and_prepend - AssertionError: assert '01 Subjects\...cs\\Unit 1.md' == '01 Subjects/...ics/Unit 1.md'
  
  - 01 Subjects/Physics/Unit 1.md
  ?            ^       ^
  + 01 Subjects\Physics\Unit 1.md
  ?            ^       ^
FAILED tests/test_obsidian.py::test_daily_note_creation_and_append - AssertionError: assert '02 Daily Not...2026-01-05.md' == '02 Daily Notes/2026-01-05.md'
  
  - 02 Daily Notes/2026-01-05.md
  ?               ^
  + 02 Daily Notes\2026-01-05.md
  ?               ^
FAILED tests/test_tools.py::test_datetime_uses_ist_by_default - assert False
 +  where False = ToolResult(ok=False, content='', data=None, error="ZoneInfoNotFoundError: 'No time zone found with key UTC'", elapsed_ms=0, tool='get_current_datetime', artifacts=[]).ok
FAILED tests/test_tools.py::test_headless_screen_tools_report_failure_not_success - AssertionError: list_screen_monitors claimed success: all monitors: 1024×768 at (0, 0)
  monitor 1: 1024×768 at (0, 0)
assert True is False
 +  where True = ToolResult(ok=True, content='all monitors: 1024×768 at (0, 0)\nmonitor 1: 1024×768 at (0, 0)', data='all monitors: 1024×768 at (0, 0)\nmonitor 1: 1024×768 at (0, 0)', error='', elapsed_ms=591, tool='list_screen_monitors', artifacts=[]).ok
```
