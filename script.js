// BugFix Agent - script.js
// Frontend-only behavior. No backend or API calls yet.

const form = document.getElementById("repo-form");
const input = document.getElementById("repo-url");
const analyzeBtn = document.getElementById("analyze-btn");
const message = document.getElementById("repo-msg");
const repoName = document.getElementById("repo-name");
const downloadBtn = document.getElementById("download-btn");

// Matches https://github.com/owner/repo (optional .git or trailing slash)
const GITHUB_URL = /^https:\/\/github\.com\/([\w.-]+)\/([\w.-]+?)(?:\.git)?\/?$/;

// Show a status message under the input
function showMessage(text, isError = false) {
  message.textContent = text;
  message.classList.toggle("form-msg--error", isError);
}

// Analyze Repository: validate, show loading state, update repo name
form.addEventListener("submit", (event) => {
  // Build one bug card from a finding (textContent keeps repo data from injecting HTML)
function bugCard(issue) {
  const card = document.createElement("article");
  card.className = `card bug bug--${issue.severity}`;

  const top = document.createElement("div");
  top.className = "bug__top";
  const sev = document.createElement("span");
  sev.className = `severity severity--${issue.severity}`;
  sev.textContent = issue.severity[0].toUpperCase() + issue.severity.slice(1);
  const loc = document.createElement("span");
  loc.className = "bug__loc";
  loc.textContent = `${issue.file} line ${issue.line}`;
  top.append(sev, loc);

  const title = document.createElement("h3");
  title.textContent = issue.title;

  const fix = document.createElement("div");
  fix.className = "fix";
  const fixTitle = document.createElement("p");
  fixTitle.className = "fix__title";
  fixTitle.textContent = "Suggested fix";
  const fixText = document.createElement("p");
  fixText.textContent = issue.fix;
  fix.append(fixTitle, fixText);

  card.append(top, title, fix);
  return card;
}

// Analyze Repository: call the backend and fill in the dashboard
form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const url = input.value.trim();
  if (!GITHUB_URL.test(url)) {
    showMessage("Enter a valid URL, like https://github.com/owner/repository", true);
    input.focus();
    return;
  }

  analyzeBtn.disabled = true;
  analyzeBtn.textContent = "Analyzing...";
  showMessage("Scanning repository...");

  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Analysis failed.");

    repoName.textContent = data.name;
    document.getElementById("files-scanned").textContent = data.files_scanned;
    document.getElementById("files-note").textContent =
      `Python files, of ${data.total_files} total`;
    document.getElementById("issues-found").textContent = data.issues.length;

    const counts = { critical: 0, high: 0, medium: 0 };
    data.issues.forEach((i) => counts[i.severity]++);
    document.getElementById("issues-note").textContent =
      `${counts.critical} critical, ${counts.high} high, ${counts.medium} medium`;

    const grid = document.getElementById("findings-grid");
    grid.replaceChildren(...data.issues.map(bugCard));
    if (data.issues.length === 0) grid.textContent = "No issues found by the current rules.";

    showMessage(`Scan complete for ${data.name}.`);
  } catch (err) {
    showMessage(err.message, true);
  } finally {
    analyzeBtn.disabled = false;
    analyzeBtn.textContent = "Analyze Repository";
  }
});
});

// Download Report: saves a sample text file
downloadBtn.addEventListener("click", () => {
  const report = [
    "Dependency Exit Capsule (sample data)",
    `Repository: ${repoName.textContent}`,
    "Files scanned: 128",
    "Issues found: 7 (1 critical, 2 high, 4 medium)",
    "Tests passing after fixes: 42 / 45",
  ].join("\n");

  const blob = new Blob([report], { type: "text/plain" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "dependency-exit-capsule.txt";
  link.click();
  URL.revokeObjectURL(link.href);
});