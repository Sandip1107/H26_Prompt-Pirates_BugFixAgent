"""
BugFix Agent - Flask backend.

Pipeline: validate GitHub URL -> fetch metadata + supported source files ->
static analysis -> (optional) open-weight AI fix recommendations ->
(optional) tests inside Docker -> Markdown / JSON "Dependency Exit Capsule".

Evidence, AI output and test results are stored and reported separately.
The original repository is never modified; fixes are only applied to a
temporary copy, and only when a Docker sandbox is available.
"""
import ast, io, json, os, re, shutil, stat, subprocess, tempfile, time, uuid, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).parent.resolve()
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # request bodies are small JSON

# ---- Configuration (environment variables only; never sent to the browser) ----
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://router.huggingface.co/v1").rstrip("/")
AI_API_KEY = os.environ.get("HF_TOKEN") or os.environ.get("AI_API_KEY")
AI_MODEL = os.environ.get("AI_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct")  # open-weight (Apache-2.0)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # optional, raises GitHub rate limit
ALLOW_NETWORK = os.environ.get("VERIFY_ALLOW_NETWORK") == "1"  # let sandbox pip-install deps

GITHUB_API = "https://api.github.com"
LANG_BY_EXT = {".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".jsx": "JavaScript",
               ".ts": "TypeScript", ".tsx": "TypeScript"}
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", ".git", "venv", ".venv", "__pycache__", "site-packages"}
MANIFESTS = {"requirements.txt", "pyproject.toml", "setup.py", "Pipfile", "package.json",
             "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Dockerfile"}
MAX_FILES, MAX_FILE_BYTES = 40, 100_000
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
ANALYSES = {}  # in-memory store: analysis_id -> dict


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify({"error": e.message}), e.status


@app.errorhandler(Exception)
def handle_unexpected(e):
    code = getattr(e, "code", 500)
    if code == 500:
        app.logger.exception("Unhandled error")
    return jsonify({"error": "Unexpected server error." if code == 500 else str(e)}), code


# ------------------------------ Static files ------------------------------
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/style.css")
def css():
    return send_from_directory(BASE_DIR, "style.css")


@app.get("/script.js")
def js():
    return send_from_directory(BASE_DIR, "script.js")


# ------------------------------ GitHub helpers ------------------------------
def gh_headers():
    h = {"Accept": "application/vnd.github+json", "User-Agent": "bugfix-agent"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def gh_get(url, **kw):
    """GET from GitHub with clear, specific error messages."""
    try:
        r = requests.get(url, headers=gh_headers(), timeout=20, **kw)
    except requests.RequestException as e:
        raise ApiError(f"Could not reach GitHub: {e.__class__.__name__}", 502)
    if r.status_code == 404:
        raise ApiError("Repository not found (it may be private or the URL is wrong).", 404)
    if r.status_code == 401:
        raise ApiError("GitHub rejected GITHUB_TOKEN. Check that it was copied correctly and has not expired.", 401)
    if r.status_code in (403, 429):
        if r.status_code == 429 or r.headers.get("X-RateLimit-Remaining") == "0":
            reset = r.headers.get("X-RateLimit-Reset", "")
            when = datetime.fromtimestamp(int(reset)).strftime("%H:%M") if reset.isdigit() else "later"
            hint = "The token's hourly limit is used up." if GITHUB_TOKEN else "Set GITHUB_TOKEN to raise the limit."
            raise ApiError(f"GitHub API rate limit reached; it resets around {when}. {hint}", 429)
        raise ApiError("GitHub refused the request (HTTP 403). The repository may be restricted.", 403)
    if not r.ok:
        raise ApiError(f"GitHub returned HTTP {r.status_code}.", 502)
    return r


def parse_github_url(url):
    m = re.match(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", (url or "").strip())
    if not m:
        raise ApiError("Enter a public repository URL like https://github.com/owner/repo")
    return m.group(1), m.group(2)


def fetch_repo(owner, repo):
    meta = gh_get(f"{GITHUB_API}/repos/{owner}/{repo}").json()
    branch = meta.get("default_branch", "main")
    langs = gh_get(f"{GITHUB_API}/repos/{owner}/{repo}/languages").json()
    tree = gh_get(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{quote(branch)}?recursive=1").json()
    return meta, branch, langs, tree


def fetch_file(owner, repo, branch, path):
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(branch)}/{quote(path)}"
    try:
        r = requests.get(url, timeout=20)
        return path, (r.content.decode("utf-8", errors="replace") if r.ok else None)
    except requests.RequestException:
        return path, None


# ------------------------------ Static analysis ------------------------------
def make_finding(path, line, rule, title, severity, explanation, text):
    lines = text.splitlines()
    evidence = ""
    if line:
        lo, hi = max(1, line - 1), min(len(lines), line + 1)
        evidence = "\n".join(f"{n:>4} | {lines[n - 1]}" for n in range(lo, hi + 1))
    return {"file": path, "line": line, "rule": rule, "title": title, "severity": severity,
            "explanation": explanation, "evidence": evidence, "source": "static-analysis"}


def analyze_python(path, text):
    out = []
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [make_finding(path, e.lineno, "py-syntax-error", "File does not parse", "high",
                             f"Python reported: {e.msg}. The file cannot be imported as written "
                             "(it may target a different Python version).", text)]
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                out.append(make_finding(path, node.lineno, "py-bare-except", "Bare except clause", "medium",
                           "A bare 'except:' also catches KeyboardInterrupt and SystemExit and can hide real errors.", text))
            if all(isinstance(s, ast.Pass) for s in node.body):
                out.append(make_finding(path, node.lineno, "py-swallowed-exception", "Exception silently ignored", "medium",
                           "The handler body is only 'pass', so failures disappear without being logged.", text))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = node.args.defaults + [d for d in node.args.kw_defaults if d is not None]
            for d in defaults:
                is_call = isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id in {"list", "dict", "set"}
                if isinstance(d, (ast.List, ast.Dict, ast.Set)) or is_call:
                    out.append(make_finding(path, node.lineno, "py-mutable-default", f"Mutable default argument in {node.name}()",
                               "medium", "Default values are created once and shared between calls, so mutations leak across calls.", text))
        elif isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(right, ast.Constant) and right.value is None:
                    out.append(make_finding(path, node.lineno, "py-none-equality", "Comparison to None with ==/!=", "low",
                               "PEP 8 recommends 'is None' / 'is not None'; == can be overridden by custom __eq__.", text))
                if (isinstance(op, (ast.Is, ast.IsNot)) and isinstance(right, ast.Constant)
                        and isinstance(right.value, (str, bytes, int, float)) and not isinstance(right.value, bool)):
                    out.append(make_finding(path, node.lineno, "py-is-literal", "Identity comparison with a literal", "medium",
                               "'is' checks object identity, not equality; results with literals are implementation-dependent.", text))
        elif isinstance(node, ast.Assert) and isinstance(node.test, ast.Tuple) and node.test.elts:
            out.append(make_finding(path, node.lineno, "py-assert-tuple", "Assertion on a non-empty tuple", "high",
                       "A non-empty tuple is always truthy, so this assertion can never fail.", text))
    return out


JS_RULES = [
    ("js-empty-catch", re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*\}"), "Empty catch block", "medium",
     "Errors are caught and discarded, which hides failures."),
    ("js-async-foreach", re.compile(r"\.forEach\(\s*async"), "async callback inside forEach", "medium",
     "forEach does not await async callbacks, so code after it may run before the work finishes."),
    ("js-parseint-radix", re.compile(r"parseInt\(\s*[^,()]+\)"), "parseInt without a radix", "low",
     "Always pass a radix (e.g. parseInt(x, 10)) to avoid surprising results."),
    ("js-loose-equality", re.compile(r"(?<![=!<>])(==|!=)(?!=)"), "Loose equality (==/!=)", "low",
     "Loose equality performs type coercion. Pattern match only: strings or comments could trigger it."),
]


def analyze_js(path, text):
    out, per_rule = [], {}
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(("//", "*", "/*")):
            continue
        for rule, rx, title, sev, why in JS_RULES:
            if rx.search(line) and per_rule.get(rule, 0) < 3:  # cap noisy rules per file
                per_rule[rule] = per_rule.get(rule, 0) + 1
                out.append(make_finding(path, i, rule, title, sev, why, text))
    return out


def analyze_todos(path, text):
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(r"(#|//|/\*)\s*(TODO|FIXME|HACK|XXX)\b", line) and len(out) < 5:
            out.append(make_finding(path, i, "todo-marker", "Unresolved TODO/FIXME marker", "info",
                       "A maintainer left a note marking unfinished or fragile code.", text))
    return out


def analyze_file(path, text):
    ext = Path(path).suffix.lower()
    found = analyze_python(path, text) if ext == ".py" else analyze_js(path, text)
    return found + analyze_todos(path, text)


# ------------------------------ AI integration ------------------------------
def ai_configured():
    return bool(AI_API_KEY) or "AI_BASE_URL" in os.environ  # local servers (e.g. Ollama) need no key


def call_ai(messages):
    headers = {"Content-Type": "application/json"}
    if AI_API_KEY:
        headers["Authorization"] = f"Bearer {AI_API_KEY}"
    try:
        r = requests.post(f"{AI_BASE_URL}/chat/completions", headers=headers, timeout=90,
                          json={"model": AI_MODEL, "messages": messages, "temperature": 0.1, "max_tokens": 900})
    except requests.RequestException as e:
        raise ApiError(f"AI endpoint unreachable: {e.__class__.__name__}", 502)
    if r.status_code in (401, 403):
        raise ApiError("AI credentials were rejected. Check HF_TOKEN / AI_API_KEY.", 502)
    if r.status_code == 402:
        raise ApiError("AI usage credits are exhausted for this token. Add credits, use another token, "
                       "or run a local open-weight model by setting AI_BASE_URL and AI_MODEL.", 502)
    if r.status_code == 429:
        raise ApiError("AI rate limit reached. Wait a minute and try again, or use a different token or model.", 502)
    if not r.ok:
        raise ApiError(f"AI endpoint returned HTTP {r.status_code}.", 502)
    try:
        return r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError):
        raise ApiError("AI endpoint returned an unexpected response format.", 502)


def parse_ai_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        raise ApiError("The model did not return valid JSON. Try again.", 502)


def build_fix_prompt(finding, file_text):
    lines = file_text.splitlines()
    lo, hi = max(0, (finding["line"] or 1) - 16), min(len(lines), (finding["line"] or 1) + 15)
    snippet = "\n".join(lines[lo:hi])
    system = ("You are a careful code maintenance assistant. Reply with ONLY a JSON object with keys: "
              "explanation (string), original_snippet (exact text copied from the code, as few lines as possible), "
              "fixed_snippet (replacement text), confidence (low|medium|high), caveats (string). "
              "Do not invent code that is not shown.")
    user = (f"File: {finding['file']}\nIssue: {finding['title']} (line {finding['line']})\n"
            f"Static analysis note: {finding['explanation']}\n\nCode (lines {lo + 1}-{hi}):\n{snippet}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ------------------------------ Analysis state ------------------------------
def get_analysis(aid):
    a = ANALYSES.get(aid)
    if not a:
        raise ApiError("Analysis not found or expired. Please run the analysis again.", 404)
    return a


def public_view(a):
    return {k: v for k, v in a.items() if k != "files"}


@app.get("/api/status")
def api_status():
    ok, reason = docker_available()
    return jsonify({"ai": {"configured": ai_configured(), "model": AI_MODEL,
                           "message": None if ai_configured() else
                           "AI is not configured. Set HF_TOKEN (or AI_API_KEY / AI_BASE_URL); deterministic analysis still works."},
                    "sandbox": {"available": ok, "message": None if ok else reason}})


@app.post("/api/analyze")
def api_analyze():
    owner, repo = parse_github_url((request.get_json(silent=True) or {}).get("url"))
    meta, branch, langs, tree = fetch_repo(owner, repo)
    items = tree.get("tree", [])
    manifests = sorted({i["path"] for i in items if i["type"] == "blob" and Path(i["path"]).name in MANIFESTS
                        and not set(Path(i["path"]).parts) & SKIP_DIRS})[:30]
    candidates = [i for i in items if i["type"] == "blob" and Path(i["path"]).suffix.lower() in LANG_BY_EXT
                  and not set(Path(i["path"]).parts) & SKIP_DIRS and not i["path"].endswith(".min.js")
                  and i.get("size", 0) <= MAX_FILE_BYTES]
    candidates.sort(key=lambda i: (i["path"].count("/"), i["path"]))
    chosen = [i["path"] for i in candidates[:MAX_FILES]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        files = {p: t for p, t in pool.map(lambda p: fetch_file(owner, repo, branch, p), chosen) if t is not None}

    findings = []
    for path, text in files.items():
        findings += analyze_file(path, text)
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["file"], f["line"] or 0))
    for n, f in enumerate(findings, 1):
        f["id"] = f"F{n}"

    unsupported = sorted(l for l in langs if l not in set(LANG_BY_EXT.values()))
    notes = []
    if not chosen:
        notes.append("No supported source files (Python, JavaScript, TypeScript) were found.")
    if tree.get("truncated"):
        notes.append("GitHub truncated the file tree for this large repository; some files were not considered.")
    if len(candidates) > MAX_FILES:
        notes.append(f"Only the first {MAX_FILES} of {len(candidates)} supported files were scanned.")
    if unsupported:
        notes.append("Not analyzed (unsupported languages): " + ", ".join(unsupported[:8]))

    aid = uuid.uuid4().hex[:12]
    ANALYSES[aid] = {
        "id": aid, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": {"owner": owner, "name": repo, "url": meta["html_url"], "description": meta.get("description"),
                 "branch": branch, "stars": meta.get("stargazers_count"), "forks": meta.get("forks_count"),
                 "license": (meta.get("license") or {}).get("spdx_id"), "pushed_at": meta.get("pushed_at"),
                 "languages": langs},
        "files": files, "files_scanned": sorted(files), "findings": findings, "manifests": manifests,
        "notes": notes, "fixes": {}, "decisions": {}, "verification": None,
    }
    while len(ANALYSES) > 20:
        ANALYSES.pop(next(iter(ANALYSES)))
    return jsonify(public_view(ANALYSES[aid]))


@app.post("/api/fix")
def api_fix():
    body = request.get_json(silent=True) or {}
    a = get_analysis(body.get("analysis_id"))
    finding = next((f for f in a["findings"] if f["id"] == body.get("finding_id")), None)
    if not finding:
        raise ApiError("Finding not found.", 404)
    if not ai_configured():
        raise ApiError("AI is not configured, so no recommendation was generated. Set HF_TOKEN and restart.", 503)
    if not finding["line"]:
        raise ApiError("This finding has no line number, so AI context cannot be built.")
    data = parse_ai_json(call_ai(build_fix_prompt(finding, a["files"][finding["file"]])))
    original, fixed = str(data.get("original_snippet", "")), str(data.get("fixed_snippet", ""))
    fix = {"explanation": str(data.get("explanation", "")), "original_snippet": original, "fixed_snippet": fixed,
           "confidence": str(data.get("confidence", "unknown")), "caveats": str(data.get("caveats", "")),
           # The patch can only be applied for testing if the quoted text is found exactly once.
           "snippet_matches_file": bool(original) and a["files"][finding["file"]].count(original) == 1,
           "model": AI_MODEL, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    a["fixes"][finding["id"]] = fix
    return jsonify(fix)


@app.post("/api/decision")
def api_decision():
    body = request.get_json(silent=True) or {}
    a = get_analysis(body.get("analysis_id"))
    if body.get("decision") not in {"accepted", "rejected", "pending"} or body.get("finding_id") not in a["fixes"]:
        raise ApiError("Invalid decision or no AI recommendation exists for that finding.")
    a["decisions"][body["finding_id"]] = body["decision"]
    return jsonify({"ok": True})


# ------------------------------ Sandboxed verification ------------------------------
_docker_cache = {"t": 0, "v": (False, "")}


def docker_available():
    if time.time() - _docker_cache["t"] < 30:
        return _docker_cache["v"]
    if not shutil.which("docker"):
        v = (False, "Docker is not installed; tests are skipped because repository code is never run on the host.")
    else:
        try:
            ok = subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
            v = (ok, "" if ok else "Docker is installed but its daemon is not running; tests are skipped.")
        except (subprocess.SubprocessError, OSError):
            v = (False, "Docker could not be queried; tests are skipped.")
    _docker_cache.update(t=time.time(), v=v)
    return v


def download_repo(owner, repo, branch, dest):
    """Download the branch zip and extract it safely (no path escapes, no symlinks)."""
    r = gh_get(f"{GITHUB_API}/repos/{owner}/{repo}/zipball/{quote(branch)}", stream=True)
    buf, size = io.BytesIO(), 0
    for chunk in r.iter_content(65536):
        size += len(chunk)
        if size > 30 * 1024 * 1024:
            raise ApiError("Repository archive is larger than 30 MB; tests skipped.", 413)
        buf.write(chunk)
    with zipfile.ZipFile(buf) as z:
        if sum(i.file_size for i in z.infolist()) > 150 * 1024 * 1024:
            raise ApiError("Extracted repository is too large; tests skipped.", 413)
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest.resolve())) or stat.S_ISLNK(info.external_attr >> 16):
                continue
            z.extract(info, dest)
    roots = [p for p in dest.iterdir() if p.is_dir()]
    return roots[0] if len(roots) == 1 else dest


PY_CMD = ("if python -c 'import pytest' 2>/dev/null; then python -m pytest -q -x -p no:cacheprovider; "
          "else python -m unittest discover -v; fi")
PY_INSTALL = "([ -f requirements.txt ] && pip install -q -r requirements.txt); pip install -q pytest; "


def detect_suites(root):
    suites, skipped = [], []
    has_py = any((p.name.startswith("test_") or p.name.endswith("_test.py"))
                 for p in root.rglob("*.py") if not set(p.relative_to(root).parts) & SKIP_DIRS)
    if has_py:
        suites.append({"name": "python", "image": "python:3.12-slim",
                       "cmd": (PY_INSTALL if ALLOW_NETWORK else "") + PY_CMD})
    pkg_file = root / "package.json"
    if pkg_file.exists():
        try:
            pkg = json.loads(pkg_file.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            pkg = {}
        script = (pkg.get("scripts") or {}).get("test", "")
        if not script or "no test specified" in script:
            skipped.append("package.json has no test script.")
        elif (pkg.get("dependencies") or pkg.get("devDependencies")) and not ALLOW_NETWORK:
            skipped.append("Node tests need npm dependencies, but the sandbox has no network. Set VERIFY_ALLOW_NETWORK=1 to allow installs.")
        else:
            install = "npm install --silent --no-audit --no-fund && " if ALLOW_NETWORK else ""
            suites.append({"name": "node", "image": "node:20-slim", "cmd": install + "npm test"})
    if not suites and not skipped:
        skipped.append("No Python or Node test files were detected in the repository.")
    return suites, skipped


def run_suite(root, suite):
    name = "bugfix-" + uuid.uuid4().hex[:10]
    cmd = ["docker", "run", "--rm", "--name", name, "--memory", "512m", "--cpus", "1", "--pids-limit", "256",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-v", f"{root}:/work", "-w", "/work",
           "-e", "PYTHONDONTWRITEBYTECODE=1"]
    if not ALLOW_NETWORK:
        cmd += ["--network", "none"]
    cmd += [suite["image"], "sh", "-c", suite["cmd"]]
    start = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=240, errors="replace")
        status = "passed" if p.returncode == 0 else ("no_tests" if p.returncode == 5 else "failed")
        output, code = (p.stdout + p.stderr)[-4000:], p.returncode
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        status, output, code = "timeout", "Stopped after 240 seconds.", None
    except OSError as e:
        status, output, code = "error", str(e), None
    return {"suite": suite["name"], "image": suite["image"], "command": suite["cmd"], "status": status,
            "exit_code": code, "seconds": round(time.time() - start, 1), "output": output}


def apply_fixes(a, root):
    applied, skipped = [], []
    for fid, decision in a["decisions"].items():
        fix, finding = a["fixes"].get(fid), next((f for f in a["findings"] if f["id"] == fid), None)
        if decision != "accepted" or not fix or not finding:
            continue
        target = root / finding["file"]
        text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        if fix["snippet_matches_file"] and text.count(fix["original_snippet"]) == 1:
            target.write_text(text.replace(fix["original_snippet"], fix["fixed_snippet"]), encoding="utf-8")
            applied.append(fid)
        else:
            skipped.append({"finding_id": fid, "reason": "Quoted original snippet was not found exactly once in the file."})
    return applied, skipped


@app.post("/api/verify")
def api_verify():
    a = get_analysis((request.get_json(silent=True) or {}).get("analysis_id"))
    base = {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "executed": False,
            "isolation": "Docker container, " + ("network enabled (VERIFY_ALLOW_NETWORK=1)" if ALLOW_NETWORK else "network disabled"),
            "baseline": [], "with_fixes": [], "applied_fixes": [], "skipped_fixes": [], "reasons": []}
    ok, reason = docker_available()
    if not ok:
        base["reasons"].append(reason)
        a["verification"] = base
        return jsonify(base)
    with tempfile.TemporaryDirectory(prefix="bugfix-") as tmp:
        tmp = Path(tmp)
        repo = a["repo"]
        root = download_repo(repo["owner"], repo["name"], repo["branch"], tmp / "base")
        suites, skipped = detect_suites(root)
        base["reasons"] += skipped
        if suites:
            base["executed"] = True
            base["baseline"] = [run_suite(root, s) for s in suites]
            if any(d == "accepted" for d in a["decisions"].values()):
                patched = tmp / "patched"
                shutil.copytree(root, patched)  # fixes touch only this temporary copy
                base["applied_fixes"], base["skipped_fixes"] = apply_fixes(a, patched)
                if base["applied_fixes"]:
                    base["with_fixes"] = [run_suite(patched, s) for s in suites]
            else:
                base["reasons"].append("No fixes were accepted, so only the unmodified repository was tested.")
    a["verification"] = base
    return jsonify(base)


# ------------------------------ Reports ------------------------------
def report_data(a):
    items = []
    for f in a["findings"]:
        item = {k: f[k] for k in ("id", "file", "line", "rule", "title", "severity", "evidence", "explanation")}
        item["ai_recommendation"] = a["fixes"].get(f["id"])
        item["user_decision"] = a["decisions"].get(f["id"], "pending") if f["id"] in a["fixes"] else None
        items.append(item)
    return {"report_type": "Dependency Exit Capsule", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": "BugFix Agent", "repository": a["repo"], "files_scanned": a["files_scanned"],
            "dependency_manifests": a["manifests"], "notes": a["notes"],
            "ai": {"used": bool(a["fixes"]), "model": AI_MODEL if a["fixes"] else None},
            "findings": items, "verification": a["verification"],
            "disclaimer": "Findings are static-analysis observations. AI recommendations are unverified suggestions. "
                          "Only the 'verification' section reflects tests that were actually executed."}


def report_markdown(a):
    d, r = report_data(a), a["repo"]
    L = [f"# Dependency Exit Capsule: {r['owner']}/{r['name']}", "",
         f"Generated {d['generated_at']} by BugFix Agent. Branch `{r['branch']}`, {len(d['files_scanned'])} files scanned.", "",
         "## Repository", f"- URL: {r['url']}", f"- License: {r['license'] or 'not detected'}",
         f"- Languages: {', '.join(r['languages']) or 'unknown'}", "", "## Dependency manifests"]
    L += [f"- `{m}`" for m in d["dependency_manifests"]] or ["- None found"]
    if d["notes"]:
        L += ["", "## Notes"] + [f"- {n}" for n in d["notes"]]
    L += ["", "## Findings", "_Sections per finding: observed evidence (from static analysis), AI recommendation (unverified), then test results below._"]
    if not d["findings"]:
        L += ["", "No findings were produced by the deterministic rules."]
    for f in d["findings"]:
        L += ["", f"### {f['id']} [{f['severity'].upper()}] {f['title']}", f"`{f['file']}`" + (f" line {f['line']}" if f["line"] else ""), "",
              "**Observed evidence**", "```", f["evidence"] or "(no line available)", "```", f"Explanation: {f['explanation']}"]
        ai = f["ai_recommendation"]
        if ai:
            L += ["", f"**AI recommendation** (model `{ai['model']}`, confidence {ai['confidence']}; not verified)", ai["explanation"],
                  "", "Original:", "```", ai["original_snippet"], "```", "Proposed:", "```", ai["fixed_snippet"], "```"]
            if ai["caveats"]:
                L.append(f"Caveats: {ai['caveats']}")
            L.append(f"Reviewer decision: {f['user_decision']}")
    L += ["", "## Test results (executed)"]
    v = d["verification"]
    if not v:
        L.append("Tests were not run.")
    else:
        L += [f"Isolation: {v['isolation']}"] + [f"- Note: {x}" for x in v["reasons"]]
        for title, runs in (("Unmodified repository", v["baseline"]), ("With accepted fixes applied to a temporary copy", v["with_fixes"])):
            for run in runs:
                L += ["", f"**{title}: {run['suite']} -> {run['status']}** (exit {run['exit_code']}, {run['seconds']}s)", "```", run["output"], "```"]
    L += ["", "---", d["disclaimer"]]
    return "\n".join(L)


@app.get("/api/report/<aid>/<fmt>")
def api_report(aid, fmt):
    a = get_analysis(aid)
    name = f"dependency-exit-capsule-{a['repo']['name']}"
    if fmt == "json":
        body, mime = json.dumps(report_data(a), indent=2), "application/json"
    elif fmt == "md":
        body, mime = report_markdown(a), "text/markdown"
    else:
        raise ApiError("Report format must be 'md' or 'json'.", 404)
    return Response(body, mimetype=mime, headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)