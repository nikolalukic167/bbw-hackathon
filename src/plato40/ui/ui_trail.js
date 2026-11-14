/* Trail view: one decision's full chain (owner: idea man).
   Backwards through its dependencies to the evidence, then the decision, the policy rule that
   set its autonomy, the verification plan, and forwards: the decisions that depend on it
   (follow-ups, supervisor additions) and the first outcome event in the data
   (DATA.outcomes, hindsight read after the fact, never by the rules). URL key: ev. */
VIEWS.trail = (() => {
  const CSS = `
  .tr-wrap { display: grid; grid-template-columns: minmax(240px, 320px) minmax(0, 1fr); gap: 12px; align-items: start; }
  @media (max-width: 900px) { .tr-wrap { grid-template-columns: minmax(0, 1fr); } }
  .tr-pick { padding: 10px 12px; display: grid; gap: 8px; max-height: 80vh; overflow-y: auto; }
  .tr-pick button { text-align: left; font: inherit; background: none; border: 0; border-bottom: 1px solid var(--grid); padding: 6px 2px; cursor: pointer; color: inherit; display: grid; gap: 2px; }
  .tr-pick button[aria-current="true"] { box-shadow: inset 3px 0 0 var(--focus); padding-left: 8px; background: var(--sunk); }
  .tr-pick small { color: var(--muted); }
  .tr-chain { padding: 16px; display: grid; gap: 0; min-width: 0; }
  .tr-step { display: grid; grid-template-columns: 9.5em minmax(0, 1fr); gap: 4px 14px; padding: 10px 0; border-left: 2px solid var(--line); margin-left: 6px; padding-left: 14px; position: relative; }
  .tr-step::before { content: ""; position: absolute; left: -6px; top: 15px; width: 10px; height: 10px; border-radius: 50%; background: var(--panel); border: 2px solid var(--line); }
  .tr-step.here::before { background: var(--focus); border-color: var(--focus); }
  .tr-step.next::before { border-color: var(--auto); }
  .tr-step.none::before { border-style: dashed; }
  .tr-step > h3 { grid-column: 1 / -1; }
  .tr-step .k { color: var(--muted); font-size: 0.85rem; }
  .tr-step .v { min-width: 0; overflow-wrap: anywhere; }
  .tr-link { font: 500 0.85rem var(--body); background: none; border: 0; color: var(--focus); cursor: pointer; padding: 0; text-align: left; }
  .tr-ref { font-family: var(--mono); font-size: 0.78rem; color: var(--muted); overflow-wrap: anywhere; }`;

  const kindOf = id => /^SV-/.test(id) ? "supervisor" : /-F\d\d$/.test(id) ? "follow-up" : "decision";
  const act = d => esc(d.action.replace(/_/g, " "));
  const pills = d => `<span class="pill p-${esc(d.priority)}">${esc(d.priority)}</span>
    <span class="pill a-${esc(d.autonomy)}">${esc(AUTONOMY[d.autonomy] || d.autonomy || "")}</span>`;
  const link = (id, text) => `<button class="tr-link" type="button" data-ev="${esc(id)}">${text}</button>`;

  /* Candidates: decisions with a trail worth showing (something before or after them). */
  let picks = null;
  function candidates() {
    if (picks) return picks;
    const out = DATA.outcomes || {};
    picks = EPS.filter(e => !e.routine).flatMap(e => e.decisions.map(d => ({ d, e })))
      .filter(({ d }) => (d.dependencies || []).length || (out[d.event_id] && (out[d.event_id].next.length || out[d.event_id].outcome)))
      .sort((a, b) => (PRIO[b.d.priority] ?? 0) - (PRIO[a.d.priority] ?? 0) || ms(a.d.timestamp_utc) - ms(b.d.timestamp_utc));
    return picks;
  }

  function ancestors(d) {
    const seen = new Set(), chain = [];
    const walk = x => {
      for (const id of x.dependencies || []) {
        if (seen.has(id) || !BY_EVENT[id]) continue;
        seen.add(id); walk(BY_EVENT[id].d); chain.push(BY_EVENT[id].d);
      }
    };
    walk(d);
    return chain;
  }

  function chain(d, e) {
    const t = ms(d.timestamp_utc);
    const signals = e.signals.filter(s => ms(s.time) <= t).slice(-3);
    const before = ancestors(d);
    const o = (DATA.outcomes || {})[d.event_id];
    let html = `<div class="dhead"><h2>${act(d)} ${pills(d)}</h2>
      <div class="meta"><span class="mono">${esc(d.event_id)}</span>${when(d.timestamp_utc)}
      <button class="tr-link" type="button" data-ep="${esc(e.id)}">open the episode</button>
      ${VIEWS.day ? `<button class="tr-link" type="button" data-day="${esc(localDay(t))}">replay the day</button>` : ""}</div></div>
      <div class="tr-chain">`;
    html += `<div class="tr-step"><h3>Evidence</h3>${signals.map(s => `<span class="k">${when(s.time)}</span>
      <span class="v"><b>${esc(s.kind.replace(/_/g, " "))}</b>: ${esc(s.detail)}<div class="tr-ref">${esc((s.evidence || []).join("; "))}</div></span>`).join("")
      || `<span class="k"></span><span class="v muted">No signal of this episode before the decision.</span>`}</div>`;
    for (const b of before) {
      html += `<div class="tr-step"><h3>It follows</h3><span class="k">${when(b.timestamp_utc)}</span>
        <span class="v">${link(b.event_id, act(b))} ${pills(b)}<div>${esc(b.reason)}</div></span></div>`;
    }
    html += `<div class="tr-step here"><h3>Decision</h3>
      <span class="k">Why</span><span class="v">${esc(d.reason)}</span>
      <span class="k">Autonomy rule</span><span class="v">${esc(d.rule || "")}</span>
      ${d.expected_outcome ? `<span class="k">Expected</span><span class="v">${esc(d.expected_outcome)}</span>` : ""}
      ${d.verification_plan ? `<span class="k">Verification</span><span class="v">${esc(d.verification_plan)}</span>` : ""}
      ${(d.evidence || []).length ? `<span class="k">Evidence cited</span><span class="v tr-ref">${esc(d.evidence.join("; "))}</span>` : ""}
      ${d.confidence != null ? `<span class="k">Confidence</span><span class="v">${d.confidence.toFixed(2)}</span>` : ""}</div>`;
    for (const id of (o && o.next) || []) {
      const x = BY_EVENT[id];
      if (!x) continue;
      html += `<div class="tr-step next"><h3>Then: ${esc(kindOf(id))}</h3><span class="k">${when(x.d.timestamp_utc)}</span>
        <span class="v">${link(id, act(x.d))} ${pills(x.d)}<div>${esc(x.d.reason)}</div></span></div>`;
    }
    html += o && o.outcome
      ? `<div class="tr-step next"><h3>What happened (from the data)</h3><span class="k">${when(o.outcome.t)}</span>
          <span class="v"><b>${esc(o.outcome.kind.replace(/_/g, " "))}</b><div class="tr-ref">${esc(o.outcome.detail)}</div></span></div>`
      : `<div class="tr-step none"><h3>What happened (from the data)</h3><span class="k"></span>
          <span class="v muted">No outcome in the data for this action.</span></div>`;
    return html + `</div>`;
  }

  return {
    render(el, st) {
      const list = candidates();
      const id = st.ev && BY_EVENT[st.ev] ? st.ev : list.length ? list[0].d.event_id : null;
      if (!id) { el.innerHTML = `<div class="panel empty">No decision to trace in this run.</div>`; return; }
      const { d, e } = BY_EVENT[id];
      // The decision on show is pinned at the top of the list, so a deep link is visible.
      const pinned = list.find(x => x.d.event_id === id) || { d, e };
      const shown = [pinned, ...list.filter(x => x !== pinned)].slice(0, 150);
      el.innerHTML = `<style>${CSS}</style><div class="tr-wrap">
        <nav class="panel tr-pick" aria-label="Decisions with a trail"><h3>Decisions with a trail</h3>
          ${shown.map(({ d: x, e: ex }) => `<button type="button" data-ev="${esc(x.event_id)}" aria-current="${x.event_id === id}">
            <span>${act(x)} <small>${esc(ex.assets.join(", "))}</small></span><small>${fmtDay.format(ms(x.timestamp_utc))} · ${esc(x.priority)}</small></button>`).join("")}
          ${list.length + (list.includes(pinned) ? 0 : 1) > shown.length ? `<small>${fmtN(list.length + (list.includes(pinned) ? 0 : 1) - shown.length)} more: open one from the Episode view.</small>` : ""}
        </nav>
        <section class="panel">${chain(d, e)}</section></div>`;
      const pick = el.querySelector(".tr-pick");
      if (pick) pick.scrollTop = 0;
      const cur = el.querySelector('.tr-pick [aria-current="true"]');
      if (cur) cur.scrollIntoView({ block: "nearest" });
      el.onclick = ev => {
        if (state.view !== "trail") return;
        const a = ev.target.closest("[data-ev]");
        if (a && a.dataset.ev) { go({ ev: a.dataset.ev }); return; }
        const p = ev.target.closest("[data-ep]");
        if (p) { go({ view: "episode", ep: p.dataset.ep }); return; }
        const y = ev.target.closest("[data-day]");
        if (y) go({ view: "day", day: y.dataset.day, step: null });
      };
    },
  };
})();
