# BugFix Agent - app.py
# Serves the frontend and scans a public GitHub repo with simple rules.
import os
import re
import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=".", static_url_path="")

GITHUB_URL = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
MAX_FILES = 30          # keep the demo fast
MAX_FILE_SIZE = 100_000  # bytes

# (severity, regex, title, suggested fix)
RULES = [
    ("critical",
     re.compile(r"(SELECT|INSERT|UPDATE|DELETE)\b.*(\"\s*\+|'\s*\+|%\s*\w|\{\w+\})", re.I),
     "Possible SQL injection",
     "Use a parameterized query instead of building SQL strings."),
    ("high",
     re.compile(r"\b(eval|exec)\("),
     "Use of eval/exec",
     "Avoid eval/exec on dynamic input; use safe parsing such as ast.literal_eval."),
    ("high",
     re.compile(r"(password|secret|api_key|token)\s*=\s*[\"'][^\"']+[\"']", re.I),
     "Hardcoded secret",
     "Move the secret to an environment variable."),
    ("medium",
     re.compile(r"^\s*except\s*:"),
     "Bare except clause",
     "Catch specific exceptions, e.g. except ValueError:."),
    ("medium",
     re.compile(r"def\s+\w+\(.*=\s*(\[\]|\{\})"),
     "Mutable default argument",
     "Default to None and create the list/dict inside the function."),
    ("medium",
     re.compile(r"^\s*\w+\s*=\s*open\("),
     "File opened without 'with'",
     "Wrap the call in: with open(...) as f:"),
]


def github_get(url):
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")  # optional, raises rate limit
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.get(url, headers=headers, timeout=15)


@app.route("/")
def home():
    return send_from_directory(".", "index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    match = GITHUB_URL.match(url)
    if not match:
        return jsonify(error="Enter a valid GitHub repository URL."), 400
    owner, repo = match.groups()

    info = github_get(f"https://api.github.com/repos/{owner}/{repo}")
    if info.status_code != 200:
        return jsonify(error="Repository not found, private, or rate limited."), 404
    branch = info.json()["default_branch"]

    tree = github_get(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
    if tree.status_code != 200:
        return jsonify(error="Could not read the repository files."), 502
    files = [f for f in tree.json().get("tree", []) if f["type"] == "blob"]
    py_files = [f for f in files
                if f["path"].endswith(".py") and f.get("size", 0) <= MAX_FILE_SIZE][:MAX_FILES]

    issues = []
    for f in py_files:
        raw = requests.get(
            f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{f['path']}",
            timeout=15)
        if raw.status_code != 200:
            continue
        for number, line in enumerate(raw.text.splitlines(), start=1):
            for severity, pattern, title, fix in RULES:
                if pattern.search(line):
                    issues.append({"severity": severity, "file": f["path"],
                                   "line": number, "title": title, "fix": fix})

    return jsonify(name=f"{owner}/{repo}",
                   files_scanned=len(py_files),
                   total_files=len(files),
                   issues=issues[:30])


if __name__ == "__main__":
    app.run(debug=True)