"use strict";

const PDFS_PER_PAGE = 1000;
const COMMIT_TIME_ZONE = "Europe/Dublin";
const CALENDAR_DAY_MS = 24 * 60 * 60 * 1000;
const COMMIT_CALENDAR_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  timeZone: COMMIT_TIME_ZONE,
});
const CALENDAR_LABEL_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});
const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];
const COMMIT_DATE_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  timeZone: COMMIT_TIME_ZONE,
});
const COMMIT_TIME_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZone: COMMIT_TIME_ZONE,
});

// The prototype data is intentionally isolated from the rendering functions so it
// can later be replaced by API responses without changing the view layer.
const projects = [
  {
    id: "PRJ-001",
    name: "Payments Platform",
    repos: [
      {
        name: "payment-services",
        pdfCount: 142,
        lastCommit: "03 Sep 2026",
        baseUrl: "https://bitbucket.org/acme/payment-services",
      },
      {
        name: "architecture-docs",
        pdfCount: 87,
        lastCommit: "01 Sep 2026",
        baseUrl: "https://bitbucket.org/acme/architecture-docs",
      },
      {
        name: "gateway-platform",
        pdfCount: 64,
        lastCommit: "28 Aug 2026",
        baseUrl: "https://bitbucket.org/acme/gateway-platform",
      },
    ],
  },
  {
    id: "PRJ-002",
    name: "Cloud Migration",
    repos: [
      {
        name: "aws-migration",
        pdfCount: 91,
        lastCommit: "04 Sep 2026",
        baseUrl: "https://bitbucket.org/acme/aws-migration",
      },
      {
        name: "legacy-documents",
        pdfCount: 53,
        lastCommit: "22 Aug 2026",
        baseUrl: "https://bitbucket.org/acme/legacy-documents",
      },
      {
        name: "cloud-governance",
        pdfCount: 63,
        lastCommit: "31 Aug 2026",
        baseUrl: "https://bitbucket.org/acme/cloud-governance",
      },
    ],
  },
  {
    id: "PRJ-003",
    name: "Identity & Access",
    repos: [
      {
        name: "auth-core",
        pdfCount: 76,
        lastCommit: "02 Sep 2026",
        baseUrl: "https://bitbucket.org/acme/auth-core",
      },
      {
        name: "identity-docs",
        pdfCount: 58,
        lastCommit: "30 Aug 2026",
        baseUrl: "https://bitbucket.org/acme/identity-docs",
      },
      {
        name: "access-gateway",
        pdfCount: 39,
        lastCommit: "25 Aug 2026",
        baseUrl: "https://bitbucket.org/acme/access-gateway",
      },
    ],
  },
];

// Explicit illustrative commit authors for the frontend sample documents.
// Live documents should use the backend commitAuthor field, never repository membership.
const sampleCommitAuthors = {
  "payment-services": "John Smith",
  "architecture-docs": "Sarah Wilson",
  "gateway-platform": "Aisha Rahman",
  "aws-migration": "Michael Chen",
  "legacy-documents": "Emma O'Brien",
  "cloud-governance": "Daniel Okafor",
  "auth-core": "Priya Patel",
  "identity-docs": "Liam Murphy",
  "access-gateway": "Sofia Almeida",
};

const pdfs = [
  createPdf(
    1,
    "Payment_Gateway_Architecture.pdf",
    "/docs/architecture/payment/Payment_Gateway_Architecture.pdf",
    "PRJ-001",
    "payment-services",
    14,
  ),
  createPdf(
    2,
    "ISO8583_Message_Flow.pdf",
    "/docs/integration/iso8583/ISO8583_Message_Flow.pdf",
    "PRJ-001",
    "payment-services",
    27,
  ),
  createPdf(
    3,
    "Payment_Service_Runbook.pdf",
    "/operations/runbooks/Payment_Service_Runbook.pdf",
    "PRJ-001",
    "payment-services",
    19,
  ),
  createPdf(
    4,
    "PCI_DSS_Architecture.pdf",
    "/standards/security/PCI_DSS_Architecture.pdf",
    "PRJ-001",
    "architecture-docs",
    31,
  ),
  createPdf(
    5,
    "Event_Driven_Payments.pdf",
    "/patterns/events/Event_Driven_Payments.pdf",
    "PRJ-001",
    "architecture-docs",
    12,
  ),
  createPdf(
    6,
    "Gateway_Failover_Design.pdf",
    "/docs/resilience/Gateway_Failover_Design.pdf",
    "PRJ-001",
    "gateway-platform",
    18,
  ),
  createPdf(
    7,
    "Merchant_Routing_Guide.pdf",
    "/docs/routing/Merchant_Routing_Guide.pdf",
    "PRJ-001",
    "gateway-platform",
    9,
  ),
  createPdf(
    8,
    "AWS_Migration_Strategy.pdf",
    "/strategy/cloud/AWS_Migration_Strategy.pdf",
    "PRJ-002",
    "aws-migration",
    42,
  ),
  createPdf(
    9,
    "Network_Architecture.pdf",
    "/architecture/network/Network_Architecture.pdf",
    "PRJ-002",
    "aws-migration",
    34,
  ),
  createPdf(
    10,
    "Landing_Zone_Standards.pdf",
    "/standards/platform/Landing_Zone_Standards.pdf",
    "PRJ-002",
    "aws-migration",
    16,
  ),
  createPdf(
    11,
    "Legacy_System_Inventory.pdf",
    "/discovery/inventory/Legacy_System_Inventory.pdf",
    "PRJ-002",
    "legacy-documents",
    22,
  ),
  createPdf(
    12,
    "Mainframe_Exit_Plan.pdf",
    "/planning/modernisation/Mainframe_Exit_Plan.pdf",
    "PRJ-002",
    "legacy-documents",
    11,
  ),
  createPdf(
    13,
    "Cloud_Tagging_Policy.pdf",
    "/policies/governance/Cloud_Tagging_Policy.pdf",
    "PRJ-002",
    "cloud-governance",
    25,
  ),
  createPdf(
    14,
    "Production_Deployment_Guide.pdf",
    "/operations/deployment/Production_Deployment_Guide.pdf",
    "PRJ-002",
    "cloud-governance",
    37,
  ),
  createPdf(
    15,
    "Authentication_Flow.pdf",
    "/docs/flows/Authentication_Flow.pdf",
    "PRJ-003",
    "auth-core",
    45,
  ),
  createPdf(
    16,
    "API_Security_Standards.pdf",
    "/standards/api/API_Security_Standards.pdf",
    "PRJ-003",
    "auth-core",
    33,
  ),
  createPdf(
    17,
    "Token_Rotation_Runbook.pdf",
    "/operations/security/Token_Rotation_Runbook.pdf",
    "PRJ-003",
    "auth-core",
    20,
  ),
  createPdf(
    18,
    "Identity_Data_Model.pdf",
    "/architecture/data/Identity_Data_Model.pdf",
    "PRJ-003",
    "identity-docs",
    17,
  ),
  createPdf(
    19,
    "Database_Architecture.pdf",
    "/architecture/database/Database_Architecture.pdf",
    "PRJ-003",
    "identity-docs",
    26,
  ),
  createPdf(
    20,
    "Disaster_Recovery_Design.pdf",
    "/resilience/recovery/Disaster_Recovery_Design.pdf",
    "PRJ-003",
    "access-gateway",
    29,
  ),
  createPdf(
    21,
    "Zero_Trust_Access_Model.pdf",
    "/architecture/security/Zero_Trust_Access_Model.pdf",
    "PRJ-003",
    "access-gateway",
    38,
  ),
];

const people = [
  createPerson(
    1,
    "John Smith",
    "john.smith@example.com",
    "PRJ-001",
    "payment-services",
    72,
    46,
  ),
  createPerson(
    2,
    "Sarah Wilson",
    "sarah.wilson@example.com",
    "PRJ-001",
    "architecture-docs",
    61,
    38,
  ),
  createPerson(
    3,
    "Aisha Rahman",
    "aisha.rahman@example.com",
    "PRJ-001",
    "gateway-platform",
    48,
    29,
  ),
  createPerson(
    4,
    "Michael Chen",
    "michael.chen@example.com",
    "PRJ-002",
    "aws-migration",
    54,
    41,
  ),
  createPerson(
    5,
    "Emma O'Brien",
    "emma.obrien@example.com",
    "PRJ-002",
    "legacy-documents",
    43,
    24,
  ),
  createPerson(
    6,
    "Daniel Okafor",
    "daniel.okafor@example.com",
    "PRJ-002",
    "cloud-governance",
    67,
    35,
  ),
  createPerson(
    7,
    "Priya Patel",
    "priya.patel@example.com",
    "PRJ-003",
    "auth-core",
    58,
    44,
  ),
  createPerson(
    8,
    "Liam Murphy",
    "liam.murphy@example.com",
    "PRJ-003",
    "identity-docs",
    39,
    31,
  ),
  createPerson(
    9,
    "Sofia Almeida",
    "sofia.almeida@example.com",
    "PRJ-003",
    "access-gateway",
    51,
    33,
  ),
];

const state = {
  selectedProject: null,
  selectedRepos: new Set(),
  selectedPdf: null,
  selectedPdfs: new Set(),
  searchQuery: "",
  peopleQuery: "",
  currentPage: 1,
  commitDateFilter: null,
  commitChartView: "periods",
  commitChartYear: null,
};

const elements = {
  allRepositories: document.querySelector("#all-repositories"),
  allRepositoriesMeta: document.querySelector("#all-repositories-meta"),
  commitDateBars: document.querySelector("#commit-date-bars"),
  commitDateSelection: document.querySelector("#commit-date-selection"),
  commitChartContent: document.querySelector("#commit-chart-content"),
  commitChartCaption: document.querySelector("#commit-chart-caption"),
  commitChartHelp: document.querySelector("#commit-chart-help"),
  commitPeriodsView: document.querySelector("#commit-periods-view"),
  commitCalendarView: document.querySelector("#commit-calendar-view"),
  commitYearControl: document.querySelector("#commit-year-control"),
  commitYearSelect: document.querySelector("#commit-year-select"),
  clearCommitDate: document.querySelector("#clear-commit-date"),
  toggleCommitChart: document.querySelector("#toggle-commit-chart"),
  formError: document.querySelector("#form-error"),
  modal: document.querySelector("#project-modal"),
  modalCancel: document.querySelector("#modal-cancel"),
  modalClose: document.querySelector("#modal-close"),
  newProjectButton: document.querySelector("#new-project-button"),
  nextPage: document.querySelector("#next-page"),
  currentPage: document.querySelector("#current-page"),
  paginationSummary: document.querySelector("#pagination-summary"),
  pdfEmptyCopy: document.querySelector("#pdf-empty-copy"),
  pdfEmptyState: document.querySelector("#pdf-empty-state"),
  pdfTableBody: document.querySelector("#pdf-table-body"),
  peopleCount: document.querySelector("#people-count"),
  peopleEmptyCopy: document.querySelector("#people-empty-copy"),
  peopleEmptyState: document.querySelector("#people-empty-state"),
  peopleList: document.querySelector("#people-list"),
  peopleSearchInput: document.querySelector("#people-search-input"),
  previousPage: document.querySelector("#previous-page"),
  projectCount: document.querySelector("#project-count"),
  projectForm: document.querySelector("#project-form"),
  projectList: document.querySelector("#project-list"),
  repositoryUrls: document.querySelector("#repository-urls"),
  searchInput: document.querySelector("#search-input"),
  selectionBreadcrumb: document.querySelector("#selection-breadcrumb"),
  selectionDescription: document.querySelector("#selection-description"),
  selectionTitle: document.querySelector("#selection-title"),
  tableScroll: document.querySelector("#table-scroll"),
  toast: document.querySelector("#toast"),
};

let lastFocusedElement = null;
let toastTimer = null;

function createPdf(id, name, path, projectId, repo, openCount) {
  const repoData = findRepository(projectId, repo);
  const committedAt = new Date(
    Date.UTC(2026, 8, 4, 13, 42) - id * 26 * 60 * 60 * 1000,
  );
  const encodedPath = path
    .split("/")
    .filter(Boolean)
    .map(encodeURIComponent)
    .join("/");
  const folderPath = path.slice(0, path.lastIndexOf("/") + 1);
  const encodedFolderPath = folderPath
    .split("/")
    .filter(Boolean)
    .map(encodeURIComponent)
    .join("/");

  return {
    id,
    name,
    path,
    projectId,
    repo,
    openCount,
    committedAt: committedAt.toISOString(),
    commitAuthor: sampleCommitAuthors[repo] || null,
    pdfUrl: `${repoData.baseUrl}/src/main/${encodedPath}`,
    folderUrl: `${repoData.baseUrl}/src/main/${encodedFolderPath}`,
  };
}

function createPerson(id, name, email, projectId, repo, commits, pdfCount) {
  return { id, name, email, projectId, repo, commits, pdfCount };
}

function findProject(projectId) {
  return projects.find((project) => project.id === projectId);
}

function findRepository(projectId, repoName) {
  return findProject(projectId)?.repos.find((repo) => repo.name === repoName);
}

function getProjectPdfTotal(project) {
  return project.repos.reduce((sum, repo) => sum + repo.pdfCount, 0);
}

function getInitials(name) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part.charAt(0).toUpperCase())
    .join("");
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-IE").format(value);
}

function formatCommitTimestamp(value) {
  if (getCommitCalendarDay(value) === null) return "Unknown date";
  const date = new Date(value);
  return `${COMMIT_DATE_FORMATTER.format(date)} · ${COMMIT_TIME_FORMATTER.format(date)}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function getScopedPdfs() {
  return pdfs.filter((pdf) => {
    const matchesProject =
      !state.selectedProject || pdf.projectId === state.selectedProject;
    const matchesRepo =
      !state.selectedRepos.size ||
      state.selectedRepos.has(repositoryKey(pdf.projectId, pdf.repo));
    return (
      matchesProject && matchesRepo && matchesPeopleFilter(pdfAuthorKey(pdf))
    );
  });
}

function getSearchMatchedPdfs() {
  const query = state.searchQuery.trim().toLocaleLowerCase();
  const scopedPdfs = getScopedPdfs();

  if (!query) return scopedPdfs;

  return scopedPdfs.filter((pdf) => {
    const searchable = [pdf.name, pdf.path, pdf.projectId, pdf.repo]
      .join(" ")
      .toLocaleLowerCase();
    return searchable.includes(query);
  });
}

// UTC numbers below represent Dublin calendar days, not UTC instants. Comparing
// those day keys keeps midnight and Monday boundaries correct across DST changes.
function getCommitCalendarDay(value) {
  if (value === null || value === undefined || value === "") return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  const parts = Object.fromEntries(
    COMMIT_CALENDAR_FORMATTER.formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
  );
}

function shiftCalendarMonths(day, months) {
  const date = new Date(day);
  const target = new Date(
    Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + months, 1),
  );
  const lastDay = new Date(
    Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0),
  ).getUTCDate();
  return Date.UTC(
    target.getUTCFullYear(),
    target.getUTCMonth(),
    Math.min(date.getUTCDate(), lastDay),
  );
}

function getCommitDateRanges(now = new Date()) {
  const today = getCommitCalendarDay(now);
  const date = new Date(today);
  const year = date.getUTCFullYear();
  const month = date.getUTCMonth();
  const tomorrow = today + CALENDAR_DAY_MS;
  const monday = today - ((date.getUTCDay() + 6) % 7) * CALENDAR_DAY_MS;
  const range = (id, label, start, end) => ({
    id,
    label,
    start,
    end,
    cutoff: now.getTime(),
  });
  return [
    range("today", "Today", today, tomorrow),
    range("yesterday", "Yesterday", today - CALENDAR_DAY_MS, today),
    range(
      "day-before-yesterday",
      "Day before yesterday",
      today - 2 * CALENDAR_DAY_MS,
      today - CALENDAR_DAY_MS,
    ),
    range("this-week", "This week", monday, tomorrow),
    range("last-week", "Last week", monday - 7 * CALENDAR_DAY_MS, monday),
    range("this-month", "This month", Date.UTC(year, month, 1), tomorrow),
    range(
      "last-month",
      "Last month",
      Date.UTC(year, month - 1, 1),
      Date.UTC(year, month, 1),
    ),
    range(
      "last-3-months",
      "Last 3 months",
      shiftCalendarMonths(today, -3),
      tomorrow,
    ),
    range(
      "last-6-months",
      "Last 6 months",
      shiftCalendarMonths(today, -6),
      tomorrow,
    ),
    range("this-year", "This year", Date.UTC(year, 0, 1), tomorrow),
    range(
      "last-year",
      "Last year",
      Date.UTC(year - 1, 0, 1),
      Date.UTC(year, 0, 1),
    ),
    range(
      "last-2-years",
      "Last 2 years",
      shiftCalendarMonths(today, -24),
      tomorrow,
    ),
    range(
      "last-3-years",
      "Last 3 years",
      shiftCalendarMonths(today, -36),
      tomorrow,
    ),
  ];
}

// First matching period wins, so overlapping calendar periods never duplicate records.
function getTimelineGroup(value, now = new Date()) {
  const day = getCommitCalendarDay(value);
  if (day === null) return "Unknown date";
  const periods = getCommitDateRanges(now).filter((range) =>
    [
      "today",
      "yesterday",
      "day-before-yesterday",
      "this-week",
      "last-week",
      "this-month",
      "last-month",
      "this-year",
    ].includes(range.id),
  );
  const period = periods.find((range) => day >= range.start && day < range.end);
  if (period) return period.label;
  const date = new Date(day);
  return `${MONTH_NAMES[date.getUTCMonth()]} ${date.getUTCFullYear()}`;
}

function getCalendarRange(year, month = null) {
  return {
    id: month === null ? `year:${year}` : `month:${year}:${month}`,
    label: month === null ? String(year) : `${MONTH_NAMES[month]} ${year}`,
    start: Date.UTC(year, month ?? 0, 1),
    end:
      month === null ? Date.UTC(year + 1, 0, 1) : Date.UTC(year, month + 1, 1),
  };
}

function getActiveCommitRange(now = new Date()) {
  const filter = state.commitDateFilter;
  if (!filter) return null;
  if (filter.kind === "custom") {
    return {
      id: "custom",
      start: filter.start,
      end: filter.end + CALENDAR_DAY_MS,
      label: `${CALENDAR_LABEL_FORMATTER.format(filter.start)} – ${CALENDAR_LABEL_FORMATTER.format(filter.end)}`,
    };
  }
  if (filter.kind === "period")
    return (
      getCommitDateRanges(now).find((range) => range.id === filter.id) ?? null
    );
  return getCalendarRange(
    filter.year,
    filter.kind === "month" ? filter.month : null,
  );
}

function isCommitInRange(pdf, range) {
  const day = getCommitCalendarDay(pdf.committedAt);
  return (
    day !== null &&
    day >= range.start &&
    day < range.end &&
    (range.cutoff === undefined ||
      new Date(pdf.committedAt).getTime() <= range.cutoff)
  );
}

function filterPdfs() {
  const matchedPdfs = getSearchMatchedPdfs();
  const range = getActiveCommitRange();
  return range
    ? matchedPdfs.filter((pdf) => isCommitInRange(pdf, range))
    : matchedPdfs;
}

function getCommitChartYears(records, now = new Date()) {
  const currentYear = new Date(getCommitCalendarDay(now)).getUTCFullYear();
  const years = new Set([
    currentYear,
    currentYear - 1,
    currentYear - 2,
    currentYear - 3,
  ]);
  records.forEach((pdf) => {
    const day = getCommitCalendarDay(pdf.committedAt);
    if (day !== null) years.add(new Date(day).getUTCFullYear());
  });
  if (state.commitChartYear !== null) years.add(state.commitChartYear);
  return [...years].sort((a, b) => b - a);
}

function renderCommitChart() {
  // Count before the selected date filter and before pagination, so alternative
  // periods remain useful while a date is selected. Text and repository scope apply.
  const records = getSearchMatchedPdfs();
  const now = new Date();
  const activeRange = getActiveCommitRange(now);
  const calendarView = state.commitChartView === "calendar";
  const years = getCommitChartYears(records, now);
  elements.commitPeriodsView.setAttribute(
    "aria-pressed",
    String(!calendarView),
  );
  elements.commitCalendarView.setAttribute(
    "aria-pressed",
    String(calendarView),
  );
  elements.commitYearControl.hidden = !calendarView;
  elements.commitYearSelect.innerHTML =
    '<option value="">All years</option>' +
    years.map((year) => `<option value="${year}">${year}</option>`).join("");
  elements.commitYearSelect.value =
    state.commitChartYear === null ? "" : String(state.commitChartYear);
  elements.commitDateSelection.textContent = activeRange?.label ?? "All dates";
  elements.clearCommitDate.hidden = !activeRange;

  let ranges;
  if (!calendarView) {
    ranges = getCommitDateRanges(now);
  } else if (state.commitChartYear === null) {
    ranges = years.map((year) => getCalendarRange(year));
  } else {
    ranges = MONTH_NAMES.map((_, month) =>
      getCalendarRange(state.commitChartYear, month),
    );
  }
  const dates = records.map((pdf) => ({
    day: getCommitCalendarDay(pdf.committedAt),
    timestamp: new Date(pdf.committedAt).getTime(),
  }));
  const counts = ranges.map(
    (range) =>
      dates.filter(
        ({ day, timestamp }) =>
          day !== null &&
          day >= range.start &&
          day < range.end &&
          (range.cutoff === undefined || timestamp <= range.cutoff),
      ).length,
  );
  const maximum = Math.max(1, ...counts);
  elements.commitDateBars.innerHTML = ranges
    .map((range, index) => {
      const count = counts[index];
      const label =
        calendarView && state.commitChartYear !== null
          ? MONTH_NAMES[index]
          : range.label;
      const dates = `${CALENDAR_LABEL_FORMATTER.format(range.start)} – ${CALENDAR_LABEL_FORMATTER.format(range.end - CALENDAR_DAY_MS)}`;
      return `<button type="button" class="commit-date-bar" data-commit-range="${range.id}"
      aria-pressed="${activeRange?.id === range.id}" aria-label="${escapeHtml(range.label)}: ${formatNumber(count)} ${count === 1 ? "PDF" : "PDFs"}"
      title="${escapeHtml(range.label)} · ${dates} · Europe/Dublin">
      <span>${escapeHtml(label)}</span><strong>${formatNumber(count)}</strong>
      <span class="commit-bar-track" aria-hidden="true"><span class="commit-bar-fill" style="--bar-width: ${(count / maximum) * 100}%"></span></span>
    </button>`;
    })
    .join("");
  const missingDates = dates.filter(({ day }) => day === null).length;
  elements.commitChartCaption.textContent = `${formatNumber(records.length)} sample ${records.length === 1 ? "PDF" : "PDFs"}`;
  const help = calendarView
    ? "Dublin time · Select a year, then a month. Counts reflect PDFs by commit date."
    : "Dublin time · Monday-start weeks · Periods overlap; 3/6 months and 2/3 years are rolling windows.";
  elements.commitChartHelp.textContent =
    help +
    (missingDates
      ? ` ${formatNumber(missingDates)} PDFs have no commit date.`
      : "");
}

function selectCommitDate(filter) {
  state.commitDateFilter = filter;
  document.querySelector("#custom-date-error").hidden = true;
  if (filter?.kind !== "custom") {
    document.querySelector("#custom-date-range").reset();
  }
  state.currentPage = 1;
  state.selectedPdf = null;
  renderCommitChart();
  renderPdfTable();
  renderPeople();
  elements.tableScroll.scrollTop = 0;
}

function getScopedPeople() {
  const range = getActiveCommitRange();
  const matchingAuthors = range
    ? new Set(
        getScopedPdfs()
          .filter((pdf) => isCommitInRange(pdf, range))
          .map(pdfAuthorKey),
      )
    : null;
  return people.filter((person) => {
    const matchesProject =
      !state.selectedProject || person.projectId === state.selectedProject;
    const matchesRepo =
      !state.selectedRepos.size ||
      state.selectedRepos.has(repositoryKey(person.projectId, person.repo));
    const matchesDate =
      !matchingAuthors || matchingAuthors.has(personKey(person));
    return (
      matchesProject &&
      matchesRepo &&
      matchesDate &&
      matchesPeopleFilter(personKey(person))
    );
  });
}

function isRepositoryInactive(repo, now = new Date()) {
  let day = null;
  if (repo.lastCommitAt) {
    day = getCommitCalendarDay(repo.lastCommitAt);
  } else {
    const match = /^(\d{1,2}) ([A-Za-z]+) (\d{4})$/.exec(repo.lastCommit || "");
    if (match) {
      const month = MONTH_NAMES.findIndex(
        (name) =>
          name.slice(0, 3).toLowerCase() === match[2].slice(0, 3).toLowerCase(),
      );
      if (month >= 0) {
        const candidate = Date.UTC(Number(match[3]), month, Number(match[1]));
        if (new Date(candidate).getUTCDate() === Number(match[1]))
          day = candidate;
      }
    }
  }
  return (
    day !== null && day < shiftCalendarMonths(getCommitCalendarDay(now), -3)
  );
}

function renderProjects() {
  const now = new Date();
  const repositoryCount = projects.reduce(
    (sum, project) => sum + project.repos.length,
    0,
  );
  const pdfCount = projects.reduce(
    (sum, project) => sum + getProjectPdfTotal(project),
    0,
  );
  elements.projectCount.textContent = formatNumber(projects.length);
  elements.allRepositoriesMeta.textContent = `${formatNumber(pdfCount)} PDFs · ${formatNumber(repositoryCount)} repositories`;
  elements.allRepositories.classList.toggle(
    "active",
    !state.selectedProject && !state.selectedRepos.size,
  );
  elements.allRepositories.setAttribute(
    "aria-current",
    !state.selectedProject && !state.selectedRepos.size ? "page" : "false",
  );

  document.querySelector("#project-bar-repos").innerHTML =
    `<button type="button" data-all-repositories aria-label="All repositories" title="All repositories" aria-pressed="${!state.selectedProject && !state.selectedRepos.size}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 4h7v7H3V4Zm11 0h7v7h-7V4ZM3 15h7v6H3v-6Zm11 0h7v6h-7v-6Z" /></svg></button>` +
    projects
      .flatMap((project) =>
        project.repos.map((repo) => {
          const initials = repo.name
            .split(/[^\p{L}\p{N}]+/u)
            .filter(Boolean)
            .slice(0, 2)
            .map((part) => part[0])
            .join("")
            .toUpperCase();
          const active = state.selectedRepos.has(
            repositoryKey(project.id, repo.name),
          );
          return `<div class="project-bar-repo">
      <button type="button" data-project-id="${escapeHtml(project.id)}" data-repo-name="${escapeHtml(repo.name)}" aria-pressed="${active}" aria-label="${escapeHtml(project.name)} / ${escapeHtml(repo.name)}" title="${escapeHtml(project.name)} / ${escapeHtml(repo.name)}"><span aria-hidden="true">${escapeHtml(initials)}</span>${pullRepoMark(project.id, repo.name)}</button>
    </div>`;
        }),
      )
      .join("");
  elements.projectList.innerHTML = projects
    .map((project) => {
      const projectIsActive =
        state.selectedProject === project.id && !state.selectedRepos.size;
      const repositories = project.repos
        .map((repo) => {
          const inactive = isRepositoryInactive(repo, now);
          const repoIsActive = state.selectedRepos.has(
            repositoryKey(project.id, repo.name),
          );
          return `
            <div class="repository-row">
            <button
              class="repository-button${repoIsActive ? " active" : ""}${inactive ? " repo-inactive" : ""}"
              type="button"
              data-project-id="${escapeHtml(project.id)}"
              data-repo-name="${escapeHtml(repo.name)}"
              aria-pressed="${repoIsActive}"
            >
              <span class="repo-selection-check" aria-hidden="true">${repoIsActive ? "✓" : ""}</span><span class="repo-name">${escapeHtml(repo.name)}${pullRepoMark(project.id, repo.name)}${inactive ? ' <span class="repo-inactive-label">Inactive</span>' : ""}</span>
              <span class="repo-meta">${formatNumber(repo.pdfCount)} PDFs</span>
              <span class="repo-date">Last commit: ${escapeHtml(repo.lastCommit)}</span>
            </button>
            </div>
          `;
        })
        .join("");

      return `
        <section class="project-group" aria-label="${escapeHtml(project.name)}">
          <button
            class="project-button${projectIsActive ? " active" : ""}"
            type="button"
            data-project-id="${escapeHtml(project.id)}"
            aria-current="${projectIsActive ? "page" : "false"}"
          >
            <span class="project-copy">
              <strong>${escapeHtml(project.name)}</strong>
              <small>${escapeHtml(project.id)}</small>
            </span>
            <span class="project-total">${formatNumber(getProjectPdfTotal(project))}</span>
          </button>
          <div class="repository-list">${repositories}</div>
        </section>
      `;
    })
    .join("");
}

function renderPdfTable() {
  const filteredPdfs = [...filterPdfs()].sort(
    (a, b) => new Date(b.committedAt) - new Date(a.committedAt),
  );
  const matchingIds = new Set(filteredPdfs.map((pdf) => pdf.id));
  state.selectedPdfs.forEach((id) => {
    if (!matchingIds.has(id)) state.selectedPdfs.delete(id);
  });
  updateBulkPdfControls(filteredPdfs);
  const scopedRecordCount = getScopedPdfs().length;
  const hasSearch = Boolean(state.searchQuery.trim());
  const commitRange = getActiveCommitRange();
  const totalPages = Math.max(
    1,
    Math.ceil(filteredPdfs.length / PDFS_PER_PAGE),
  );
  state.currentPage = Math.min(state.currentPage, totalPages);
  const pageStart = (state.currentPage - 1) * PDFS_PER_PAGE;
  const pagePdfs = filteredPdfs.slice(pageStart, pageStart + PDFS_PER_PAGE);
  const visibleStart = filteredPdfs.length ? pageStart + 1 : 0;
  const visibleEnd = Math.min(pageStart + pagePdfs.length, filteredPdfs.length);

  elements.paginationSummary.textContent = `Showing ${formatNumber(visibleStart)}–${formatNumber(visibleEnd)} of ${formatNumber(filteredPdfs.length)} PDFs`;
  elements.currentPage.textContent = `${formatNumber(state.currentPage)} / ${formatNumber(totalPages)}`;
  elements.previousPage.disabled = state.currentPage === 1;
  elements.nextPage.disabled = state.currentPage === totalPages;
  document.querySelector("#current-page-top").textContent =
    elements.currentPage.textContent;
  document.querySelector("#previous-page-top").disabled =
    elements.previousPage.disabled;
  document.querySelector("#next-page-top").disabled =
    elements.nextPage.disabled;

  let previousGroup;
  const timelineNow = new Date();
  const groupCounts = new Map();
  for (const pdf of filteredPdfs) {
    const group = getTimelineGroup(pdf.committedAt, timelineNow);
    groupCounts.set(group, (groupCounts.get(group) || 0) + 1);
  }
  elements.pdfTableBody.innerHTML = pagePdfs
    .map((pdf) => {
      const day = getCommitCalendarDay(pdf.committedAt);
      const dateLabel =
        day === null
          ? "Unknown date"
          : CALENDAR_LABEL_FORMATTER.format(new Date(day));
      const group = getTimelineGroup(pdf.committedAt, timelineNow);
      const separator =
        group !== previousGroup
          ? `<tr class="timeline-date-row"><th colspan="9" scope="rowgroup"><span class="timeline-marker" aria-hidden="true"></span><strong>${escapeHtml(group)}</strong><span class="timeline-group-count" title="Matching PDFs in this period across all pages">${formatNumber(groupCounts.get(group))} ${groupCounts.get(group) === 1 ? "PDF" : "PDFs"}</span></th></tr>`
          : "";
      previousGroup = group;
      return `${separator}
      <tr class="timeline-document ${state.selectedPdfs.has(pdf.id) ? "selected" : ""}" data-pdf-id="${pdf.id}">
        <td class="select-column"><input class="row-radio" type="checkbox" name="selected-pdf" value="${pdf.id}" aria-label="Select ${escapeHtml(pdf.name)}" ${state.selectedPdfs.has(pdf.id) ? "checked" : ""} /></td>
        <td><a class="timeline-file pdf-link" href="${escapeHtml(pdf.pdfUrl)}" data-open-pdf="${pdf.id}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(pdf.name)}"><span class="timeline-pdf-icon" aria-hidden="true">PDF</span><span>${escapeHtml(pdf.name)}</span></a></td>
        <td><button class="path-button" type="button" data-copy-path="${pdf.id}" title="Copy ${escapeHtml(pdf.path)}">${escapeHtml(pdf.path)}</button></td>
        <td><span class="badge project-badge">${escapeHtml(pdf.projectId)}</span></td>
        <td><span class="badge" title="${escapeHtml(pdf.repo)}">${escapeHtml(pdf.repo)}</span></td>
        <td><time class="commit-time" datetime="${escapeHtml(pdf.committedAt)}">${escapeHtml(dateLabel)}<small>${day === null ? "" : escapeHtml(COMMIT_TIME_FORMATTER.format(new Date(pdf.committedAt)))}</small></time></td>
        <td class="commit-author" title="${escapeHtml(pdf.commitAuthor || "Unknown")}">${escapeHtml(pdf.commitAuthor || "Unknown")}${isPersonStarred(pdfAuthorKey(pdf)) ? ' <span class="author-star" role="img" aria-label="Starred person">★</span>' : ""}</td>
        <td class="number-column"><span class="open-count">${formatNumber(pdf.openCount)}</span></td>
        <td class="actions-column"><div class="timeline-actions">
          <button class="folder-button notes-button${readPdfNote(pdf) ? " has-notes" : ""}" type="button" data-pdf-notes="${pdf.id}" aria-label="${readPdfNote(pdf) ? "Edit" : "Add"} notes for ${escapeHtml(pdf.name)}" title="${readPdfNote(pdf) ? "Edit notes" : "Add notes"}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3h14v13l-5 5H5V3Zm9 18v-5h5M8 7h8M8 11h8M8 15h3" /></svg></button>
          <button class="folder-button" type="button" data-copy-url="${pdf.id}" aria-label="Copy URL for ${escapeHtml(pdf.name)}" title="Copy URL"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 8h11v13H9V8ZM15 8V3H4v13h5" /></svg></button>
          <button class="folder-button" type="button" data-open-folder="${pdf.id}" aria-label="Open Bitbucket folder for ${escapeHtml(pdf.name)}" title="Open Bitbucket folder"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 7h6l1.8 2h9.2v9.5a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5V7ZM3.5 10h17" /></svg></button>
        </div></td>
      </tr>`;
    })
    .join("");

  elements.pdfEmptyState.hidden = filteredPdfs.length > 0;
  if (!filteredPdfs.length) {
    elements.pdfEmptyCopy.textContent =
      scopedRecordCount === 0
        ? "This selection has no sample PDF records loaded yet."
        : `No PDFs match ${hasSearch ? `“${state.searchQuery.trim()}”` : "the current selection"}${commitRange ? ` in ${commitRange.label}` : ""}. Try another period or clear the filters.`;
  }
}

function renderPeople() {
  renderTeamFilters();
  const scopedPeople = getScopedPeople();
  const range = getActiveCommitRange();
  const query = state.peopleQuery.trim().toLocaleLowerCase();
  const visiblePeople = query
    ? scopedPeople.filter((person) =>
        `${person.name} ${person.email}`.toLocaleLowerCase().includes(query),
      )
    : scopedPeople;
  elements.peopleCount.textContent = formatNumber(visiblePeople.length);
  elements.peopleEmptyState.hidden = visiblePeople.length > 0;
  elements.peopleEmptyCopy.textContent = query
    ? `No contributors match “${state.peopleQuery.trim()}”.`
    : range
      ? "No contributors match the current team, repository, and date filters."
      : "No contributor data is loaded for this selection.";

  elements.peopleList.innerHTML = visiblePeople
    .map(
      (person, index) => `
        <article class="person-card">
          <div class="avatar avatar-tone-${(index % 3) + 1}" aria-hidden="true">${escapeHtml(getInitials(person.name))}</div>
          <div class="person-main">
            <div class="person-name-row"><span class="person-name">${escapeHtml(person.name)}</span><button class="person-star" type="button" data-star-person="${escapeHtml(personKey(person))}" aria-label="${isPersonStarred(personKey(person)) ? "Unstar" : "Star"} ${escapeHtml(person.name)}" aria-pressed="${isPersonStarred(personKey(person))}">${isPersonStarred(personKey(person)) ? "★" : "☆"}</button></div>
            <span class="person-email" title="${escapeHtml(person.email)}">${escapeHtml(person.email)}</span>
            <div class="person-metrics">
              <span><strong>${formatNumber(person.commits)}</strong> commits</span>
              <span><strong>${formatNumber(person.pdfCount)}</strong> PDFs</span>
            </div>
          </div>
        </article>
      `,
    )
    .join("");
}

function repositoryKey(projectId, repoName) {
  return JSON.stringify([projectId, repoName]);
}

function selectedRepositories() {
  return projects.flatMap((project) =>
    project.repos.filter((repo) =>
      state.selectedRepos.has(repositoryKey(project.id, repo.name)),
    ),
  );
}

function updateSelectionHeader() {
  const selected = selectedRepositories();
  const deleteButton = document.querySelector("#delete-selected-repo");
  deleteButton.disabled = selected.length !== 1;
  deleteButton.title =
    selected.length === 1
      ? `Delete ${selected[0].name} (locked)`
      : "Select exactly one repository to delete";
  const project = state.selectedProject
    ? findProject(state.selectedProject)
    : null;
  document.querySelector("#pull-repositories").disabled = pullProgress.active;
  elements.newProjectButton.disabled = pullProgress.active;
  document.querySelector("#pull-repositories").title =
    "Git pull all repositories";
  document.querySelector("#repository-selection-status").textContent =
    selected.length
      ? `${selected.length} repositories selected · Click again to deselect.`
      : "";
  document.querySelector("#repository-selection-status").hidden =
    !selected.length;
  if (selected.length) {
    elements.selectionTitle.textContent =
      selected.length === 1
        ? selected[0].name
        : `${selected.length} repositories selected`;
    elements.selectionDescription.textContent = selected
      .map((repo) => repo.name)
      .join(", ");
    elements.selectionBreadcrumb.textContent =
      "PDF index / Selected repositories";
  } else {
    elements.selectionTitle.textContent = project?.name || "All Repositories";
    elements.selectionDescription.textContent = project
      ? `PDF files across all repositories in ${project.name}`
      : "PDF files across all projects and repositories";
    elements.selectionBreadcrumb.textContent = project
      ? `PDF index / ${project.id}`
      : "PDF index / All repositories";
  }
}

function openPullDialog() {
  startPullPreview();
}

function renderApp({ resetScroll = false } = {}) {
  renderProjects();
  renderCommitChart();
  renderPdfTable();
  renderPeople();
  updateSelectionHeader();

  if (resetScroll) elements.tableScroll.scrollTop = 0;
}

function selectAllRepositories() {
  state.selectedProject = null;
  state.selectedRepos.clear();
  state.selectedPdf = null;
  state.currentPage = 1;
  renderApp({ resetScroll: true });
}

function selectProject(projectId) {
  state.selectedProject = projectId;
  state.selectedRepos.clear();
  state.selectedPdf = null;
  state.currentPage = 1;
  renderApp({ resetScroll: true });
}

function selectRepository(projectId, repoName) {
  state.selectedProject = null;
  const key = repositoryKey(projectId, repoName);
  if (state.selectedRepos.has(key)) state.selectedRepos.delete(key);
  else state.selectedRepos.add(key);
  state.selectedPdf = null;
  state.currentPage = 1;
  renderApp({ resetScroll: true });
}

function updateBulkPdfControls(matching = filterPdfs()) {
  const count = state.selectedPdfs.size;
  const all = document.querySelector("#select-all-pdfs");
  all.checked = matching.length > 0 && count === matching.length;
  all.indeterminate = count > 0 && count < matching.length;
  all.disabled = matching.length === 0;
  document.querySelector("#selected-pdf-count").textContent =
    `${count} selected`;
  document.querySelector("#copy-selected-pdfs").disabled = count === 0;
  document.querySelector("#open-selected-pdfs").disabled = count === 0;
}

function selectPdf(pdfId) {
  if (state.selectedPdfs.has(pdfId)) state.selectedPdfs.delete(pdfId);
  else state.selectedPdfs.add(pdfId);
  renderPdfTable();
}

function incrementOpenCount(pdfId) {
  const pdf = pdfs.find((item) => item.id === pdfId);
  if (!pdf) return null;
  pdf.openCount += 1;
  renderPdfTable();
  return pdf;
}

function openPdf(pdfId) {
  const pdf = incrementOpenCount(pdfId);
  if (!pdf) return;
  window.open(pdf.pdfUrl, "_blank", "noopener,noreferrer");
}

function openFolder(pdfId) {
  const pdf = incrementOpenCount(pdfId);
  if (!pdf) return;
  window.open(pdf.folderUrl, "_blank", "noopener,noreferrer");
}

const pathCopyTimers = new WeakMap();

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const temporaryInput = document.createElement("textarea");
    temporaryInput.value = value;
    temporaryInput.setAttribute("readonly", "");
    temporaryInput.style.position = "fixed";
    temporaryInput.style.opacity = "0";
    document.body.appendChild(temporaryInput);
    temporaryInput.select();
    try {
      if (!document.execCommand("copy")) throw new Error("Copy failed");
    } finally {
      temporaryInput.remove();
    }
  }
}

const urlCopyTimers = new WeakMap();

async function copyUrl(pdfId, button) {
  const pdf = pdfs.find((item) => item.id === pdfId);
  if (!pdf) return;
  try {
    await copyText(pdf.pdfUrl);
    if (!button?.isConnected) return;
    clearTimeout(urlCopyTimers.get(button));
    button.querySelector("path").setAttribute("d", "m5 12 4 4L19 6");
    button.classList.add("url-copied");
    button.title = "Copied";
    button.setAttribute("aria-label", "URL copied");
    urlCopyTimers.set(
      button,
      setTimeout(() => {
        button
          .querySelector("path")
          .setAttribute("d", "M9 8h11v13H9V8ZM15 8V3H4v13h5");
        button.classList.remove("url-copied");
        button.title = "Copy URL";
        button.setAttribute("aria-label", `Copy URL for ${pdf.name}`);
        urlCopyTimers.delete(button);
      }, 1000),
    );
  } catch {
    showToast("Unable to copy URL", false);
  }
}

async function copyPath(pdfId, button) {
  const pdf = pdfs.find((item) => item.id === pdfId);
  if (!pdf) return;
  try {
    await copyText(pdf.path);
  } catch {
    showToast("Unable to copy path", false);
    return;
  }

  if (!button?.isConnected) return;
  clearTimeout(pathCopyTimers.get(button));
  button.textContent = "Copied";
  button.setAttribute("aria-live", "polite");
  button.classList.add("path-copied");
  pathCopyTimers.set(
    button,
    setTimeout(() => {
      button.textContent = pdf.path;
      button.classList.remove("path-copied");
      pathCopyTimers.delete(button);
    }, 1000),
  );
}

function showToast(message, success = true) {
  window.clearTimeout(toastTimer);
  elements.toast.querySelector("span").textContent = message;
  elements.toast.querySelector("svg").style.stroke = success
    ? "#4cdfac"
    : "#ffb0a8";
  elements.toast.classList.add("visible");
  toastTimer = window.setTimeout(
    () => elements.toast.classList.remove("visible"),
    1900,
  );
}

function openProjectModal() {
  if (pullProgress.active) return;
  lastFocusedElement = document.activeElement;
  elements.modal.hidden = false;
  document.body.setAttribute("data-modal-open", "true");
  window.setTimeout(() => elements.repositoryUrls.focus(), 0);
}

function closeProjectModal() {
  elements.modal.hidden = true;
  document.body.removeAttribute("data-modal-open");
  elements.projectForm.reset();
  clearFormError();
  if (lastFocusedElement instanceof HTMLElement) lastFocusedElement.focus();
}

function showFormError(message, fields = []) {
  elements.formError.textContent = message;
  elements.formError.hidden = false;
  elements.repositoryUrls.classList.toggle("invalid", fields.includes("urls"));
}

function clearFormError() {
  elements.formError.hidden = true;
  elements.formError.textContent = "";
  elements.repositoryUrls.classList.remove("invalid");
}

function parseRepositoryUrls(rawValue) {
  const uniqueUrls = [
    ...new Set(
      rawValue
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean),
    ),
  ];
  const parsed = [];

  for (const rawUrl of uniqueUrls) {
    let url;
    try {
      url = new URL(rawUrl);
    } catch (error) {
      return { error: `“${rawUrl}” is not a valid URL.` };
    }

    const segments = url.pathname.split("/").filter(Boolean);
    const repositoryName = segments.at(-1)?.replace(/\.git$/i, "");
    if (url.protocol !== "https:" || !repositoryName) {
      return { error: `“${rawUrl}” must be a complete HTTPS repository URL.` };
    }

    parsed.push({
      name: repositoryName,
      baseUrl: url.href.replace(/\/$/, "").replace(/\.git$/i, ""),
    });
  }

  return { repositories: parsed };
}

function stringHash(value) {
  return Array.from(value).reduce(
    (hash, character) => (hash * 31 + character.charCodeAt(0)) >>> 0,
    0,
  );
}

function createRepositoryFromUrl(repository, index) {
  const hash = stringHash(repository.baseUrl) + index;
  const date = new Date(Date.UTC(2026, 8, 4));
  date.setUTCDate(date.getUTCDate() - (hash % 26));

  return {
    name: repository.name,
    baseUrl: repository.baseUrl,
    pdfCount: 35 + (hash % 140),
    lastCommit: new Intl.DateTimeFormat("en-GB", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      timeZone: "UTC",
    }).format(date),
  };
}

function generateProjectId() {
  const highestId = projects.reduce((highest, project) => {
    const number = Number.parseInt(project.id.replace(/\D/g, ""), 10);
    return Number.isNaN(number) ? highest : Math.max(highest, number);
  }, 0);
  return `PRJ-${String(highestId + 1).padStart(3, "0")}`;
}

function addProject(event) {
  event.preventDefault();
  if (pullProgress.active) {
    showFormError("Wait for the current operation to finish.");
    return;
  }
  clearFormError();

  const rawUrls = elements.repositoryUrls.value;
  if (!rawUrls.trim()) {
    showFormError("Enter at least one repository URL.", ["urls"]);
    return;
  }

  const parsedUrls = parseRepositoryUrls(rawUrls);
  if (parsedUrls.error) {
    showFormError(parsedUrls.error, ["urls"]);
    return;
  }

  const firstUrl = new URL(parsedUrls.repositories[0].baseUrl);
  const segments = firstUrl.pathname.split("/").filter(Boolean);
  const projectSegment = segments.indexOf("projects");
  const name =
    projectSegment >= 0
      ? segments[projectSegment + 1] || firstUrl.hostname
      : segments.at(-2) || firstUrl.hostname;
  const projectId = generateProjectId();
  projects.push({
    id: projectId,
    name,
    repos: parsedUrls.repositories.map(createRepositoryFromUrl),
  });

  state.selectedProject = projectId;
  state.selectedRepos.clear();
  state.selectedPdf = null;
  state.currentPage = 1;
  closeProjectModal();
  renderApp({ resetScroll: true });
  startPullPreview(
    [projects.find((project) => project.id === projectId)],
    "New",
  );
  showToast(`${name} added to frontend preview`);
}

function trapModalFocus(event) {
  if (event.key !== "Tab" || elements.modal.hidden) return;
  const focusable = [
    ...elements.modal.querySelectorAll("button, input, textarea"),
  ].filter((element) => !element.disabled && element.offsetParent !== null);
  if (!focusable.length) return;

  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function handleProjectNavigation(event) {
  if (event.target.closest("[data-all-repositories]")) {
    selectAllRepositories();
    return;
  }
  const repositoryButton = event.target.closest("[data-repo-name]");
  if (repositoryButton) {
    selectRepository(
      repositoryButton.dataset.projectId,
      repositoryButton.dataset.repoName,
    );
    return;
  }

  const projectButton = event.target.closest(
    ".project-button[data-project-id]",
  );
  if (projectButton) selectProject(projectButton.dataset.projectId);
}

function handleTableInteraction(event) {
  const radio = event.target.closest(".row-radio");
  if (radio) {
    selectPdf(Number(radio.value));
    return;
  }

  const pdfLink = event.target.closest("[data-open-pdf]");
  if (pdfLink) {
    event.preventDefault();
    openPdf(Number(pdfLink.dataset.openPdf));
    return;
  }

  const pathButton = event.target.closest("[data-copy-path]");
  if (pathButton) {
    copyPath(Number(pathButton.dataset.copyPath), pathButton);
    return;
  }

  const copyUrlButton = event.target.closest("[data-copy-url]");
  if (copyUrlButton) {
    void copyUrl(Number(copyUrlButton.dataset.copyUrl), copyUrlButton);
    return;
  }

  const folderButton = event.target.closest("[data-open-folder]");
  if (folderButton) openFolder(Number(folderButton.dataset.openFolder));
}

function bindEvents() {
  document
    .querySelector("#custom-date-range")
    .addEventListener("submit", (event) => {
      event.preventDefault();
      const from = document.querySelector("#commit-date-from");
      const to = document.querySelector("#commit-date-to");
      const error = document.querySelector("#custom-date-error");
      const start = from.valueAsNumber;
      const end = to.valueAsNumber;
      if (!Number.isFinite(start) || !Number.isFinite(end) || start > end) {
        error.textContent = "Choose valid dates with From on or before To.";
        error.hidden = false;
        to.focus();
        return;
      }
      state.commitChartYear = null;
      selectCommitDate({ kind: "custom", start, end });
    });
  document
    .querySelector("#pull-repositories")
    .addEventListener("click", openPullDialog);
  document
    .querySelector("#pull-dialog-close")
    .addEventListener("click", () =>
      document.querySelector("#pull-dialog").close(),
    );
  elements.allRepositories.addEventListener("click", selectAllRepositories);
  elements.projectList.addEventListener("click", handleProjectNavigation);
  document
    .querySelector("#project-bar-repos")
    .addEventListener("click", handleProjectNavigation);
  elements.pdfTableBody.addEventListener("click", handleTableInteraction);
  elements.newProjectButton.addEventListener("click", openProjectModal);
  elements.modalClose.addEventListener("click", closeProjectModal);
  elements.modalCancel.addEventListener("click", closeProjectModal);
  elements.projectForm.addEventListener("submit", addProject);
  elements.repositoryUrls.addEventListener("input", clearFormError);

  elements.searchInput.addEventListener("input", (event) => {
    state.searchQuery = event.target.value;
    state.selectedPdf = null;
    state.currentPage = 1;
    renderCommitChart();
    renderPdfTable();
    elements.tableScroll.scrollTop = 0;
  });

  elements.commitDateBars.addEventListener("click", (event) => {
    const button = event.target.closest("[data-commit-range]");
    if (!button) return;
    const [kind, year, month] = button.dataset.commitRange.split(":");
    if (kind === "year") {
      state.commitChartYear = Number(year);
      selectCommitDate({ kind: "year", year: Number(year) });
    } else if (kind === "month") {
      selectCommitDate({
        kind: "month",
        year: Number(year),
        month: Number(month),
      });
    } else {
      selectCommitDate({ kind: "period", id: button.dataset.commitRange });
    }
  });

  elements.commitPeriodsView.addEventListener("click", () => {
    state.commitChartView = "periods";
    renderCommitChart();
  });
  elements.commitCalendarView.addEventListener("click", () => {
    state.commitChartView = "calendar";
    renderCommitChart();
  });
  elements.commitYearSelect.addEventListener("change", (event) => {
    state.commitChartYear =
      event.target.value === "" ? null : Number(event.target.value);
    selectCommitDate(
      state.commitChartYear === null
        ? null
        : { kind: "year", year: state.commitChartYear },
    );
  });
  elements.clearCommitDate.addEventListener("click", () => {
    state.commitChartYear = null;
    selectCommitDate(null);
  });
  elements.toggleCommitChart.addEventListener("click", () => {
    const expanded = elements.commitChartContent.hidden;
    elements.commitChartContent.hidden = !expanded;
    elements.toggleCommitChart.setAttribute("aria-expanded", String(expanded));
    elements.toggleCommitChart.textContent = expanded
      ? "Hide chart"
      : "Show chart";
  });

  document
    .querySelector("#previous-page-top")
    .addEventListener("click", () => elements.previousPage.click());
  document
    .querySelector("#next-page-top")
    .addEventListener("click", () => elements.nextPage.click());

  elements.previousPage.addEventListener("click", () => {
    if (state.currentPage <= 1) return;
    state.currentPage -= 1;
    state.selectedPdf = null;
    renderPdfTable();
    elements.tableScroll.scrollTop = 0;
  });

  elements.nextPage.addEventListener("click", () => {
    const totalPages = Math.max(
      1,
      Math.ceil(filterPdfs().length / PDFS_PER_PAGE),
    );
    if (state.currentPage >= totalPages) return;
    state.currentPage += 1;
    state.selectedPdf = null;
    renderPdfTable();
    elements.tableScroll.scrollTop = 0;
  });

  elements.peopleSearchInput.addEventListener("input", (event) => {
    state.peopleQuery = event.target.value;
    renderPeople();
  });

  elements.modal.addEventListener("click", (event) => {
    if (event.target === elements.modal) closeProjectModal();
  });

  document.addEventListener("keydown", (event) => {
    if (document.querySelector("dialog[open]")) return;
    if (event.key === "Escape" && !elements.modal.hidden) {
      closeProjectModal();
      return;
    }

    if (
      event.key === "/" &&
      elements.modal.hidden &&
      document.activeElement !== elements.searchInput
    ) {
      event.preventDefault();
      elements.searchInput.focus();
    }

    trapModalFocus(event);
  });
}

bindEvents();
renderApp();

// A successful HTTP response alone is not proof of a working Bitbucket connection.
// Test each saved server with the existing backend credential-check endpoint.
let connectionCheckRunning = false;

function setConnectionStatus(status, detail = "") {
  if (pullProgress.active) {
    pullProgress.connectionState = { status, detail };
    return;
  }
  const container = document.querySelector("#connection-status");
  const labels = {
    connecting: "Connecting…",
    connected: "Index ready",
    failed: "Connection failed",
  };
  container.dataset.state = status;
  container.title = detail || labels[status];
  document.querySelector("#connection-image").src =
    status === "connected"
      ? "assets/connected.png"
      : status === "failed"
        ? "assets/disconnected.png"
        : "assets/no-connection.gif";
  document.querySelector("#connection-label").textContent = labels[status];
  const button = document.querySelector("#test-connection");
  button.disabled = status === "connecting";
  const actionLabel =
    status === "connecting"
      ? labels[status]
      : `${labels[status]}. Click to retest connection.`;
  button.setAttribute("aria-label", actionLabel);
  button.title = actionLabel;
}

async function connectionJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    credentials: "same-origin",
    cache: "no-store",
    signal: options.signal,
  });
  let result;
  try {
    result = await response.json();
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new Error(
      "The OWL backend is unavailable. Open the frontend with the backend connected and try again.",
    );
  }
  if (!response.ok || result.ok !== true) {
    throw new Error(result.message || "The connection check did not succeed.");
  }
  return result;
}

async function testConnection() {
  if (connectionCheckRunning || pullProgress.active) return;
  connectionCheckRunning = true;
  setConnectionStatus("connecting");
  const controller = new AbortController();
  let resultState = "failed";
  let resultMessage =
    "Connection check timed out after 10 seconds. Click to retry.";
  const animationWindow = new Promise((resolve) => {
    setTimeout(() => {
      controller.abort();
      resolve();
    }, 10000);
  });
  try {
    const workspace = await connectionJson(
      document.querySelector("#connection-status").dataset.workspaceUrl,
      { signal: controller.signal },
    );
    if (!workspace.credentials?.length)
      throw new Error("No Bitbucket server is configured.");
    const endpoint = new URL(workspace.settingsTestUrl, window.location.href);
    if (endpoint.origin !== window.location.origin)
      throw new Error("Invalid connection test endpoint.");
    for (const server of workspace.credentials) {
      const body = new URLSearchParams({
        base_url: server.baseUrl,
        username: server.username || "",
      });
      if (server.verifySsl) body.set("verify_ssl", "on");
      await connectionJson(endpoint, {
        method: "POST",
        signal: controller.signal,
        headers: { "X-CSRFToken": workspace.csrfToken },
        body,
      });
    }
    resultState = "connected";
    resultMessage =
      "Connection test successful for all saved Bitbucket servers.";
  } catch (error) {
    resultMessage = controller.signal.aborted
      ? "Connection check timed out after 10 seconds. Click to retry."
      : error.message || "Unable to check the Bitbucket connection.";
  } finally {
    await animationWindow;
    setConnectionStatus(resultState, resultMessage);
    connectionCheckRunning = false;
  }
}

document
  .querySelector("#test-connection")
  .addEventListener("click", testConnection);
void testConnection();

// Bulk selection applies to matching documents across all pages.
document
  .querySelector("#select-all-pdfs")
  .addEventListener("change", (event) => {
    state.selectedPdfs = new Set(
      event.target.checked ? filterPdfs().map((pdf) => pdf.id) : [],
    );
    renderPdfTable();
  });
document
  .querySelector("#copy-selected-pdfs")
  .addEventListener("click", async () => {
    const selected = filterPdfs().filter((pdf) =>
      state.selectedPdfs.has(pdf.id),
    );
    if (!selected.length) return;
    try {
      await copyText(selected.map((pdf) => pdf.pdfUrl).join("\n"));
      showToast(`Copied ${selected.length} PDF URLs`);
    } catch {
      showToast("Unable to copy PDF URLs. Please try again.", false);
    }
  });
document.querySelector("#open-selected-pdfs").addEventListener("click", () => {
  const selected = filterPdfs().filter((pdf) => state.selectedPdfs.has(pdf.id));
  for (const pdf of selected)
    window.open(pdf.pdfUrl, "_blank", "noopener,noreferrer");
  if (selected.length)
    showToast(
      "PDF tabs requested. If any are missing, allow pop-ups for this site.",
    );
});
