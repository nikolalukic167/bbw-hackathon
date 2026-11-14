/* Year view: the jury's ten-second read (owner: idea man).
   Five figures in one band, a thin secondary row, the year strip (days x lanes in lane colours;
   amber tick = a manager approval that day, red dot = a guest-facing miss that day; click a day to
   open it), the replay-day cards and the Limits panel. Every number comes from DATA.kpi, DATA.misses and DATA.year, computed by
   plato40.kpi when the page is written; this file types no figure of its own. */
VIEWS.year = (() => {
  const CSS = `
  .yr-band { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); background: var(--panel); border: 1px solid var(--line); border-radius: 4px; }
  .yr-band > div { padding: 16px 20px 18px; display: grid; gap: 6px; align-content: start; border-right: 1px solid var(--line); min-width: 0; }
  .yr-band > div:last-child { border-right: 0; }
  .yr-band b { font: 700 2.6rem/1 var(--display); font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
  .yr-band b small { font-size: 0.42em; color: var(--muted); font-weight: 400; letter-spacing: 0; }
  .yr-band .cap { font-size: 0.85rem; font-weight: 600; color: var(--fg); }
  .yr-band .sub { font-size: 0.8rem; color: var(--muted); }
  .yr-band .ok b { color: var(--auto); }
  .yr-band .alarm { box-shadow: inset 0 3px 0 var(--miss); }
  .yr-band .alarm b { color: var(--miss); }
  .yr-row2 { display: flex; flex-wrap: wrap; gap: 6px 22px; font-size: 0.85rem; color: var(--muted); padding: 8px 20px; }
  .yr-row2 b { color: var(--fg); font-weight: 600; font-variant-numeric: tabular-nums; }
  .yr-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 360px); gap: 14px; align-items: start; }
  .yr-col { display: grid; gap: 14px; min-width: 0; }
  @media (max-width: 980px) { .yr-grid { grid-template-columns: minmax(0, 1fr); } }
  .yr-strip { padding: 14px 16px 6px; overflow-x: auto; }
  .yr-strip svg { display: block; width: 100%; min-width: 760px; height: auto; }
  .yr-strip text { font: 11px var(--mono); fill: var(--muted); }
  .yr-strip .lab { font: 500 13px var(--body); fill: var(--fg); }
  .yr-strip rect.day { cursor: pointer; }
  .yr-strip rect.day:hover { fill: var(--focus); fill-opacity: 0.1; }
  .yr-legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 0.78rem; color: var(--muted); padding: 4px 16px 12px; }
  .yr-legend i { display: inline-block; width: 10px; height: 10px; margin-right: 5px; vertical-align: -1px; border-radius: 2px; }
  .yr-days { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
  .yr-day { font: inherit; color: inherit; text-align: left; cursor: pointer; padding: 14px 16px; display: grid; gap: 8px; align-content: start; border-top: 3px solid var(--focus); }
  .yr-day:hover { background: var(--hover); }
  .yr-day .cap { font-size: 0.78rem; color: var(--muted); }
  .yr-day b { font: 600 1.15rem/1.2 var(--display); }
  .yr-day .lanes { display: flex; flex-wrap: wrap; gap: 5px; }
  .yr-day .chip { cursor: inherit; background: var(--sunk); }
  .yr-day .sub { font-size: 0.8rem; color: var(--focus); font-weight: 500; }
  .yr-limits { padding: 16px 18px; display: grid; gap: 10px; }
  .yr-limits h3 { color: var(--fg); font-size: 0.95rem; }
  .yr-limits ul { margin: 0; padding-left: 18px; display: grid; gap: 7px; font-size: 0.88rem; }
  .yr-limits li::marker { color: var(--line-strong); }
  .yr-limits li b { font-variant-numeric: tabular-nums; }
  .yr-limits .gen { color: var(--muted); border-top: 1px solid var(--line); padding-top: 10px; }`;

  const n = x => (x == null ? "n/a" : fmtN(x));
  const pct = (a, b) => (b ? `${Math.round((100 * a) / b)} %` : "n/a");
  const pair = p => (p ? `<b>${n(p[0])}</b> of ${n(p[1])}` : "n/a");
  // A miss line only when there was something to miss: no "0 of 0".
  const li = (text, p) => (p && p[1] ? `<li>${text}: ${pair(p)}.</li>` : "");
  const EXTRA = { comfort_hot: "too-hot replies", routine: "routine" };
  // Strip rows: the scenario code and one short word, so no label is clipped.
  const WORD = { water_leak: "leaks", room_turnaround: "turnaround", occupancy_surge: "busy days",
    heating_comfort: "heating", ventilation_maintenance: "filters", logistics: "logistics",
    energy_optimization: "energy", guest_service: "guest desk", data_quality: "data" };
  const rowName = k => (SCEN[k] && WORD[k] ? `${SCEN[k].short} ${WORD[k]}` : label(k));
  const label = k => (SCEN[k] ? SCEN[k].label : EXTRA[k] || k.replace(/_/g, " "));
  const words = obj => Object.entries(obj || {}).map(([k, v]) => `${n(v)} ${k.replace(/_/g, " ")}`).join(", ") || "none";

  function tiles(k) {
    const req = k.requests, dec = k.decisions, ap = k.approvals, inc = k.incidents || {};
    const auto = (dec.by_autonomy || {}).autonomous || 0;
    const top = Object.entries(inc.incidents || {}).sort((a, b) => b[1] - a[1]).slice(0, 3)
      .map(([l, v]) => `${esc(label(l))} ${n(v)}`).join(", ");
    const perWeek = ap.manager_per_week == null ? "n/a" : ap.manager_per_week.toFixed(1);
    const viol = k.violations || {};
    const rerun = k.rerun;
    const rerunText = rerun == null ? "not rerun" : rerun.identical ? "rerun byte-identical" : "rerun differs";
    const bad = (viol.n ?? 0) > 0 || (rerun && !rerun.identical);
    return `<div class="yr-band">
      <div><span class="cap">Guest requests answered before due</span>
        <b>${n(req.on_time)} <small>of ${n(req.total)}</small></b><span class="sub">${n(req.answered)} answered in all</span></div>
      <div><span class="cap">Decisions OSAM took alone</span>
        <b>${pct(auto, dec.total)}</b><span class="sub">${n(auto)} of ${n(dec.total)} decisions autonomous</span></div>
      <div><span class="cap">Manager approvals per week</span>
        <b>${perWeek}</b><span class="sub">${n(ap.manager)} in the run, plus ${n(ap.guest_desk)} guest-consent requests from the guest desk</span></div>
      <div><span class="cap">Incidents against routine work</span>
        <b>${n(inc.incidents_total)} <small>incidents</small></b><span class="sub">${n(inc.routine_total)} routine replies, pre-heats and plans. Most: ${top || "none"}</span></div>
      <div class="${bad ? "alarm" : "ok"}"><span class="cap">Contract, known-at and policy</span>
        <b>${viol.checked === false ? "n/a" : n(viol.n)} <small>violations</small></b><span class="sub">${esc(rerunText)}</span></div>
    </div>`;
  }

  function row2(k) {
    const sv = k.supervisor;
    const fu = Object.values(k.followups || {}).reduce((s, v) => s + v, 0);
    return `<div class="panel yr-row2">
      ${fu ? `<span>Follow-ups fired <b>${n(fu)}</b> (${esc(words(k.followups))})</span>` : `<span>No follow-up needed</span>`}
      ${sv ? `<span>Supervisor <b>${n(sv.reviews)}</b> reviews, <b>${n((sv.added || []).length)}</b> added${sv.rejected ? `, <b>${n(sv.rejected)}</b> rejected by the checks` : ""}</span>`
           : `<span>Supervisor not part of this run</span>`}
    </div>`;
  }

  function strip(y) {
    if (!y || !y.days || !y.days.length) return `<div class="panel empty">No year data: write the page with --data.</div>`;
    const lanes = ORDER.filter(l => (y.lanes[l] || []).some(v => v > 0));
    const rows = [...lanes.map(l => ({ key: l, name: rowName(l), vals: y.lanes[l] })),
      { key: "routine", name: "routine", vals: y.routine || [] }];
    // A fixed drawing width, so a short run (quickstart) and a year read at the same scale.
    const D = y.days.length, L = 150, W = 1000, cw = (W - L - 4) / D, ch = 19, top = 26, H = top + rows.length * ch + 16;
    const max = Math.max(1, ...lanes.flatMap(l => y.lanes[l]));
    const shade = v => (v ? 0.45 + 0.55 * Math.log1p(v) / Math.log1p(max) : 0);
    let out = "";
    let lastMonth = "";
    y.days.forEach((d, i) => {
      const m = d.slice(0, 7);
      if (m !== lastMonth) {
        lastMonth = m;
        const x = L + i * cw;
        out += `<line x1="${x}" x2="${x}" y1="${top - 4}" y2="${H - 14}" stroke="var(--grid)"/>`;
        out += `<text x="${x + 2}" y="${top - 8}">${esc(new Intl.DateTimeFormat("en-GB", { month: "short", timeZone: "UTC" }).format(Date.parse(d + "T12:00Z")))}</text>`;
      }
    });
    rows.forEach((r, j) => {
      const yy = top + j * ch;
      out += `<text class="lab" x="0" y="${yy + 12}">${esc(r.name.length > 18 ? r.name.slice(0, 17) + "." : r.name)}</text>`;
      r.vals.forEach((v, i) => {
        if (!v) return;
        const a = r.key === "routine" ? 0.1 + 0.25 * Math.min(1, v / 20) : shade(v);
        const fill = r.key === "routine" ? "var(--muted)" : SCEN[r.key]?.c || "var(--fg)";
        out += `<rect x="${(L + i * cw).toFixed(2)}" y="${yy + 2}" width="${Math.max(0.6, cw - 0.5).toFixed(2)}" height="${ch - 5}" rx="0.6" fill="${fill}" fill-opacity="${a.toFixed(3)}"/>`;
      });
    });
    y.days.forEach((d, i) => {
      const x = L + i * cw;
      if (y.approval && y.approval[i]) out += `<rect x="${x}" y="${top - 5}" width="${Math.max(1.2, cw - 0.5)}" height="4" fill="var(--approve)"/>`;
      if (y.miss && y.miss[i]) out += `<circle cx="${x + cw / 2}" cy="${H - 9}" r="2.2" fill="var(--miss)"/>`;
      const tip = `${d}: ` + [...lanes.map(l => [label(l), y.lanes[l][i]]), ["routine", (y.routine || [])[i]]]
        .filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(", ");
      out += `<rect class="day" data-day="${esc(d)}" x="${x}" y="${top}" width="${cw}" height="${rows.length * ch}" fill="transparent"><title>${esc(tip || d)}</title></rect>`;
    });
    return `<section class="panel"><div class="yr-strip">
        <svg viewBox="0 0 ${W} ${H}" style="max-width:${W * 1.4}px" role="img" aria-label="Decisions per day and lane over the run">${out}</svg></div>
      <div class="yr-legend"><span><i style="background:linear-gradient(90deg,var(--ln-water_leak),var(--ln-room_turnaround),var(--ln-heating_comfort),var(--ln-logistics));width:22px"></i>incident decisions, coloured by lane (stronger = more)</span>
        <span><i style="background:var(--approve)"></i>a manager approval that day</span>
        <span><i style="background:var(--miss);border-radius:50%"></i>a guest-facing miss that day</span>
        <span>Click a day to replay it.</span></div></section>`;
  }

  // The replay days of this page, with the lanes that fired on each (counts from DATA.year).
  function replayDays(y) {
    const keys = Object.keys(DATA.days || {}).sort();
    if (!keys.length || !y || !y.days) return "";
    const fmt = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
    return `<section class="yr-days" aria-label="Replay days">${keys.map(d => {
      const i = y.days.indexOf(d);
      const hits = i < 0 ? [] : ORDER.filter(l => (y.lanes[l] || [])[i] > 0).map(l => [l, y.lanes[l][i]]);
      const steps = (DATA.days[d].bookmarks || []).length;
      return `<button type="button" class="panel yr-day" data-day="${esc(d)}">
        <span class="cap">Replay</span><b>${esc(fmt.format(Date.parse(d + "T12:00Z")))}</b>
        <span class="lanes">${hits.map(([l, v]) => `<span class="chip" style="--c:${SCEN[l].c}"><i></i>${esc(rowName(l))}<small>${n(v)}</small></span>`).join("") || `<span class="muted">routine only</span>`}</span>
        <span class="sub">${n(steps)} steps &middot; open the replay</span></button>`;
    }).join("")}</section>`;
  }

  function limits(m, k) {
    if (!m) return `<section class="panel yr-limits"><h3>Limits and verification</h3><p class="muted">Write the page with --data to see what OSAM missed.</p></section>`;
    const sv = k && k.supervisor;
    const why = sv && sv.rejected_why && Object.keys(sv.rejected_why).length ? ` (${esc(words(sv.rejected_why))})` : "";
    const bat = m.battery;
    const svr = m.supervisor_rejected;
    const svLine = !svr || !svr[1] ? ""
      : svr[0] ? `<li>Supervisor answers rejected by the checks: ${pair(svr)}${why}.</li>`
      : `<li>Supervisor answers: <b>${n(svr[1])}</b> answered, all passed the checks.</li>`;
    const few = (m.hp_incidents || 0) + (m.leak_incidents || 0);
    return `<section class="panel yr-limits"><h3>Limits and verification</h3>
      <ul>
        ${li("Waiting guests OSAM had not acted on that morning", m.waits)}
        ${li("Waiting guests told a ready time after their arrival", m.waits_promised_late)}
        ${li("Pre-heated rooms still below the comfort band at check-in", m.preheat)}
        ${li("Extra waste or vending orders that found no shortage", m.orders)}
        ${svLine}
        ${few ? `<li>The heat pump and leak rules rest on few cases: <b>${n(m.hp_incidents)}</b> heat pump and <b>${n(m.leak_incidents)}</b> leak incidents.</li>` : ""}
        ${bat ? `<li>Battery plan on <b>${n(bat.days)}</b> days, median saving EUR <b>${bat.saving_eur_median.toFixed(2)}</b> a day against no battery: simulated.</li>` : ""}
      </ul>
      <ul class="gen">
        <li>The data is a replay: no decision changes later readings, so every effect is simulated and outcomes are read from what people logged.</li>
        <li>The data has no costs beyond tariff prices: OSAM claims no savings except the simulated battery figure.</li>
        <li>Guest messages are proposals; delivery and replies are not in the data.</li>
      </ul></section>`;
  }

  return {
    render(el, st) {
      if (!DATA.kpi) {
        el.innerHTML = `<div class="panel empty">The Year view needs the dataset: write the page with plato40 ui ... --data &lt;dir&gt;.</div>`;
        return;
      }
      el.innerHTML = `<style>${CSS}</style>${tiles(DATA.kpi)}${row2(DATA.kpi)}
        <div class="yr-grid"><div class="yr-col">${strip(DATA.year)}${replayDays(DATA.year)}</div>${limits(DATA.misses, DATA.kpi)}</div>`;
      el.onclick = ev => {
        if (state.view !== "year") return;
        const r = ev.target.closest("[data-day]");
        if (r) go({ view: "day", day: r.dataset.day, step: null });
      };
    },
  };
})();
