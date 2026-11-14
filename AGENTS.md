# Contributing

This is the entry point for anyone who changes this repository, whether a person or a coding agent.

muster reads test reports after a test run and checks them against a testing policy ([POLICY.md](POLICY.md)). The project is pre-release: the tool, its tests and `docs/` arrive with 0.1.

## Code

- Python 3.11 or newer, standard library only. No third-party packages, at runtime or in tests.
- Code runs on Linux and Windows.
- Text files are plain ASCII with LF line endings.

## Tests

```sh
python -m unittest discover -s tests -t . -v
```

The tests arrive together with the tool.

Fixtures are synthetic; never copy names, reports or paths from another project.

## Checks

CI runs three checks on every branch and pull request:

- **gitleaks** over the full history and the working tree;
- **hygiene**, `python tools/hygiene.py`, over the tree and over the commits of the change; run it locally before committing, and with `--range origin/main..HEAD` before pushing;
- **identity**: every commit must carry the project's commit identity.

## Commits and pull requests

- Subject line in the imperative mood, at most 72 characters.
- A blank line, then a body that explains why the change is needed.
- No AI trailers and no AI attribution lines in commit messages or pull request descriptions.
- Pull requests follow the template: What, Why, How verified.

## No scanner suppression

The repository has no mechanism that silences a scanner:

- no `.gitleaksignore` file, anywhere;
- no inline gitleaks suppression markers;
- no edits to `.gitleaks.toml` or `.gitattributes`; both are pinned by hash.

If a scanner reports something, remove the finding from the change. Do not suppress it.
