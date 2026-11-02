"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const REQUEST_TIMEOUT_MS = 180000;

const $ = (id) => document.getElementById(id);
const els = {
  form: $("search-form"), input: $("report-url"), status: $("status"), error: $("error"),
  picker: $("picker"), results: $("results"), intro: $("intro"), how: $("how"), tooltip: $("tooltip"),
  skeleton: $("skeleton"), newLog: $("new-log"),
};

const state = { report: null, fight: null, player: null, compare: null, analysis: null };
let requestId = 0;
let redrawCharts = [];

// Log text is outside data, so it only ever reaches the page as text nodes.
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value);
  }
  el.append(...children.flat().filter((c) => c !== null && c !== undefined));
  return el;
}

function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  el.append(...children);
  return el;
}

function formatTime(seconds) {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function compact(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(Math.round(n));
}

function niceMax(value) {
  if (value <= 0) return 1;
  const step = 10 ** Math.floor(Math.log10(value));
  for (const m of [1, 1.5, 2, 3, 4, 5, 6, 8, 10]) {
    if (m * step >= value) return m * step;
  }
  return value;
}

function setStatus(text, showSkeleton = false) {
  els.status.replaceChildren();
  els.status.hidden = !text;
  els.skeleton.hidden = !showSkeleton;
  if (text) els.status.append(h("span", { class: "spinner", "aria-hidden": "true" }), h("span", {}, text));
}

function setError(text) {
  els.error.textContent = text || "";
  els.error.hidden = !text;
}

function setLanding(isLanding) {
  document.body.classList.toggle("is-landing", isLanding);
  els.intro.hidden = !isLanding;
  els.how.hidden = !isLanding;
  els.form.hidden = !isLanding;
  els.newLog.hidden = isLanding;
}

async function api(path, params) {
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== ""));
  let response;
  try {
    response = await fetch(`${path}?${query}`, { signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
  } catch {
    throw new Error("The request didn't finish. Check your connection and try again.");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw Object.assign(new Error(body.error || "Something went wrong. Try again."), { players: body.players });
  }
  return body;
}

function updateUrl() {
  const params = new URLSearchParams();
  if (state.report) params.set("report", state.report.code);
  if (state.fight) params.set("fight", state.fight);
  if (state.player) params.set("player", state.player);
  if (state.compare) params.set("compare", state.compare);
  const query = params.toString();
  history.replaceState(null, "", query ? `/?${query}` : "/");
}

async function openReport(url, preset = {}) {
  const id = ++requestId;
  setError("");
  setLanding(false);
  els.picker.hidden = true;
  els.results.hidden = true;
  setStatus("Opening the log...");
  try {
    const report = await api("/api/report", { url });
    if (id !== requestId) return;
    state.report = report;
    state.player = preset.player || null;
    state.compare = preset.compare || null;
    setStatus("");
    const fight = preset.fight || report.fight_hint;
    if (fight) await runAnalysis(fight);
    else showFights();
  } catch (e) {
    if (id !== requestId) return;
    setStatus("");
    setError(e.message);
    setLanding(!state.report);
  }
}

function usableFights() {
  const real = state.report.fights.filter((f) => f.duration_s >= 1);
  const encounters = real.filter((f) => f.encounter);
  return encounters.length ? encounters : real;
}

function fightLabel(f) {
  return f.keystone ? `${f.name} +${f.keystone}` : f.name;
}

function showFights() {
  state.fight = null;
  updateUrl();
  const fights = usableFights();
  els.results.hidden = true;
  els.picker.hidden = false;
  els.picker.replaceChildren(
    h("h2", { class: "section-title" }, state.report.title || "Report"),
    h("p", { class: "section-note" }, fights.length ? "Pick a fight." : "This report has no fights."),
    h("div", { class: "pick-list" }, fights.map((f) => h("button", {
      type: "button", class: "pick", onclick: () => { state.compare = null; state.player = null; runAnalysis(f.id); },
    },
      h("span", { class: "pick-main" }, `#${f.id} ${fightLabel(f)}`),
      h("span", { class: "pick-side" }, `${formatTime(f.duration_s)} · ${f.kill ? "Kill" : "Not completed"}`),
    ))),
  );
}

function showPlayers(message, players) {
  els.results.hidden = true;
  els.picker.hidden = false;
  const sorted = [...players].sort((a, b) => Number(b.specs.includes("Blood")) - Number(a.specs.includes("Blood")));
  els.picker.replaceChildren(
    h("h2", { class: "section-title" }, "Who should we look at?"),
    h("p", { class: "section-note" }, message),
    h("div", { class: "pick-list" }, sorted.map((p) => h("button", {
      type: "button", class: "pick", onclick: () => { state.player = p.name; runAnalysis(state.fight); },
    },
      h("span", { class: "pick-main" }, p.name),
      h("span", { class: "pick-side" }, p.specs.join("/") || "Unknown spec"),
    ))),
  );
}

async function runAnalysis(fight) {
  const id = ++requestId;
  state.fight = fight;
  updateUrl();
  setError("");
  setLanding(false);
  els.picker.hidden = true;
  els.results.hidden = true;
  setStatus(state.compare ? "Reading both fights. This can take up to a minute..." : "Reading the fight. This can take up to 30 seconds...", true);
  try {
    const analysis = await api("/api/analyze", {
      report: state.report.code, fight, player: state.player, compare: state.compare,
    });
    if (id !== requestId) return;
    state.analysis = analysis;
    setStatus("");
    renderResults(analysis);
  } catch (e) {
    if (id !== requestId) return;
    setStatus("");
    if (e.players) showPlayers(e.message, e.players);
    else { setError(e.message); showFights(); }
  }
}

function legend(items) {
  return h("div", { class: "legend" }, items.map(([label, cls]) =>
    h("span", { class: "legend-item" }, h("span", { class: `key ${cls}`, "aria-hidden": "true" }), label)));
}

function table(headers, rows) {
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, headers.map(([label, cls]) => h("th", { class: cls || "", scope: "col" }, label)))),
    h("tbody", {}, rows.map((row) => h("tr", {}, row.map((cell, i) => h("td", { class: headers[i][1] || "" }, cell))))),
  ));
}

function showTip(target, title, lines) {
  els.tooltip.replaceChildren(h("strong", {}, title), ...lines.map((l) => h("div", {}, l)));
  els.tooltip.hidden = false;
  const box = target.getBoundingClientRect();
  const tip = els.tooltip.getBoundingClientRect();
  const left = Math.min(window.innerWidth - tip.width - 8, Math.max(8, box.left + box.width / 2 - tip.width / 2));
  const top = box.top - tip.height - 8 < 8 ? box.bottom + 8 : box.top - tip.height - 8;
  els.tooltip.style.left = `${left}px`;
  els.tooltip.style.top = `${top}px`;
}

function hideTip() {
  els.tooltip.hidden = true;
}

function withTip(el, title, lines) {
  el.setAttribute("class", "mark-hit");
  el.setAttribute("tabindex", "0");
  el.setAttribute("aria-label", `${title}. ${lines.join(". ")}`);
  el.addEventListener("mouseenter", () => showTip(el, title, lines));
  el.addEventListener("mouseleave", hideTip);
  el.addEventListener("focus", () => showTip(el, title, lines));
  el.addEventListener("blur", hideTip);
  return el;
}

function timelineChart(container, a) {
  const width = Math.max(320, container.clientWidth);
  const narrow = width < 560;
  const labelW = narrow ? 0 : 150;
  const rowH = narrow ? 44 : 30;
  const top = 22;
  const right = 12;
  const rows = [...a.cooldowns, { name: "Deaths", deaths: true }];
  const height = top + rows.length * rowH + 26;
  const plotW = width - labelW - right;
  const x = (t) => labelW + (t / a.fight.duration_s) * plotW;
  const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": "Cooldown timeline" });

  a.pulls.forEach((p, i) => {
    if (i % 2 === 0) {
      svg.append(s("rect", { x: x(p.start_s), y: top - 4, width: Math.max(1, x(p.end_s) - x(p.start_s)), height: rows.length * rowH + 4, fill: "var(--band)" }));
    }
    if (x(p.end_s) - x(p.start_s) > 14) {
      const label = s("text", { x: (x(p.start_s) + x(p.end_s)) / 2, y: 12, "text-anchor": "middle" });
      label.textContent = String(p.id);
      svg.append(label);
    }
  });

  rows.forEach((row, i) => {
    const lineY = narrow ? top + i * rowH + 30 : top + i * rowH + rowH / 2;
    if (narrow) {
      const label = s("text", { x: 0, y: top + i * rowH + 12, class: "row-label" });
      label.textContent = row.name;
      svg.append(label);
    } else {
      const label = s("text", { x: labelW - 12, y: lineY + 4, "text-anchor": "end", class: "row-label" });
      label.textContent = row.name;
      svg.append(label);
    }
    svg.append(s("line", { x1: labelW, x2: width - right, y1: lineY, y2: lineY, stroke: "var(--grid)" }));

    if (row.deaths) {
      for (const d of a.deaths) {
        svg.append(s("rect", { x: x(d.t_s) - 4, y: lineY - 4, width: 8, height: 8, rx: 2, fill: "var(--critical)", stroke: "var(--surface)", "stroke-width": 2 }));
        svg.append(withTip(s("circle", { cx: x(d.t_s), cy: lineY, r: 12 }), d.name, [`Died at ${formatTime(d.t_s)}`, `Killed by ${d.killed_by}`]));
      }
      return;
    }
    const color = row.group === "control" ? "var(--series-2)" : "var(--series-1)";
    for (const t of row.casts) {
      svg.append(s("circle", { cx: x(t), cy: lineY, r: 4, fill: color, stroke: "var(--surface)", "stroke-width": 2 }));
      svg.append(withTip(s("circle", { cx: x(t), cy: lineY, r: 12 }), row.name, [`Cast at ${formatTime(t)}`]));
    }
  });

  const tickEvery = niceMax(a.fight.duration_s / (narrow ? 3 : 7));
  for (let t = 0; t <= a.fight.duration_s; t += tickEvery) {
    const label = s("text", { x: x(t), y: height - 6, "text-anchor": t === 0 ? "start" : "middle" });
    label.textContent = formatTime(t);
    svg.append(label);
  }
  container.replaceChildren(svg);
}

function barPath(x, y, w, baseline) {
  const r = Math.min(4, w / 2, baseline - y);
  return `M${x},${baseline}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${baseline}Z`;
}

function yAxis(svg, max, left, right, y) {
  for (let i = 0; i <= 4; i++) {
    const value = (max * i) / 4;
    svg.append(s("line", { x1: left, x2: right, y1: y(value), y2: y(value), stroke: i ? "var(--grid)" : "var(--axis)" }));
    const label = s("text", { x: left - 8, y: y(value) + 4, "text-anchor": "end" });
    label.textContent = compact(value);
    svg.append(label);
  }
}

function pullChart(container, a) {
  const width = Math.max(320, container.clientWidth);
  const height = 240;
  const left = 46;
  const right = 4;
  const top = 8;
  const bottom = 24;
  const baseline = height - bottom;
  const max = niceMax(Math.max(...a.pulls.flatMap((p) => [p.dtps, p.hps]), 1));
  const y = (v) => baseline - (v / max) * (baseline - top);
  const band = (width - left - right) / a.pulls.length;
  const barW = Math.max(3, Math.min(24, (band - 10) / 2));
  const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": "Damage and healing on the tank per pull" });
  yAxis(svg, max, left, width - right, y);

  a.pulls.forEach((p, i) => {
    const center = left + band * i + band / 2;
    if (p.dtps > 0) svg.append(s("path", { d: barPath(center - barW - 1, y(p.dtps), barW, baseline), fill: "var(--series-1)", class: "grow" }));
    if (p.hps > 0) svg.append(s("path", { d: barPath(center + 1, y(p.hps), barW, baseline), fill: "var(--series-2)", class: "grow" }));
    if (band > 16 || i % 2 === 0) {
      const label = s("text", { x: center, y: height - 6, "text-anchor": "middle" });
      label.textContent = String(p.id);
      svg.append(label);
    }
    svg.append(withTip(s("rect", { x: left + band * i, y: top, width: band, height: baseline - top }),
      `Pull ${p.id}: ${p.name}`, [`Damage taken ${compact(p.dtps)}/s`, `Healing received ${compact(p.hps)}/s`]));
  });
  container.replaceChildren(svg);
}

function trimDeadWindows(values) {
  let end = values.length;
  while (end > 0 && values[end - 1] === 0) end--;
  return values.slice(0, end);
}

function compareChart(container, cmp, thisLabel, otherLabel) {
  const width = Math.max(320, container.clientWidth);
  const height = 240;
  const left = 46;
  const right = 12;
  const top = 8;
  const bottom = 24;
  const baseline = height - bottom;
  // A window of zero after a fight ends isn't a safe stretch, so those aren't drawn
  const here = trimDeadWindows(cmp.windows.map((w) => w.damage));
  const there = trimDeadWindows(cmp.windows.map((w) => w.other_damage));
  const max = niceMax(Math.max(...here, ...there, 1));
  const step = (width - left - right) / Math.max(1, cmp.windows.length - 1);
  const x = (i) => left + step * i;
  const y = (v) => baseline - (v / max) * (baseline - top);
  const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": "Damage per five seconds in both fights" });
  yAxis(svg, max, left, width - right, y);

  for (const [values, color] of [[there, "var(--series-2)"], [here, "var(--series-1)"]]) {
    if (values.length > 1) {
      const d = values.map((v, i) => `${i ? "L" : "M"}${x(i)},${y(v)}`).join("");
      svg.append(s("path", { d, class: "draw", pathLength: 1, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    }
    values.forEach((v, i) => svg.append(s("circle", { cx: x(i), cy: y(v), r: 4, fill: color, stroke: "var(--surface)", "stroke-width": 2 })));
  }

  cmp.windows.forEach((w, i) => {
    const label = s("text", { x: x(i), y: height - 6, "text-anchor": i === 0 ? "start" : "middle" });
    label.textContent = `${w.offset}s`;
    if (step > 30 || i % 2 === 0) svg.append(label);
    const lines = [
      `${thisLabel}: ${i < here.length ? compact(w.damage) : "no data"}`,
      `${otherLabel}: ${i < there.length ? compact(w.other_damage) : "no data"}`,
    ];
    svg.append(withTip(s("rect", { x: x(i) - step / 2, y: top, width: step, height: baseline - top }), `${w.offset}-${w.offset + 5}s into the pull`, lines));
  });
  container.replaceChildren(svg);
}

function addChart(render) {
  const container = h("div", { class: "chart" });
  // Only the first draw animates, so resizing the window doesn't replay it
  redrawCharts.push(() => {
    if (container.firstChild) container.classList.add("settled");
    render(container);
  });
  return container;
}

function panel(title, note, ...children) {
  return h("section", { class: "panel" },
    h("h3", { class: "section-title" }, title),
    note ? h("p", { class: "section-note" }, note) : null,
    ...children);
}

function renderVerdict(a) {
  const top = a.findings.find((f) => f.severity !== "good");
  const severity = top ? top.severity : "good";
  const label = severity[0].toUpperCase() + severity.slice(1);
  return h("section", { class: `verdict verdict-${severity}` },
    h("span", { class: `badge badge-${severity}` }, label),
    h("h3", { class: "verdict-title" }, top ? top.title : "Nothing went badly wrong here"),
    h("p", { class: "verdict-detail" }, top ? top.detail : "No deaths or major cooldown problems stood out in this fight."));
}

function renderFindings(a) {
  const problems = a.findings.filter((f) => f.severity !== "good").slice(1);
  const strengths = a.findings.filter((f) => f.severity === "good");
  const panels = [];
  if (problems.length) {
    panels.push(panel("What else went wrong", null, h("div", { class: "findings" }, problems.map((f) =>
      h("div", { class: "finding" },
        h("span", { class: `badge badge-${f.severity}` }, f.severity[0].toUpperCase() + f.severity.slice(1)),
        h("div", {},
          h("div", { class: "finding-title" }, f.title),
          h("div", { class: "finding-detail" }, f.detail)))))));
  }
  if (strengths.length) {
    panels.push(panel("What went well", null, h("div", { class: "findings" }, strengths.map((f) =>
      h("div", { class: "finding finding-plain" },
        h("div", {},
          h("div", { class: "finding-title" }, f.title),
          h("div", { class: "finding-detail" }, f.detail)))))));
  }
  if (!panels.length) return null;
  return h("div", { class: `findings-grid${panels.length === 1 ? " findings-grid-single" : ""}` }, panels);
}

function renderTiles(a) {
  const hardest = a.pulls.reduce((best, p) => (p.dtps > best.dtps ? p : best), a.pulls[0]);
  const tile = (label, value, note) => h("div", { class: "tile" },
    h("div", { class: "tile-label" }, label),
    h("div", { class: "tile-value" }, value),
    h("div", { class: "tile-note" }, note));
  return h("div", { class: "tiles" },
    tile("Result", a.fight.kill ? "Kill" : "Incomplete", `${formatTime(a.fight.duration_s)} long`),
    tile("Deaths", String(a.deaths.length), a.deaths.some((d) => d.is_tank) ? "Including the tank" : "Tank survived"),
    tile("Close calls", String(a.low_hp.length), "Tank below 30% health"),
    tile("Hardest pull", `${compact(hardest.dtps)}/s`, hardest.name));
}

function renderCompareControl(a) {
  const options = state.report.fights.filter((f) => f.id !== a.fight.id && f.name === a.fight.name && f.duration_s >= 1);
  if (!options.length) return null;
  const select = h("select", { "aria-label": "Compare with another fight", onchange: (e) => {
    state.compare = e.target.value ? Number(e.target.value) : null;
    runAnalysis(state.fight);
  } },
    h("option", { value: "" }, "Compare with..."),
    options.map((f) => h("option", { value: String(f.id) }, `Fight #${f.id} (${formatTime(f.duration_s)})`)));
  select.value = state.compare ? String(state.compare) : "";
  return select;
}

function renderCompare(a) {
  const cmp = a.compare;
  if (!cmp) return null;
  if (cmp.missing) return h("div", { class: "notice" }, cmp.missing);
  const thisLabel = `Fight #${a.fight.id}`;
  const otherLabel = `Fight #${cmp.fight_id}`;
  return panel(`Same pull, two fights: ${cmp.pull}`, "Damage on the tank per 5 seconds from the start of the pull. Groups may differ between fights.",
    addChart((c) => compareChart(c, cmp, thisLabel, otherLabel)),
    legend([[thisLabel, "key-1"], [otherLabel, "key-2"]]),
    h("details", {}, h("summary", {}, "Show as a table"), table(
      [["Window"], [`Damage ${thisLabel}`, "num"], [`Damage ${otherLabel}`, "num"], [`Healing ${thisLabel}`, "num"], [`Healing ${otherLabel}`, "num"], [`Attacks ${thisLabel}`, "num"], [`Attacks ${otherLabel}`, "num"]],
      cmp.windows.map((w) => [`${w.offset}-${w.offset + 5}s`, compact(w.damage), compact(w.other_damage), compact(w.healing), compact(w.other_healing), String(w.attacks), String(w.other_attacks)]),
    )));
}

function renderResults(a) {
  redrawCharts = [];
  hideTip();
  const key = a.fight.keystone ? ` +${a.fight.keystone}` : "";
  const copy = h("button", { type: "button", class: "button-ghost", onclick: async (e) => {
    const button = e.currentTarget;
    try {
      await navigator.clipboard.writeText(location.href);
      button.textContent = "Link copied";
    } catch {
      button.textContent = "Copy from address bar";
    }
    setTimeout(() => { button.textContent = "Copy link"; }, 2000);
  } }, "Copy link");

  els.picker.hidden = true;
  els.results.hidden = false;
  els.results.replaceChildren(...[
    h("div", { class: "result-head" },
      h("div", {},
        h("h2", {}, `${a.fight.name}${key}`),
        h("p", { class: "result-sub" }, `${a.tank.name} · ${a.tank.specs.join("/")} · ${a.group.filter((p) => p.name !== a.tank.name).map((p) => p.name).join(", ")}`)),
      h("div", { class: "result-actions" },
        renderCompareControl(a),
        h("button", { type: "button", class: "button-ghost", onclick: showFights }, "Other fights"),
        copy)),
    a.warning ? h("div", { class: "notice" }, a.warning) : null,
    renderVerdict(a),
    renderTiles(a),
    renderFindings(a),
    a.cooldowns.length ? panel("Cooldown timeline", "Each dot is a cast. Numbers across the top are pulls.",
      addChart((c) => timelineChart(c, a)),
      legend([["Defensive", "key-1"], ["Crowd control", "key-2"], ["Player death", "key-death"]]),
      h("details", {}, h("summary", {}, "Show as a table"), table(
        [["Spell"], ["Casts", "num"], ["Times"]],
        a.cooldowns.map((c) => [c.name, String(c.casts.length), c.casts.map(formatTime).join(", ")])))) : null,
    renderCompare(a),
    panel("Pressure on the tank", "Average damage taken and healing received per second, by pull.",
      addChart((c) => pullChart(c, a)),
      legend([["Damage taken", "key-1"], ["Healing received", "key-2"]]),
      h("details", {}, h("summary", {}, "Show as a table"), table(
        [["#"], ["Pull"], ["Time"], ["Damage/s", "num"], ["Healing/s", "num"]],
        a.pulls.map((p) => [String(p.id), p.name, `${formatTime(p.start_s)}-${formatTime(p.end_s)}`, compact(p.dtps), compact(p.hps)])))),
    panel("Cooldowns at each pull", "Estimated from the shortest gap between uses in this fight. Spells cast fewer than 3 times aren't estimated.",
      table([["Pull"], ["Rest", "num"], ["On cooldown"], ["Ready, not used"]],
        a.pulls.map((p) => [
          `${p.id}. ${p.name}`,
          p.rest_s === null ? "-" : `${Math.round(p.rest_s)}s`,
          p.on_cooldown.map((w) => `${w.name} (${formatTime(w.ready_s)})`).join(", ") || "None",
          p.ready_unused.join(", ") || "None",
        ]))),
    a.deaths.length ? panel("Deaths", null,
      table([["Time"], ["Player"], ["Killed by"], ["Last 5 seconds"]],
        a.deaths.map((d) => [formatTime(d.t_s), d.name, d.killed_by, d.recap.map((x) => `${x.ability} ${compact(x.amount)}`).join(", ")]))) : null,
    h("div", { class: "two-col" },
      panel("Damage taken by the tank", null,
        table([["Ability"], ["Total", "num"], ["Share", "num"], ["Biggest", "num"]],
          a.damage_taken.map((d) => [d.name, compact(d.total), `${d.pct.toFixed(1)}%`, compact(d.max)]))),
      panel("Enemy casts", "The log can't show which spells can be interrupted.",
        table([["Spell"], ["Started", "num"], ["Finished", "num"], ["Kicked", "num"]],
          a.enemy_casts.map((e) => [e.name, String(e.started), String(e.finished), String(e.kicked)])),
        h("p", { class: "section-note" }, `Kicks: ${a.kicks.map((k) => `${k.name} ${k.count}`).join(", ") || "none"}`))),
    h("div", { class: "two-col" },
      panel("Buff uptime", "Share of time in combat.",
        table([["Buff"], ["Uptime", "num"]], a.uptime.map((u) => [u.name, `${u.pct.toFixed(1)}%`]))),
      panel("Abilities", `${a.casts.total} casts, ${a.casts.cpm.toFixed(1)} per minute in combat.`,
        table([["Ability"], ["Casts", "num"], ["Per min", "num"]],
          a.abilities.slice(0, 12).map((x) => [x.name, String(x.count), x.per_min.toFixed(1)])))),
  ].filter(Boolean));
  redrawCharts.forEach((draw) => draw());
  window.scrollTo({ top: 0, behavior: "instant" });
}

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  const url = els.input.value.trim();
  if (url) openReport(url);
  else setError("Paste a Warcraft Logs report link first.");
});

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => redrawCharts.forEach((draw) => draw()), 120);
});

const initial = new URLSearchParams(location.search);
if (initial.get("report")) {
  const report = initial.get("report");
  els.input.value = report;
  openReport(report, {
    fight: Number(initial.get("fight")) || null,
    player: initial.get("player"),
    compare: Number(initial.get("compare")) || null,
  });
}
