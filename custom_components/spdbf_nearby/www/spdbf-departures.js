// spdbf Nearby Stops: opening a stop (map marker, entity search, ...) shows
// its departures instead of the generic entity dialog. Every other entity
// keeps Home Assistant's own dialog. Loaded on every page by the integration.
(() => {
  if (window.__spdbfDepartures) return;
  window.__spdbfDepartures = true;

  const SOURCE = "spdbf_nearby";
  const REFRESH_MS = 1000;
  const RERENDER_MS = 30000; // keeps "in N min" current between updates

  const STYLE = `
    :host { position: fixed; inset: 0; z-index: 1000; }
    .scrim { position: absolute; inset: 0; background: var(--mdc-dialog-scrim-color, rgba(0, 0, 0, 0.32)); }
    .panel {
      position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
      width: min(480px, calc(100% - 32px)); max-height: calc(100% - 64px);
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
    .late, .cancelled .min { color: var(--error-color, #db4437); }
    li.cancelled .dest div { text-decoration: line-through; opacity: 0.7; }
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

  let host = null;
  let entityId = null;
  let lastState = null;
  let lastRender = 0;
  let timer = null;

  function close() {
    clearInterval(timer);
    document.removeEventListener("keydown", onKey, true);
    host?.remove();
    host = entityId = lastState = null;
  }

  function onKey(ev) {
    if (ev.key === "Escape") {
      ev.stopPropagation();
      close();
    }
  }

  function departureRow(dep, ageMinutes) {
    const minutes = Math.max(0, (dep.minutes ?? 0) - ageMinutes);
    const delay = dep.delay ? `<span class="late"> +${esc(dep.delay)}</span>` : "";
    const when = dep.cancelled
      ? `<div class="min">cancelled</div><small>${esc(dep.scheduled)}</small>`
      : `<div class="min">${minutes === 0 ? "now" : `${minutes} min`}</div><small>${esc(dep.scheduled)}${delay}</small>`;
    const platform = dep.platform ? `<small>Platform ${esc(dep.platform)}</small>` : "";
    return `
      <li class="${dep.cancelled ? "cancelled" : ""}">
        <span class="line">${esc(dep.line)}</span>
        <div class="dest"><div>${esc(dep.destination)}</div>${platform}</div>
        <div class="when">${when}</div>
      </li>`;
  }

  function render(force = false) {
    const state = hass()?.states[entityId];
    if (!state) {
      close(); // the stop is no longer nearby
      return;
    }
    if (!force && state === lastState && Date.now() - lastRender < RERENDER_MS) return;
    lastState = state;
    lastRender = Date.now();

    const attrs = state.attributes;
    const deps = attrs.departures;
    // "minutes" was computed when the data was fetched.
    const ageMinutes = Math.floor((Date.now() - Date.parse(state.last_updated)) / 60000);
    let body;
    if (!Array.isArray(deps)) {
      body = `<div class="empty">Departures are turned off in the integration settings.</div>`;
    } else if (!deps.length && attrs.departures_error) {
      body = `<div class="empty">spdbf has no departures for this stop: ${esc(attrs.departures_error)}</div>`;
    } else if (!deps.length) {
      body = `<div class="empty">No departures right now.</div>`;
    } else {
      body = `<ul>${deps.map((dep) => departureRow(dep, ageMinutes)).join("")}</ul>`;
    }
    const distance = attrs.distance_km != null ? ` · ${esc(attrs.distance_km)} km` : "";
    const via = attrs.departures_via ? ` · departures via ${esc(attrs.departures_via)}` : "";
    const link = attrs.url
      ? `<a class="btn" href="${esc(attrs.url)}" target="_blank" rel="noopener">Open in spdbf</a>`
      : "";

    host.shadowRoot.querySelector(".panel").innerHTML = `
      <header>
        ${attrs.entity_picture ? `<img src="${esc(attrs.entity_picture)}" alt="">` : ""}
        <div>
          <h2 id="spdbf-title">${esc(attrs.friendly_name ?? entityId)}</h2>
          <div class="sub">${esc(attrs.network)}${distance}${via}</div>
        </div>
        <button class="icon-btn" data-action="close" aria-label="Close">×</button>
      </header>
      ${body}
      <footer>
        <button class="btn" data-action="details">Entity details</button>
        ${link}
      </footer>`;
  }

  function open(id) {
    close();
    entityId = id;
    host = document.createElement("div");
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `<style>${STYLE}</style>
      <div class="scrim" data-action="close"></div>
      <div class="panel" role="dialog" aria-modal="true" aria-labelledby="spdbf-title"></div>`;
    shadow.addEventListener("click", (ev) => {
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
    timer = setInterval(render, REFRESH_MS);
  }

  const isStop = (state) =>
    state?.attributes?.source === SOURCE
    || (state?.entity_id?.startsWith("geo_location.")
      && state.attributes?.eva != null && state.attributes?.network != null);

  // Capture on window runs before Home Assistant's own handler, which
  // listens on the <home-assistant> element.
  window.addEventListener("hass-more-info", (ev) => {
    const id = ev.detail?.entityId;
    if (!id || ev.detail.spdbfDetails) return;
    if (!isStop(hass()?.states[id])) return;
    ev.stopImmediatePropagation();
    open(id);
  }, true);

  console.info("spdbf Nearby Stops: departure list active");
})();
