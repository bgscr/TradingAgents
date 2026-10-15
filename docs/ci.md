# Personal CI and manual compatibility checks

[CI](../.github/workflows/ci.yml) has one workflow. Automatic pushes to `main`
and pull requests validate the owner's Windows runtime, **Python 3.13.7**, verified
from the project `.venv`. No Linux or other Python version runs automatically.
When that local runtime changes, update the daily test, clean-install and package
lanes together. This exact patch pin does not follow later Python patches.

| Coverage | Automatic push / PR | Manual default | Manual full compatibility |
| --- | --- | --- | --- |
| Windows full pytest, offline cold bootstrap and full-repo Ruff | Python 3.13.7 | Same, once | Same, once |
| Windows clean package install/imports, without dev extras | Python 3.13.7, separate fresh runner | Same | Same |
| Windows launcher / PyInstaller / packaged CLI smoke | Python 3.13.7 | Same | Same |
| Additional full pytest + offline cold bootstrap | None | None | Linux 3.10 / 3.11 / 3.12 / 3.13; Windows 3.12 |
| Minimum dependency versions, full pytest + Ruff floor | None | None | Linux Python 3.13 |
| Linux clean package install/imports, without dev extras | None | None | Python 3.12, separate fresh runner |
| **Executed jobs** | **3** | **3** | **10** |

The minimum-dependency job is skipped unless full compatibility is explicitly
selected; that is not a test pass. Previously each automatic event ran nine jobs: five full-test
lanes, minimum dependencies, clean install, Windows packaging and standalone
Ruff. Ruff now runs once inside the daily Windows test job, using the same
`ruff>=0.15` dev dependency. The complete Windows pytest command remains intact;
`pytest -m windows` or packaging smoke would miss tests that use platform or
PowerShell availability checks instead of a Windows marker.

This is a personal-use validation policy, not a reduction of the declared
`requires-python >=3.10` compatibility claim. Routine CI no longer validates
Linux, older interpreters or dependency floors. Use manual compatibility checks
before distributing a release, changing that compatibility claim, or making
changes that need those environments (including dependencies and CI coverage).
Keep change-related tests; a passing Windows run cannot establish Linux or older
Python compatibility. Manual full mode retains the former compatibility lanes plus
the new personal Windows runtime, and does not run a second daily matrix.

After the workflow is on the default branch and Actions is enabled, select
**Actions → CI → Run workflow**, select **main**, and leave **full_compatibility**
unchecked (the default is **false**) to run only the three Windows jobs. Check
**full_compatibility** only when you intentionally want all ten compatibility
jobs, including Linux and other Python versions. The typed boolean input is
checked only for a manual event; automatic events always use the personal mode.
Verify the run's exact commit, event, branch, attempt and the three or ten executed
job results for the chosen mode. A manual run on a different SHA does not validate a PR merge
commit. Do not dispatch redundant runs solely because multiple branches contain
the same commit.

The `test`, `smoke-install`, `minimum-compatibility` and `windows-package-smoke`
job IDs remain. The clean-install and package check names remain; clean-install
now validates Windows by default. The former test lane names occur only in manual
full mode, `minimum dependencies (py3.13)` is full-mode-only, and the standalone
`ruff (strict, full repo)` check is replaced by the Windows test job's lint step.
Before publishing, a maintainer must verify the actual required-check policy is
compatible with these changed/absent/skipped check contexts. Do not change branch
protections or treat a skipped compatibility check as qualification. If an old
check is required, resolve that policy explicitly before adopting this workflow.

Pushes still trigger only for `main`; PR triggers have no broad path filters.
There is no automatic development-branch push copy. Existing per-ref concurrency
still cancels superseded runs, now with separate automatic/manual groups so they
cannot cancel each other. It is not a durable same-SHA result cache: completed
reruns or different refs can run again. A PR merge commit and a main commit can
test different trees even with the same PR head SHA. See
[GitHub's concurrency semantics](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

The full-test and minimum-dependency jobs cache only pip downloads/wheels, keyed
by interpreter/platform and the dependency/workflow files. Every run still
installs dependencies and executes its checks. The clean-install and packaged
release jobs remain uncached and use separate runners; no dev environment is
reused for clean-install evidence. Floating versions can limit cache reuse; see
[setup-python caching](https://github.com/actions/setup-python#caching-packages-dependencies).
There are no artifact uploads or workflow-specific artifact retention settings.
Repository log/artifact retention, permissions and hosting settings are unchanged.

For historical scale, successful [run 38599281216](https://github.com/bgscr/TradingAgents/actions/runs/38599281216)
tested `e55df7dd3946980f27635809602e374d345dffa1`: nine jobs totalled 123m46s of
elapsed job time, with Windows full tests the longest job at 34m48s. The five
Linux full-test/dependency-floor jobs totalled 82m28s and now run only on demand.
These are observed historical durations, not billed minutes, a savings percentage
or a prediction for Windows Python 3.13.7. Windows remains a possible wall-clock
bottleneck. The records do not establish why GitHub paused Actions or qualify
any newer commit as CI-passing.

Local workflow checks validate YAML, event/matrix expansion and retained commands.
They do not run hosted installs, full suites or packaged Windows Python 3.13.7.
Formal Windows qualification and real provider acceptance remain separately
authorized activities; no such launch or credential-based diagnostic is added
to CI here.
