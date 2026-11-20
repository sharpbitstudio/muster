# Testing policy

This is the policy muster checks. It is the same for every repository; each repository fits it to its own code through its profile (point 12). Where a point names a threshold, the value comes from that profile.

## 1. Test a behaviour once, at the lowest level that can see it

A behaviour gets one test, on the lowest level that observes it: unit before integration, integration before end-to-end. A higher level proves that the parts are wired together, protected and mapped correctly. It does not repeat the branches a lower level already covers.

## 2. Critical zones thoroughly, the rest minimally

A critical zone is code where a defect is expensive: authentication and authorization, personal data, secrets, money, core domain rules, delivery guarantees.

- In a critical zone, every branch of a state machine or a policy is tested at unit level, every access rule has its negative case, and concurrency or idempotency is proven once at the level that can see it.
- Everywhere else, one happy path per entry point is enough, plus unit tests for non-trivial functions. Tests that repeat an assertion another level already makes, tests of accessors and plain data types, tests of framework behaviour and tests of log lines are not written.

## 3. Every source file is classified

Each source file belongs to a critical zone or to a standard package. The zones and the standard packages are lists of globs in the repository. A file that no list covers fails the check. So do a glob that matches nothing, a file in two zones, and a standard entry that matches only critical files. When a file matches both lists, critical wins. Adding a new package means adding it to one of the lists; existing globs are not widened to avoid that step.

## 4. A global floor and a delta gate on critical lines

Coverage is a floor for critical zones, never a goal of its own.

- **Floor:** line and branch coverage of the whole code base stays above a global minimum.
- **Delta:** lines changed in critical files since the merge base must be covered above a stricter threshold. Standard packages have no delta gate.
- **Exemptions:** an exemption names exactly one source file, which must be critical, with a reason and an expiry date at most 30 days ahead. An expired exemption fails the check on its own, without any code change. The fix is to extend it, with a new reason, or to remove it.

## 5. A duration budget with warn and error thresholds

The wall time of the test suite is compared with two thresholds. Above the warning threshold the run gets a warning; above the error threshold it gets an error. A failed or interrupted run has no duration verdict. A test that noticeably lengthens the suite needs a reason in review.

## 6. Mutation testing over an allowlist

Mutation testing runs over an allowlist of pure-logic files, each with its own unit test. The measure is test strength: detected mutants over mutants that tests reach.

- **Threshold:** below it the check reports a violation.
- **Target:** the level the repository aims for.
- **Known variation:** mutants whose outcome changes between identical runs are listed in the profile, one by one, so they do not produce noise. Any other change is real.

Renaming or deleting a listed file or its unit test updates the allowlist in the same change.

## 7. The test-to-main ratio is a metric; the per-change budget is about 1x

The ratio of test lines to production lines is reported as a trend and is never a gate. It must not become a reason to under-test.

Per change, added test lines should stay at about one times the production lines the change adds, with a small grace allowance. A change over budget is not a failure. It needs a written justification inside the change itself, for example a commit trailer.

## 8. Removing tests is proven with coverage-diff and mutation-diff

Tests are removed one method at a time, never by file or by count, and only when they duplicate a check that stays on the same or a lower level. The change shows its evidence:

- `coverage-diff`: per-file coverage of two full runs, before and after. A critical file does not lose a covered line without an explanation;
- `mutation-diff`: mutation results before and after, when the change touches unit tests of allowlisted files.

Refactoring shared fixtures does not change what a test checks: test bodies, assertions and inputs stay the same.

## 9. A bug fix carries a test that fails before the fix

Every bug fix adds a regression test that fails without the fix and passes with it.

## 10. Policy changes are visible at merge

Any change to the policy files or to the profile is reported as a policy change: zones, standard packages, exemptions, mutation targets, thresholds, exclusions and known variation. Changes that weaken the policy, such as a lowered threshold, a new exemption, a file leaving a critical zone or a newly excluded path, are flagged as weakening. The decision is made at merge time, by whoever reviews the change.

## 11. Warn-only first, strict later

Every check starts warn-only: it reports but does not fail the build. A repository makes a check strict, so that a violation fails the build, once that check has been stable on its main branch. Strict is a choice made by the caller, for example the CI workflow, not a setting inside the profile.

Warn-only covers findings measured against a valid policy. An invalid policy, such as an unclassified file or a dead glob (point 3) or an expired exemption (point 4), always fails the check, strict or not: nothing can be judged against it.

## 12. The repository profile carries zones, thresholds and deviations

The policy text is generic. Everything specific to a repository lives in its profile: where the sources and reports are, the zone and package lists, exemptions, the mutation allowlist, thresholds, budgets and any deliberate deviation from the defaults. Changing the profile is itself a policy change (point 10).
