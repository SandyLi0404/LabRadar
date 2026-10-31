/* LabRadar frontend.
 *
 * One activity number per professor: papers published since a cutoff year,
 * shown with the date it was observed. The interface never renders a verdict --
 * a card says "3 papers since 2025", not "active". Someone with no linked
 * publication record is shown as unknown rather than as silent, because those
 * two states mean entirely different things to a student choosing a group.
 */

const DEBOUNCE_MS = 220;

const state = {
  q: "",
  area: null,
  includeInactive: false,   // no recent papers -- hidden, never deleted
  includeNoWebsite: false,
  includeDeparted: false,
  sort: "name",
  page: 1,
  pageSize: 25,
  view: "people",
};

const el = (id) => document.getElementById(id);
const results = el("results");

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* The same page runs two ways. Served by the API, it asks the server for each
 * filtered page. Exported as a static site (GitHub Pages has no server), it
 * loads one snapshot of every record and applies the API's filter rules here.
 * The export marks the page with data-source="static". */
const STATIC = document.documentElement.dataset.source === "static";
// Set by the export, so a cached page only ever reads the data it was built with.
const BUILD = document.documentElement.dataset.build;

const snapshots = {};
function snapshot(name) {
  const version = BUILD ? `?v=${BUILD}` : "";
  snapshots[name] ??= getJSON(`data/${name}.json${version}`).catch((error) => {
    delete snapshots[name];
    throw error;
  });
  return snapshots[name];
}

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

// As in the API: no linked record means unknown, so never filtered as inactive.
const isUnmapped = (activity) => !activity || !activity.linked_author_count;

function fold(text) {
  return String(text ?? "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

/* The server uses Postgres full-text search over the same four fields. Here
 * every word of the query must appear somewhere in them; a trailing "s" is
 * dropped so "networks" still finds "network". */
function matches(person, q) {
  const haystack = fold(
    [person.full_name, person.title, person.interests_raw, ...person.areas].join(" ")
  );
  return fold(q)
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word.length > 3 ? word.replace(/s$/, "") : word));
}

// Descending with missing values last, as the API's NULLS LAST ordering.
const descending = (pick) => (a, b) => (pick(b) ?? -Infinity) - (pick(a) ?? -Infinity);

async function staticFaculty() {
  const [people, areas] = await Promise.all([snapshot("faculty"), snapshot("areas")]);
  const areaName = areas.find((a) => a.slug === state.area)?.name;
  const rows = people.filter(
    (p) =>
      (state.includeDeparted || p.is_current) &&
      (state.includeNoWebsite || p.homepage_url) &&
      (!state.area || p.areas.includes(areaName)) &&
      (!state.q || matches(p, state.q)) &&
      (state.includeInactive || p.activity?.works_since > 0 || isUnmapped(p.activity))
  );
  const byName = (a, b) => a.full_name.localeCompare(b.full_name);
  const byKey = descending((p) => p.activity?.[state.sort]);
  rows.sort(state.sort === "name" ? byName : (a, b) => byKey(a, b) || byName(a, b));
  const start = (state.page - 1) * state.pageSize;
  return {
    total: rows.length,
    page: state.page,
    page_size: state.pageSize,
    results: rows.slice(start, start + state.pageSize),
  };
}

async function staticLabs() {
  const labs = await snapshot("labs");
  const byWorks = descending((l) => l.activity?.works_since);
  return labs
    .filter(
      (l) =>
        state.includeInactive ||
        l.activity?.works_since > 0 ||
        !l.activity?.mapped_member_count
    )
    .sort((a, b) => byWorks(a, b) || a.name.localeCompare(b.name));
}

function buildQuery() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.area) params.set("area", state.area);
  params.set("include_inactive", state.includeInactive);
  params.set("include_no_website", state.includeNoWebsite);
  params.set("include_departed", state.includeDeparted);
  params.set("sort", state.sort);
  params.set("page", state.page);
  params.set("page_size", state.pageSize);
  return params.toString();
}

/* The figures carry their as-of date, so a stale one can never be mistaken for
 * a current one. The window label is rendered from `since_year` in the same
 * payload as the count, so the two cannot disagree -- an earlier version
 * hardcoded "last year" against a figure that actually spanned two. */
function renderTally(activity) {
  if (!activity || activity.is_unmapped) {
    return `<div class="tally unknown"><strong>Unknown</strong></div>`;
  }
  const n = activity.works_since;
  return `<div class="tally">
      <strong>${n} ${n === 1 ? "paper" : "papers"}</strong>
      <span>since ${activity.since_year}</span>
    </div>`;
}

function renderMeta(activity, unknownNote) {
  if (!activity || activity.is_unmapped) {
    return `<p class="meta"><span class="missing">${unknownNote}</span></p>`;
  }
  const last = activity.last_pub_year
    ? `Most recent ${activity.last_pub_year}`
    : `<span class="missing">No dated publications found</span>`;

  // Show the deduplication rather than silently applying it.
  const raw = activity.raw_records_since;
  const records =
    raw != null && raw > activity.works_since
      ? ` · <span title="OpenAlex lists preprint, conference and journal versions separately. LabRadar counts each paper once.">from ${raw} records</span>`
      : "";
  return `<p class="meta">${last}${records} · as of ${esc(activity.as_of_date)}</p>`;
}

/* A group is read off someone's personal page, so it is shown as an
 * attribution with a link to check, and a weak match is marked, not hidden. */
function renderLabs(labs) {
  if (!labs || !labs.length) return "";
  const tags = labs.map((lab) => {
    const label = lab.url
      ? `<a href="${esc(lab.url)}" target="_blank" rel="noopener">${esc(lab.name)}</a>`
      : esc(lab.name);
    const weak = lab.confidence < 0.7 ? " low" : "";
    const title = `Found on ${esc(lab.source_url || "their page")} (confidence ${lab.confidence.toFixed(2)})`;
    return `<span class="lab-tag${weak}" title="${title}">${label}</span>`;
  });
  return `<div class="lab-line">Group: ${tags.join(", ")}</div>`;
}

/* OpenAlex titles sometimes carry markup (<i>, <sup>); show the text only. */
const plainTitle = (title) => String(title ?? "").replace(/<[^>]*>/g, "");

function renderPapers(papers) {
  if (!papers || !papers.length) return "";
  const items = papers.map((paper) => {
    const title = esc(plainTitle(paper.title));
    const link = paper.url
      ? `<a href="${esc(paper.url)}" target="_blank" rel="noopener">${title}</a>`
      : title;
    const year = paper.year ? `<span class="year">${paper.year}</span>` : "";
    return `<li>${link}${year}</li>`;
  });
  return `<p class="papers-label">Recent papers</p><ul class="papers">${items.join("")}</ul>`;
}

/* A stable macaron color per research area (see .mac-* in style.css). */
const MACARONS = 6;
function macaron(areaName) {
  let hash = 0;
  // Multiplier picked so the eleven current areas spread evenly over the six colors.
  for (const c of String(areaName)) hash = (hash * 87 + c.codePointAt(0)) >>> 0;
  return `mac-${hash % MACARONS}`;
}

function renderCard(person) {
  const areas = person.areas.length
    ? `<div class="areas">${person.areas
        .map((a) => `<span class="area-tag ${macaron(a)}">${esc(a)}</span>`)
        .join(" ")}</div>`
    : "";

  const interests = person.interests_raw
    ? `<p class="interests">${esc(person.interests_raw)}</p>`
    : "";

  const departed = person.is_current
    ? ""
    : `<span class="flag">No longer listed in the department directory</span>`;

  const home = person.homepage_url
    ? `<a href="${esc(person.homepage_url)}" target="_blank" rel="noopener">${esc(person.full_name)}</a>`
    : `${esc(person.full_name)} <span class="nosite">no website listed</span>`;

  const details = areas + renderLabs(person.labs) + interests;

  return `
    <article class="entry${person.is_current ? "" : " departed"}">
      <header class="entry-head">
        <div class="who">
          <h2 class="name">${home}</h2>
          <span class="role">${esc(person.title ?? "")}</span>
          ${departed}
        </div>
        ${renderTally(person.activity)}
      </header>
      ${details ? `<div class="entry-body">${details}</div>` : ""}
      <div class="entry-foot">
        ${renderPapers(person.recent_papers)}
        ${renderMeta(person.activity, person.activity ? "Publication record not linked yet" : "No activity data yet")}
      </div>
    </article>`;
}

function describeFilters(total) {
  const parts = [`${total} ${total === 1 ? "professor" : "professors"}`];
  if (!state.includeInactive) parts.push("with recent publications");
  const area = el("areas").querySelector('[aria-pressed="true"]');
  if (area) parts.push(`in ${area.firstChild.textContent}`);
  if (state.q) parts.push(`matching “${state.q}”`);
  return parts.join(" · ");
}

async function load() {
  results.setAttribute("aria-busy", "true");
  try {
    const page = STATIC ? await staticFaculty() : await getJSON(`/api/faculty?${buildQuery()}`);

    el("count").textContent = describeFilters(page.total);

    results.innerHTML = page.results.length
      ? page.results.map(renderCard).join("")
      : `<div class="empty">
           Nothing matches these filters. Try showing professors with no recent
           papers — they are hidden, never removed.
         </div>`;

    const pages = Math.max(1, Math.ceil(page.total / page.page_size));
    el("pager").hidden = pages <= 1;
    el("pageInfo").textContent = `Page ${page.page} of ${pages}`;
    el("prev").disabled = page.page <= 1;
    el("next").disabled = page.page >= pages;
  } catch (error) {
    results.innerHTML = `<div class="error">Could not load professors: ${esc(error.message)}</div>`;
    el("count").textContent = "";
  } finally {
    results.removeAttribute("aria-busy");
  }
}

function renderLabCard(lab) {
  const act = lab.activity;
  const name = lab.url
    ? `<a href="${esc(lab.url)}" target="_blank" rel="noopener">${esc(lab.name)}</a>`
    : esc(lab.name);
  const unmappedNote =
    act && act.mapped_member_count < act.member_count
      ? ` <em>(${act.mapped_member_count} of ${act.member_count} with a linked publication record)</em>`
      : "";
  const members = lab.member_names.length
    ? `<div class="members">Faculty: ${lab.member_names.map(esc).join(", ")}${unmappedNote}</div>`
    : "";
  return `
    <article class="entry">
      <header class="entry-head">
        <div class="who"><h2 class="name">${name}</h2></div>
        ${renderTally(act)}
      </header>
      ${members ? `<div class="entry-body">${members}</div>` : ""}
      <div class="entry-foot">${renderMeta(act, "No member has a linked publication record yet")}</div>
    </article>`;
}

async function loadLabs() {
  const target = el("labResults");
  try {
    const params = new URLSearchParams({ sort: "works_since", limit: "200" });
    params.set("include_inactive", state.includeInactive);
    const labs = STATIC ? await staticLabs() : await getJSON(`/api/labs?${params}`);

    el("count").textContent =
      `${labs.length} ${labs.length === 1 ? "group" : "groups"} named on faculty pages` +
      (state.includeInactive ? "" : " · with recent publications");

    target.innerHTML = labs.length
      ? labs.map(renderLabCard).join("")
      : `<div class="empty">No groups match. Groups are only found when a faculty
           page names one, so this list is a floor, not a census.</div>`;
  } catch (error) {
    target.innerHTML = `<div class="error">Could not load groups: ${esc(error.message)}</div>`;
  }
}

async function loadAreas() {
  try {
    const areas = STATIC ? await snapshot("areas") : await getJSON("/api/areas");
    el("areas").innerHTML = areas
      .map(
        (a) => `<button type="button" class="chip ${macaron(a.name)}" data-slug="${esc(a.slug)}"
                  aria-pressed="false">${esc(a.name)}<span class="n">${a.faculty_count}</span></button>`
      )
      .join("");
  } catch {
    /* Areas are a convenience; the list still works without them. */
  }
}

function setView(view) {
  state.view = view;
  el("viewPeople").setAttribute("aria-selected", String(view === "people"));
  el("viewLabs").setAttribute("aria-selected", String(view === "labs"));
  el("results").hidden = view !== "people";
  el("labResults").hidden = view !== "labs";
  el("pager").hidden = view !== "people";
  // Groups have no search, sort or area, so only offer the filter that applies.
  for (const node of document.querySelectorAll(".people-only")) {
    node.hidden = view !== "people";
  }
  el("inactiveLabel").textContent =
    view === "people" ? "professors with no recent papers" : "groups with no recent papers";
  refresh();
}

function refresh() {
  return state.view === "labs" ? loadLabs() : load();
}

function wire() {
  const reload = () => { state.page = 1; refresh(); };

  el("q").addEventListener("input", debounce((e) => {
    state.q = e.target.value.trim();
    reload();
  }, DEBOUNCE_MS));

  el("sort").addEventListener("change", (e) => {
    state.sort = e.target.value;
    reload();
  });

  for (const id of ["includeInactive", "includeNoWebsite", "includeDeparted"]) {
    el(id).addEventListener("change", (e) => {
      state[id] = e.target.checked;
      reload();
    });
  }

  el("viewPeople").addEventListener("click", () => setView("people"));
  el("viewLabs").addEventListener("click", () => setView("labs"));

  el("areas").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    const slug = chip.dataset.slug;
    const selecting = state.area !== slug;
    state.area = selecting ? slug : null;
    for (const node of el("areas").querySelectorAll(".chip")) {
      node.setAttribute("aria-pressed", String(node === chip && selecting));
    }
    reload();
  });

  el("prev").addEventListener("click", () => { state.page--; refresh(); });
  el("next").addEventListener("click", () => { state.page++; refresh(); });

  el("reset").addEventListener("click", () => {
    Object.assign(state, {
      q: "", area: null, includeInactive: false, includeNoWebsite: false,
      includeDeparted: false, sort: "name", page: 1,
    });
    el("q").value = "";
    el("sort").value = "name";
    for (const id of ["includeInactive", "includeNoWebsite", "includeDeparted"]) {
      el(id).checked = false;
    }
    for (const node of el("areas").querySelectorAll(".chip")) {
      node.setAttribute("aria-pressed", "false");
    }
    refresh();
  });
}

wire();
loadAreas();
load();
