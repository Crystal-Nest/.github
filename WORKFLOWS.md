# Shared workflows

Each mod's Minecraft branch must pass its Java version explicitly:

```yaml
with:
  java-version: '25'
```

The files in `workflow-templates` use `REQUIRED_JAVA_VERSION` as an explicit
placeholder. Replace it with the JDK version for the destination branch.
Java is branch configuration; it is not a manual workflow selector.

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

There is no pull, merge, rebase, forced push, or automatic retry. If the branch
changes during a release, rerun against the updated branch. The publication
initialization script verifies and reuses existing archive outputs; it fails if a
required archive was not built or its checksum changed. Non-archive tasks such
as publication metadata generation still run normally.

## Single releases

`single_release.yml` uses the existing version. Select at least one supported
loader (`loader_fabric`, `loader_neoforge`, `loader_common`) and one platform
(`platform_maven`, `platform_github`, `platform_curseforge`, `platform_modrinth`).
All selection flags are booleans and default to false. Forge is no longer an
accepted input. The selected publishing tasks run together after one build and
one Sonar analysis.
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

Sonar failures always stop the run, including the known first-branch case where
Sonar has no computed quality gate. Retrying is always manual.

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
repositories and a simulated Gradle runner; they do not publish remotely.
