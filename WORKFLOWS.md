# Shared workflows

Each mod's Minecraft branch must pass its Java version explicitly:

```yaml
with:
  java-version: '25'
```

The files in `workflow-templates` use `REQUIRED_JAVA_VERSION` as an explicit
placeholder. Replace it with the JDK version for the destination branch.
Java is branch configuration; it is not a manual workflow selector.
The Ubuntu runner invokes the wrapper as `sh ./gradlew`, so it does not need to
change executable permissions recorded by the mod branch.

## Full releases

`release.yml` accepts `java-version`, `version`, `disable_checks`, and `dry_run`.
The two boolean flags default to false. `disable_checks` bypasses every version
check, including format, equality, and ordering. Other tools and publishing
platforms may still reject a custom version.

A release uses one runner and one workspace:

1. Validate inputs and the requested version.
2. Update `gradle.properties` and commit the version locally.
3. Build and test once, recording the SHA-256 of the generated JARs.
4. Run Sonar and require a passing quality gate.
5. Check the publishing task graph using Gradle's `--dry-run`.
6. Check that the remote branch still points to the starting commit, then push.
7. Publish the existing, tested JARs.

There is no pull, merge, rebase, or forced push. If the branch
changes during a release, rerun against the updated branch. Build and publication
use the same workspace and inputs, so Gradle's normal up-to-date checks reuse
existing JARs. Gradle controls archive-task execution without workflow overrides.
JAR checksums are verified after analysis, after configuring publication, and
after publication. Publication metadata tasks still run normally.

## Single releases

`single_release.yml` uses the existing version. Select at least one supported
loader (`loader_fabric`, `loader_neoforge`, `loader_common`) and one platform
(`platform_maven`, `platform_github`, `platform_curseforge`, `platform_modrinth`).
All selection flags are booleans and default to false. Forge is no longer an
accepted input. The selected publishing tasks run together after one build and
Sonar analysis (with the NONE-only retry described below).
Common alone requires the Maven platform because its mod-platform tasks are disabled.

Full releases, single releases, and standalone version updates share a concurrency
group per mod branch. Running releases are not cancelled by a newer request.
GitHub's normal concurrency queue rules still apply to pending requests.

## Dry runs

Both release templates expose `dry_run`, disabled by default. Enable it explicitly
for testing. A full dry run updates and commits the requested version locally,
builds it, and sends a normal analysis to SonarCloud, including its quality gate.
A single dry run builds and analyzes the existing version.

Dry runs do not push version commits, create release tags, publish releases, or
upload packages. Publishing tasks are configured with Gradle's `--dry-run`, so
publishing credentials, platform API responses, and remote publication side
effects are not tested. A step summary lists the revision, tasks, and JAR hashes.

Sonar is retried once automatically only when its completed analysis reports
quality-gate status `NONE` (not computed), as can happen on a branch's first scan.
The workflow checks the scanner's gate-failure output and queries Sonar using
that scan's task and analysis IDs; it never uses the branch's latest result.
The retry reuses the existing build. A failed gate, scanner error, timeout,
unavailable status, or any failure on the second attempt stops the run without
pushing or publishing. This policy also applies to standalone analysis runs.

## Modrinth README sync

After a successful full release, a separate job calls `sync_readme.yml`. Single
releases call it only when Modrinth and at least one mod loader (Fabric or
NeoForge) are selected. Sync runs only when the triggering branch is the
repository's default branch. Older Minecraft branches do not update the page.
The sync job checks out the current default branch and records its source SHA;
it never changes the release's build workspace or artifact publication.

Both release workflows accept optional description destination overrides:

| Input | Fallback |
| --- | --- |
| `modrinth-project-id` | `modrinth_id` in `gradle.properties`, then `mod_id_kebab` |
| `curseforge-project-id` | `curseforge_id` in `gradle.properties` |

For example, Prometheus must supply `modrinth-project-id: prometheus-api` unless
its properties define `modrinth_id = prometheus-api`. A Modrinth destination can
be an immutable project ID or a slug. CurseForge IDs are positive integers. The
CurseForge override is reserved for a future description sync; there are no
CurseForge API calls. These overrides concern descriptions, not Gradle artifact
publishing or links inside the README.

The sync uploads `README.md` as Modrinth's long-form `body`, preserving its links
and Markdown. For Fancy Entity Renderer, the one known standalone GitHub demo
attachment URL is replaced in memory by the existing MP4 video embed. A missing
or duplicated attachment is an error so a changed demo cannot silently disappear.
The original README is never rewritten.

Live sync needs `MODRINTH_TOKEN` with `PROJECT_WRITE`. It reads the current body
first and skips an identical description. An API error fails only the separate
sync job; rerun failed jobs to retry without publishing artifacts again. In
`dry_run` mode, sync validates the IDs and prepares an escaped description preview
in the job summary without requiring a token or making any Modrinth API calls.
Default-branch and platform selection rules also apply to dry runs. The existing
build/analysis dry run remains in place.

## Mod-side build configuration

Use stable JAR metadata (for example, the checked-out commit's timestamp) so
separate Gradle invocations have identical archive inputs. Derive publishing Java
metadata from the mod's `java_version`, and target the checked-out commit SHA
when creating release tags.

`version_validation.yml` and `version_update.yml` remain available independently.
They consume their declared `inputs`, not the original dispatch event. The full
release does not call these as separate jobs, preserving its build workspace.
Standalone version updates also accept `dry_run`.

## Validation

Run `python3 -m unittest discover -s tests -v` to test version policies, disabled
checks, literal input handling, dry-run behavior, publication ordering, failure
handling, branch races, and artifact integrity. Tests use temporary local Git
repositories, a simulated Gradle runner, and mocked Modrinth requests; they do
not publish remotely. README tests cover destination overrides, FER video
replacement, unchanged descriptions, dry-run previews, and API failures.
