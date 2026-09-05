"use strict";
const PEOPLE_PREFERENCES_KEY = "owl-bitbucket-people-v1";
let peoplePreferences = loadPeoplePreferences();
let activePeopleFilter = "all";

function loadPeoplePreferences() {
  try {
    const value = JSON.parse(
      localStorage.getItem(PEOPLE_PREFERENCES_KEY) || "{}",
    );
    return {
      stars: Array.isArray(value.stars)
        ? value.stars.filter((item) => typeof item === "string")
        : [],
      teams: Array.isArray(value.teams)
        ? value.teams.filter(
            (team) =>
              team &&
              typeof team.id === "string" &&
              typeof team.name === "string" &&
              Array.isArray(team.members) &&
              team.members.every((member) => typeof member === "string"),
          )
        : [],
    };
  } catch {
    return { stars: [], teams: [] };
  }
}

function savePeoplePreferences(next) {
  try {
    localStorage.setItem(PEOPLE_PREFERENCES_KEY, JSON.stringify(next));
  } catch {
    showToast(
      "Could not save stars or teams: browser storage is unavailable.",
      false,
    );
    return false;
  }
  peoplePreferences = next;
  return true;
}

function personKey(person) {
  return person.email.toLocaleLowerCase();
}
function pdfAuthorKey(pdf) {
  if (pdf.commitAuthorEmail) return pdf.commitAuthorEmail.toLocaleLowerCase();
  // Resolve the explicit sample author, not every contributor in the repository.
  const matches = people.filter(
    (person) =>
      person.name === pdf.commitAuthor &&
      person.repo === pdf.repo &&
      person.projectId === pdf.projectId,
  );
  return matches.length === 1 ? personKey(matches[0]) : null;
}
function isPersonStarred(key) {
  return key !== null && peoplePreferences.stars.includes(key);
}
function matchesPeopleFilter(key) {
  if (activePeopleFilter === "all") return true;
  if (activePeopleFilter === "starred") return isPersonStarred(key);
  if (activePeopleFilter.startsWith("person:"))
    return key === activePeopleFilter.slice(7);
  return (
    peoplePreferences.teams
      .find((team) => team.id === activePeopleFilter)
      ?.members.includes(key) || false
  );
}
function activePeopleFilterLabel() {
  if (activePeopleFilter === "all") return "";
  if (activePeopleFilter === "starred") return "Favourite people";
  if (activePeopleFilter.startsWith("person:"))
    return (
      people.find((person) => personKey(person) === activePeopleFilter.slice(7))
        ?.name || "Selected person"
    );
  return (
    peoplePreferences.teams.find((team) => team.id === activePeopleFilter)
      ?.name || ""
  );
}
function renderTeamFilters() {
  const renderButtons = (filters) =>
    filters
      .map(
        (filter) =>
          `<button type="button" data-team-filter="${escapeHtml(filter.id)}" aria-pressed="${activePeopleFilter === filter.id}">${escapeHtml(filter.name)}</button>`,
      )
      .join("");
  document.querySelector("#people-heading-filters").innerHTML = renderButtons([
    { id: "all", name: "All people" },
    { id: "starred", name: "★ Favourites" },
  ]);
  document.querySelector("#team-filter-list").innerHTML = renderButtons(
    peoplePreferences.teams,
  );
  document.querySelector("#people-bar-filters").innerHTML = [
    { id: "all", name: "All people" },
    { id: "starred", name: "Favourites" },
    ...peoplePreferences.teams,
  ]
    .map((filter) => {
      const icon =
        filter.id === "all"
          ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="7" r="3"/><path d="M3 20v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v3"/></svg>'
          : filter.id === "starred"
            ? "★"
            : escapeHtml(getInitials(filter.name));
      return `<button type="button" data-team-filter="${escapeHtml(filter.id)}" aria-pressed="${activePeopleFilter === filter.id}" aria-label="${escapeHtml(filter.name)}" title="${escapeHtml(filter.name)}"><span aria-hidden="true">${icon}</span></button>`;
    })
    .join("");
  const favourites = [
    ...new Map(
      people
        .filter((person) => isPersonStarred(personKey(person)))
        .map((person) => [personKey(person), person]),
    ).values(),
  ];
  document.querySelector("#people-bar-favourites").hidden = !favourites.length;
  document.querySelector("#people-bar-favourite-list").innerHTML = favourites
    .map(
      (person) =>
        `<button class="people-bar-person" type="button" data-team-filter="person:${escapeHtml(personKey(person))}" aria-pressed="${activePeopleFilter === `person:${personKey(person)}`}" title="${escapeHtml(person.name)}" aria-label="${escapeHtml(person.name)}"><span class="bar-person-initials" aria-hidden="true">${escapeHtml(getInitials(person.name))}</span><span class="bar-person-star author-star" aria-hidden="true">★</span></button>`,
    )
    .join("");
  fitBarFavourites();
  const teamSelected = peoplePreferences.teams.some(
    (team) => team.id === activePeopleFilter,
  );
  document.querySelector("#edit-team").hidden = !teamSelected;
  document.querySelector("#remove-team").hidden = !teamSelected;
}
function fitBarFavourites() {
  const list = document.querySelector("#people-bar-favourite-list");
  const slots = Math.max(0, Math.floor((list.clientHeight + 6) / 50));
  [...list.children].forEach((button, index) => {
    button.hidden = index >= slots;
  });
}

function refreshPeopleFilter() {
  state.currentPage = 1;
  state.selectedPdf = null;
  renderApp({ resetScroll: true });
}

(() => {
  const favouriteList = document.querySelector("#people-bar-favourite-list");
  new ResizeObserver(fitBarFavourites).observe(favouriteList);
  const dialog = document.querySelector("#team-dialog");
  const form = document.querySelector("#team-form");
  const name = document.querySelector("#team-name");
  const feedback = document.querySelector("#team-feedback");
  let editingId = null;
  function openTeamEditor(id = null) {
    editingId = id;
    const team = peoplePreferences.teams.find((item) => item.id === id);
    name.value = team?.name || "";
    feedback.textContent = "";
    document.querySelector("#team-dialog-title").textContent = team
      ? "Edit team"
      : "New team";
    document.querySelector("#team-delete").hidden = !team;
    const directory = [
      ...new Map(people.map((person) => [personKey(person), person])).values(),
    ];
    document.querySelector("#team-members-list").innerHTML = directory
      .map(
        (person) =>
          `<label class="team-member"><input type="checkbox" name="member" value="${escapeHtml(personKey(person))}" ${team?.members.includes(personKey(person)) ? "checked" : ""} /><span>${escapeHtml(person.name)}<small>${escapeHtml(person.email)}</small></span></label>`,
      )
      .join("");
    dialog.showModal();
    name.focus();
  }
  document.querySelector("#people-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-star-person]");
    if (!button) return;
    const key = button.dataset.starPerson;
    const stars = isPersonStarred(key)
      ? peoplePreferences.stars.filter((item) => item !== key)
      : [...peoplePreferences.stars, key];
    if (savePeoplePreferences({ ...peoplePreferences, stars }))
      refreshPeopleFilter();
  });
  document
    .querySelector(".right-sidebar")
    .addEventListener("click", (event) => {
      const button = event.target.closest("[data-team-filter]");
      if (!button) return;
      activePeopleFilter = button.dataset.teamFilter;
      refreshPeopleFilter();
    });
  document
    .querySelector("#create-team")
    .addEventListener("click", () => openTeamEditor());
  document
    .querySelector("#edit-team")
    .addEventListener("click", () => openTeamEditor(activePeopleFilter));
  document.querySelector("#remove-team").addEventListener("click", () => {
    const team = peoplePreferences.teams.find(
      (item) => item.id === activePeopleFilter,
    );
    if (
      !team ||
      !window.confirm(
        `Remove team “${team.name}”? People, stars, and PDFs will be kept.`,
      )
    )
      return;
    if (
      !savePeoplePreferences({
        ...peoplePreferences,
        teams: peoplePreferences.teams.filter((item) => item.id !== team.id),
      })
    )
      return;
    activePeopleFilter = "all";
    refreshPeopleFilter();
    showToast("Team removed");
  });
  for (const id of ["team-close", "team-cancel"])
    document.getElementById(id).addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    form.reset();
    editingId = null;
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const teamName = name.value.trim();
    const members = [
      ...form.querySelectorAll('input[name="member"]:checked'),
    ].map((input) => input.value);
    if (!teamName || !members.length) {
      feedback.textContent =
        "Enter a team name and select at least one person.";
      return;
    }
    if (
      peoplePreferences.teams.some(
        (team) =>
          team.id !== editingId &&
          team.name.toLocaleLowerCase() === teamName.toLocaleLowerCase(),
      )
    ) {
      feedback.textContent = "A team with this name already exists.";
      return;
    }
    const team = {
      id: editingId || crypto.randomUUID(),
      name: teamName,
      members,
    };
    const teams = editingId
      ? peoplePreferences.teams.map((item) =>
          item.id === editingId ? team : item,
        )
      : [...peoplePreferences.teams, team];
    if (!savePeoplePreferences({ ...peoplePreferences, teams })) {
      feedback.textContent = "Team could not be saved. Please try again.";
      return;
    }
    activePeopleFilter = team.id;
    dialog.close();
    refreshPeopleFilter();
  });
  document.querySelector("#team-delete").addEventListener("click", () => {
    if (!editingId) return;
    if (
      !savePeoplePreferences({
        ...peoplePreferences,
        teams: peoplePreferences.teams.filter((team) => team.id !== editingId),
      })
    )
      return;
    activePeopleFilter = "all";
    dialog.close();
    refreshPeopleFilter();
  });
  window.addEventListener("storage", (event) => {
    if (event.key !== PEOPLE_PREFERENCES_KEY && event.key !== null) return;
    peoplePreferences = loadPeoplePreferences();
    if (
      !["all", "starred"].includes(activePeopleFilter) &&
      !activePeopleFilter.startsWith("person:") &&
      !peoplePreferences.teams.some((team) => team.id === activePeopleFilter)
    )
      activePeopleFilter = "all";
    refreshPeopleFilter();
  });
})();
