# muster

It doesn't run tests. After a test run it reads the reports any stack produces (coverage, test results, mutants) and checks them against the repo's policy: critical zones covered thoroughly, the suite inside its time budget, and test code growing in proportion to production code rather than past it. It reports the result in the CI summary, as a warning or, in strict mode, as a failed build, and flags every change to the policy itself so it is visible at merge time, whichever agent made it.

## Status

Pre-release. Nothing is published yet: no release, no Action tag, no `.pyz`. Everything below describes what 0.1 is planned to ship, and it may change until that release.

## What it reads (0.1)

| Report | Format |
|---|---|
| Coverage | JaCoCo XML |
| Mutants | PIT `mutations.xml` |
| Test results | JUnit XML |

The repository describes itself in a `muster.toml` profile: source roots, zones, thresholds and report paths. muster needs Python 3.11 or newer and nothing else.

## Ways to run it (planned)

None of these works before the 0.1 release.

1. **GitHub Action.** Pin an exact release tag; release tags are immutable, and Dependabot can bump the pin:

   ```yaml
   - uses: sharpbitstudio/muster@v0.1.0
   ```

   There is no moving major tag before 1.0. The Action runs its own source and uses the Python already on the runner.

2. **Release `.pyz`.** Each release carries `muster.pyz` and `muster.pyz.sha256`. Download both, verify the checksum, then run the archive:

   ```sh
   sha256sum -c muster.pyz.sha256
   python3 -I muster.pyz check --base origin/main
   ```

3. **Per-repo bootstrap.** A small standard-library script committed to the repository reads the pinned version and SHA-256 from `muster.toml`, downloads that release's `muster.pyz` into a per-user cache, verifies the SHA-256 on every run and then runs it. No setting skips the checksum.

## Documentation

- [POLICY.md](POLICY.md): the testing policy muster checks.
- [docs/](docs/): configuration, report formats, exit codes and JSON output. It arrives with 0.1.

## License

Apache License 2.0. See [LICENSE](LICENSE).
