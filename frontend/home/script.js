"use strict";
const { projects, documents, people } = overviewData;
const repos = projects.flatMap((project) =>
  project.repos.map((repo) => ({ ...repo, project: project.name })),
);
const total = repos.reduce((sum, repo) => sum + repo.pdfCount, 0);
const number = (value) => value.toLocaleString("en-GB");
const escape = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
// Compare calendar dates in the same timezone as the Bitbucket explorer.
const calendarParts = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Dublin",
  year: "numeric",
  month: "numeric",
  day: "numeric",
}).formatToParts(new Date());
const calendarValue = (type) =>
  Number(calendarParts.find((part) => part.type === type).value);
const cutoff = new Date(
  Date.UTC(calendarValue("year"), calendarValue("month") - 4, 1),
);
const lastDay = new Date(
  Date.UTC(cutoff.getUTCFullYear(), cutoff.getUTCMonth() + 1, 0),
).getUTCDate();
cutoff.setUTCDate(Math.min(calendarValue("day"), lastDay));
const inactiveCount = repos.filter((repo) => {
  const date = new Date(`${repo.lastCommit} 00:00:00 GMT`);
  return Number.isFinite(date.getTime()) && date < cutoff;
}).length;
const metrics = [
  ["PDF library", total, "Across all sample repositories"],
  [
    "Repositories",
    repos.length,
    `${projects.length} projects in your workspace`,
  ],
  ["Contributors", people.length, "People across your repositories"],
  [
    "Document opens",
    documents.reduce((sum, doc) => sum + doc.opens, 0),
    "Across 21 preview documents",
  ],
  ["New PDFs this week", "—", "Awaiting document-added dates · Monday–Sunday"],
  ["New PDFs in period", "—", "Awaiting document-added dates"],
  ["Updated PDFs in period", "—", "Awaiting document update history"],
  [
    "Inactive repositories",
    inactiveCount,
    "No commits for over 3 months · sample data",
  ],
];
document.querySelector("#metrics").innerHTML = metrics
  .map(
    ([label, value, detail]) =>
      `<div class="metric"><p>${label}</p><strong>${number(value)}</strong><small>${detail}</small></div>`,
  )
  .join("");
const projectTotals = projects
  .map((project) => ({
    ...project,
    total: project.repos.reduce((sum, repo) => sum + repo.pdfCount, 0),
  }))
  .sort((a, b) => b.total - a.total);
document.querySelector("#insight-title").textContent =
  `${Math.round((projectTotals[0].total / total) * 100)}% of your document library`;
document.querySelector("#insight-copy").textContent =
  `${projectTotals[0].name} holds the largest collection, with ${projectTotals[0].total} PDFs across ${projectTotals[0].repos.length} repositories.`;
document.querySelector("#project-bars").innerHTML = projectTotals
  .map(
    (project) =>
      `<div class="bar-row"><div class="bar-title"><span>${escape(project.name)}</span><strong>${project.total}</strong></div><div class="track" role="img" aria-label="${escape(project.name)}: ${project.total} PDFs"><span style="width:${(project.total / total) * 100}%"></span></div></div>`,
  )
  .join("");
// Optional event arrays must be complete for their advertised period. Missing arrays
// mean unavailable, while an empty supplied array means a verified zero.
const commits = overviewData.commits;
const opens = overviewData.openEvents;
let activeRange;
const dayMs = 86400000;
const today = Date.UTC(
  calendarValue("year"),
  calendarValue("month") - 1,
  calendarValue("day"),
);
const iso = (day) => new Date(day).toISOString().slice(0, 10);
const empty = (message) => `<p class="empty-stat">${escape(message)}</p>`;
function eventDay(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return NaN;
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Dublin",
    year: "numeric",
    month: "numeric",
    day: "numeric",
  }).formatToParts(date);
  const part = (type) => Number(parts.find((item) => item.type === type).value);
  return Date.UTC(part("year"), part("month") - 1, part("day"));
}
function inRange(value) {
  const day = eventDay(value);
  return (
    Number.isFinite(day) && day >= activeRange.start && day <= activeRange.end
  );
}
function row(name, detail, value) {
  return `<div class="repo"><div><strong>${escape(name)}</strong><small>${escape(detail)}</small></div><span>${escape(value)}</span></div>`;
}
function setMetric(label, value, detail) {
  const index = metrics.findIndex((item) => item[0] === label);
  if (index < 0) return;
  metrics[index] = [label, value, detail];
}
function renderActivity() {
  document.querySelector("#period-label").textContent = activeRange.label;
  document
    .querySelectorAll(".ranking-period")
    .forEach((node) => (node.textContent = activeRange.label));
  const filtered = Array.isArray(commits)
    ? commits.filter((commit) => inRange(commit.committedAt))
    : null;
  const byRepo = new Map(),
    byPerson = new Map();
  if (filtered)
    for (const commit of filtered) {
      byRepo.set(commit.repo, (byRepo.get(commit.repo) || 0) + 1);
      const key = commit.authorEmail || commit.author || "Unknown";
      const person = byPerson.get(key) || {
        name: commit.author || "Unknown",
        count: 0,
        repos: new Set(),
      };
      person.count++;
      person.repos.add(commit.repo);
      byPerson.set(key, person);
    }
  document.querySelector("#active-repos").innerHTML = filtered
    ? Array.from(byRepo)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 10)
        .map(([repo, count]) =>
          row(repo, "Commits in selected period", `${count} commits`),
        )
        .join("") || empty("No commits in this period.")
    : empty(
        "Timestamped commit history is not connected. Monthly repository rankings will appear here.",
      );
  document.querySelector("#contributors").innerHTML = filtered
    ? [...byPerson.values()]
        .sort((a, b) => b.count - a.count)
        .slice(0, 20)
        .map((person) =>
          row(
            person.name,
            `${person.repos.size} repositories`,
            `${person.count} commits`,
          ),
        )
        .join("") || empty("No contributors in this period.")
    : activeRange.all
      ? [...people]
          .sort((a, b) => b.commits - a.commits)
          .slice(0, 20)
          .map((person) =>
            row(person.name, person.repo, `${person.commits} sample commits`),
          )
          .join("")
      : empty(
          "Monthly contributor activity requires timestamped commits. Choose All time to see available sample totals.",
        );
  const docCounts = new Map();
  if (Array.isArray(opens))
    for (const event of opens.filter((event) => inRange(event.openedAt)))
      docCounts.set(event.pdfId, (docCounts.get(event.pdfId) || 0) + 1);
  const ranked = Array.isArray(opens)
    ? documents
        .map((doc) => ({ ...doc, count: docCounts.get(doc.id) || 0 }))
        .filter((doc) => doc.count > 0)
    : activeRange.all
      ? documents.map((doc) => ({ ...doc, count: doc.opens }))
      : null;
  document.querySelector("#popular").innerHTML = ranked
    ? ranked
        .sort((a, b) => b.count - a.count)
        .slice(0, 20)
        .map((doc) => {
          const repo = repos.find((repo) => repo.name === doc.repo);
          if (!repo) return "";
          const url =
            repo.baseUrl +
            "/src/main/" +
            doc.path
              .replace(/^\//, "")
              .split("/")
              .map(encodeURIComponent)
              .join("/");
          return `<a class="doc" href="${escape(url)}" target="_blank" rel="noopener noreferrer"><span class="pdf-icon">PDF</span><div><strong>${escape(doc.name)}</strong><small>${escape(doc.repo)}</small></div><span>${doc.count} opens ↗</span></a>`;
        })
        .join("") || empty("No PDF opens in this period.")
    : empty(
        "Dated open events are not connected. Choose All time to see the top 20 PDFs by sample opens.",
      );
  document.querySelector("#recent").innerHTML = filtered
    ? [...filtered]
        .sort((a, b) => new Date(b.committedAt) - new Date(a.committedAt))
        .slice(0, 10)
        .map((commit) =>
          row(
            commit.message || commit.hash || "Commit",
            `${commit.repo} · ${commit.author || "Unknown"}`,
            new Date(commit.committedAt).toLocaleString("en-GB", {
              timeZone: "Europe/Dublin",
              dateStyle: "medium",
              timeStyle: "short",
            }),
          ),
        )
        .join("") || empty("No commits in this period.")
    : empty(
        "Full commit history is not connected. Repository last-commit dates alone cannot identify the latest 10 commits.",
      );
  // Added/updated counts need coverage of the whole library, not just preview rows.
  if (Array.isArray(overviewData.documentEvents)) {
    const events = overviewData.documentEvents;
    const unique = (kind, predicate) =>
      new Set(
        events
          .filter((event) => event.kind === kind && predicate(event.at))
          .map((event) => event.pdfId),
      ).size;
    const monday = today - ((new Date(today).getUTCDay() + 6) % 7) * dayMs;
    setMetric(
      "New PDFs this week",
      unique("added", (at) => eventDay(at) >= monday && eventDay(at) <= today),
      "Monday–today · Dublin calendar",
    );
    setMetric(
      "New PDFs in period",
      unique("added", inRange),
      activeRange.label,
    );
    setMetric(
      "Updated PDFs in period",
      unique("updated", inRange),
      activeRange.label,
    );
  }
  document.querySelector("#metrics").innerHTML = metrics
    .map(
      ([label, value, detail]) =>
        `<div class="metric"><p>${escape(label)}</p><strong>${number(value)}</strong><small>${escape(detail)}</small></div>`,
    )
    .join("");
}
function applyPeriod() {
  const mode = document.querySelector("#activity-period").value;
  document.querySelector("#custom-period").hidden = mode !== "custom";
  const monthStart = Date.UTC(
    calendarValue("year"),
    calendarValue("month") - 1,
    1,
  );
  document.querySelector("#range-error").textContent = "";
  if (mode === "custom") {
    const from = document.querySelector("#activity-from").value,
      to = document.querySelector("#activity-to").value;
    if (!from || !to) {
      document.querySelector("#range-error").textContent =
        "Choose both dates, then Apply.";
      return;
    }
    if (from > to) {
      document.querySelector("#range-error").textContent =
        "From must be on or before To.";
      return;
    }
    activeRange = {
      start: Date.parse(from),
      end: Date.parse(to),
      label: `${from} – ${to}`,
    };
  } else if (mode === "all")
    activeRange = {
      start: -Infinity,
      end: Infinity,
      all: true,
      label: "All time",
    };
  else if (mode === "last-month")
    activeRange = {
      start: Date.UTC(calendarValue("year"), calendarValue("month") - 2, 1),
      end: monthStart - dayMs,
      label: "Last month",
    };
  else activeRange = { start: monthStart, end: today, label: "This month" };
  renderActivity();
}
document.querySelector("#activity-from").value = iso(
  Date.UTC(calendarValue("year"), calendarValue("month") - 1, 1),
);
document.querySelector("#activity-to").value = iso(today);
document
  .querySelector("#activity-period")
  .addEventListener("change", applyPeriod);
document
  .querySelector("#statistics-range")
  .addEventListener("submit", (event) => {
    event.preventDefault();
    applyPeriod();
  });
document.querySelector("#inactive-repos").innerHTML =
  repos
    .filter((repo) => new Date(`${repo.lastCommit} 00:00:00 GMT`) < cutoff)
    .map((repo) =>
      row(repo.name, repo.project, `Last commit ${repo.lastCommit}`),
    )
    .join("") ||
  empty(
    "No sample repositories have been inactive for more than three months.",
  );
applyPeriod();
