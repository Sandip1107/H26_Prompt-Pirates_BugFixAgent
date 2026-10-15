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
  event.preventDefault();

  const match = input.value.trim().match(GITHUB_URL);
  if (!match) {
    showMessage("Enter a valid URL, like https://github.com/owner/repository", true);
    input.focus();
    return;
  }

  const name = `${match[1]}/${match[2]}`;
  analyzeBtn.disabled = true;
  analyzeBtn.textContent = "Analyzing...";
  showMessage(`Analyzing ${name}...`);

  // Fake delay to simulate a scan
  setTimeout(() => {
    repoName.textContent = name;
    analyzeBtn.disabled = false;
    analyzeBtn.textContent = "Analyze Repository";
    showMessage(`Done. Showing sample results for ${name}.`);
  }, 1200);
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