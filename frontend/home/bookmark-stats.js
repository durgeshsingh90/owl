"use strict";
(() => {
  function read(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem(key) || "null") ?? fallback;
    } catch {
      return fallback;
    }
  }
  function renderStats() {
    const saved = read("owl-bookmark-demo", {}),
      deleted = new Set(read("owl-bookmark-deleted", [])),
      snapshot = read("owl-bookmark-overview", null),
      custom = read("owl-bookmark-added", []);
    const fallback = bookmarkSeed.map(
      ([title, url, description, views, last], index) => {
        const parsed = new URL(url);
        if (parsed.hostname === "confluence.example.com") {
          const base = new URL(sampleConfluenceBaseUrl);
          url =
            base.origin +
            base.pathname.replace(/\/$/, "") +
            parsed.pathname.replace(/^\/wiki/, "");
        }
        return {
          id: index + 1,
          title,
          url,
          domain: new URL(url).hostname,
          views,
          added: null,
          lastViewed: null,
          favorite: [0, 2, 6].includes(index),
          pinned: [0, 1].includes(index),
          ...saved[index + 1],
        };
      },
    );
    const source = Array.isArray(snapshot?.bookmarks)
      ? snapshot.bookmarks
      : [...fallback, ...(Array.isArray(custom) ? custom : [])];
    const items = source.filter((item) => !deleted.has(item.id));
    const periodItems = items.filter(
      (item) => item.added && inRange(item.added),
    );
    const weekStart = today - ((new Date(today).getUTCDay() + 6) % 7) * dayMs;
    const addedThisWeek = items.filter(
      (item) =>
        item.added &&
        eventDay(item.added) >= weekStart &&
        eventDay(item.added) <= today,
    ).length;
    const domains = new Map();
    items.forEach((item) =>
      domains.set(item.domain, (domains.get(item.domain) || 0) + 1),
    );
    const monthStart = Date.UTC(
      calendarValue("year"),
      calendarValue("month") - 1,
      1,
    );
    const previousMonthStart = Date.UTC(
      calendarValue("year"),
      calendarValue("month") - 2,
      1,
    );
    const savedThisMonth = items.filter(
      (item) =>
        item.added &&
        eventDay(item.added) >= monthStart &&
        eventDay(item.added) <= today,
    ).length;
    const savedLastMonth = items.filter(
      (item) =>
        item.added &&
        eventDay(item.added) >= previousMonthStart &&
        eventDay(item.added) < monthStart,
    ).length;
    const values = [
      ["Total bookmarks", items.length, "Current library"],
      [
        "Favourites",
        items.filter((item) => item.favorite).length,
        "Your starred bookmarks",
      ],
      [
        "Pinned",
        items.filter((item) => item.pinned).length,
        "Pinned for quick access",
      ],
      ["Domains", domains.size, "Unique domains"],
      [
        "Total opens",
        items.reduce((sum, item) => sum + (Number(item.views) || 0), 0),
        "All-time local totals",
      ],
      [
        "Never viewed",
        items.filter((item) => !item.views).length,
        "Bookmarks with no opens",
      ],
      ["Added this week", addedThisWeek, "Monday–today · known added dates"],
      ["Added in period", periodItems.length, activeRange.label],
      ["Saved this month", savedThisMonth, "Added to OWL · known saved dates"],
      [
        "Saved last month",
        savedLastMonth,
        "Previous calendar month · known saved dates",
      ],
    ];
    document.querySelector("#bookmark-stat-metrics").innerHTML = values
      .map(
        ([label, value, detail]) =>
          `<div class="metric"><p>${escape(label)}</p><strong>${number(value)}</strong><small>${escape(detail)}</small></div>`,
      )
      .join("");
    function bookmarkRow(item, right) {
      return `<div class="repo"><div><strong>${escape(item.title)}</strong><small>${escape(item.domain)}</small></div><span>${escape(right)}</span></div>`;
    }
    document.querySelector("#bookmark-stat-popular").innerHTML =
      [...items]
        .filter((item) => item.views > 0)
        .sort((a, b) => b.views - a.views)
        .slice(0, 20)
        .map((item) => bookmarkRow(item, `${item.views} opens`))
        .join("") || empty("No opened bookmarks yet.");
    document.querySelector("#bookmark-stat-domains").innerHTML =
      [...domains]
        .sort((a, b) => b[1] - a[1])
        .map(([domain, count]) => row(domain, "", `${count} bookmarks`))
        .join("") || empty("No domains yet.");
    document.querySelector("#bookmark-stat-period").textContent =
      activeRange.label;
    document.querySelector("#bookmark-stat-recent").innerHTML =
      periodItems
        .sort((a, b) => b.added - a.added)
        .slice(0, 10)
        .map((item) =>
          bookmarkRow(
            item,
            new Date(item.added).toLocaleDateString("en-GB", {
              day: "numeric",
              month: "short",
              year: "numeric",
            }),
          ),
        )
        .join("") ||
      empty("No bookmarks with known added dates in this period.");
    const notes = read("owl-bookmark-notes", {}),
      groups = read("owl-bookmark-domain-groups", []),
      lastUpdate = read("owl-bookmarks-last-update-all", null);
    const rankedGroups = (Array.isArray(groups) ? groups : [])
      .map((group) => {
        const members = items.filter(
          (item) =>
            Array.isArray(group.domains) && group.domains.includes(item.domain),
        );
        return {
          name: group.name,
          count: members.length,
          opens: members.reduce(
            (sum, item) => sum + (Number(item.views) || 0),
            0,
          ),
        };
      })
      .sort(
        (a, b) =>
          b.opens - a.opens || String(a.name).localeCompare(String(b.name)),
      );
    document.querySelector("#bookmark-stat-groups").innerHTML =
      rankedGroups
        .map((group) =>
          row(
            group.name,
            `${group.count} bookmarks`,
            `${number(group.opens)} opens`,
          ),
        )
        .join("") ||
      empty(
        "Create domain groups in Bookmarks to see the most viewed groups here.",
      );
    document.querySelector("#bookmark-stat-health").innerHTML =
      row(
        "Domain groups",
        "Saved domain collections",
        Array.isArray(groups) ? groups.length : 0,
      ) +
      row(
        "Bookmarks with notes",
        "Non-empty notes",
        items.filter(
          (item) => typeof notes[item.id] === "string" && notes[item.id].trim(),
        ).length,
      ) +
      row("Broken links", "Link checking is not connected", "—") +
      row(
        "Last update all",
        lastUpdate?.status === "succeeded_with_errors"
          ? "Completed with errors"
          : "Last known backend completion",
        lastUpdate?.at
          ? new Date(lastUpdate.at).toLocaleString("en-GB", {
              dateStyle: "medium",
              timeStyle: "short",
            })
          : "Unavailable",
      );
  }
  renderStats();
  window.addEventListener("storage", renderStats);
  window.addEventListener("focus", renderStats);
  document
    .querySelector("#activity-period")
    .addEventListener("change", renderStats);
  document.querySelector("#statistics-range").addEventListener("submit", () => {
    if (!document.querySelector("#range-error").textContent) renderStats();
  });
})();
