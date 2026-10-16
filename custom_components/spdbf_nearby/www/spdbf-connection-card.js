// spdbf Nearby Stops: a dashboard card for one connection you use, from a
// db_infoscreen sensor. It uses the frontend core (dbf-core.js) for line
// names and colours, and for the departure dialog: tapping a departure opens
// its trip, tapping the title the stop's full board. A link row under the
// card leads to the next leg, for changing trains: "↓ 15 min ride · 4 min
// walk". The ride is timed on this card's lines (S-Bahn to S-Bahn, bus to
// bus), so it ends where that train arrives; the walk is the change from
// there. The last leg can instead ride to a station on its line, typed by
// name: "↓ 6 min ride to Senefelderplatz · 4 min walk".
// Loaded on every page by the integration (through spdbf-loader.js), so no
// dashboard resource is needed.
//
//   type: custom:spdbf-connection-card
//   entity: sensor.db_infoscreen_berlin_westkreuz_departures
//   title: Westkreuz                 # optional, default: the sensor's station
//   departures: 6                    # optional
//   platforms: ["2"]                 # optional filters; "2" also matches "2 (U2)"
//   lines: [S41]
//   destinations: [Pankow]           #   (part of the destination is enough)
//   hide_destinations: [Staaken]     #   hides these; one ending early still shows
//   ride_to: sensor.db_infoscreen_berlin_alexanderplatz_departures
//                                    # optional: the next card's sensor
//   ride_to_station: Senefelderplatz # optional, or a station on this line
//   walk_minutes: 4                  # optional walk after the ride
//   show_ride: true                  # optional, turn either part off
//   show_walk: true
//
// Before 0.8.0 the row sat above the second card (ride_from: the previous
// card's sensor); a card with ride_from and neither ride_to option still
// shows it that way.
(() => {
  if (customElements.get("spdbf-connection-card")) return;

  const TICK_MS = 30000; // keeps "in N min" current between sensor updates
  const RIDE_REFRESH_MS = 10 * 60000; // scheduled ride times hardly change
  const RIDE_TYPING_MS = 1000; // in the editor, look up once typing pauses
  const DEFAULT_DEPARTURES = 6;

  const STYLE = `
    :host { display: block; }
    ha-card { overflow: hidden; }
    .walk {
      display: flex; align-items: center; gap: 8px; padding: 8px 16px;
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
    hide_destinations: [
      "Hide these destinations",
      "For stops without platforms (buses): hide the other direction's last stops, so a bus ending early in your direction still shows.",
    ],
    ride_to: [
      "Ride to (the next card's sensor)",
      "Shows under this card the ride time on its lines to that station.",
    ],
    ride_to_station: [
      "Or ride to a station on this line",
      "For the last leg: the station's name, e.g. where you get off. Used when \"Ride to\" is empty.",
    ],
    walk_minutes: [
      "Walk after the ride (minutes)",
      "From where the train arrives to the next card's platform, or on from the station.",
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
    const hidden = asList(config.hide_destinations).map((d) => d.toLowerCase());
    return ({ dep, line }) => {
      const platform = String(dep.platform ?? "").trim().toLowerCase();
      const destination = String(dep.destination ?? "").toLowerCase();
      return (!platforms.length || platforms.some((p) => platform === p || platform.startsWith(`${p} `)))
        && (!lines.length || lines.includes(line.toUpperCase()))
        && (!destinations.length || destinations.some((d) => destination.includes(d)))
        && !hidden.some((d) => destination.includes(d));
    };
  };

  const sensorRows = (state) => (Array.isArray(state?.attributes?.next_departures)
    ? state.attributes.next_departures : [])
    .map((dep) => ({ dep, line: tidy(dep.train, dep.trainNumber) }));

  const platformOf = ({ dep }) => String(dep.platform ?? "").trim();
  const destinationOf = ({ dep }) => String(dep.destination ?? "").trim();
  const unique = (values) => [...new Set(values.filter(Boolean))];

  // Rows by platform, line or destination, in first-seen order.
  const byKey = (rows, keyOf) => {
    const groups = new Map();
    for (const row of rows) {
      const key = keyOf(row);
      if (key) groups.set(key, [...(groups.get(key) ?? []), row]);
    }
    return groups;
  };

  // "U2 → Zoologischer Garten, Alexanderplatz · S41 → Landsberger Allee"
  const routes = (rows) => [...byKey(rows, ({ line }) => line)]
    .map(([line, onLine]) => {
      const destinations = unique(onLine.map(destinationOf));
      return destinations.length ? `${line} → ${destinations.join(", ")}` : line;
    })
    .join(" · ");

  // HA loads ha-form only when an editor needs it; the tile card's editor
  // does, so loading that makes it available to ours.
  const loadHaForm = async () => {
    if (customElements.get("ha-form")) return;
    const helpers = await window.loadCardHelpers?.();
    const tile = await helpers?.createCardElement({ type: "tile", entity: "sun.sun" });
    await tile?.constructor.getConfigElement?.();
  };

  const destinationLabel = (destination, rows) => `${destination} (${unique(rows.map(({ line }) => line)).join(", ")})`;

  const FILTER_FIELDS = ["platforms", "lines", "destinations", "hide_destinations"];
  const NOT_SEEN = {
    platforms: (v) => `No departure from platform ${v} right now.`,
    lines: (v) => `${v} doesn't run here right now (on the chosen platforms).`,
    destinations: (v) => `No train to "${v}" right now; the line may be turning back early. A platform and line filter keeps working when it does.`,
    hide_destinations: (v) => `Nothing to "${v}" right now, so hiding it changes nothing at the moment.`,
  };

  // The visual editor: Home Assistant's own form, with the platforms, lines
  // and destinations the sensor has right now as suggestions, and a check of
  // what the card would show. Values not in the lists can still be typed.
  class SpdbfConnectionCardEditor extends HTMLElement {
    setConfig(config) {
      // The card also takes a single value, e.g. "lines: U2"; the form needs lists.
      this._config = { ...config };
      for (const field of FILTER_FIELDS) {
        if (this._config[field] !== undefined) this._config[field] = asList(this._config[field]);
      }
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
      // A multiple select with the values seen now; chosen values that aren't
      // running now stay listed, and others can be typed.
      const pick = (name, groups, label) => {
        const options = [...groups].map(([value, grouped]) => ({ value, label: label(value, grouped) }));
        // Destinations match part of the name, the others the whole value.
        const partial = name.endsWith("destinations");
        for (const value of asList(config[name])) {
          const v = value.toLowerCase();
          const seen = options.some((o) => (partial ? o.value.toLowerCase().includes(v) : o.value.toLowerCase() === v));
          if (!seen) options.push({ value, label: `${value} (not seen now)` });
        }
        return { name, selector: { select: { multiple: true, custom_value: true, options } } };
      };
      const platforms = [...byKey(rows, platformOf)].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }));
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
        pick("platforms", platforms, (platform, grouped) => `${platform}: ${routes(grouped)}`),
        pick("lines", byKey(onPlatforms, ({ line }) => line), (line, grouped) => routes(grouped)),
        pick("destinations", byKey(onLines, destinationOf), destinationLabel),
        pick("hide_destinations", byKey(onLines, destinationOf), destinationLabel),
        { name: "ride_to", selector: { entity: { domain: "sensor", integration: "db_infoscreen" } } },
        { name: "ride_to_station", selector: { text: {} } },
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
      if (!this._hass.states[config.entity]) return [];
      if (!rows.length) return [{ type: "info", text: "The sensor has no departures right now, so nothing can be checked." }];
      const checks = [];
      let scope = rows;
      for (const field of FILTER_FIELDS) {
        for (const value of asList(config[field])) {
          // A hidden destination is seen when hiding it removes a departure.
          const keeps = departureFilter({ [field]: [value] });
          const seen = field === "hide_destinations" ? scope.some((row) => !keeps(row)) : scope.some(keeps);
          if (!seen) checks.push({ type: "warning", text: NOT_SEEN[field](value) });
        }
        scope = scope.filter(departureFilter({ [field]: config[field] }));
      }
      const shown = rows.filter(departureFilter(config));
      if (FILTER_FIELDS.some((field) => asList(config[field]).length)) {
        checks.unshift(shown.length
          ? { type: "success", text: `Matches ${shown.length} of the sensor's ${rows.length} departures now: ${routes(shown)}` }
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
      // In the editor the ride follows the options as they change, once
      // typing (e.g. a station name) pauses.
      if (this._hass) {
        this._state = this._hass.states[this._config.entity];
        clearTimeout(this._rideTimer);
        this._rideTimer = setTimeout(() => this._updateRide(), RIDE_TYPING_MS);
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
      clearTimeout(this._rideTimer);
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
    static async getConfigElement() {
      await loadHaForm();
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

    // Where the link row leads: below the card to the next card's station or
    // to a station typed in, or (before 0.8.0) above it from the previous card.
    _link() {
      const config = this._config;
      const station = String(config.ride_to_station ?? "").trim();
      if (config.ride_to || station) return { below: true, to: config.ride_to, station };
      if (config.ride_from) return { below: false, from: config.ride_from };
      return null;
    }

    // The ride on the leg's lines, so an S-Bahn leg is timed S-Bahn to
    // S-Bahn. Leaving from this card, those are the lines it shows (all the
    // sensor's if its filters match none now). Looked up now and then; the
    // card renders without it until it arrives.
    _updateRide() {
      const config = this._config;
      const link = this._link();
      if (!link || !this._state || config.show_ride === false || !core()?.ride) return;
      let start;
      let end;
      let rows;
      if (link.below) {
        const next = link.to && this._hass?.states[link.to];
        if (link.to && !next) return;
        start = this._stop();
        end = next ? this._stop(next, "") : { eva: "", name: link.station };
        const all = sensorRows(this._state);
        rows = all.filter(departureFilter(config));
        if (!rows.length) rows = all;
      } else {
        const from = this._hass?.states[link.from];
        if (!from) return;
        start = this._stop(from, "");
        end = this._stop();
        rows = sensorRows(from);
      }
      // Sorted, so the next train changing doesn't start a new lookup.
      const lines = unique(rows.map(({ line }) => line))
        .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
      const key = JSON.stringify([start.eva, end.eva, end.name, lines]);
      if (this._ride && this._ride.key === key && Date.now() - this._ride.time < RIDE_REFRESH_MS) return;
      this._ride = { key, time: Date.now(), result: this._ride?.key === key ? this._ride.result : null };
      core().ride(
        { eva: start.eva, name: start.name, network: start.network },
        end.eva ? { eva: end.eva, name: end.name } : { name: end.name },
        lines,
      ).then((result) => {
        if (this._ride?.key !== key) return;
        this._ride.result = result;
        this._render();
      }).catch(() => {});
    }

    _linkRow(link) {
      const config = this._config;
      const parts = [];
      const ride = this._ride?.result;
      if (config.show_ride !== false && ride?.minutes != null) {
        const to = link?.below && !link.to && link.station ? ` to ${esc(link.station)}` : "";
        parts.push(`<ha-icon icon="mdi:train"></ha-icon>${esc(ride.minutes)} min ride${to}`);
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
      const link = this._link();
      const walk = this._linkRow(link);
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
        ${link?.below ? "" : walk}
        <ha-card>
          <header title="Show all departures"><ha-icon icon="${esc(icon)}"></ha-icon>
            <span class="title">${esc(title)}</span></header>
          ${body}
        </ha-card>
        ${link?.below ? walk : ""}`;
    }
  }

  customElements.define("spdbf-connection-card", SpdbfConnectionCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "spdbf-connection-card",
    name: "spdbf connection",
    description: "Departures of a connection you use (db_infoscreen sensor), with the ride and walk to the next card or station.",
    preview: true,
    documentationURL: "https://github.com/Drakknar/ha-spdbf-nearby#connection-card",
  });
})();
