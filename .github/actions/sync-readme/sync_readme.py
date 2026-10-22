"""Sync a checked-out README to Modrinth, using only the Python standard library."""

import html
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote
from urllib.request import Request, urlopen


API = "https://api.modrinth.com/v2/project/"
PROPERTY = re.compile(r"^\s*(modrinth_id|mod_id_kebab|curseforge_id)\s*[=:]\s*(.*?)\s*$")
FER_ATTACHMENT = "https://github.com/user-attachments/assets/0fe2bd35-b13d-4632-8cc9-1154405800f4"
FER_VIDEO = '''<video width="100%" controls autoplay loop muted>
  <source src="https://raw.githubusercontent.com/crystal-nest/mod-fancy-assets/main/fancy-entity-renderer/fancymenu.mp4" type="video/mp4">
</video>'''


def read_properties(path):
    properties = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PROPERTY.fullmatch(line)
        if match:
            key, value = match.groups()
            if key in properties:
                raise ValueError(f"Duplicate {key} property")
            properties[key] = value
    return properties


def project_ids(properties):
    modrinth = (os.environ.get("CN_MODRINTH_PROJECT_ID", "").strip()
                or properties.get("modrinth_id") or properties.get("mod_id_kebab"))
    curseforge = (os.environ.get("CN_CURSEFORGE_PROJECT_ID", "").strip()
                  or properties.get("curseforge_id", ""))
    if not modrinth or any(character.isspace() for character in modrinth):
        raise ValueError("A Modrinth project ID or slug is required")
    if curseforge and (not re.fullmatch(r"[0-9]+", curseforge) or int(curseforge) == 0):
        raise ValueError("The CurseForge project ID must be a positive integer")
    return modrinth, curseforge


def prepare_body(readme, properties):
    if not readme.strip():
        raise ValueError("README.md is empty")
    if properties.get("mod_id_kebab") == "fancy-entity-renderer":
        pattern = re.compile(r"^" + re.escape(FER_ATTACHMENT) + r"[ \t]*$", re.MULTILINE)
        readme, count = pattern.subn(FER_VIDEO, readme)
        if count != 1:
            raise ValueError("FER README must contain exactly one standalone demo attachment URL; update the video transformation")
    return readme


def api_request(project_id, token, body=None):
    request = Request(
        API + quote(project_id, safe=""),
        method="GET" if body is None else "PATCH",
        headers={
            "Authorization": token,
            "User-Agent": "Crystal-Nest/readme-sync (https://github.com/Crystal-Nest/.github)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        data=None if body is None else json.dumps({"body": body}, ensure_ascii=False).encode("utf-8"),
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response) if body is None else None


def summary(modrinth, curseforge, body, status):
    print(f"Modrinth README: {status} (project {modrinth})", flush=True)
    lines = ["## Modrinth README", "", f"Status: {status}.",
             f"Project: `{modrinth}`.", f"Source commit: `{os.environ.get('CN_README_SHA', 'unknown')}`.",
             f"Prepared description: {len(body.encode('utf-8'))} UTF-8 bytes.", ""]
    if curseforge:
        lines.extend([f"Reserved CurseForge project: `{curseforge}`; no CurseForge description is uploaded.", ""])
    if status == "dry run":
        lines.extend(["No Modrinth API calls were made.", "", "<details><summary>Prepared description</summary>",
                      "", "<pre>" + html.escape(body) + "</pre>", "", "</details>", ""])
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")


def execute(root=Path("."), request=api_request):
    dry_run = os.environ.get("CN_DRY_RUN", "false")
    if dry_run not in ("true", "false"):
        raise ValueError("dry_run must be true or false")
    properties = read_properties(root / "gradle.properties")
    modrinth, curseforge = project_ids(properties)
    body = prepare_body((root / "README.md").read_text(encoding="utf-8"), properties)
    status = "dry run"
    if dry_run == "false":
        token = os.environ.get("MODRINTH_TOKEN", "").strip()
        if not token:
            raise ValueError("MODRINTH_TOKEN with PROJECT_WRITE permission is required to sync the README")
        current = request(modrinth, token)
        if not isinstance(current, dict) or not isinstance(current.get("body"), str):
            raise ValueError("Modrinth returned an invalid project body")
        status = "unchanged"
        if current["body"] != body:
            request(modrinth, token, body)
            status = "updated"
    summary(modrinth, curseforge, body, status)
    return status


if __name__ == "__main__":
    try:
        execute()
    except (ValueError, OSError) as error:
        # Do not print request headers or the authentication token.
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
