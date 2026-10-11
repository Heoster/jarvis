# Pull request

## What changed

<!-- one or two lines, in plain language -->

## Why

<!-- the request or bug this answers -->

## Checklist

- [ ] `python -m pytest tests/ -q` passes locally
- [ ] `python -m jarvis doctor` still reports every category as ready/optional
- [ ] `ruff check --select F821,F822,F811,F823,F841,B023,B904,E713,E9 .` is clean
- [ ] New tools are registered and appear in `python -m jarvis tools`
- [ ] Nothing here needs a paid API, a paid plan or a GPU
- [ ] Docs touched when behaviour changed (`README.md`, `knowledge/`)

## Verification

<!-- paste the commands you actually ran and what they printed -->
