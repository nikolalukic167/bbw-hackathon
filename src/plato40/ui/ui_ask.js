/* LLM view: the shift-handover copilot. Three fixed answers computed here from the rows on the
   page (what happened since a time, why the open episode happened, what waits on a human), and a
   question box for the model, served by the local copilot (python -m plato40.ui.copilot). Both
   see only rows known at the clock. The model reads the rows through tools and its answer is
   kept only if every cited id and number is in what the tools returned; it never writes a
   decision. URL keys: at and since (Europe/Vienna wall time), ep, q (asks once, for a demo link). */
VIEWS.ask = (() => {
  const CSS = `
  .ak-bar { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; padding: 8px 12px; font-size: 0.85rem; }
  .ak-bar input { font: inherit; background: var(--sunk); color: var(--fg); border: 1px solid var(--line); border-radius: 4px; padding: 3px 6px; }
  .ak-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; align-items: start; }
  @media (max-width: 1100px) { .ak-grid { grid-template-columns: minmax(0, 1fr); } }
  .ak-card { padding: 12px 14px; display: grid; gap: 8px; min-width: 0; }
  .ak-card p { margin: 0; }
  .ak-row { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 2px 8px; padding: 6px 0; border-top: 1px solid var(--grid); font-size: 0.88rem; }
  .ak-row small { color: var(--muted); grid-column: 2; }
  .ak-link { font: inherit; color: var(--focus); background: none; border: 0; padding: 0; cursor: pointer; text-align: left; text-decoration: underline; text-underline-offset: 2px; }
  .ak-ask { padding: 12px 14px; display: grid; gap: 10px; }
  .ak-ask textarea { font: inherit; width: 100%; min-height: 3.2em; resize: vertical; background: var(--sunk); color: var(--fg); border: 1px solid var(--line); border-radius: 4px; padding: 6px 8px; box-sizing: border-box; }
  .ak-ask .go { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .ak-ask button.q, .ak-ask button.send { font: 500 0.82rem var(--body); border: 1px solid var(--line); background: var(--sunk); color: var(--fg); border-radius: 999px; padding: 3px 10px; cursor: pointer; }
  .ak-ask button.send { background: var(--focus); color: var(--bg); border-color: var(--focus); font-weight: 600; }
  .ak-ask button:disabled { opacity: 0.5; cursor: default; }
  .ak-out { display: grid; gap: 6px; line-height: 1.5; }
  .ak-out p, .ak-out ul { margin: 0; }
  .ak-check { font-size: 0.78rem; color: var(--muted); }
  .ak-fail { color: var(--miss); }`;

  const wall = t => {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(t).map(x => [x.type, x.value]));
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
  };
  const offset = t => Date.parse(`${wall(t)}:00Z`) - t;
  const fromWall = s => { const g = Date.parse(`${s}:00Z`); return g - offset(g - offset(g)); };
  const act = d => esc(d.action.replace(/_/g, " "));
  const pill = p => p ? `<span class="pill p-${esc(p)}">${esc(p)}</span>` : "";
  const epLink = (e, text) => `<button class="ak-link" type="button" data-ep="${esc(e.id)}">${text}</button>`;
  const pickLink = (e, text) => `<button class="ak-link" type="button" data-pick="${esc(e.id)}">${text}</button>`;
  const evLink = (d, text) => `<button class="ak-link" type="button" data-ev="${esc(d.event_id)}">${text}</button>`;
  const assets = e => e.assets.length > 3 ? `${e.assets.slice(0, 3).join(", ")} +${e.assets.length - 3}` : e.assets.join(", ");
  const known = (e, t) => ({ ...e, signals: e.signals.filter(s => ms(s.time) <= t), decisions: e.decisions.filter(d => ms(d.timestamp_utc) <= t) });
  const topPrio = e => e.decisions.reduce((p, d) => (PRIO[d.priority] ?? 0) > (PRIO[p] ?? -1) ? d.priority : p, null);
  const byPrio = (a, b) => (PRIO[topPrio(b)] ?? -1) - (PRIO[topPrio(a)] ?? -1) || a.t0 - b.t0;

  let health = null, last = null, busy = false, asked = null;
  const live = () => location.protocol.startsWith("http")
    ? (health ??= fetch("api/health").then(r => r.ok ? r.json() : null).catch(() => null)) : Promise.resolve(null);

  function since(at, from) {
    const eps = EPS.filter(e => !e.routine && e.t0 >= from && e.t0 <= at).map(e => known(e, at)).filter(e => e.decisions.length).sort(byPrio);
    const crit = eps.filter(e => topPrio(e) === "critical" || topPrio(e) === "high").length;
    const n = eps.reduce((s, e) => s + e.decisions.length, 0);
    return { eps, html: `<h3>Since ${esc(fmtDay.format(from))}</h3>
      <p>${eps.length ? `${fmtN(eps.length)} incidents with ${fmtN(n)} decisions; ${fmtN(crit)} of them high or critical.` : "No incident in this window."}</p>
      ${eps.slice(0, 10).map(e => `<div class="ak-row">${pill(topPrio(e))}<span>${pickLink(e, `${esc(SCEN[e.issue]?.label ?? e.issue)}: ${esc(assets(e))}`)}</span>
        <small>${fmtDay.format(e.t0)} · ${fmtN(e.decisions.length)} decision${e.decisions.length === 1 ? "" : "s"}</small></div>`).join("")}
      ${eps.length > 10 ? `<small class="muted">${fmtN(eps.length - 10)} more in the Episode view.</small>` : ""}` };
  }

  function why(e, at) {
    if (!e) return `<h3>Why this episode</h3><p class="muted">Open an episode, or pick one on the left.</p>`;
    const k = known(e, at);
    const sv = k.decisions.filter(d => /^SV-/.test(d.event_id));
    const rules = [...new Set(k.decisions.filter(d => !/^SV-/.test(d.event_id)).map(d => d.rule).filter(Boolean))];
    const first = k.signals[0];
    return `<h3>Why this episode</h3>
      <p>${epLink(e, `${esc(SCEN[e.issue]?.label ?? e.issue)}: ${esc(assets(e))}`)}</p>
      <p>${first ? `It starts with ${esc(first.kind.replace(/_/g, " "))} at ${fmtDay.format(ms(first.time))}: ${esc(first.detail)}.` : "No signal is known at this clock."}
        ${k.signals.length > 1 ? ` ${fmtN(k.signals.length - 1)} more signals follow.` : ""}</p>
      ${k.decisions.filter(d => !/^SV-/.test(d.event_id)).slice(0, 6).map(d => `<div class="ak-row">${pill(d.priority)}<span>${evLink(d, act(d))}
        <span class="muted">(${esc(AUTONOMY[d.autonomy] || d.autonomy)})</span></span><small>${esc(d.expected_outcome || d.reason || "")}</small></div>`).join("")}
      ${rules.length ? `<p class="muted">Autonomy rules: ${rules.map(esc).join("; ")}.</p>` : ""}
      ${sv.length ? `<p>The supervisor added ${sv.map(d => evLink(d, act(d))).join(", ")}.</p>` : ""}`;
  }

  function waiting(at, from) {
    const ds = EPS.flatMap(e => e.decisions.filter(d => d.autonomy !== "autonomous" && ms(d.timestamp_utc) >= from && ms(d.timestamp_utc) <= at).map(d => ({ d, e })));
    const open = ds.filter(x => !store.get(x.d.event_id))
      .sort((a, b) => (PRIO[b.d.priority] ?? 0) - (PRIO[a.d.priority] ?? 0) || ms(a.d.timestamp_utc) - ms(b.d.timestamp_utc));
    return `<h3>What needs you</h3>
      <p>${ds.length ? `${fmtN(open.length)} of ${fmtN(ds.length)} decisions in this window still wait for a human in this browser.` : "Nothing in this window needs a human."}</p>
      ${open.slice(0, 10).map(({ d, e }) => `<div class="ak-row">${pill(d.priority)}<span>${evLink(d, act(d))} <span class="muted">${esc((d.affected_assets || e.assets).slice(0, 3).join(", "))}</span></span>
        <small>${fmtDay.format(ms(d.timestamp_utc))} · ${esc(AUTONOMY[d.autonomy] || d.autonomy)}</small></div>`).join("")}`;
  }

  // The model's text is escaped first; only [id] citations that the page knows become links.
  function prose(text) {
    const cite = s => s.replace(/\[([^\]]+)\]/g, (m, inner) => inner.split(/[,;]\s*/).map(id => {
      const x = BY_EVENT[id], e = EPS.find(y => y.id === id);
      if (x) return `<button class="ak-link" type="button" data-ev="${id}" title="${id}">${act(x.d)} ${esc((x.d.affected_assets || x.e.assets).slice(0, 2).join(", "))}</button>`;
      if (e) return `<button class="ak-link" type="button" data-ep="${id}" title="${id}">${esc(SCEN[e.issue]?.label ?? e.issue)}: ${esc(assets(e))}</button>`;
      return id;
    }).join(", "));
    const out = [];
    for (const line of text.split("\n")) {
      const item = line.match(/^\s*[-*]\s+(.*)$/);
      if (item) { if (out.at(-1)?.ul) out.at(-1).ul.push(item[1]); else out.push({ ul: [item[1]] }); }
      else if (!line.trim()) out.push({ gap: true });
      else if (out.at(-1)?.p != null) out.at(-1).p += ` ${line}`;
      else out.push({ p: line });
    }
    return out.map(b => b.ul ? `<ul>${b.ul.map(l => `<li>${cite(esc(l))}</li>`).join("")}</ul>` : b.p != null ? `<p>${cite(esc(b.p))}</p>` : "").join("");
  }

  function result(r) {
    if (!r) return "";
    if (!r.ok) return `<div class="ak-out"><p class="ak-fail">No answer shown: ${esc(r.reason || "the copilot failed")}.</p></div>`;
    return `<div class="ak-out">${prose(r.answer)}
      <p class="ak-check">Checked: ${fmtN(r.citations)} citations and ${fmtN(r.numbers)} numbers, all in the rows the tools returned
      (${fmtN(r.tool_calls)} tool calls${r.retried ? ", one retry" : ""}). ${esc(r.model)}, ${esc(r.prompt)}${r.cached ? ", from the cache" : ""}. The model cannot change a decision.</p></div>`;
  }

  return {
    render(el, st) {
      const at = st.at ? fromWall(st.at) : T1;
      const from = st.since ? fromWall(st.since) : at - 12 * 3600e3;
      const s = since(at, from);
      const e = EPS.find(x => x.id === st.ep && x.t0 <= at) || s.eps[0];
      const qs = [
        "What happened overnight, and what should I look at first?",
        "Which approvals are still open, highest priority first?",
        e ? `Why did OSAM act on ${e.assets[0]}, and what does it expect next?` : null,
      ].filter(Boolean);
      el.innerHTML = `<style>${CSS}</style>
        <section class="panel ak-bar" aria-label="Clock">
          <label>Since <input type="datetime-local" id="ak-since" value="${wall(from)}"></label>
          <label>Clock <input type="datetime-local" id="ak-at" value="${wall(at)}"></label>
          <span class="muted">Both answers below see only what was known at the clock.</span>
        </section>
        <div class="ak-grid">
          <section class="panel ak-card">${s.html}</section>
          <section class="panel ak-card">${why(e, at)}</section>
          <section class="panel ak-card">${waiting(at, from)}</section>
        </div>
        <section class="panel ak-ask" aria-label="Ask the model">
          <h3>Ask the model</h3>
          <textarea id="ak-q" placeholder="Ask about this window, an asset or the open episode" maxlength="500"></textarea>
          <div class="go"><button class="send" type="button" id="ak-send" disabled>Ask</button>
            ${qs.map(q => `<button class="q" type="button" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>
          <div id="ak-res">${result(last)}</div>
          <p class="ak-check" id="ak-live"></p>
        </section>`;
      const send = el.querySelector("#ak-send"), out = el.querySelector("#ak-res"), note = el.querySelector("#ak-live");
      live().then(h => {
        if (h && st.q && asked !== location.hash) { asked = location.hash; el.querySelector("#ak-q").value = st.q; ask(st.q); }
        if (h) { send.disabled = busy; note.textContent = `Copilot ${h.live ? "online" : "offline (cached answers only)"}: ${h.model}, ${h.prompt}.`; }
        else note.innerHTML = `The model needs the local copilot: <span class="mono">uv run python -m plato40.ui.copilot ${esc(DATA.source.replace(/\.jsonl$/, ".html"))} ${esc(DATA.source)} ${esc(DATA.source.replace(/\.jsonl$/, ".trace.jsonl"))}</span>. The three answers above work without it.`;
      });
      const ask = async q => {
        if (!q || busy) return;
        busy = true; send.disabled = true;
        out.innerHTML = `<p class="muted">Reading the rows...</p>`;
        try {
          const r = await fetch("api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: q, as_of: new Date(at).toISOString(), since: wall(from), episode: e ? e.id : "" }) });
          last = await r.json();
        } catch (err) { last = { ok: false, reason: String(err) }; }
        busy = false;
        if (state.view === "ask") { out.innerHTML = result(last); send.disabled = false; }
      };
      el.querySelector("#ak-at").onchange = ev => ev.target.value && go({ at: ev.target.value });
      el.querySelector("#ak-since").onchange = ev => ev.target.value && go({ since: ev.target.value });
      send.onclick = () => ask(el.querySelector("#ak-q").value.trim());
      el.onclick = ev => {
        if (state.view !== "ask") return;
        const q = ev.target.closest("[data-q]");
        if (q) { el.querySelector("#ak-q").value = q.dataset.q; if (!send.disabled) ask(q.dataset.q); return; }
        const k = ev.target.closest("[data-pick]");
        if (k) { go({ ep: k.dataset.pick }); return; }
        const a = ev.target.closest("[data-ev]");
        if (a) { go({ view: "trail", ev: a.dataset.ev }); return; }
        const p = ev.target.closest("[data-ep]");
        if (p) go({ view: "episode", ep: p.dataset.ep });
      };
    },
  };
})();
