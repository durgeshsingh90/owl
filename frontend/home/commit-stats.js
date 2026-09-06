"use strict";
function weekdayCommitCounts(documents, start, end, calendarDay) {
  const seen = new Set(), counts = Array(7).fill(0);
  for (const doc of documents) {
    if (!doc.commitId || !doc.committedAt) continue;
    const day = calendarDay(doc.committedAt);
    if (!Number.isFinite(day) || day < start || day > end) continue;
    const identity = JSON.stringify([doc.projectId, doc.repo, doc.commitId]);
    if (seen.has(identity)) continue;
    seen.add(identity);
    counts[new Date(day).getUTCDay()]++;
  }
  return counts;
}
if (typeof module !== "undefined") module.exports = weekdayCommitCounts;
