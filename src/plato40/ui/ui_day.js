// Day view: one day replayed in 15-min steps. Floor plan of recorded room states (left),
// the curated timeline of what OSAM saw and proposed up to the cursor (right), plant and
// shift strip, and three sparklines from the hourly series. Recorded data fills the tiles;
// OSAM's proposals are outlines and badges only, because the replay never applies them.
(() => {
  const STEP_MS = 15 * 60 * 1000;
  const PLAY_MS = 120; // one 15-min step per tick: a day in about 12 s
  const STATE_STYLE = {
    occupied: { fill: "var(--dv-occ)", label: "occupied" },
    dirty: { fill: "var(--dv-dirty)", label: "dirty" },
    cleaning: { fill: "var(--dv-cleaning)", label: "cleaning" },
    clean: { fill: "var(--dv-clean)", label: "clean, vacant" },
    unknown: { fill: "var(--dv-unknown)", label: "no event yet" },
  };
  const MARK = { water_leak: "L", heating_comfort: "H", room_turnaround: "T", occupancy_surge: "S",
    guest_service: "G", ventilation_maintenance: "V", energy_optimization: "E", logistics: "W",
    data_quality: "D" };
  const SERIES = {
    hp1_cop_ratio: { label: "HP1 COP ratio", unit: "", lanes: ["heating_comfort"] },
    outdoor_c: { label: "Outdoor", unit: " degC", lanes: ["heating_comfort", "energy_optimization"] },
    main_flow_l_h: { label: "Main water flow", unit: " L/h", lanes: ["water_leak"] },
    occupied: { label: "Rooms occupied", unit: "", lanes: ["room_turnaround", "occupancy_surge", "guest_service"] },
    soc_kwh: { label: "BAT1 SOC", unit: " kWh", lanes: ["energy_optimization"] },
    price_eur_kwh: { label: "Price", unit: " EUR/kWh", lanes: ["energy_optimization"] },
  };
  const RANK = typeof PRIO !== "undefined" ? PRIO : { low: 0, medium: 1, high: 2, critical: 3 };
  // Thresholds come from the run (DATA.thresholds), never typed here.
  const BAND = () => DATA.thresholds?.comfort_c ?? null;
  const COP_LOW = () => DATA.thresholds?.cop_ratio_low ?? null;

  let cur = { day: null, i: 0, step: null, timer: null, open: new Set(), showTemp: false, el: null };
  const cache = {};

  function css() {
    if (document.getElementById("dv-style")) return;
    const s = document.createElement("style");
    s.id = "dv-style";
    s.textContent = `
:root { --dv-occ: #aab4bd; --dv-dirty: #d3c9b6; --dv-cleaning: #c2d3cf; --dv-clean: #e8ecef; --dv-unknown: #f4f6f8; --dv-tile-fg: #17202a; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --dv-occ: #4a5862; --dv-dirty: #5c5444; --dv-cleaning: #3d5550; --dv-clean: #26323a; --dv-unknown: #1b242a; --dv-tile-fg: #e3e9ee; } }
:root[data-theme="dark"] { --dv-occ: #4a5862; --dv-dirty: #5c5444; --dv-cleaning: #3d5550; --dv-clean: #26323a; --dv-unknown: #1b242a; --dv-tile-fg: #e3e9ee; }
.dv { display: grid; gap: 14px; }
.dv-head { display: flex; flex-wrap: wrap; gap: 10px 20px; align-items: center; justify-content: space-between; padding: 10px 12px; }
.dv-head .ttl { display: grid; gap: 2px; }
.dv-head .now { font: 600 1.5rem/1 var(--display); font-variant-numeric: tabular-nums; }
.dv-badge { font: 600 0.72rem/1 var(--body); text-transform: uppercase; letter-spacing: 0.06em; border: 1px solid var(--line); border-radius: 4px; padding: 4px 7px; color: var(--muted); }
.dv-days { display: flex; flex-wrap: wrap; gap: 6px; }
.dv-days button, .dv-ctl button { font: 600 0.82rem var(--body); border: 1px solid var(--line); background: var(--sunk); color: var(--fg); border-radius: 5px; padding: 5px 10px; cursor: pointer; }
.dv-days button[aria-current="true"] { box-shadow: inset 0 -2px 0 var(--focus); }
.dv-scrub { padding: 8px 12px 10px; display: grid; gap: 4px; }
.dv-scrub svg { display: block; width: 100%; height: 34px; }
.dv-scrub input[type=range] { width: 100%; margin: 0; }
.dv-ctl { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 0.85rem; }
.dv-ctl .bm { color: var(--muted); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1 1 200px; }
.dv-main { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr); gap: 14px; align-items: start; }
@media (max-width: 980px) { .dv-main { grid-template-columns: minmax(0, 1fr); } }
.dv-plan { padding: 10px 12px; display: grid; gap: 10px; }
.dv-plan svg { width: 100%; height: auto; display: block; font-family: var(--body); }
.dv-plan .tile text { fill: var(--dv-tile-fg); font-size: 13px; }
.dv-plan .tile .id { font-weight: 600; }
.dv-plan .tile .t { font-family: var(--mono); font-size: 12px; }
.dv-plan .tile .cold { fill: #2b63c4; font-weight: 600; }
.dv-plan .tile .hot { fill: #c45a1c; font-weight: 600; }
.dv-legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 0.78rem; color: var(--muted); align-items: center; }
.dv-legend i { display: inline-block; width: 12px; height: 12px; border-radius: 2px; vertical-align: -2px; margin-right: 4px; border: 1px solid var(--line); }
.dv-legend .prop { border: 2px dashed var(--critical); background: none; }
.dv-plant { display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); gap: 8px; }
.dv-plant div { background: var(--sunk); border: 1px solid var(--line); border-radius: 5px; padding: 5px 8px; min-width: 0; }
.dv-plant b { display: block; font: 600 1rem/1.2 var(--display); font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dv-plant span { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.dv-plant .alarm b { color: var(--critical); }
.dv-tl { max-height: 70vh; overflow-y: auto; }
.dv-tl .head { position: sticky; top: 0; background: var(--panel); padding: 8px 12px; border-bottom: 1px solid var(--line); font-size: 0.8rem; color: var(--muted); z-index: 1; display: flex; justify-content: space-between; gap: 8px; }
.dv-it { display: grid; grid-template-columns: 4px 3.4em minmax(0, 1fr); gap: 2px 10px; padding: 7px 12px 7px 0; border-bottom: 1px solid var(--grid); }
.dv-it .bar { background: var(--c); border-radius: 0 2px 2px 0; grid-row: 1 / span 3; }
.dv-it .tm { font-family: var(--mono); font-size: 0.78rem; color: var(--muted); padding-top: 2px; }
.dv-it .what { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; min-width: 0; }
.dv-it .act { font-weight: 600; }
.dv-it .why { grid-column: 3; font-size: 0.85rem; min-width: 0; overflow-wrap: anywhere; }
.dv-it .sub { grid-column: 3; font-size: 0.76rem; color: var(--muted); min-width: 0; overflow-wrap: anywhere; }
.dv-it.sig .act { font-weight: 500; color: var(--muted); }
.dv-it.routine { opacity: 0.6; }
.dv-it.human { background: color-mix(in srgb, var(--approve) 9%, transparent); }
.dv-it.human.consent { background: color-mix(in srgb, var(--consent) 9%, transparent); }
.dv-it.flash { background: color-mix(in srgb, var(--focus) 12%, transparent); outline: 2px solid var(--focus); outline-offset: -2px; }
.dv-it .beat { font: 600 0.68rem/1 var(--body); text-transform: uppercase; letter-spacing: 0.05em; color: var(--approve); }
.dv-it button.lnk { font: inherit; background: none; border: 0; padding: 0; color: var(--focus); cursor: pointer; }
.dv-it.fold { cursor: pointer; }
.dv-spark { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; padding: 10px 12px; }
.dv-spark figure { margin: 0; display: grid; gap: 2px; }
.dv-spark figcaption { font-size: 0.75rem; color: var(--muted); display: flex; justify-content: space-between; gap: 8px; }
.dv-spark figcaption b { color: var(--fg); font-variant-numeric: tabular-nums; }
.dv-spark svg { width: 100%; height: 56px; display: block; }
.dv-empty { padding: 30px 16px; color: var(--muted); text-align: center; }
`;
    document.head.appendChild(s);
  }

  const hhmm = t => new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit" }).format(t);
  const lane = issue => (typeof SCEN !== "undefined" && SCEN[issue]) || { label: issue, c: "var(--dq)" };
  const ROUTINE = () => (cache.routine ??= new Set(DATA.routine || []));
  const isRoutine = e => !!e && (e.routine || ROUTINE().has(e.id));
  const short = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };

  // Everything OSAM saw or proposed on one day, in time order, from the episodes.
  function items(dayKey, D) {
    if (cache[dayKey]) return cache[dayKey];
    const t0 = D.t[0], t1 = D.t[D.t.length - 1] + STEP_MS;
    const out = [];
    for (const e of DATA.episodes || []) {
      const inDay = s => { const m = ms(s); return m >= t0 && m < t1; };
      e.signals.forEach((s, k) => {
        if (!inDay(s.time)) return;
        // One signal row per episode and day keeps detector noise out of the story.
        if (e.signals.slice(0, k).some(p => inDay(p.time))) return;
        out.push({ kind: "signal", t: ms(s.time), e, s });
      });
      for (const d of e.decisions) if (inDay(d.timestamp_utc)) out.push({ kind: "decision", t: ms(d.timestamp_utc), e, d });
    }
    const ids = new Set(out.filter(x => x.d).map(x => x.d.event_id));
    for (const b of D.bookmarks || []) {
      if (b.kind === "supervisor" || (b.kind === "followup" && !ids.has(b.event_id))) {
        out.push({ kind: b.kind, t: ms(b.t), b });
      }
    }
    out.sort((a, b) => a.t - b.t || (a.kind === "signal" ? -1 : 1));
    return (cache[dayKey] = out);
  }

  function indexAt(D, t) {
    let i = 0;
    while (i + 1 < D.t.length && D.t[i + 1] <= t) i++;
    return i;
  }

  function roomName(code, D) { return (D.states || [])[code] ?? "unknown"; }

  // Rooms OSAM proposed to block and has not released by time t: an overlay, never a state.
  function proposals(list, t) {
    const blocked = new Set(), recent = new Map(), open = new Map();
    for (const x of list) {
      if (x.t > t) break;
      if (x.kind === "decision") {
        for (const a of x.d.affected_assets || []) {
          if (x.d.action === "block_room") blocked.add(a);
          if (x.d.action === "release_room") blocked.delete(a);
          if (t - x.t <= 60 * 60 * 1000) recent.set(a, x);
        }
      }
      if (x.e && !isRoutine(x.e) && t - x.t <= 6 * 60 * 60 * 1000) for (const a of x.e.assets) open.set(a, x.e.issue);
    }
    return { blocked, recent, open };
  }

  function floorPlan(D, i, list) {
    const t = D.t[i];
    const meta = new Map((DATA.building?.rooms || []).map(r => [r.id, r]));
    const rooms = Object.keys(D.rooms).sort();
    const floorOf = r => String(meta.get(r)?.floor ?? r[1]);
    const floors = [...new Set(rooms.map(floorOf))].sort().reverse();
    const per = Math.max(...floors.map(f => rooms.filter(r => floorOf(r) === f).length));
    const W = 78, H = 58, G = 6, L = 26;
    const P = proposals(list, t);
    let svg = `<svg viewBox="0 0 ${L + per * (W + G)} ${floors.length * (H + G)}" role="img" aria-label="Floor plan at ${hhmm(t)}">`;
    floors.forEach((f, fi) => {
      const y = fi * (H + G);
      svg += `<text x="0" y="${y + H / 2 + 4}" font-size="12" font-weight="600" fill="currentColor">F${f}</text>`;
      rooms.filter(r => floorOf(r) === f).forEach((r, k) => {
        const x = L + k * (W + G), R = D.rooms[r];
        const st = roomName(R.state?.[i], D), sty = STATE_STYLE[st] || STATE_STYLE.unknown;
        const temp = R.temp?.[i], sp = R.sp?.[i], win = R.win?.[i];
        const occ = st === "occupied";
        const band = BAND();
        const tcls = temp == null || !band ? "" : occ && temp < band[0] ? "cold" : occ && temp > band[1] ? "hot" : "";
        const tip = [`${r}: ${sty.label}`, temp != null ? `${temp.toFixed(1)} degC` : "no reading",
          sp != null ? `setpoint ${sp.toFixed(1)}` : "", win ? "window open" : "", meta.get(r)?.orientation ? `faces ${meta.get(r).orientation}` : ""].filter(Boolean).join(", ");
        const rec = P.recent.get(r), issue = P.open.get(r);
        svg += `<g class="tile"><title>${esc(tip)}${rec ? esc(` | proposed ${hhmm(rec.t)}: ${rec.d.action} (simulated)`) : ""}</title>`;
        svg += `<rect x="${x}" y="${y}" width="${W}" height="${H}" rx="4" fill="${sty.fill}" stroke="var(--line)"/>`;
        if (P.blocked.has(r)) svg += `<rect x="${x + 2}" y="${y + 2}" width="${W - 4}" height="${H - 4}" rx="3" fill="none" stroke="var(--critical)" stroke-width="2.5" stroke-dasharray="5 3"/><text x="${x + 6}" y="${y + 52}" font-size="11" style="fill:var(--critical);font-weight:700">BLOCK</text>`;
        else if (rec) svg += `<rect x="${x + 1.5}" y="${y + 1.5}" width="${W - 3}" height="${H - 3}" rx="3" fill="none" stroke="${lane(rec.e.issue).c}" stroke-width="2.5" stroke-dasharray="5 3"/>`;
        svg += `<text class="id" x="${x + 6}" y="${y + 17}">${r}</text>`;
        if (issue) svg += `<circle cx="${x + W - 11}" cy="${y + 11}" r="8" fill="${lane(issue).c}"/><text x="${x + W - 11}" y="${y + 15}" text-anchor="middle" font-size="10" font-weight="700" style="fill:#fff">${MARK[issue] || "?"}</text>`;
        const show = cur.showTemp || tcls;
        if (temp != null && show) svg += `<text class="t ${tcls}" x="${x + 6}" y="${y + 36}">${temp.toFixed(1)}°C</text>`;
        else svg += `<text class="t" x="${x + 6}" y="${y + 36}" style="fill:var(--muted)">${sty.label.split(",")[0]}</text>`;
        if (win && !P.blocked.has(r)) svg += `<text class="t" x="${x + 6}" y="${y + 52}">window open</text>`;
        svg += `</g>`;
      });
    });
    svg += `</svg>`;
    const legend = Object.entries(STATE_STYLE).map(([, v]) => `<span><i style="background:${v.fill}"></i>${v.label}</span>`).join("")
      + `<span><i class="prop"></i>OSAM proposal (simulated, not applied in the replay)</span>`
      + `<span>letter = incident active in the last 6 h${BAND() ? `; blue / orange = occupied room below / above ${BAND()[0]}-${BAND()[1]} degC` : ""}</span>`;
    return `${svg}<div class="dv-legend">${legend}</div>`;
  }

  function plant(D, i) {
    const p = D.plant || {}, s = D.shift || {};
    const v = (a, f) => (a && a[i] != null ? f(a[i]) : "n/a");
    const cop = p.cop?.[i];
    const cells = [
      ["HP1", v(p.hp1_status, x => x), p.hp1_status?.[i] && p.hp1_status[i] !== "running"],
      ["HP1 COP ratio", cop != null ? cop.toFixed(2) : "n/a", cop != null && COP_LOW() != null && cop < COP_LOW()],
      ["AHU1 filter dp", v(p.ahu_dp, x => `${x.toFixed(0)} Pa`), false],
      ["BAT1 SOC (plan)", v(p.soc, x => `${x.toFixed(0)} kWh`) + (p.soc_plan?.[i] != null ? ` (${p.soc_plan[i].toFixed(0)})` : ""), false],
      ["Main water flow", v(p.flow, x => `${x.toFixed(0)} L/h`), false],
      ["Cleaners on shift", v(s.cleaner, x => x.length ? x.join(", ") : "none"), false],
      ["Robots", v(s.robot, x => x.length ? x.join(", ") : "none"), false],
      ["Technician", v(s.technician, x => x.length ? x.join(", ") : "none"), false],
    ];
    return `<div class="dv-plant">${cells.map(([k, val, bad]) => `<div class="${bad ? "alarm" : ""}"><b title="${esc(val)}">${esc(val)}</b><span>${esc(k)}</span></div>`).join("")}</div>`;
  }

  function itemHtml(x, flash) {
    const L = lane(x.e?.issue);
    if (x.kind === "signal") {
      return `<div class="dv-it sig ${isRoutine(x.e) ? "routine" : ""}" style="--c:${L.c}"><span class="bar"></span><span class="tm">${hhmm(x.t)}</span>
        <div class="what"><span class="act">saw: ${esc(x.s.kind)}</span><span class="muted" style="font-size:.78rem">${esc(L.label)}</span></div>
        <div class="why">${esc(short(x.s.detail, 220))}</div>
        <div class="sub mono" title="${esc(x.s.evidence.join("\n"))}">evidence: ${esc(short(x.s.evidence.join("; "), 140))}</div></div>`;
    }
    if (x.kind === "decision") {
      const d = x.d, human = d.autonomy && d.autonomy !== "autonomous";
      const out = DATA.outcomes?.[d.event_id];
      const next = out?.outcome ? `next: ${hhmm(ms(out.outcome.t))} ${esc(out.outcome.kind)}: ${esc(short(out.outcome.detail, 120))}` : "";
      return `<div class="dv-it ${human ? "human" : ""} ${d.autonomy === "approval_and_consent" ? "consent" : ""} ${isRoutine(x.e) ? "routine" : ""} ${flash ? "flash" : ""}" style="--c:${L.c}" data-ev="${esc(d.event_id)}">
        <span class="bar"></span><span class="tm">${hhmm(x.t)}</span>
        <div class="what"><span class="act">${esc(d.action.replaceAll("_", " "))}</span>
          <span class="pill p-${esc(d.priority)}">${esc(d.priority)}</span>
          <span class="pill a-${esc(d.autonomy)}">${esc(AUTONOMY[d.autonomy] ?? d.autonomy)}</span>
          ${human ? `<span class="beat">OSAM did not act alone</span>` : ""}</div>
        <div class="why">${esc(short(d.reason, 260))}</div>
        <div class="sub">${esc((d.affected_assets || []).join(", "))} &middot; rule: ${esc(short(d.rule, 120))}${d.verification_plan ? ` &middot; verify: ${esc(short(d.verification_plan, 120))}` : ""}
          ${next ? `<br>${next}` : ""} &middot; <button class="lnk" type="button" data-trail="${esc(d.event_id)}">trail</button></div></div>`;
    }
    const b = x.b;
    return `<div class="dv-it ${flash ? "flash" : ""}" style="--c:var(--focus)"><span class="bar"></span><span class="tm">${hhmm(x.t)}</span>
      <div class="what"><span class="act">${esc(b.kind)}</span></div><div class="why">${esc(b.label)}</div></div>`;
  }

  function timeline(list, t, flashId) {
    const shown = list.filter(x => x.t <= t);
    const rows = [];
    let fold = null;
    for (const x of shown) {
      const routine = isRoutine(x.e);
      if (routine) {
        const h = Math.floor(x.t / 3600000);
        if (!fold || fold.h !== h) { fold = { h, list: [] }; rows.push(fold); }
        fold.list.push(x);
      } else { fold = null; rows.push(x); }
    }
    const html = rows.map(r => {
      if (!r.list) return itemHtml(r, r.d && r.d.event_id === flashId);
      const key = String(r.h);
      if (cur.open.has(key)) return r.list.map(x => itemHtml(x, false)).join("");
      const n = r.list.filter(x => x.kind === "decision").length;
      const kinds = [...new Set(r.list.map(x => lane(x.e.issue).label))].join(", ");
      return `<div class="dv-it routine fold" data-fold="${key}" style="--c:var(--line)"><span class="bar"></span><span class="tm">${hhmm(r.list[0].t)}</span>
        <div class="what"><span class="act">${n} routine decision${n === 1 ? "" : "s"}</span><span class="muted" style="font-size:.78rem">${esc(kinds)} &middot; click to open</span></div></div>`;
    }).join("");
    const nd = shown.filter(x => x.kind === "decision"), human = nd.filter(x => x.d.autonomy !== "autonomous").length;
    return `<div class="head"><span>Up to ${hhmm(t + 1)}: ${nd.length} decisions, ${human} with a person</span><span>newest last</span></div>
      ${html || `<div class="dv-empty">Nothing yet today. Step forward or jump to the first bookmark.</div>`}`;
  }

  function spark(key, D, t) {
    const S = DATA.series;
    if (!S || !S.t || !S[key]) return "";
    const t0 = D.t[0], t1 = D.t[D.t.length - 1] + STEP_MS;
    const pts = [];
    S.t.forEach((x, k) => { if (x >= t0 && x <= t1 && S[key][k] != null) pts.push([x, S[key][k]]); });
    if (pts.length < 2) return "";
    const W = 300, H = 56, pad = 4;
    const vs = pts.map(p => p[1]);
    let lo = Math.min(...vs), hi = Math.max(...vs);
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    const X = x => pad + (x - t0) / (t1 - t0) * (W - 2 * pad), Y = v => H - pad - (v - lo) / (hi - lo) * (H - 2 * pad);
    const path = pts.map((p, k) => `${k ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
    const past = pts.filter(p => p[0] <= t), last = past[past.length - 1];
    const meta = SERIES[key] || { label: key, unit: "" };
    const fmt = v => (Math.abs(v) < 10 ? v.toFixed(2) : v.toFixed(0)) + meta.unit;
    return `<figure><figcaption><span>${esc(meta.label)}</span><span>${last ? `<b>${fmt(last[1])}</b> at ${hhmm(last[0])}` : ""} &middot; ${fmt(lo)} to ${fmt(hi)}</span></figcaption>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(meta.label)} over the day">
      <path d="${path}" fill="none" stroke="var(--muted)" stroke-width="1.2" vector-effect="non-scaling-stroke"/>
      ${past.length > 1 ? `<path d="${past.map((p, k) => `${k ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("")}" fill="none" stroke="var(--fg)" stroke-width="2" vector-effect="non-scaling-stroke"/>` : ""}
      <line x1="${X(Math.min(t, t1))}" x2="${X(Math.min(t, t1))}" y1="0" y2="${H}" stroke="var(--focus)" stroke-width="1" vector-effect="non-scaling-stroke"/></svg></figure>`;
  }

  // Recorded BAT1 SOC (solid) against OSAM's plan (dashed), on the day's 15-min grid.
  function socSpark(D, t) {
    const rec = D.plant?.soc, plan = D.plant?.soc_plan;
    if (!rec || !plan || !plan.some(v => v != null)) return "";
    const W = 300, H = 56, pad = 4, n = D.t.length;
    const vals = [...rec, ...plan].filter(v => v != null);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    const X = k => pad + k / (n - 1) * (W - 2 * pad), Y = v => H - pad - (v - lo) / (hi - lo) * (H - 2 * pad);
    const line = (a, upto) => {
      let d = "", pen = false;
      a.forEach((v, k) => { if (k > upto || v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${X(k).toFixed(1)},${Y(v).toFixed(1)}`; pen = true; });
      return d;
    };
    const ci = indexAt(D, t);
    return `<figure><figcaption><span>BAT1 SOC: recorded vs OSAM plan (dashed, simulated)</span><span>${rec[ci] != null ? `<b>${rec[ci].toFixed(0)} kWh</b>` : ""}${plan[ci] != null ? ` plan ${plan[ci].toFixed(0)}` : ""}</span></figcaption>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="BAT1 state of charge, recorded and planned">
      <path d="${line(plan, n)}" fill="none" stroke="var(--focus)" stroke-width="1.6" stroke-dasharray="4 3" vector-effect="non-scaling-stroke"/>
      <path d="${line(rec, ci)}" fill="none" stroke="var(--fg)" stroke-width="2" vector-effect="non-scaling-stroke"/>
      <line x1="${X(ci)}" x2="${X(ci)}" y1="0" y2="${H}" stroke="var(--focus)" stroke-width="1" vector-effect="non-scaling-stroke"/></svg></figure>`;
  }

  function sparks(D, list, t) {
    // Lanes ranked by the highest priority OSAM gave them that day: the leak beats a cold room.
    const score = {};
    for (const x of list) if (x.d && !isRoutine(x.e)) score[x.e.issue] = Math.max(score[x.e.issue] ?? -1, RANK[x.d.priority] ?? 0);
    const best = k => Math.max(-1, ...SERIES[k].lanes.map(l => score[l] ?? -1));
    const soc = socSpark(D, t);
    const keys = Object.keys(SERIES).filter(k => DATA.series?.[k] && !(soc && k === "soc_kwh"));
    const ranked = [...keys.filter(k => best(k) >= 0).sort((a, b) => best(b) - best(a)), ...keys];
    const pick = [...new Set(ranked)].slice(0, soc ? 2 : 3);
    const out = pick.map(k => spark(k, D, t)).join("") + soc;
    return out || `<div class="dv-empty">No hourly series in this page.</div>`;
  }

  function scrubber(D, i) {
    const t0 = D.t[0], span = D.t.length * STEP_MS, W = 1000;
    const bms = D.bookmarks || [];
    const col = { signal: "var(--muted)", decision: "var(--fg)", followup: "var(--focus)", supervisor: "var(--consent)" };
    const ticks = bms.map((b, k) => {
      const x = ((ms(b.t) - t0) / span) * W;
      return `<g data-bm="${k}" style="cursor:pointer"><title>${esc(hhmm(ms(b.t)))} ${esc(b.label)}</title><rect x="${x - 4}" y="0" width="8" height="34" fill="transparent"/><line x1="${x}" x2="${x}" y1="${b.kind === "decision" ? 4 : 12}" y2="30" stroke="${col[b.kind] || "var(--fg)"}" stroke-width="2"/></g>`;
    }).join("");
    const hours = [0, 6, 12, 18].map(h => { const x = (h * 4 / D.t.length) * W; return `<text x="${x + 2}" y="33" font-size="10" fill="var(--muted)">${String(h).padStart(2, "0")}:00</text>`; }).join("");
    const cx = (i / D.t.length) * W;
    return `<svg viewBox="0 0 ${W} 34" preserveAspectRatio="none">${hours}${ticks}<line x1="${cx}" x2="${cx}" y1="0" y2="34" stroke="var(--focus)" stroke-width="3"/></svg>
      <input type="range" min="0" max="${D.t.length - 1}" value="${i}" aria-label="Time of day">`;
  }

  // A day without a 15-min extract (picked on the year strip): the timeline, bookmarks and the
  // hourly sparklines still work; the room tiles and plant strip say they are not extracted.
  function synth(day) {
    if (cache["synth:" + day]) return cache["synth:" + day];
    const noonUtc = Date.parse(`${day}T12:00:00Z`);
    const localNoon = +new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "numeric", hourCycle: "h23" }).format(noonUtc);
    const t0 = Date.parse(`${day}T00:00:00Z`) - (localNoon - 12) * 3600000;
    const t = Array.from({ length: 96 }, (_, k) => t0 + k * STEP_MS);
    const D = { t, rooms: {}, states: [], plant: {}, shift: {}, bookmarks: [], synthetic: true };
    for (const x of items("synth-items:" + day, D)) {
      if (x.kind === "decision" && !isRoutine(x.e)) {
        D.bookmarks.push({ t: x.d.timestamp_utc, label: `${x.d.action.replaceAll("_", " ")}: ${(x.d.affected_assets || []).slice(0, 2).join(", ")}`, event_id: x.d.event_id, kind: "decision" });
      }
    }
    return (cache["synth:" + day] = D);
  }

  function stepOfBookmark(D, step) {
    const b = (D.bookmarks || [])[step];
    return b ? { i: indexAt(D, ms(b.t)), b } : null;
  }

  function stop() { if (cur.timer) { clearInterval(cur.timer); cur.timer = null; } }

  function draw(el, D, dayKey, flashId) {
    const list = items(dayKey, D);
    const t = D.t[cur.i] + STEP_MS - 1;
    const bms = D.bookmarks || [];
    const nextBm = jump(D, 1);
    const days = Object.keys(DATA.days || {}).sort();
    el.innerHTML = `<div class="dv">
      <section class="panel dv-head">
        <div class="ttl"><h2>${esc(fmtDate.format(D.t[0]))}</h2><span class="muted">PLATO-40 replay, 15-min steps. Tiles are recorded data; OSAM's proposals are dashed outlines (simulated).</span></div>
        <div class="now" aria-live="polite">${hhmm(D.t[cur.i])}</div>
        <div class="dv-days">${(D.synthetic ? [...days, dayKey] : days).map(k => `<button type="button" data-day="${k}" aria-current="${k === dayKey}">${esc(fmtDate.format((DATA.days[k] || D).t[0]))}</button>`).join("")}</div>
      </section>
      <section class="panel dv-scrub" aria-label="Replay controls">
        ${scrubber(D, cur.i)}
        <div class="dv-ctl">
          <button type="button" data-act="prev" title="previous bookmark (P)">&#9664; bookmark</button>
          <button type="button" data-act="play" title="play x16 (space)">${cur.timer ? "pause" : "play x16"}</button>
          <button type="button" data-act="next" title="next bookmark (N)">bookmark &#9654;</button>
          <label class="toggle"><input type="checkbox" data-act="temp" ${cur.showTemp ? "checked" : ""}> temperatures on all tiles</label>
          <span class="bm">${nextBm >= 0 ? `next: ${esc(hhmm(ms(bms[nextBm].t)))} ${esc(bms[nextBm].label)}` : "no more bookmarks today"}</span>
        </div>
      </section>
      <div class="dv-main">
        <section class="panel dv-plan" aria-label="Building">${D.synthetic
          ? `<div class="dv-empty">Room tiles and the plant strip are extracted only for the replay days of this page: ${days.map(k => esc(fmtDate.format(DATA.days[k].t[0]))).join(", ")}. The timeline and the hourly series below are complete for this day.</div>`
          : floorPlan(D, cur.i, list) + plant(D, cur.i)}</section>
        <section class="panel dv-tl" aria-label="What OSAM saw and proposed">${timeline(list, t, flashId)}</section>
      </div>
      <section class="panel dv-spark" aria-label="Day series">${sparks(D, list, t)}</section>
    </div>`;
    wire(el, D, dayKey);
    const tl = el.querySelector(".dv-tl");
    if (tl) {
      const f = tl.querySelector(".flash");
      if (f) f.scrollIntoView({ block: "nearest" });
      else tl.scrollTop = tl.scrollHeight;
    }
  }

  function jump(D, dir) {
    const bms = D.bookmarks || [];
    if (cur.step != null) {
      const k = cur.step + dir;
      return k >= 0 && k < bms.length ? k : -1;
    }
    const now = D.t[cur.i] + STEP_MS - 1;
    if (dir > 0) return bms.findIndex(b => ms(b.t) > now);
    for (let k = bms.length - 1; k >= 0; k--) if (ms(bms[k].t) < D.t[cur.i]) return k;
    return -1;
  }

  function wire(el, D, dayKey) {
    const redraw = () => draw(el, D, dayKey, null);
    el.querySelector("input[type=range]")?.addEventListener("input", ev => { stop(); cur.i = +ev.target.value; cur.step = null; redraw(); });
    el.querySelectorAll("[data-day]").forEach(b => b.addEventListener("click", () => { stop(); go({ view: "day", day: b.dataset.day }); }));
    el.querySelectorAll("[data-bm]").forEach(g => g.addEventListener("click", () => { stop(); go({ view: "day", day: dayKey, step: +g.dataset.bm }); }));
    el.querySelectorAll("[data-fold]").forEach(f => f.addEventListener("click", () => { cur.open.add(f.dataset.fold); redraw(); }));
    el.querySelectorAll("[data-trail]").forEach(b => b.addEventListener("click", () => go({ view: "trail", ev: b.dataset.trail })));
    el.querySelector("[data-act=temp]")?.addEventListener("change", ev => { cur.showTemp = ev.target.checked; redraw(); });
    el.querySelectorAll("[data-act]").forEach(b => {
      if (b.tagName !== "BUTTON") return;
      b.addEventListener("click", () => {
        const a = b.dataset.act;
        if (a === "play") {
          if (cur.timer) { stop(); redraw(); return; }
          if (cur.i >= D.t.length - 1) cur.i = 0;
          cur.timer = setInterval(() => {
            if (cur.i >= D.t.length - 1 || cur.el !== el) { stop(); redraw(); return; }
            cur.i++; cur.step = null; redraw();
          }, PLAY_MS);
          redraw();
        } else {
          stop();
          const k = jump(D, a === "next" ? 1 : -1);
          if (k >= 0) go({ view: "day", day: dayKey, step: k });
        }
      });
    });
  }

  // N / P / space while the Day view is shown; the shell's arrow keys set state.step.
  document.addEventListener("keydown", ev => {
    if (!cur.el || !cur.el.isConnected || !cur.day || ev.target.closest?.("input,select,textarea")) return;
    const D = DATA.days?.[cur.day] || synth(cur.day);
    if (!D) return;
    const k = ev.key.toLowerCase();
    if (k === "n" || k === "p") {
      stop();
      const s = jump(D, k === "n" ? 1 : -1);
      if (s >= 0) go({ view: "day", day: cur.day, step: s });
      ev.preventDefault();
    } else if (k === " ") {
      cur.el.querySelector("[data-act=play]")?.click();
      ev.preventDefault();
    }
  });

  VIEWS.day = {
    render(el, state) {
      css();
      const days = Object.keys(DATA.days || {}).sort();
      if (!days.length && !state.day) {
        el.innerHTML = `<div class="panel dv-empty">This page has no day extract. Write it with plato40 ui ... --data &lt;dir&gt; --days &lt;date&gt;.</div>`;
        return;
      }
      const dayKey = state.day || days[0];
      const D = DATA.days[dayKey] || synth(dayKey);
      if (cur.day !== dayKey) { stop(); cur = { ...cur, day: dayKey, i: 0, open: new Set() }; }
      cur.el = el;
      let flash = null;
      if (state.step != null && state.step !== "") {
        const hit = stepOfBookmark(D, +state.step);
        if (hit) { stop(); cur.i = hit.i; cur.step = +state.step; flash = hit.b.event_id; }
      }
      draw(el, D, dayKey, flash);
    },
  };
})();
