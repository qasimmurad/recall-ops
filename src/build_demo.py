"""Build the static live demo into site/, for GitHub Pages.

The demo is the same web/index.html, switched into static mode: instead of
calling the Python server, it runs web/kernel.js (a port of src/kernel.py,
checked by tests/test_parity.py) in the browser, over a snapshot of the
pipeline output. Visitors' actions are saved in their own browser only.

Run python3 src/pipeline.py first.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
OBJECTS = ROOT / "state" / "objects.json"
MARKER = "<!-- static-demo -->"


def repo_url():
    """Where the demo's 'Source on GitHub' link points, wherever the build runs."""
    repository = os.environ.get("GITHUB_REPOSITORY")  # GitHub Actions
    owner, slug = os.environ.get("VERCEL_GIT_REPO_OWNER"), os.environ.get("VERCEL_GIT_REPO_SLUG")  # Vercel
    if not repository and owner and slug:
        repository = f"{owner}/{slug}"
    if not repository:
        try:
            remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True,
                                    text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            remote = ""
        match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote)
        repository = match.group(1) if match else None
    return f"https://github.com/{repository}" if repository else None


def main():
    if not OBJECTS.exists():
        print("No data yet. Build it first: python3 src/pipeline.py", file=sys.stderr)
        return 1
    html = (ROOT / "web" / "index.html").read_text()
    if html.count(MARKER) != 1:
        print(f"web/index.html must contain {MARKER} exactly once.", file=sys.stderr)
        return 1

    config = json.dumps({"repo": repo_url()}).replace("</", "<\\/")
    injected = f'<script src="kernel.js"></script>\n<script>window.RECALL_OPS_STATIC = {config};</script>'

    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "data").mkdir(parents=True)
    (SITE / "index.html").write_text(html.replace(MARKER, injected))
    shutil.copy(ROOT / "web" / "kernel.js", SITE / "kernel.js")
    shutil.copy(ROOT / "ontology" / "ontology.json", SITE / "data" / "ontology.json")
    snapshot = json.loads(OBJECTS.read_text())
    (SITE / "data" / "objects.json").write_text(json.dumps(snapshot, separators=(",", ":")))
    (SITE / ".nojekyll").write_text("")  # serve files as-is, no Jekyll processing

    size = sum(p.stat().st_size for p in SITE.rglob("*") if p.is_file())
    print(f"Built the static demo in site/ ({size // 1024} KB). Preview it with: python3 -m http.server -d site 8766")
    return 0


if __name__ == "__main__":
    sys.exit(main())
