"""Self-check for the Lecture 7 (deploy, observe, respond) labs.

Run from the root of your hollowmere-app folder (the one with app.py):

    uv run tools/check_lab7.py beginner
    uv run tools/check_lab7.py intermediate
    uv run tools/check_lab7.py advanced

(`python3 tools/check_lab7.py beginner` works too: it uses only the standard
library.) It prints PASS, FAIL or SKIP for each check, with a hint for every
FAIL. It reads four things:

- your answer sheet in lab7/ and your files in ops/;
- the running stack, over HTTP: the app (localhost:8000), Prometheus
  (localhost:9090) and, for Intermediate, Grafana (localhost:3000). If the
  stack is down, those checks SKIP: start it with `docker compose up -d` in
  ops/;
- Docker itself (`docker`), to ask whether the app is healthy and to run
  promtool. Without Docker those checks SKIP;
- for Advanced, your Git history (it never changes it) and, if the GitHub
  CLI (`gh`) is installed and logged in, your fork on GitHub.

Hidden checks run in a subprocess on a temporary copy, never on your files.
`--offline` skips every GitHub check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path.cwd()
OPS = ROOT / "ops"
LAB = ROOT / "lab7"
PROMETHEUS_IMAGE = "prom/prometheus:v3.16.0"
IMAGE = "ghcr.io/novastack-hq/hollowmere-app"
FIX_BRANCH = "fix/FEST-43-vip-tier"
# The instructor's test script runs a second stack on other ports.
# You never need these.
APP_URL = os.environ.get(
    "LAB7_APP_URL", "http://localhost:8000").rstrip("/")
PROM_URL = os.environ.get(
    "LAB7_PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
GRAFANA_URL = os.environ.get(
    "LAB7_GRAFANA_URL", "http://localhost:3000").rstrip("/")
STACK_HINT = ("Start the stack: `cd ops`, then `docker compose up -d` "
              "(Docker Desktop running).")
results: list[tuple[str, str, str]] = []
OFFLINE = False


# ----------------------------------------------------------------- helpers
def report(status: str, title: str, hint: str = "") -> None:
    results.append((status, title, hint))
    print(f"{status:<5} {title}")
    if hint and status != "PASS":
        print(f"        hint: {hint}")


def ok(passed: bool, title: str, hint: str = "") -> None:
    report("PASS" if passed else "FAIL", title, hint)


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 300,
        env: dict[str, str] | None = None) -> tuple[int, str]:
    full_env = {**os.environ, **env} if env else None
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8",
                           errors="replace", env=full_env, check=False)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def read_text(path: Path) -> str | None:
    """A file's text, or None if it is missing or not UTF-8."""
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return None


def answers(path: Path) -> dict[str, str]:
    """Read `KEY: value` lines from an answer sheet."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    text = read_text(path)
    if text is None:
        print(f"{path.name} isn't saved as UTF-8 (PowerShell 5's > writes "
              "UTF-16). Open it in VS Code and save it as UTF-8.")
        return out
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z]{1,7}\d?)(?:\s*\([^)]*\))?:\s*(.*?)\s*$",
                     line)
        if m and m.group(2) and not m.group(2).startswith("<"):
            out[m.group(1)] = m.group(2).strip("`")
    return out


def number(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group()) if m else None


def words(text: str | None) -> int:
    return len((text or "").split())


def seconds_of_day(text: str | None) -> int | None:
    """'02:55:05' or '2:55' (or a timestamp holding one) -> seconds."""
    m = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", text or "")
    if not m:
        return None
    return (int(m.group(1)) * 3600 + int(m.group(2)) * 60
            + int(m.group(3) or 0))


# ------------------------------------------------------------ HTTP helpers
def get(url: str, timeout: float = 5) -> tuple[int, str]:
    """(status, body); status 0 means nothing answered."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as reply:
            return reply.status, reply.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return 0, ""


def get_json(url: str):
    status, body = get(url)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


_app_state: list[str | None] = []


def live_version() -> str | None:
    """The version the running app reports, or None if nothing answers."""
    if not _app_state:
        data = get_json(f"{APP_URL}/version")
        _app_state.append(
            data.get("version") if isinstance(data, dict) else None)
    return _app_state[0]


def prom(query: str) -> list | None:
    """Run a PromQL query now; the result list, or None if no answer."""
    url = (f"{PROM_URL}/api/v1/query?"
           + urllib.parse.urlencode({"query": query}))
    data = get_json(url)
    if not isinstance(data, dict) or data.get("status") != "success":
        return None
    return data["data"]["result"]


def prom_value(query: str) -> float | None:
    found = prom(query)
    if not found:
        return None
    try:
        return float(found[0]["value"][1])
    except (KeyError, IndexError, ValueError):
        return None


def prom_rules() -> list[dict] | None:
    data = get_json(f"{PROM_URL}/api/v1/rules")
    if not isinstance(data, dict) or data.get("status") != "success":
        return None
    return [r for g in data["data"]["groups"] for r in g["rules"]]


def app_target_up() -> bool | None:
    data = get_json(f"{PROM_URL}/api/v1/targets")
    if not isinstance(data, dict):
        return None
    return any(t.get("labels", {}).get("job") == "hollowmere"
               and t.get("health") == "up"
               for t in data["data"]["activeTargets"])


# ---------------------------------------------------------- Docker helpers
_docker_state: list[str | None] = []


def docker_problem() -> str | None:
    """None if `docker` works; otherwise why not (used as a SKIP hint)."""
    if not _docker_state:
        if not shutil.which("docker"):
            _docker_state.append(
                "Docker isn't installed (or isn't on your PATH).")
        elif run(["docker", "info", "--format", "{{.ServerVersion}}"],
                 timeout=30)[0]:
            _docker_state.append("Start Docker Desktop, then check again.")
        else:
            _docker_state.append(None)
    return _docker_state[0]


def app_health() -> str | None:
    """Docker's health status of the stack's app container."""
    rc, out = run(["docker", "compose", "ps", "-q", "app"], cwd=OPS,
                  timeout=60)
    ids = out.split()
    if rc != 0 or not ids:
        return None
    rc, out = run(["docker", "inspect", "--format",
                   "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                   ids[0]])
    return out.strip() if rc == 0 else None


# -------------------------------------------- compose.yaml (a tiny reader)
def service_block(text: str, name: str) -> list[str]:
    """The lines of one service in compose.yaml (two-space indents)."""
    out: list[str] = []
    inside = False
    indent = 0
    for line in text.splitlines():
        stripped = line.strip()
        here = len(line) - len(line.lstrip(" "))
        if inside:
            if stripped and not stripped.startswith("#") and here <= indent:
                break
            out.append(line)
        elif (re.match(rf"^\s+{re.escape(name)}:\s*(#.*)?$", line)
              and here > 0):
            inside, indent = True, here
    return out


def service_image(text: str, name: str) -> str | None:
    for line in service_block(text, name):
        m = re.match(r"^\s*image:\s*['\"]?([^'\"\s#]+)", line)
        if m:
            return m.group(1)
    return None


def service_env(text: str, name: str, key: str) -> str | None:
    """A setting under `environment:` (map or list form), or None."""
    for line in service_block(text, name):
        if line.strip().startswith("#"):
            continue
        m = re.match(rf"^\s*-?\s*['\"]?{re.escape(key)}['\"]?\s*[:=]\s*"
                     r"['\"]?([^'\"\s#]*)", line)
        if m:
            return m.group(1)
    return None


def compose_text() -> str | None:
    return read_text(OPS / "compose.yaml")


# ------------------------------------------------- Git and test helpers
def git(root: Path, *args: str) -> tuple[int, str]:
    return run(["git", *args], cwd=root)


def has_ref(root: Path, ref: str) -> bool:
    return git(root, "rev-parse", "--verify", "--quiet",
               f"{ref}^{{commit}}")[0] == 0


def show(root: Path, ref: str, path: str) -> str | None:
    rc, out = git(root, "show", f"{ref}:{path}")
    return out if rc == 0 else None


def best_ref(root: Path, branch: str, want: str = "") -> str | None:
    """The local branch or origin's copy, preferring one whose app.py
    holds `want` (you may have merged on GitHub and not pulled)."""
    found = [r for r in (branch, f"origin/{branch}") if has_ref(root, r)]
    for ref in found:
        if want and want in (show(root, ref, "app.py") or ""):
            return ref
    return found[0] if found else None


def export(root: Path, ref: str, dest: Path) -> bool:
    """Copy the files of `ref` (not your working folder) into dest."""
    tar = dest.parent / f"export-{dest.name}.tar"
    rc, _ = git(root, "archive", "--format=tar", "-o", str(tar), ref)
    if rc != 0:
        return False
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar) as t:
        t.extractall(dest, filter="data")
    tar.unlink()
    return True


def interpreter(candidates: list[Path], need: str) -> list[str] | None:
    names = [c / ".venv" / "bin" / "python" for c in candidates]
    names += [c / ".venv" / "Scripts" / "python.exe" for c in candidates]
    names.append(Path(sys.executable))
    for py in names:
        if py.exists() and run([str(py), "-c", f"import {need}"],
                               timeout=60)[0] == 0:
            return [str(py)]
    if shutil.which("uv"):
        return ["uv", "run", "--frozen", "--quiet", "python"]
    return None


def pytest_in(copy: Path, py: list[str], *args: str) -> tuple[int, str]:
    return run([*py, "-m", "pytest", "-p", "no:cacheprovider", *args],
               cwd=copy, env={"PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)


def summary(out: str) -> str:
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return lines[-1].strip("= ") if lines else "(no output)"


def count(out: str, word: str) -> int:
    m = re.search(rf"(\d+) {word}", out)
    return int(m.group(1)) if m else 0


# ------------------------------------------------- GitHub helpers (gh)
_gh_state: list[str | None] = []


def gh_problem() -> str | None:
    if not _gh_state:
        if OFFLINE:
            _gh_state.append("--offline was given.")
        elif not shutil.which("gh"):
            _gh_state.append(
                "Install the GitHub CLI (https://cli.github.com) and run "
                "`gh auth login`, or check your fork's Actions tab.")
        elif run(["gh", "auth", "status"], timeout=30)[0] != 0:
            _gh_state.append("Run `gh auth login` first.")
        else:
            _gh_state.append(None)
    return _gh_state[0]


def gh_api(path: str):
    rc, out = run(["gh", "api", "-H", "Accept: application/vnd.github+json",
                   path], timeout=60)
    if rc != 0:
        return None, out.strip()[-200:]
    try:
        return json.loads(out), ""
    except json.JSONDecodeError:
        return None, out.strip()[:200]


def github_repo(root: Path) -> str | None:
    rc, out = git(root, "remote", "get-url", "origin")
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
                  out.strip())
    return f"{m.group(1)}/{m.group(2)}" if rc == 0 and m else None


# ------------------------------------------------- Part 0: the exercise
def exercise(sheet: dict[str, str], required: bool) -> None:
    keys = ("E1", "E2", "E3", "E4")
    if not required and not any(sheet.get(k) for k in keys):
        report("SKIP", "(optional) X0 Part 0: the exercise answers (E1-E4)",
               "Do the warm-up (FEST-44) if you have ten minutes.")
        return
    tag = "Part 0" if required else "(optional) Part 0"
    ok(sheet.get("E1", "").strip() == "1.1",
       f"X1 {tag}: E1, the version that served 4f2a91c0",
       "grep the reference; read the \"version\" field of its lines.")
    e2 = sheet.get("E2", "")
    ok("keyerror" in e2.lower() and "vip" in e2.lower(),
       f"X2 {tag}: E2, the error",
       "The \"error\" field of the ERROR line, word for word.")
    ok("02:55:01" in sheet.get("E3", ""),
       f"X3 {tag}: E3, the time of the first error",
       "grep the ERROR lines and take the first one (`| head -1`); "
       "hh:mm:ss.")
    e4 = number(sheet.get("E4"))
    ok(e4 == 68, f"X4 {tag}: E4, how many requests failed",
       "70? Two request IDs contain the text 500. Search the field "
       "('\"status\": 500'), not the text." if e4 == 70 else
       "Count the lines whose status field is 500.")


# ------------------------------------------------- the stack (all levels)
def stack_running(cid: str) -> bool:
    version = live_version()
    if version is None:
        report("SKIP", f"{cid} The stack is running (the app answers on "
               f"{APP_URL.split('//')[-1]})", STACK_HINT)
        return False
    report("PASS", f"{cid} The stack is running (the app says version "
           f"{version})")
    return True


def check_health(cid: str) -> None:
    title = f"{cid} Docker says the app container is (healthy)"
    problem = docker_problem()
    if problem:
        report("SKIP", title, problem)
        return
    status = app_health()
    ok(status == "healthy", title,
       f"Docker says {status!r}. Wait 10 seconds and check again; "
       "`docker compose ps` in ops/ shows it." if status else
       "No app container found for ops/compose.yaml: run "
       "`docker compose up -d` in ops/.")


# ---------------------------------------------------------------- beginner
NAMES = r"\b(Kofi|Riya|Sam|Luna|Vale)\b"
BLAME = (r"should\s+have|should'?ve|shouldn'?t\s+have|\bforg[eo]t|\bfault"
         r"|careless|\bblam|without\s+checking"
         r"|didn'?t\s+(?:check|test|bother)")
HEADINGS = ["Summary", "Impact", "Timeline", "Contributing factors",
            "What went well", "Actions"]
POSTMORTEM = "incident-FEST-43.md"


def blame_sentences(text: str) -> list[str]:
    """Sentences (or table rows) with a name next to a blaming phrase."""
    # skip the template's guidance above the first heading
    body = text.split("\n## ", 1)[1] if "\n## " in text else text
    found = []
    for piece in re.split(r"(?<=[.!?])\s+|\n", body):
        if (re.search(NAMES, piece)
                and re.search(BLAME, piece, re.IGNORECASE)):
            found.append(piece.strip(" |#*-")[:100])
    return found


def section(text: str, title: str) -> str:
    m = re.search(rf"^##\s+{re.escape(title)}[^\n]*\n(.*?)(?=^##\s|\Z)",
                  text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else ""


def check_postmortem() -> None:
    path = OPS / POSTMORTEM
    text = read_text(path)
    if text is None:
        ok(False, f"B20 ops/{POSTMORTEM}: your post-mortem exists",
           f"Copy ops/POSTMORTEM-TEMPLATE.md to ops/{POSTMORTEM}."
           if not path.exists() else "Save it as UTF-8 (VS Code: bottom "
           "bar).")
        return
    report("PASS", f"B20 ops/{POSTMORTEM}: your post-mortem exists")
    missing = [h for h in HEADINGS
               if not re.search(rf"^##\s+{re.escape(h)}", text,
                                re.MULTILINE | re.IGNORECASE)]
    ok(not missing, "B21 Post-mortem: the six headings are there",
       "Missing: " + ", ".join(missing) + ". Keep the template's headings.")
    own = re.sub(r"Two or three sentences:.*", "", section(text, "Summary"))
    n = words(own)
    ok(n >= 15, "B22 Post-mortem: the Summary is written",
       f"{n} words of your own; write two or three sentences: what "
       "broke, for whom, for how long, how it was fixed.")
    timeline = section(text, "Timeline")
    times = {m.group() for m in
             re.finditer(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", timeline)}
    ok(len(times) >= 4 and "hh:mm" not in timeline,
       "B23 Post-mortem: the timeline has at least four real times",
       f"{len(times)} different times found"
       + (" and an `hh:mm` left over" if "hh:mm" in timeline else "")
       + ". Use your T1-T3 and the times in incident.log.")
    rows = [r for r in section(text, "Actions").splitlines()
            if r.strip().startswith("|")
            and not re.match(r"^\|[\s|:-]*$", r.strip())]
    filled = [r for r in rows[1:] if words(r.replace("|", " ")) >= 3]
    ok(bool(filled),
       "B24 Post-mortem: at least one action, with an owner (a role)",
       "Fill a row of the Actions table: what, who (a role), a ticket.")
    blamed = blame_sentences(text)
    ok(not blamed, "B25 Hidden check: the post-mortem is blameless",
       "This names a person next to a blaming phrase: \""
       + (blamed or [""])[0] + "\". Describe what the process allowed "
       "(\"the release had no VIP test\").")


def check_incident_log() -> None:
    path = OPS / "incident.log"
    title = "B19 ops/incident.log holds a 1.1 ERROR line (the evidence)"
    if not path.exists():
        ok(False, title,
           "Save the logs before rolling back: in ops/, "
           "`docker compose logs --no-log-prefix app > incident.log` "
           "while 1.1 is still running.")
        return
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        ok(False, title + " (as UTF-8)",
           "PowerShell 5's > writes UTF-16. Use "
           "`| Out-File -Encoding utf8 incident.log`.")
        return
    found = False
    for line in raw.decode("utf-8", "replace").splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            entry = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if (isinstance(entry, dict) and entry.get("level") == "ERROR"
                and entry.get("version") == "1.1"
                and "VIP" in str(entry.get("error", ""))):
            found = True
            break
    ok(found, title,
       "Save the logs while 1.1 is running and after a VIP request has "
       "failed (the alert firing is a good sign).")


def beginner() -> None:
    sheet = answers(LAB / "BEGINNER.md")
    ok(bool(sheet), "B00 lab7/BEGINNER.md has answers",
       "Fill in the lines after the colons (B1: ...).")
    exercise(sheet, required=True)

    # ---- the stack, now (after the rollback)
    image = service_image(compose_text() or "", "app")
    ok(image == f"{IMAGE}:1.0", "B01 ops/compose.yaml: the app is back on 1.0",
       f"It says {image!r}. The rollback: put the tag back to "
       f"{IMAGE}:1.0, then `docker compose up -d`.")
    if stack_running("B02"):
        ok(live_version() == "1.0",
           "B03 The running app is version 1.0 (rolled back)",
           f"/version says {live_version()}. Run `docker compose up -d` "
           "in ops/.")
        check_health("B04")
        up = app_target_up()
        if up is None:
            report("SKIP", "B05 Prometheus scrapes the app (target up)",
                   f"Prometheus doesn't answer on {PROM_URL}. " + STACK_HINT)
        else:
            ok(up, "B05 Prometheus scrapes the app (target up)",
               "Open http://localhost:9090/targets: is `hollowmere` up?")
            rules = prom_rules() or []
            state = next((r.get("state") for r in rules
                          if r.get("name") == "FestErrorsHigh"), None)
            ok(state == "inactive", "B06 FestErrorsHigh is back to inactive",
               f"It is {state!r}. After a rollback it takes about a minute "
               "to clear." if state else
               "Prometheus has no FestErrorsHigh rule: is ops/alerts.yml "
               "there?")

    # ---- the answers
    ok(number(sheet.get("B1")) == 3,
       "B07 B1: how many services `docker compose ps` lists",
       "Count the rows (one per running service).")
    ok(sheet.get("B2", "").strip().strip('"{}').endswith("1.0"),
       "B08 B2: the version the stack started on",
       "What did /version say first?")
    ok(sheet.get("B3", "").strip().lower() == "x-request-id",
       "B09 B3: the response header that carries the request ID",
       "`curl -i localhost:8000/lineup` shows the headers; it starts "
       "with X-.")
    ok(sheet.get("B4", "").strip().lower() == "inactive",
       "B10 B4: FestErrorsHigh's state before the deploy",
       "One word, from the Alerts page.")
    b5 = number(sheet.get("B5"))
    if b5 is not None and b5 > 1:
        b5 /= 100  # they wrote a percentage
    ok(b5 is not None and 0.05 <= b5 <= 0.5,
       "B11 B5: the share of /lineup requests failing on 1.1 (Prometheus)",
       "Run the alert's expression without `> 0.05` in the Query page "
       "while 1.1 runs.")
    b6 = sheet.get("B6", "")
    ok("keyerror" in b6.lower() and "vip" in b6.lower(),
       "B12 B6: the error in your incident.log",
       "Copy the \"error\" field.")
    t = [seconds_of_day(sheet.get(k)) for k in ("T1", "T2", "T3")]
    ordered = None not in t and t[0] < t[1] < t[2]  # type: ignore[operator]
    ok(ordered, "B13 T1-T3: deploy, alert firing, service restored, in order",
       "hh:mm:ss for each, from your clock or `date`. T1 < T2 < T3.")
    b7 = number(sheet.get("B7"))
    sensible = b7 is not None and 0.5 <= b7 <= 10
    if sensible and ordered:
        gap = (t[1] - t[0]) / 60  # type: ignore[operator]
        sensible = abs(gap - b7) <= 1.5  # type: ignore[operator]
    ok(sensible, "B14 B7: minutes from deploy to alert firing (T2 - T1)",
       "T2 minus T1, in minutes (about 1-2: pending, then `for: 1m`).")
    ok(words(sheet.get("B8")) >= 6,
       "B15 B8: why save the logs before rolling back",
       "One sentence, six words or more, in your own words.")

    # ---- the evidence and the write-up
    check_incident_log()
    check_postmortem()


# ------------------------------------------------------------ intermediate
def dashboard_files() -> list[Path]:
    folder = OPS / "grafana" / "dashboards"
    return sorted(folder.glob("*.json")) if folder.exists() else []


def panel_exprs(dashboard: dict) -> tuple[int, list[str]]:
    """(number of panels with a query, every query), rows included."""
    panels = list(dashboard.get("panels", []))
    for p in list(panels):
        panels.extend(p.get("panels", []) if isinstance(p, dict) else [])
    exprs: list[str] = []
    with_query = 0
    for p in panels:
        if not isinstance(p, dict):
            continue
        found = [t.get("expr", "") for t in p.get("targets", [])
                 if isinstance(t, dict)]
        found = [e for e in found if e]
        with_query += bool(found)
        exprs += found
    return with_query, exprs


def dashboard_ok(dashboard: dict) -> list[str]:
    """What's missing from a dashboard (empty if it has the three panels)."""
    n, exprs = panel_exprs(dashboard)
    every = " ".join(exprs)
    missing = []
    if n < 3:
        missing.append(f"three panels with a query (found {n})")
    if "fest_requests_total" not in every:
        missing.append("a query on fest_requests_total")
    if not re.search(r"status\s*=~\s*\\?\"5", every):
        missing.append('an error query (status=~"5..")')
    if not ("histogram_quantile" in every
            and "fest_request_duration_seconds_bucket" in every):
        missing.append("a p95 query (histogram_quantile on ..._bucket)")
    return missing


def latency_rule(text: str) -> str | None:
    """The name of an alert in alerts.yml that uses histogram_quantile."""
    blocks = re.split(r"^\s*-\s+alert:\s*", text, flags=re.MULTILINE)[1:]
    for block in blocks:
        name = block.split()[0].strip("'\"") if block.split() else ""
        if ("histogram_quantile" in block
                and "fest_request_duration_seconds_bucket" in block):
            return name
    return None


def hidden_promtool() -> None:
    title = "I06 Hidden check: `promtool check rules` passes on alerts.yml"
    problem = docker_problem()
    if problem:
        report("SKIP", title, problem)
        return
    alerts = (OPS / "alerts.yml").resolve()
    rc, out = run(["docker", "run", "--rm", "--entrypoint", "promtool",
                   "-v", f"{alerts}:/alerts.yml:ro", PROMETHEUS_IMAGE,
                   "check", "rules", "/alerts.yml"], timeout=300)
    m = re.search(r"SUCCESS: (\d+) rules found", out)
    found = int(m.group(1)) if m else 0
    ok(rc == 0 and found >= 2, title,
       (f"promtool found {found} rule(s): add your latency rule."
        if rc == 0 else "promtool says: " + " ".join(out.split())[-220:]))


def slow_test_seen(rule: str | None) -> None:
    """Prometheus remembers the slow test: p95 high, the alert firing."""
    p95 = ('histogram_quantile(0.95, sum by (le) (rate('
           'fest_request_duration_seconds_bucket{path="/lineup"}[1m])))')
    worst = prom_value(f"max_over_time(({p95})[2h:15s])")
    ok(worst is not None and worst >= 0.25,
       "I13 Prometheus saw /lineup's p95 above 0.25 s (your slow test)",
       f"The worst p95 in the last 2 hours was {worst or 0:.3f} s. Run "
       "release 1.0.1 with SLOW_MS: \"800\" for 3 minutes (Part 5).")
    if not rule:
        return
    fired = prom_value('max_over_time(ALERTS{alertname="' + rule
                       + '", alertstate="firing"}[2h])')
    ok(fired == 1, f"I14 {rule} fired during your slow test (last 2 hours)",
       "Keep SLOW_MS on until the Alerts page says firing (pending, then "
       "`for: 1m`). `docker compose down` wipes Prometheus's history: use `up -d`.")


def intermediate() -> None:
    sheet = answers(LAB / "INTERMEDIATE.md")
    ok(bool(sheet), "I00 lab7/INTERMEDIATE.md has answers",
       "Fill in the lines after the colons (G1: ...).")
    exercise(sheet, required=False)

    # ---- Part 1: Grafana in compose.yaml, pinned
    compose = compose_text() or ""
    grafana_image = service_image(compose, "grafana") or ""
    tag = grafana_image.split(":", 1)[1] if ":" in grafana_image else ""
    ok(grafana_image.startswith("grafana/grafana:")
       and re.fullmatch(r"\d+\.\d+\.\d+", tag) is not None,
       "I01 ops/compose.yaml: a grafana service on a pinned version",
       f"Found image {grafana_image or 'none'!r}. Use an exact version "
       "such as grafana/grafana:13.3.1, never :latest.")
    app_image = service_image(compose, "app") or ""
    slow = service_env(compose, "app", "SLOW_MS")
    fast = (slow is None or number(slow) == 0) and re.search(
        r"hollowmere-app:1\.0(\.1)?$", app_image) is not None
    ok(fast, "I02 ops/compose.yaml: the app is fast again (no SLOW_MS)",
       f"image {app_image!r}, SLOW_MS {slow!r}. After the slow test set "
       f"SLOW_MS: \"0\" (or put back {IMAGE}:1.0), then "
       "`docker compose up -d`.")

    # ---- Part 2: provisioning
    ds = "".join(read_text(p) or "" for p in sorted(
        (OPS / "grafana" / "provisioning" / "datasources").glob("*.y*ml")))
    url = re.search(r"^\s*url:\s*(\S+)", ds, re.MULTILINE)
    ok(bool(url) and "____" not in ds
       and "prometheus:9090" in url.group(1),  # type: ignore[union-attr]
       "I03 ops/grafana/provisioning/datasources/: the data source "
       "points at Prometheus",
       "Grafana is in its own container: `localhost` is Grafana itself. "
       "Use the service's name, as prometheus.yml does for the app.")
    best: list[str] = ["a dashboard JSON file in ops/grafana/dashboards/"]
    for f in dashboard_files():
        try:
            board = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            ok(False, f"I04 {f.name} is valid JSON", str(e)[:160])
            continue
        missing = dashboard_ok(board.get("dashboard", board))
        if len(missing) < len(best):
            best = missing
    ok(not best, "I04 The dashboard file has rate, errors and p95 panels",
       "Missing: " + "; ".join(best) + ".")

    # ---- Part 3: the second alert
    alerts = read_text(OPS / "alerts.yml") or ""
    rule = latency_rule(alerts)
    ok(bool(rule), "I05 ops/alerts.yml: a second alert on p95 latency",
       "A rule whose expr uses histogram_quantile(0.95, ...) on "
       "fest_request_duration_seconds_bucket.")
    hidden_promtool()

    # ---- Part 4: the runbook entry
    runbook = read_text(OPS / "RUNBOOK.md") or ""
    entry = ""
    if rule:
        m = re.search(rf"^#+[^\n]*\b{re.escape(rule)}\b[^\n]*\n(.*?)"
                      r"(?=^#\s|^##\s|\Z)", runbook,
                      re.MULTILINE | re.DOTALL)
        entry = m.group(1) if m else ""
    steps = len(re.findall(r"^\s*\d+\.\s", entry, re.MULTILINE))
    ok(bool(entry) and steps >= 3,
       "I07 ops/RUNBOOK.md: an entry for your latency alert",
       f"A heading with the alert's name ({rule or 'your rule'}) and at "
       f"least three numbered steps (found {steps}).")

    # ---- the running stack
    if stack_running("I08"):
        rules = prom_rules()
        if rules is None:
            report("SKIP", "I09 Prometheus has loaded both rules",
                   f"Nothing answers on {PROM_URL}.")
        else:
            names = {r.get("name") for r in rules}
            ok(len(rules) >= 2 and (rule in names if rule else False),
               "I09 Prometheus has loaded both rules",
               f"Prometheus knows {sorted(n for n in names if n)}. After "
               "editing alerts.yml run `docker compose restart prometheus`.")
            state = next((r.get("state") for r in rules
                          if r.get("name") == rule), None)
            if rule:
                ok(state == "inactive",
                   f"I10 {rule} is inactive again (the app is fast)",
                   f"It is {state!r}. Set SLOW_MS back to 0 and wait a "
                   "minute.")
            slow_test_seen(rule)
        health = get_json(f"{GRAFANA_URL}/api/health")
        if not isinstance(health, dict):
            report("SKIP", "I11 Grafana answers on "
                   + GRAFANA_URL.split("//")[-1],
                   "Add the grafana service (Part 1), then "
                   "`docker compose up -d`.")
        else:
            running = health.get("version", "")
            ok(bool(tag) and running == tag,
               f"I11 Grafana {running} is running, the version you pinned",
               f"compose.yaml pins {tag or 'nothing'}: run "
               "`docker compose up -d`.")
            ok(sheet.get("G1", "").strip().lstrip("v") == running,
               "I12 G1: the Grafana version",
               "Grafana's Help menu (?) or /api/health shows it.")
            q = urllib.parse.urlencode({"query": "up"})
            data = get_json(f"{GRAFANA_URL}/api/datasources/proxy/uid/"
                            f"prometheus/api/v1/query?{q}")
            ok(isinstance(data, dict) and data.get("status") == "success"
               and bool(data["data"]["result"]),
               "I15 Grafana reaches Prometheus through the provisioned "
               "data source",
               "The data source needs `uid: prometheus` and the right url; "
               "`docker compose logs grafana` shows provisioning errors. "
               "If Grafana asks you to log in, add the two "
               "GF_AUTH_ANONYMOUS_* settings (Part 1).")
            found = get_json(f"{GRAFANA_URL}/api/search?type=dash-db") or []
            problems = ["no dashboard found"]
            for hit in found if isinstance(found, list) else []:
                board = get_json(
                    f"{GRAFANA_URL}/api/dashboards/uid/{hit.get('uid')}")
                if isinstance(board, dict):
                    missing = dashboard_ok(board.get("dashboard", {}))
                    if len(missing) < len(problems) or not missing:
                        problems = missing
            ok(not problems,
               "I16 Grafana shows your dashboard with the three panels",
               "; ".join(problems) + ". Grafana reads the file at start: "
               "`docker compose restart grafana`.")

    # ---- the answers
    ok("prometheus:9090" in sheet.get("U1", ""),
       "I17 U1: the address Grafana uses for Prometheus",
       "The url from your data source file.")
    p1, p2 = number(sheet.get("P1")), number(sheet.get("P2"))
    ok(p1 is not None and 0 < p1 < 100,
       "I18 P1: p95 of /lineup on 1.0, in milliseconds",
       "Read the p95 panel while 1.0 runs (seconds x 1000).")
    ok(p2 is not None and 250 <= p2 <= 1000,
       "I19 P2: p95 of /lineup with SLOW_MS=800, in milliseconds",
       "Read the p95 panel during the slow test (seconds x 1000).")
    a1 = number(sheet.get("A1"))
    ok(a1 is not None and 30 <= a1 <= 300,
       "I20 A1: seconds from the slow deploy to your alert firing",
       "Pending, then `for: 1m`: usually 60-120 seconds.")
    filled = sum(1 for k in ("W1", "W2", "W3") if words(sheet.get(k)) >= 6)
    ok(filled == 3, "I21 W1-W3: your reasons, in your own words",
       f"{filled} of 3 answered with 6 or more words.")


# ---------------------------------------------------------------- advanced
HIDDEN_TEST = '''\
"""Written by check_lab7.py into a temporary copy. Never in your repo."""
import app as fest


def _sets(ticket=None, tier=None):
    client = fest.app.test_client()
    if tier:
        client.set_cookie("ticket", ticket)
        client.set_cookie("tier", tier)
        reply = client.get("/lineup")
    else:
        reply = client.get(f"/lineup?ticket={ticket}" if ticket
                           else "/lineup")
    assert reply.status_code == 200, f"{ticket}: HTTP {reply.status_code}"
    return reply.get_json()["sets"]


def test_lab7_luna_103_sees_backstage_sets():
    assert any(s.get("backstage") for s in _sets("103"))


def test_lab7_a_vip_phone_sees_backstage_sets():
    assert any(s.get("backstage") for s in _sets("345", "VIP"))


def test_lab7_general_102_sees_no_backstage_sets():
    assert not any(s.get("backstage") for s in _sets("102"))


def test_lab7_no_wristband_sees_the_public_lineup():
    assert len(_sets()) == 8


def test_lab7_version_is_1_2():
    reply = fest.app.test_client().get("/version")
    assert reply.get_json() == {"version": "1.2"}
'''


def hidden_fix_checks(ref: str, test_name: str, tmp: Path) -> None:
    title_fail = "A09 Hidden check: your VIP test FAILS on release 1.1"
    title_pass = "A08 Hidden check: all tests pass on production (your fix)"
    title_hidden = ("A10 Hidden check: VIPs (103 and phones) see backstage, "
                    "General doesn't, /version 1.2")
    fixed, old = tmp / "fixed", tmp / "old"
    if not export(ROOT, ref, fixed):
        for title in (title_fail, title_pass, title_hidden):
            ok(False, title, f"git couldn't export {ref}.")
        return
    py = interpreter([ROOT], "flask, prometheus_client, segno, pytest")
    if not py:
        for title in (title_fail, title_pass, title_hidden):
            report("SKIP", title, "Run `uv sync` first.")
        return
    rc, out = pytest_in(fixed, py, "tests")
    ok(rc == 0, title_pass, f"On a copy of {ref}: {summary(out)}")
    v11 = show(ROOT, "v1.1", "app.py")
    if not test_name:
        ok(False, title_fail, "Write your VIP test's name on line F1.")
    elif v11 is None:
        report("SKIP", title_fail,
               "No tag v1.1 here. Fetch it: `git fetch https://github.com/"
               "novastack-hq/hollowmere-app.git tag v1.1`.")
    else:
        shutil.copytree(fixed, old)
        (old / "app.py").write_text(v11, encoding="utf-8")
        rc, out = pytest_in(old, py, "tests", "-k", test_name, "-q")
        ok(rc == 1 and count(out, "failed") >= 1, title_fail,
           f"I put release 1.1's app.py in a copy and ran {test_name}: "
           f"{summary(out)}. It must ask for a VIP's line-up (103) and "
           "check for the backstage sets.")
    (fixed / "tests" / "test_lab7_hidden.py").write_text(
        HIDDEN_TEST, encoding="utf-8")
    rc, out = pytest_in(fixed, py, "tests/test_lab7_hidden.py", "-q")
    ok(rc == 0, title_hidden, f"On a copy of {ref}: {summary(out)}. "
       "Fix the key (\"VIP\") and set APP_VERSION = \"1.2\".")


def fix_branch_merged(dev: str | None) -> bool:
    if any(has_ref(ROOT, r) for r in (FIX_BRANCH, f"origin/{FIX_BRANCH}")):
        return True
    if dev:  # deleted after the merge: GitHub's merge message names it
        rc, out = git(ROOT, "log", dev, "--merges", "--format=%s")
        return rc == 0 and "FEST-43-vip-tier" in out
    return False


def advanced() -> None:
    sheet = answers(LAB / "ADVANCED.md")
    ok(bool(sheet), "A00 lab7/ADVANCED.md has answers",
       "Fill in the lines after the colons.")
    exercise(sheet, required=False)

    # ---- what is on production (and dev)
    if git(ROOT, "rev-parse", "--git-dir")[0] != 0:
        ok(False, "A01 This folder is a Git repository",
           "Clone your fork with `git clone`, don't download a ZIP.")
        return
    prod = best_ref(ROOT, "production", 'APP_VERSION = "1.2"')
    dev = best_ref(ROOT, "dev", 'APP_VERSION = "1.2"')
    if prod is None:
        ok(False, "A01 There is a production branch",
           "`git fetch origin` to get origin/production.")
        return
    app = show(ROOT, prod, "app.py") or ""
    ok('APP_VERSION = "1.2"' in app,
       f"A01 On {prod}, app.py says APP_VERSION = \"1.2\"",
       "Bump the version on dev, then release dev into production by a "
       "pull request. `git fetch origin` afterwards.")
    ok("BACKSTAGE" in app,
       f"A02 On {prod}, the VIP backstage feature is still there",
       "Fix forward: keep FEST-42's code and fix the key; don't delete it.")
    tag_app = show(ROOT, "v1.2", "app.py")
    ok(tag_app is not None and 'APP_VERSION = "1.2"' in tag_app,
       "A03 Tag v1.2 exists and its app.py says 1.2",
       "Tag the release commit on production: `git tag -a v1.2 -m ...` "
       "then `git push origin v1.2` (or `git fetch --tags`).")
    ok(fix_branch_merged(dev),
       f"A04 The fix was on its own branch, {FIX_BRANCH}",
       f"`git switch -c {FIX_BRANCH} dev` before the fix, push it, and "
       "open a pull request into dev.")
    f1 = sheet.get("F1", "").strip().removesuffix("()")
    tests = "".join(show(ROOT, prod, f"tests/{p.name}") or ""
                    for p in (ROOT / "tests").glob("test_*.py"))
    ok("vip" in f1.lower() and f"def {f1}(" in tests,
       "A05 F1: your VIP test's name, and it is in tests/ on production",
       "The function name, e.g. test_vip_sees_backstage_sets.")
    tmp = Path(tempfile.mkdtemp(prefix="lab7check-"))
    try:
        hidden_fix_checks(prod, f1 if "vip" in f1.lower() else "", tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    f2 = sheet.get("F2", "")
    ok("500" in f2 or "keyerror" in f2.lower(),
       "A06 F2: what your VIP test said on 1.1",
       "The assertion line pytest printed (E   ...).")

    # ---- deployed and quiet
    image = service_image(compose_text() or "", "app") or ""
    ok(re.search(r"hollowmere-app:1\.2$", image) is not None,
       "A11 ops/compose.yaml: the app runs release 1.2",
       f"It says {image!r}. Build or pull hollowmere-app:1.2, change the "
       "tag, `docker compose up -d`.")
    if stack_running("A12"):
        ok(live_version() == "1.2", "A13 The running app is version 1.2",
           f"/version says {live_version()}.")
        status, body = get(f"{APP_URL}/lineup?ticket=103")
        ok(status == 200,
           "A14 Luna Vale (103, VIP) gets the line-up again (HTTP 200)",
           f"HTTP {status}: {body[:80]}")
        uptime = prom_value(
            'time() - process_start_time_seconds{job="hollowmere"}')
        errors = prom_value('sum(increase(fest_requests_total{'
                            'path="/lineup",status=~"5.."}[2m]))')
        total = prom_value(
            'sum(increase(fest_requests_total{path="/lineup"}[2m]))')
        if uptime is None and total is None:
            report("SKIP", "A15 Prometheus: no 5xx on /lineup for 2 minutes",
                   f"Prometheus doesn't answer on {PROM_URL}.")
        else:
            ok(uptime is not None and uptime >= 120,
               "A15 Prometheus: the app has been up for at least 2 minutes",
               f"Up for {uptime or 0:.0f} s. Wait, then check again.")
            ok((total or 0) > 0 and not errors,
               "A16 Prometheus: no 5xx on /lineup in the last 2 minutes, "
               "with traffic",
               f"{errors or 0:.0f} failed of {total or 0:.0f} requests. Is "
               "the crowd service running? Did 1.1 run in the last 2 "
               "minutes?")

    # ---- SLO and error budget
    s = {k: number(sheet.get(k)) for k in ("S1", "S2", "S3", "S4")}

    def near(key: str, want: float) -> bool:
        got = s[key]
        return got is not None and abs(got - want) < 0.06

    ok(near("S1", 43.2), "A17 S1: the 99% budget over 3 days, in minutes",
       "3 days x 24 hours x 60 minutes x (1 - 0.99).")
    ok(near("S2", 4.32), "A18 S2: the 99.9% budget over 3 days, in minutes",
       "The same sum with 0.999.")
    ok(near("S3", 2.8),
       "A19 S3: the incident in budget minutes (20% failing, 14 minutes)",
       "Counted by requests, 20% failing from 02:55 to 03:09 costs "
       "0.20 x 14 minutes.")
    ok(near("S4", 40.4), "A20 S4: the 99% budget left for the festival",
       "S1 minus S3.")
    filled = sum(1 for k in ("W1", "W2", "W3") if words(sheet.get(k)) >= 8)
    ok(filled == 3, "A21 W1-W3: your reasoning, in your own words",
       f"{filled} of 3 answered with 8 or more words.")

    # ---- GitHub: the gate in your fork
    problem = gh_problem()
    repo = github_repo(ROOT)
    if problem:
        report("SKIP", "A22 GitHub: the fix went through the gate in your "
               "fork", problem)
    elif not repo or repo.lower().startswith("novastack-hq/"):
        ok(False, "A22 `origin` is your own fork on GitHub",
           f"origin is {repo or 'not GitHub'}. Clone your fork.")
    else:
        runs, _ = gh_api(f"repos/{repo}/actions/runs"
                         "?event=pull_request&per_page=50")
        green = [r for r in (runs or {}).get("workflow_runs", [])
                 if r.get("conclusion") == "success"]
        ok(bool(green), "A22 GitHub: a pull request in your fork has a "
           "green check",
           "Enable Actions in your fork, then open the pull request with "
           "base repository = your fork.")
        pulls, _ = gh_api(f"repos/{repo}/pulls?state=closed&per_page=50")
        merged = [p for p in pulls or [] if isinstance(p, dict)
                  and p.get("merged_at")]
        bases = {p.get("base", {}).get("ref") for p in merged}
        ok({"dev", "production"} <= bases,
           "A23 GitHub: merged pull requests into dev (the fix) and "
           "production (the release)",
           f"Merged into: {sorted(b for b in bases if b) or 'nothing'}.")

    cloud = sheet.get("CLOUD", "").strip().lower()
    report("SKIP", "A24 (optional) the cloud deploy",
           "Not checked: look at it in the console, then delete it."
           if cloud in ("yes", "y") else
           "Optional; write `CLOUD: yes` if you did it.")


def main() -> int:
    global OFFLINE
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("level",
                        choices=["beginner", "intermediate", "advanced"])
    parser.add_argument("--offline", action="store_true",
                        help="skip every check that asks GitHub")
    args = parser.parse_args()
    OFFLINE = args.offline
    print(f"Lecture 7 lab self-check: {args.level}\n")
    if not (ROOT / "app.py").exists() or not (OPS / "compose.yaml").exists():
        report("FAIL", "Run from the root of your hollowmere-app folder",
               "cd into the folder that holds app.py and ops/ (not ops/ "
               "itself); `git pull` if ops/ is missing.")
    else:
        {"beginner": beginner, "intermediate": intermediate,
         "advanced": advanced}[args.level]()
    p = sum(1 for r in results if r[0] == "PASS")
    f = sum(1 for r in results if r[0] == "FAIL")
    s = sum(1 for r in results if r[0] == "SKIP")
    print(f"\n{p} passed, {f} failed, {s} skipped")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
