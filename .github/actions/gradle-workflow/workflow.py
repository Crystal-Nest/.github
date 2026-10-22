"""Release orchestration shared by the reusable workflows (Python standard library only)."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:-(alpha|beta))?")
PROPERTY = re.compile(r"^(\s*mod_version\s*[=:][ \t]*)([^\r\n]*)(\r?\n|$)", re.MULTILINE)
LOADERS = ("fabric", "neoforge", "common")
PLATFORMS = {"maven": "publish", "github": "publishGitHub", "curseforge": "publishCurseforge", "modrinth": "publishModrinth"}


def flag(name):
    value = os.environ["CN_" + name.upper()]
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be a boolean, received {value!r}")
    return value == "true"


def run(args, *, capture=False, env=None, record=False):
    print("+ " + " ".join(map(str, args)), flush=True)
    if record:
        # Keep Sonar output visible while retaining the failure reason. A timeout
        # or scanner error must never be mistaken for a completed gate of NONE.
        with subprocess.Popen(args, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env) as process:
            lines = []
            for line in process.stdout:
                print(line, end="", flush=True)
                lines.append(line)
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, args, output="".join(lines))
        return
    result = subprocess.run(args, check=True, text=True, encoding="utf-8", stdout=subprocess.PIPE if capture else None, env=env)
    return result.stdout.strip() if capture else None


def git(*args, capture=False):
    return run(["git", *args], capture=capture)


def property_text():
    text = Path("gradle.properties").read_bytes().decode("utf-8")
    matches = list(PROPERTY.finditer(text))
    if len(matches) != 1:
        raise ValueError("Expected exactly one mod_version property in gradle.properties")
    return text, matches[0]


def unescape(value):
    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", lambda m: chr(int(m[1][1:], 16)) if m[1].startswith("u") and len(m[1]) == 5 else {"n": "\n", "r": "\r", "t": "\t", "f": "\f"}.get(m[1], m[1]), value)


def escape(value):
    value = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t").replace("\f", "\\f")
    # Preserve leading spaces when Java reads the property.
    return re.sub(r"^ +", lambda m: "\\ " * len(m[0]), value)


def validate_version(current, requested, disabled):
    if disabled:
        print("All version checks are disabled, including format validation.")
        return
    new = VERSION.fullmatch(requested)
    old = VERSION.fullmatch(current)
    if not new:
        raise ValueError("Invalid version format; expected number.number.number[-alpha|-beta]")
    if not old:
        raise ValueError("Current version cannot be compared; use disable_checks to override")
    if requested == current:
        raise ValueError("Version did not change")
    new_numbers = tuple(map(int, new.groups()[:3]))
    old_numbers = tuple(map(int, old.groups()[:3]))
    ranks = {"alpha": 0, "beta": 1, None: 2}
    if new_numbers < old_numbers or (new_numbers == old_numbers and ranks[new[4]] < ranks[old[4]]):
        raise ValueError("Version cannot be decreased")


def publishing_tasks(operation):
    if operation == "release":
        return ["publishAll"]
    loaders = [loader for loader in LOADERS if flag("loader_" + loader)]
    platforms = [platform for platform in PLATFORMS if flag("platform_" + platform)]
    if not loaders or not platforms:
        raise ValueError("Select at least one loader and one publishing platform")
    if loaders == ["common"] and "maven" not in platforms:
        raise ValueError("Common alone requires the Maven platform; mod-platform publishing is disabled for Common")
    return [f":{loader}:{PLATFORMS[platform]}" for loader in loaders for platform in platforms]


def release_ref():
    ref = os.environ["GITHUB_REF"]
    if not ref.startswith("refs/heads/"):
        raise ValueError("Releases must run on a branch")
    git("check-ref-format", ref)
    return ref


def preflight(operation):
    if operation not in ("analyze", "release", "single-release", "validate-version", "update-version"):
        raise ValueError(f"Unknown operation: {operation}")
    if operation in ("analyze", "release", "single-release") and not os.environ["CN_JAVA_VERSION"].strip():
        raise ValueError("java-version is required; no Java version fallback is provided")
    if operation in ("release", "validate-version"):
        _, match = property_text()
        validate_version(unescape(match[2]), os.environ["CN_VERSION"], flag("disable_checks"))
    if operation in ("release", "single-release"):
        tasks = publishing_tasks(operation)
        print("Publishing tasks: " + " ".join(tasks))
        release_ref()
    if operation == "update-version":
        property_text()
        release_ref()


def prepare_version(requested):
    text, match = property_text()
    if unescape(match[2]) != requested:
        text = text[:match.start(2)] + escape(requested) + text[match.end(2):]
        Path("gradle.properties").write_bytes(text.encode("utf-8"))
        git("add", "--", "gradle.properties")
        git("-c", "user.name=GitHub Actions", "-c", "user.email=actions@users.noreply.github.com", "commit", "-m", "Update mod_version to " + requested, "--", "gradle.properties")
    return git("rev-parse", "HEAD", capture=True)


def ensure_clean():
    git("diff", "--exit-code", "HEAD", "--")


def push_version(base_sha, ref):
    ensure_clean()
    remote = git("ls-remote", "--exit-code", "origin", ref, capture=True).split()[0]
    if remote != base_sha:
        raise ValueError("Branch changed during the release. Nothing will be published; rerun against the updated branch.")
    # A normal fast-forward push also rejects a competing push after ls-remote.
    git("push", "origin", f"HEAD:{ref}")


def gradle(*args, env=None):
    # Workflows run on Ubuntu. Tests can supply a Gradle runner directly to execute().
    run(["./gradlew", *args, "--stacktrace", "--console=plain"], env=env, record=args[0] == "sonar")


def artifacts():
    paths = [p for p in Path.cwd().glob("**/build/libs/*.jar") if "buildSrc" not in p.relative_to(Path.cwd()).parts]
    if not paths:
        raise ValueError("The build produced no release JARs")
    result = {}
    for path in sorted(paths):
        with path.open("rb") as file:
            result[str(path.resolve())] = hashlib.file_digest(file, "sha256").hexdigest()
    return result


def verify_artifacts(expected):
    if artifacts() != expected:
        raise ValueError("Release artifacts changed after the build; refusing to continue")


def sonar_gate_status(metadata):
    """Read the completed gate for this scan, never the branch's latest scan."""
    try:
        report = dict(line.split("=", 1) for line in metadata.read_text(encoding="utf-8").splitlines() if "=" in line)
        server = report["serverUrl"].rstrip("/")
        parsed = urlsplit(server)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Invalid Sonar server URL")

        def get(endpoint, **query):
            request = Request(server + "/api/" + endpoint + "?" + urlencode(query), headers={"Authorization": "Bearer " + os.environ["SONAR_TOKEN"]})
            with urlopen(request, timeout=30) as response:
                return json.load(response)

        task = get("ce/task", id=report["ceTaskId"])["task"]
        if task["status"] != "SUCCESS" or not task.get("analysisId"):
            return None
        return get("qualitygates/project_status", analysisId=task["analysisId"])["projectStatus"]["status"]
    except (OSError, ValueError, KeyError, TypeError):
        print("Could not confirm a completed Sonar quality gate; the original failure is preserved.", flush=True)
        return None


def analyze(gradle_runner):
    args = ["sonar", "-Dsonar.qualitygate.wait=true"]
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        args.append("-Dsonar.branch.name=" + os.environ["GITHUB_REF_NAME"])
    with tempfile.TemporaryDirectory(prefix="crystal-nest-sonar-", dir=os.environ.get("RUNNER_TEMP")) as temp:
        metadata = Path(temp) / "report-task.txt"
        args.append("-Dsonar.scanner.metadataFilePath=" + str(metadata))
        try:
            gradle_runner(*args)
        except subprocess.CalledProcessError as error:
            # The scanner reports FAILED for NONE too. Check the exact analysis
            # only after a completed gate failure, excluding scanner errors/timeouts.
            if "QUALITY GATE STATUS: FAILED" not in (error.output or "") or sonar_gate_status(metadata) != "NONE":
                raise
            print("::warning::Sonar quality gate is NONE (not computed). Retrying Sonar once using the existing build.", flush=True)
            metadata.unlink()
            # Deliberately outside another handler: every second failure is final.
            gradle_runner(*args)


def summary(sha, tasks, dry_run, jars):
    lines = ["## Release dry run" if dry_run else "## Release", "", f"Commit: `{sha}`", "", "Publishing tasks: " + ", ".join(f"`{t}`" for t in tasks), "", "Sonar quality gate: passed.", ""]
    if dry_run:
        lines.extend(["No version commit was pushed and no release was published.", "Gradle's publishing task graph was checked with --dry-run; publishing API calls were not tested.", ""])
    lines.extend(f"- `{Path(path).relative_to(Path.cwd())}` — SHA-256 `{digest}`" for path, digest in jars.items())
    print("\n".join(lines), flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")


def execute(operation, gradle_runner=gradle):
    preflight(operation)
    if operation == "validate-version":
        return
    if operation == "analyze":
        gradle_runner("build")
        analyze(gradle_runner)
        return
    ensure_clean()
    ref = release_ref()
    base_sha = git("rev-parse", "HEAD", capture=True)
    sha = prepare_version(os.environ["CN_VERSION"]) if operation in ("release", "update-version") else base_sha
    dry_run = flag("dry_run")
    if operation == "update-version":
        if dry_run:
            print("Dry run: version commit remains local.")
        else:
            push_version(base_sha, ref)
        return
    tasks = publishing_tasks(operation)
    gradle_runner("build")
    jars = artifacts()
    analyze(gradle_runner)
    verify_artifacts(jars)
    ensure_clean()
    # Configure publication before changing the remote branch. Build and publish
    # share a workspace; Gradle's normal up-to-date checks reuse existing outputs.
    gradle_runner(*tasks, "--dry-run")
    verify_artifacts(jars)
    if not dry_run:
        push_version(base_sha, ref)
        gradle_runner(*tasks)
        verify_artifacts(jars)
    summary(sha, tasks, dry_run, jars)


if __name__ == "__main__":
    try:
        operation = os.environ["CN_OPERATION"]
        if sys.argv[1:] == ["check"]:
            preflight(operation)
        else:
            execute(operation)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
