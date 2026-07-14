# Contributing to claude-pop-quiz

Thanks for considering a contribution. Issues, discussions and PRs are all
welcome.

## Getting set up

```
git clone https://github.com/jay739/claude-pop-quiz && cd claude-pop-quiz
./install.sh   # installs the hook into ~/.claude
```

Python 3.6+ only, no third-party dependencies.

## Running the checks

```
python3 -m pytest tests -q
```

## Ground rules

- Open an issue or discussion before large changes, so effort isn't wasted.
- Keep PRs focused: one change per PR.
- New behavior needs a test where the repo has a test suite.
- CI must pass before merge.

## Licensing

By contributing you agree your contributions are licensed under the repo's
MIT license.
