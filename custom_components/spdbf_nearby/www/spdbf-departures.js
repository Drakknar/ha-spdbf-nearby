// spdbf Nearby Stops: opening a stop (map marker, entity search, the stop
// search card, ...) shows its departures instead of the generic entity
// dialog. Every other entity keeps Home Assistant's own dialog. Loaded on
// every page by the integration.
//
// The dialog loads the stop's full board from the integration
// (spdbf_nearby/board), so busy stations show every line, not only the few
// departures kept on the map entity. window.spdbfShowStop(stop) opens it for
// any stop, which is what the stop search card uses.
(() => {
  if (window.__spdbfDepartures) return;
  window.__spdbfDepartures = true;

  const SOURCE = "spdbf_nearby";
  const TICK_MS = 1000;
  const RERENDER_MS = 30000; // keeps "in N min" current between updates
  const BOARD_REFRESH_MS = 30000;
  const PER_LINE = 3; // departures shown per line and destination
  const VIEW_KEY = "spdbf-departures-view";

  // BVG's colours for S- and U-Bahn lines, used for BVG and VBB stops.
  const LINE_COLORS = {
    U1: "#7dad4c", U2: "#da421e", U3: "#16683d", U4: "#f0d722", U5: "#7e5330",
    U6: "#8c6dab", U7: "#528dba", U8: "#224f86", U9: "#f3791d",
    S1: "#de4da4", S2: "#005f27", S25: "#005f27", S26: "#005f27", S3: "#0a4c99",
    S41: "#a23b1e", S42: "#c26a36", S45: "#c38737", S46: "#c38737", S47: "#c38737",
    S5: "#ff5900", S7: "#6f4e9c", S75: "#6f4e9c", S8: "#55a822", S85: "#55a822",
    S9: "#8a0e30",
  };
  const DARK_TEXT = new Set(["U4"]);
  const COLORED_NETWORKS = new Set(["BVG", "VBB"]);

  const STYLE = `
    :host { position: fixed; inset: 0; z-index: 1000; }
    .scrim { position: absolute; inset: 0; background: var(--mdc-dialog-scrim-color, rgba(0, 0, 0, 0.32)); }
    .panel {
      position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
      width: min(520px, calc(100% - 32px)); max-height: calc(100% - 64px);
      display: flex; flex-direction: column; overflow: hidden;
      background: var(--ha-card-background, var(--card-background-color, #fff));
      color: var(--primary-text-color, #212121);
      border-radius: var(--ha-dialog-border-radius, 16px);
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
      font-family: var(--ha-font-family-body, Roboto, system-ui, sans-serif);
    }
    @media (max-width: 450px) {
      .panel {
        top: auto; bottom: 0; transform: translateX(-50%); width: 100%;
        max-height: 85%; border-radius: 16px 16px 0 0;
        padding-bottom: env(safe-area-inset-bottom, 0px);
      }
    }
    header { display: flex; align-items: flex-start; gap: 8px; padding: 16px 8px 8px 24px; }
    header img { flex: none; width: 40px; height: 40px; margin: -4px 4px 0 -8px; }
    header div { flex: 1; min-width: 0; }
    h2 { margin: 0; font-size: 20px; font-weight: 500; line-height: 28px; }
    .sub { color: var(--secondary-text-color, #727272); font-size: 14px; }
    .icon-btn {
      flex: none; width: 40px; height: 40px; border: none; border-radius: 50%;
      background: none; color: inherit; font-size: 24px; line-height: 1; cursor: pointer;
    }
    .icon-btn:hover, .btn:hover { background: var(--secondary-background-color, rgba(0, 0, 0, 0.06)); }
    .views { display: flex; gap: 4px; padding: 0 24px 8px; }
    .views button {
      padding: 4px 12px; border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
      border-radius: 16px; background: none; color: inherit; font: inherit; font-size: 13px;
      cursor: pointer;
    }
    .views button[aria-pressed="true"] {
      background: var(--primary-color, #03a9f4); border-color: var(--primary-color, #03a9f4);
      color: var(--text-primary-color, #fff);
    }
    .status { padding: 0 24px 8px; color: var(--secondary-text-color, #727272); font-size: 12px; }
    ul { list-style: none; margin: 0; padding: 0 8px; overflow-y: auto; }
    li {
      display: grid; grid-template-columns: auto 1fr auto; gap: 12px; align-items: center;
      padding: 10px 16px; border-top: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
    }
    .line {
      min-width: 32px; padding: 2px 8px; border-radius: 6px; text-align: center;
      background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff);
      font-weight: 600; font-size: 14px; white-space: nowrap;
    }
    .dest { min-width: 0; }
    .dest div { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    small { color: var(--secondary-text-color, #727272); font-size: 12px; }
    .when { text-align: right; white-space: nowrap; }
    .min { font-weight: 600; }
    .times { font-weight: 600; }
    .times span + span::before { content: " · "; color: var(--secondary-text-color, #727272); font-weight: 400; }
    .late, .cancelled .min { color: var(--error-color, #db4437); }
    li.cancelled .dest div, s { text-decoration: line-through; opacity: 0.7; }
    .empty { padding: 16px 24px; color: var(--secondary-text-color, #727272); }
    footer { display: flex; justify-content: flex-end; gap: 8px; padding: 8px 16px 16px; }
    .btn {
      padding: 8px 12px; border: none; border-radius: 20px; background: none; cursor: pointer;
      color: var(--primary-color, #03a9f4); font: inherit; font-weight: 500; text-decoration: none;
    }
  `;

  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  const homeAssistant = () => document.querySelector("home-assistant");
  const hass = () => homeAssistant()?.hass;

  const readView = () => {
    try { return localStorage.getItem(VIEW_KEY) === "time" ? "time" : "line"; } catch (e) { return "line"; }
  };
  const saveView = (value) => {
    try { localStorage.setItem(VIEW_KEY, value); } catch (e) { /* private mode */ }
  };

  let host = null;
  let stop = null; // { name, eva, network, entity_picture, distance_km, url, merged_stops }
  let entityId = null; // set when opened from a map stop entity
  let board = null; // last spdbf_nearby/board answer
  let boardTime = 0;
  let boardError = null;
  let loading = false;
  let view = readView();
  let lastState = null;
  let lastRender = 0;
  let timer = null;
  let session = 0; // ignores answers that arrive after the dialog changed

  function close() {
    clearInterval(timer);
    document.removeEventListener("keydown", onKey, true);
    host?.remove();
    host = stop = entityId = board = boardError = lastState = null;
    boardTime = 0;
    loading = false;
    session += 1;
  }

  function onKey(ev) {
    if (ev.key === "Escape") {
      ev.stopPropagation();
      close();
    }
  }

  async function loadBoard() {
    const ha = hass();
    if (!ha || !stop || loading) return;
    const mine = session;
    loading = true;
    try {
      const request = { type: `${SOURCE}/board`, eva: stop.eva, network: stop.network };
      // A searched DB + BVG station names its other network; a map stop
      // doesn't need to, the integration knows it.
      if (!entityId && stop.linked?.length) request.linked = stop.linked;
      const result = await ha.callWS(request);
      if (mine !== session) return;
      board = result;
      boardError = null;
    } catch (err) {
      if (mine !== session) return;
      boardError = err?.message || String(err);
    } finally {
      if (mine === session) {
        loading = false;
        boardTime = Date.now();
        render(true);
      }
    }
  }

  const lineKey = (line) => String(line ?? "").replace(/\s+/g, "");

  function lineChip(line) {
    const key = lineKey(line);
    const color = COLORED_NETWORKS.has(stop.network) ? LINE_COLORS[key] : null;
    const style = color
      ? ` style="background:${color};color:${DARK_TEXT.has(key) ? "#212121" : "#fff"}"`
      : "";
    return `<span class="line"${style}>${esc(line)}</span>`;
  }

  // S-Bahn, then U-Bahn, then everything else; lines in natural order.
  function lineRank(line) {
    const key = lineKey(line);
    if (/^S\d/.test(key)) return 0;
    if (/^U\d/.test(key)) return 1;
    return 2;
  }
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

  function minutesOf(dep, ageMinutes) {
    return Math.max(0, (dep.minutes ?? 0) - ageMinutes);
  }

  function departureRow(dep, ageMinutes) {
    const minutes = minutesOf(dep, ageMinutes);
    const delay = dep.delay ? `<span class="late"> +${esc(dep.delay)}</span>` : "";
    const when = dep.cancelled
      ? `<div class="min">cancelled</div><small>${esc(dep.scheduled)}</small>`
      : `<div class="min">${minutes === 0 ? "now" : `${minutes} min`}</div><small>${esc(dep.scheduled)}${delay}</small>`;
    const platform = dep.platform ? `<small>Platform ${esc(dep.platform)}</small>` : "";
    return `
      <li class="${dep.cancelled ? "cancelled" : ""}">
        ${lineChip(dep.line)}
        <div class="dest"><div>${esc(dep.destination)}</div>${platform}</div>
        <div class="when">${when}</div>
      </li>`;
  }

  function groupRow(group, ageMinutes) {
    const first = group.deps[0];
    const times = group.deps.slice(0, PER_LINE).map((dep) => {
      const minutes = minutesOf(dep, ageMinutes);
      const label = minutes === 0 ? "now" : String(minutes);
      if (dep.cancelled) return `<span><s>${esc(label)}</s></span>`;
      return `<span class="${dep.delay ? "late" : ""}" title="${esc(dep.scheduled)}${dep.delay ? ` +${esc(dep.delay)}` : ""}">${esc(label)}</span>`;
    });
    const platforms = [...new Set(group.deps.map((dep) => dep.platform).filter(Boolean))];
    const platform = platforms.length
      ? `<small>Platform ${platforms.slice(0, 2).map(esc).join(", ")}</small>`
      : "";
    const delayed = group.deps.slice(0, PER_LINE).some((dep) => dep.delay && !dep.cancelled);
    return `
      <li>
        ${lineChip(first.line)}
        <div class="dest"><div>${esc(first.destination)}</div>${platform}</div>
        <div class="when"><div class="times">${times.join("")}</div><small>min${delayed ? ' · <span class="late">delayed</span>' : ""}</small></div>
      </li>`;
  }

  function groupByLine(deps) {
    const groups = new Map();
    for (const dep of deps) {
      const key = `${lineKey(dep.line)}\u0000${dep.destination ?? ""}`;
      if (!groups.has(key)) groups.set(key, { line: dep.line, destination: dep.destination ?? "", deps: [] });
      groups.get(key).deps.push(dep);
    }
    return [...groups.values()].sort((a, b) =>
      lineRank(a.line) - lineRank(b.line)
      || collator.compare(lineKey(a.line), lineKey(b.line))
      || collator.compare(a.destination, b.destination));
  }

  // The stop as currently known: from its entity when opened from the map.
  function currentStop() {
    if (!entityId) return stop;
    const state = hass()?.states[entityId];
    if (!state) return null;
    const attrs = state.attributes;
    return {
      ...stop,
      name: attrs.friendly_name ?? stop.name,
      distance_km: attrs.distance_km,
      merged_stops: attrs.merged_stops ?? [],
      networks: attrs.networks,
    };
  }

  function render(force = false) {
    if (!host) return;
    if (Date.now() - boardTime > BOARD_REFRESH_MS && !loading && boardTime) loadBoard();
    const state = entityId ? hass()?.states[entityId] : null;
    const info = currentStop();
    if (!info) {
      close(); // the stop is no longer nearby
      return;
    }
    if (!force && state === lastState && Date.now() - lastRender < RERENDER_MS) return;
    lastState = state;
    lastRender = Date.now();

    // Until the full board arrives, show what the map entity already has.
    let deps = board?.departures;
    let age = Math.floor((Date.now() - boardTime) / 60000);
    let error = board?.departures_error;
    if (!board && state && Array.isArray(state.attributes.departures)) {
      deps = state.attributes.departures;
      age = Math.floor((Date.now() - Date.parse(state.last_updated)) / 60000);
      error = state.attributes.departures_error;
    }

    let body;
    if (!deps) {
      body = boardError
        ? `<div class="empty">Couldn't load departures: ${esc(boardError)}</div>`
        : `<div class="empty">Loading departures…</div>`;
    } else if (!deps.length && error && board?.server_unreachable) {
      body = `<div class="empty">${esc(error)}</div>`;
    } else if (!deps.length && error) {
      body = `<div class="empty">spdbf has no departures for this stop: ${esc(error)}</div>`;
    } else if (!deps.length) {
      body = `<div class="empty">No departures right now.</div>`;
    } else if (view === "line") {
      body = `<ul>${groupByLine(deps).map((group) => groupRow(group, age)).join("")}</ul>`;
    } else {
      body = `<ul>${deps.map((dep) => departureRow(dep, age)).join("")}</ul>`;
    }

    const distance = info.distance_km != null ? ` · ${esc(info.distance_km)} km` : "";
    const viaName = board?.departures_via ?? state?.attributes.departures_via;
    const fallback = board ? board.fallback : state?.attributes.fallback;
    const via = (viaName ? ` · departures via ${esc(viaName)}` : "")
      + (fallback ? " · from the public DBF (yours is unreachable)" : "");
    const merged = info.merged_stops?.length
      ? `<div class="sub">Includes ${info.merged_stops.map(esc).join(", ")}</div>`
      : "";
    const url = board?.url ?? info.url;
    const link = url
      ? `<a class="btn" href="${esc(url)}" target="_blank" rel="noopener">Open in spdbf</a>`
      : "";
    const status = board && boardError
      ? `<div class="status">Update failed: ${esc(boardError)}</div>`
      : !board && deps
        ? `<div class="status">Loading all departures…</div>`
        : "";
    const pressed = (value) => `aria-pressed="${view === value}"`;

    host.shadowRoot.querySelector(".panel").innerHTML = `
      <header>
        ${info.entity_picture ? `<img src="${esc(info.entity_picture)}" alt="">` : ""}
        <div>
          <h2 id="spdbf-title">${esc(info.name)}</h2>
          <div class="sub">${esc(info.networks?.length > 1 ? info.networks.join(" + ") : info.network)}${distance}${via}</div>
          ${merged}
        </div>
        <button class="icon-btn" data-action="close" aria-label="Close">×</button>
      </header>
      <div class="views" role="group" aria-label="Sort departures">
        <button data-view="line" ${pressed("line")}>By line</button>
        <button data-view="time" ${pressed("time")}>By time</button>
      </div>
      ${status}
      ${body}
      <footer>
        ${entityId ? `<button class="btn" data-action="details">Entity details</button>` : ""}
        ${link}
      </footer>`;
  }

  function show(info, id = null) {
    close();
    stop = info;
    entityId = id;
    host = document.createElement("div");
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `<style>${STYLE}</style>
      <div class="scrim" data-action="close"></div>
      <div class="panel" role="dialog" aria-modal="true" aria-labelledby="spdbf-title"></div>`;
    shadow.addEventListener("click", (ev) => {
      const viewButton = ev.target.closest("[data-view]");
      if (viewButton) {
        view = viewButton.dataset.view;
        saveView(view);
        render(true);
        return;
      }
      const action = ev.target.closest("[data-action]")?.dataset.action;
      if (action === "close") close();
      if (action === "details") {
        const target = entityId;
        close();
        homeAssistant()?.dispatchEvent(new CustomEvent("hass-more-info", {
          detail: { entityId: target, spdbfDetails: true }, bubbles: true, composed: true,
        }));
      }
    });
    document.body.appendChild(host);
    document.addEventListener("keydown", onKey, true);
    render(true);
    shadow.querySelector("[data-action=close].icon-btn")?.focus();
    timer = setInterval(render, TICK_MS);
    loadBoard();
  }

  function fromEntity(state) {
    const attrs = state.attributes;
    return {
      name: attrs.friendly_name ?? state.entity_id,
      eva: String(attrs.eva),
      network: attrs.network,
      entity_picture: attrs.entity_picture,
      distance_km: attrs.distance_km,
      url: attrs.url,
      merged_stops: attrs.merged_stops ?? [],
      networks: attrs.networks,
    };
  }

  const isStop = (state) =>
    state?.attributes?.source === SOURCE
    || (state?.entity_id?.startsWith("geo_location.")
      && state.attributes?.eva != null && state.attributes?.network != null);

  // Opens the dialog for any stop. A stop that is also on the map opens as
  // that entity, so "Entity details" works and the distance stays live.
  window.spdbfShowStop = (info) => {
    const states = hass()?.states ?? {};
    const match = Object.values(states).find((state) =>
      isStop(state) && String(state.attributes.eva) === String(info.eva)
      && state.attributes.network === info.network);
    if (match) show(fromEntity(match), match.entity_id);
    else show(info);
  };

  // Capture on window runs before Home Assistant's own handler, which
  // listens on the <home-assistant> element.
  window.addEventListener("hass-more-info", (ev) => {
    const id = ev.detail?.entityId;
    if (!id || ev.detail.spdbfDetails) return;
    const state = hass()?.states[id];
    if (!isStop(state)) return;
    ev.stopImmediatePropagation();
    show(fromEntity(state), id);
  }, true);

  console.info("spdbf Nearby Stops: departure list active");
})();
