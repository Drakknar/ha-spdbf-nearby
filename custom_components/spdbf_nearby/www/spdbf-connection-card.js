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
//   platforms: ["2"]                 # optional filters; "2" also matches "2 (U2)"
//   lines: [S41]
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
    platforms: ["Only these platforms", "The platform is the direction. Empty: all platforms."],
    lines: ["Only these lines", "Suggested: the lines the sensor has now. Empty: all lines."],
    destinations: [
      "Only these destinations",
      "The train's last stop; part of the name is enough. Lines often end early, so prefer platform and line. Empty: all.",
    ],
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

  // The card's filters, shared by the card and its editor. A platform "2"
  // also matches BVG's "2 (U2)"; a destination matches part of the name.
  const departureFilter = (config) => {
    const platforms = asList(config.platforms).map((p) => p.toLowerCase());
    const lines = asList(config.lines).map((line) => tidy(line).toUpperCase());
    const destinations = asList(config.destinations).map((d) => d.toLowerCase());
    return ({ dep, line }) => {
      const platform = String(dep.platform ?? "").trim().toLowerCase();
      return (!platforms.length || platforms.some((p) => platform === p || platform.startsWith(`${p} `)))
        && (!lines.length || lines.includes(line.toUpperCase()))
        && (!destinations.length
          || destinations.some((d) => String(dep.destination ?? "").toLowerCase().includes(d)));
    };
  };

  const sensorRows = (state) => (Array.isArray(state?.attributes?.next_departures)
    ? state.attributes.next_departures : [])
    .map((dep) => ({ dep, line: tidy(dep.train, dep.trainNumber) }));

  // Values in first-seen order, each with what it is seen with: platform ->
  // lines and their destinations, line -> destinations, destination -> lines.
  const group = (rows, keyOf, describe) => {
    const seen = new Map();
    for (const row of rows) {
      const key = keyOf(row);
      if (!key) continue;
      if (!seen.has(key)) seen.set(key, new Map());
      const detail = seen.get(key);
      const [sub, item] = describe(row);
      if (!detail.has(sub)) detail.set(sub, new Set());
      if (item) detail.get(sub).add(item);
    }
    return seen;
  };
  const platformOf = ({ dep }) => String(dep.platform ?? "").trim();
  const destinationOf = ({ dep }) => String(dep.destination ?? "").trim();
  const routes = (detail) => [...detail]
    .map(([line, dests]) => (dests.size ? `${line} → ${[...dests].join(", ")}` : line)).join(" · ");

  const FILTER_FIELDS = ["platforms", "lines", "destinations"];
  const NOT_SEEN = {
    platforms: (v) => `No departure from platform ${v} right now.`,
    lines: (v) => `${v} doesn't run here right now (on the chosen platforms).`,
    destinations: (v) => `No train to "${v}" right now; the line may be turning back early. A platform and line filter keeps working when it does.`,
  };

  // The visual editor: Home Assistant's own form, with the platforms, lines
  // and destinations the sensor has right now as suggestions, and a check of
  // what the card would show. Values not in the lists can still be typed.
  class SpdbfConnectionCardEditor extends HTMLElement {
    setConfig(config) {
      this._config = config;
      this._render();
    }

    set hass(hass) {
      const changed = !this._hass || hass.states[this._config?.entity] !== this._hass.states[this._config?.entity];
      this._hass = hass;
      if (changed) this._render();
      else if (this._form) this._form.hass = hass;
    }

    _schema(rows) {
      const config = this._config;
      const onPlatforms = rows.filter(departureFilter({ platforms: config.platforms }));
      const onLines = onPlatforms.filter(departureFilter({ lines: config.lines }));
      const options = (seen, label, chosen) => {
        const list = [...seen].map(([value, detail]) => ({ value, label: label(value, detail) }));
        // Chosen values that aren't running now stay selectable.
        for (const value of asList(chosen)) {
          if (!list.some((o) => o.value.toLowerCase() === value.toLowerCase())) list.push({ value, label: `${value} (not seen now)` });
        }
        return list;
      };
      const pick = (name, list) => ({ name, selector: { select: { multiple: true, custom_value: true, options: list } } });
      return [
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
        pick("platforms", options(
          new Map([...group(rows, platformOf, ({ dep, line }) => [line, destinationOf({ dep })])]
            .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }))),
          (platform, detail) => `${platform}: ${routes(detail)}`, config.platforms)),
        pick("lines", options(
          group(onPlatforms, ({ line }) => line, ({ dep }) => [destinationOf({ dep })]),
          (line, detail) => `${line} → ${[...detail.keys()].join(", ")}`, config.lines)),
        pick("destinations", options(
          group(onLines, destinationOf, ({ line }) => [line]),
          (dest, detail) => `${dest} (${[...detail.keys()].join(", ")})`, config.destinations)),
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
      ];
    }

    // What the filters would show now, and any filter value that matches nothing.
    _checks(rows) {
      const config = this._config;
      if (!config.entity || !this._hass?.states[config.entity]) return [];
      if (!rows.length) return [{ type: "info", text: "The sensor has no departures right now, so nothing can be checked." }];
      const checks = [];
      let scope = rows;
      for (const field of FILTER_FIELDS) {
        for (const value of asList(config[field])) {
          if (!scope.some(departureFilter({ [field]: [value] }))) checks.push({ type: "warning", text: NOT_SEEN[field](value) });
        }
        scope = scope.filter(departureFilter({ [field]: config[field] }));
      }
      const shown = rows.filter(departureFilter(config));
      if (FILTER_FIELDS.some((field) => asList(config[field]).length)) {
        const lines = group(shown, () => "all", ({ line, dep }) => [line, destinationOf({ dep })]).get("all");
        checks.unshift(shown.length
          ? { type: "success", text: `Matches ${shown.length} of the sensor's ${rows.length} departures now: ${routes(lines)}` }
          : { type: "error", text: `Matches none of the sensor's ${rows.length} departures right now.` });
      }
      return checks;
    }

    _render() {
      if (!this._config || !this._hass) return;
      if (!this.shadowRoot) this.attachShadow({ mode: "open" });
      const rows = sensorRows(this._hass.states[this._config.entity]);
      if (!this._form) {
        this.shadowRoot.innerHTML = `<style>
          :host { display: block; }
          #checks { display: flex; flex-direction: column; gap: 8px; margin-bottom: 8px; }
          #checks:empty { display: none; }
        </style><div id="checks"></div>`;
        this._form = document.createElement("ha-form");
        this._form.computeLabel = (field) => FORM_LABELS[field.name]?.[0];
        this._form.computeHelper = (field) => FORM_LABELS[field.name]?.[1] || undefined;
        this._form.addEventListener("value-changed", (ev) => this._changed(ev.detail.value));
        this.shadowRoot.append(this._form);
      }
      const checks = this.shadowRoot.getElementById("checks");
      checks.replaceChildren(...this._checks(rows).map(({ type, text }) => {
        const alert = document.createElement("ha-alert");
        alert.alertType = type;
        alert.textContent = text;
        return alert;
      }));
      this._form.hass = this._hass;
      this._form.schema = this._schema(rows);
      this._form.data = this._config;
    }

    _changed(value) {
      const config = { ...value };
      // Empty filters are left out of the YAML.
      for (const field of FILTER_FIELDS) if (!asList(config[field]).length) delete config[field];
      this._config = config;
      this._render();
      this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
    }
  }
  customElements.define("spdbf-connection-card-editor", SpdbfConnectionCardEditor);

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

    // A native ha-form, but its suggestions depend on the chosen sensor,
    // which getConfigForm's fixed schema can't do.
    static getConfigElement() {
      return document.createElement("spdbf-connection-card-editor");
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
      if (!Array.isArray(this._state?.attributes?.next_departures)) return null;
      const matches = departureFilter(this._config);
      const now = Date.now() / 1000;
      const max = Number(this._config.departures) || DEFAULT_DEPARTURES;
      return sensorRows(this._state)
        .filter((row) => matches(row)
          && (row.dep.departure_timestamp == null || row.dep.departure_timestamp > now - 60))
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
