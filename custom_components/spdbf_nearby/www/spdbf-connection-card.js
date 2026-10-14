// spdbf Nearby Stops: a dashboard card for one connection you use, from a
// db_infoscreen sensor. It uses the frontend core (dbf-core.js) for line
// names and colours, and for the departure dialog: tapping a departure opens
// its trip, tapping the title the stop's full board. A link row joins it to
// the card before, for changing trains: "↓ 15 min ride · 4 min walk". The
// ride is timed on the previous card's lines (S-Bahn to S-Bahn, bus to bus),
// so it ends where that train arrives; the walk is the change from there.
// Loaded on every page by the integration, so no dashboard resource is needed.
//
//   type: custom:spdbf-connection-card
//   entity: sensor.db_infoscreen_berlin_westkreuz_departures
//   title: Westkreuz                 # optional, default: the sensor's station
//   departures: 6                    # optional
//   lines: [S41]                     # optional filters
//   destinations: [Pankow]           #   (part of the destination is enough)
//   ride_from: sensor.db_infoscreen_berlin_savignyplatz_departures
//                                    # optional: the previous card's sensor
//   walk_minutes: 4                  # optional walk from the previous card
//   show_ride: true                  # optional, turn either part off
//   show_walk: true
(() => {
  if (customElements.get("spdbf-connection-card")) return;

  const TICK_MS = 30000; // keeps "in N min" current between sensor updates
  const RIDE_REFRESH_MS = 10 * 60000; // scheduled ride times hardly change
  const DEFAULT_DEPARTURES = 6;

  const STYLE = `
    :host { display: block; }
    ha-card { overflow: hidden; }
    .walk {
      display: flex; align-items: center; gap: 8px; padding: 8px 16px 0;
      color: var(--secondary-text-color, #727272); font-size: 13px;
    }
    .walk ha-icon { --mdc-icon-size: 18px; }
    header {
      display: flex; align-items: center; gap: 8px; padding: 12px 16px 4px; cursor: pointer;
    }
    header ha-icon { color: var(--state-icon-color, var(--primary-color, #03a9f4)); }
    header .title {
      flex: 1; min-width: 0; font-size: var(--ha-font-size-l, 18px); font-weight: 600;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    ul { list-style: none; margin: 0; padding: 0 0 8px; }
    li {
      display: grid; grid-template-columns: auto 1fr auto; gap: 12px; align-items: center;
      padding: 8px 16px; border-top: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
    }
    li:first-child { border-top: none; }
    li.trip { cursor: pointer; }
    li.trip:hover { background: var(--secondary-background-color, rgba(0, 0, 0, 0.04)); }
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
    .late, li.cancelled .min { color: var(--error-color, #db4437); }
    li.cancelled .dest div { text-decoration: line-through; opacity: 0.7; }
    .empty { padding: 8px 16px 16px; color: var(--secondary-text-color, #727272); font-size: 14px; }
  `;

  const FORM_LABELS = {
    entity: ["Departures sensor", "A db_infoscreen departures sensor."],
    title: ["Title", "Default: the sensor's station."],
    icon: ["Icon", ""],
    departures: ["Departures to show", ""],
    lines: ["Only these lines", "e.g. S41 or U2. Empty: all lines."],
    destinations: ["Only these destinations", "Part of the name is enough, e.g. Pankow. Empty: all."],
    ride_from: [
      "Ride from (the previous card's sensor)",
      "Shows the ride time from that station, on its lines, to this one.",
    ],
    walk_minutes: [
      "Walk from the previous card (minutes)",
      "The change from where the previous train arrives to this card's platform.",
    ],
    show_ride: ["Show ride time", ""],
    show_walk: ["Show walk time", ""],
  };

  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  const core = () => window.dbfCore;
  const tidy = (train, number) => core()?.tidyLine(train, number) ?? String(train ?? "");
  const asList = (value) => (Array.isArray(value) ? value : value ? [value] : [])
    .map((item) => String(item).trim()).filter(Boolean);

  // db_infoscreen rows from DB (IRIS) have scheduledDeparture "HH:MM"; HAFAS
  // rows (BVG, ...) have scheduledTime "YYYY-MM-DD HH:MM:SS".
  const isIris = (dep) => dep.scheduledDeparture !== undefined;

  // Same-day "HH:MM" or a date-time string to "HH:MM".
  const hhmm = (value) => {
    const text = String(value ?? "");
    return text.length >= 16 ? text.slice(11, 16) : text.slice(0, 5);
  };

  class SpdbfConnectionCard extends HTMLElement {
    constructor() {
      super();
      this._config = {};
      this._timer = null;
      this._rows = [];
      this._ride = null; // { key, time, result }
    }

    setConfig(config) {
      if (!config?.entity) throw new Error("Pick a db_infoscreen departures sensor (entity).");
      this._config = { departures: DEFAULT_DEPARTURES, ...config };
      if (!this.shadowRoot) {
        this.attachShadow({ mode: "open" });
        this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
      }
      this._render();
    }

    set hass(hass) {
      const state = hass.states[this._config.entity];
      const changed = state !== this._state;
      this._hass = hass;
      this._state = state;
      if (changed) {
        this._updateRide();
        this._render();
      }
    }

    connectedCallback() {
      this._timer = setInterval(() => this._render(), TICK_MS);
    }

    disconnectedCallback() {
      clearInterval(this._timer);
    }

    getCardSize() {
      return 1 + Math.min(Number(this._config.departures) || DEFAULT_DEPARTURES, 10);
    }

    getGridOptions() {
      return { columns: 6, min_columns: 4, rows: "auto" };
    }

    static getStubConfig(hass) {
      const sensor = Object.keys(hass?.states ?? {}).find(
        (id) => id.startsWith("sensor.") && hass.states[id].attributes?.next_departures !== undefined,
      );
      return { entity: sensor ?? "", departures: DEFAULT_DEPARTURES };
    }

    // Rendered by Home Assistant's card editor as a native form.
    static getConfigForm() {
      return {
        schema: [
          { name: "entity", required: true, selector: { entity: { domain: "sensor", integration: "db_infoscreen" } } },
          {
            type: "grid",
            name: "",
            schema: [
              { name: "title", selector: { text: {} } },
              { name: "icon", selector: { icon: {} } },
            ],
          },
          { name: "departures", selector: { number: { min: 1, max: 20, mode: "box" } } },
          { name: "lines", selector: { text: { multiple: true } } },
          { name: "destinations", selector: { text: { multiple: true } } },
          { name: "ride_from", selector: { entity: { domain: "sensor", integration: "db_infoscreen" } } },
          { name: "walk_minutes", selector: { number: { min: 0, max: 60, mode: "box", unit_of_measurement: "min" } } },
          {
            type: "grid",
            name: "",
            schema: [
              { name: "show_ride", selector: { boolean: {} } },
              { name: "show_walk", selector: { boolean: {} } },
            ],
          },
        ],
        computeLabel: (field) => FORM_LABELS[field.name]?.[0],
        computeHelper: (field) => FORM_LABELS[field.name]?.[1] || undefined,
      };
    }

    // The station as the departure dialog needs it, from a sensor's state.
    _stop(state = this._state, title = this._config.title) {
      const attrs = state?.attributes ?? {};
      const station = String(attrs.station ?? "");
      const first = (attrs.next_departures ?? [])[0];
      // HAFAS stop ids are numbers (BVG: 900...); IRIS stations are names.
      const network = first && !isIris(first) ? "BVG" : "DB";
      const name = /^\d+$/.test(station) ? title || station : station;
      return { name, eva: station, network, colors: "berlin" };
    }

    // The ride from the previous card's station, on that sensor's lines, so an
    // S-Bahn leg is timed S-Bahn to S-Bahn. Looked up now and then; the
    // card renders without it until it arrives.
    _updateRide() {
      const config = this._config;
      const from = config.ride_from && this._hass?.states[config.ride_from];
      if (!from || !this._state || config.show_ride === false || !core()?.ride) return;
      const lines = [...new Set((from.attributes?.next_departures ?? [])
        .map((dep) => tidy(dep.train, dep.trainNumber)).filter(Boolean))];
      const start = this._stop(from, "");
      const end = this._stop();
      const key = JSON.stringify([start.eva, end.eva, lines]);
      if (this._ride && this._ride.key === key && Date.now() - this._ride.time < RIDE_REFRESH_MS) return;
      this._ride = { key, time: Date.now(), result: this._ride?.key === key ? this._ride.result : null };
      core().ride(
        { eva: start.eva, name: start.name, network: start.network },
        { eva: end.eva, name: end.name },
        lines,
      ).then((result) => {
        if (this._ride?.key !== key) return;
        this._ride.result = result;
        this._render();
      }).catch(() => {});
    }

    _linkRow() {
      const config = this._config;
      const parts = [];
      const ride = this._ride?.result;
      if (config.show_ride !== false && ride?.minutes != null) {
        parts.push(`<ha-icon icon="mdi:train"></ha-icon>${esc(ride.minutes)} min ride`);
      }
      if (config.show_walk !== false && Number(config.walk_minutes) > 0) {
        parts.push(`<ha-icon icon="mdi:walk"></ha-icon>${esc(config.walk_minutes)} min walk`);
      }
      return parts.length ? `<div class="walk">↓ ${parts.join(" · ")}</div>` : "";
    }

    _departures() {
      const all = this._state?.attributes?.next_departures;
      if (!Array.isArray(all)) return null;
      const lines = asList(this._config.lines).map((line) => tidy(line).toUpperCase());
      const destinations = asList(this._config.destinations).map((d) => d.toLowerCase());
      const now = Date.now() / 1000;
      const max = Number(this._config.departures) || DEFAULT_DEPARTURES;
      return all
        .map((dep) => ({ dep, line: tidy(dep.train, dep.trainNumber) }))
        .filter(({ dep, line }) =>
          (!lines.length || lines.includes(line.toUpperCase()))
          && (!destinations.length
            || destinations.some((d) => String(dep.destination ?? "").toLowerCase().includes(d)))
          && (dep.departure_timestamp == null || dep.departure_timestamp > now - 60))
        .slice(0, max);
    }

    // What dbfCore.showTrip needs: the trip reference and the row's look.
    _tripDeparture({ dep, line }, stop) {
      let trip = null;
      if (isIris(dep)) {
        const type = String(dep.train ?? "").trim().split(/\s+/)[0];
        if (type && dep.trainNumber) trip = { train: `${type} ${dep.trainNumber}`, station: stop.eva };
      } else if (dep.trip_id || dep.tripId) {
        trip = { id: String(dep.trip_id || dep.tripId), hafas: stop.network };
      }
      return { line, destination: dep.destination, trip };
    }

    _onClick(ev) {
      if (!core()) return;
      if (ev.target.closest("header")) {
        core().showStop(this._stop());
        return;
      }
      const row = ev.target.closest("li[data-index]");
      const item = row && this._rows[Number(row.dataset.index)];
      if (!item) return;
      const stop = this._stop();
      const dep = this._tripDeparture(item, stop);
      if (dep.trip) core().showTrip(stop, dep);
      else core().showStop(stop);
    }

    _row({ dep, line }, index, network) {
      const now = Date.now() / 1000;
      const delay = Number(dep.delayDeparture ?? dep.delay ?? 0) || 0;
      const scheduled = hhmm(dep.scheduledDeparture ?? dep.scheduledTime);
      const minutes = dep.departure_timestamp != null
        ? Math.max(0, Math.floor((dep.departure_timestamp - now) / 60))
        : null;
      const cancelled = Boolean(dep.is_cancelled ?? dep.isCancelled);
      const style = core()?.lineStyle(line, network);
      const css = style ? ` style="background:${style.background};color:${style.color}"` : "";
      const late = delay ? `<span class="late"> +${esc(delay)}</span>` : "";
      const main = cancelled ? "cancelled" : minutes === null ? scheduled : minutes === 0 ? "now" : `${minutes} min`;
      const platform = dep.platform ? `<small>Platform ${esc(dep.platform)}</small>` : "";
      const tripClass = isIris(dep) ? dep.trainNumber : dep.trip_id || dep.tripId;
      const classes = [cancelled && "cancelled", tripClass && "trip"].filter(Boolean).join(" ");
      return `
        <li class="${classes}" data-index="${index}">
          <span class="line"${css}>${esc(line)}</span>
          <div class="dest"><div>${esc(dep.destination)}</div>${platform}</div>
          <div class="when"><div class="min">${esc(main)}</div><small>${esc(scheduled)}${late}</small></div>
        </li>`;
    }

    _render() {
      if (!this.shadowRoot) return;
      const config = this._config;
      const attrs = this._state?.attributes ?? {};
      const title = config.title || attrs.station || attrs.friendly_name || config.entity;
      const icon = config.icon || attrs.icon || "mdi:train";
      const walk = this._linkRow();
      // Berlin S- and U-Bahn colours, as on BVG stops.
      const network = "BVG";
      let body;
      if (!this._state) {
        body = `<div class="empty">Sensor ${esc(config.entity)} not found.</div>`;
      } else {
        this._rows = this._departures() ?? [];
        body = this._rows.length
          ? `<ul>${this._rows.map((item, i) => this._row(item, i, network)).join("")}</ul>`
          : `<div class="empty">No departures right now.</div>`;
      }
      this.shadowRoot.innerHTML = `<style>${STYLE}</style>
        ${walk}
        <ha-card>
          <header title="Show all departures"><ha-icon icon="${esc(icon)}"></ha-icon>
            <span class="title">${esc(title)}</span></header>
          ${body}
        </ha-card>`;
    }
  }

  customElements.define("spdbf-connection-card", SpdbfConnectionCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "spdbf-connection-card",
    name: "spdbf connection",
    description: "Departures of a connection you use (db_infoscreen sensor), with the ride and walk from the previous card.",
    preview: true,
    documentationURL: "https://github.com/Drakknar/ha-spdbf-nearby#connection-card",
  });
})();
