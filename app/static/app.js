/* At Yarışı Tahmin Dashboard — istemci tarafı.
   /api/dashboard'u periyodik olarak çeker, geri sayımları yerelde işletir. */

const REFRESH_MS = 5000;

let state = null;
let tz = "Europe/Istanbul";

const money = (v) =>
  new Intl.NumberFormat("tr-TR", { style: "currency", currency: "TRY", maximumFractionDigits: 2 })
    .format(Number(v || 0));

const pct = (v, digits = 1) => `${Number(v || 0).toFixed(digits)}%`;

const timeFmt = () =>
  new Intl.DateTimeFormat("tr-TR", { timeZone: tz, hour: "2-digit", minute: "2-digit" });

const dateTimeFmt = () =>
  new Intl.DateTimeFormat("tr-TR", {
    timeZone: tz, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });

const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
};

function countdown(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const neg = seconds < 0;
  const s = Math.floor(Math.abs(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const body = h > 0
    ? `${h}s ${String(m).padStart(2, "0")}dk`
    : `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return neg ? `-${body}` : body;
}

/* ---------------------------------------------------------------- KPI */

function renderTiles(stats) {
  const box = document.getElementById("tiles");
  box.replaceChildren();

  const profit = Number(stats.total_profit || 0);
  const profitClass = profit > 0 ? "pos" : profit < 0 ? "neg" : "";
  const profitIcon = profit > 0 ? "▲" : profit < 0 ? "▼" : "•";

  const tiles = [
    {
      label: "Kasa",
      value: money(stats.bankroll),
      sub: `başlangıç ${money(stats.bankroll_start)}`,
    },
    {
      label: "Toplam kâr",
      value: `${profitIcon} ${money(profit)}`,
      cls: profitClass,
      sub: `${money(stats.total_staked)} oynandı`,
    },
    {
      label: "ROI",
      value: pct(stats.roi, 1),
      cls: stats.roi > 0 ? "pos" : stats.roi < 0 ? "neg" : "",
      sub: "yatırılan tutar başına getiri",
    },
    {
      label: "İsabet",
      value: pct(stats.hit_rate, 1),
      sub: `${stats.win_count}/${stats.bet_count} bahis tuttu`,
    },
    {
      label: "Bekleyen",
      value: String(stats.pending_count),
      sub: `${money(stats.pending_stake)} kilitli`,
    },
  ];

  for (const t of tiles) {
    box.append(
      el("div", { class: "tile" },
        el("div", { class: "label" }, t.label),
        el("div", { class: `value ${t.cls || ""}` }, t.value),
        el("div", { class: "sub" }, t.sub),
      ),
    );
  }
}

/* -------------------------------------------------------------- grafik */

const SVG_NS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
};

function renderCurve(curve) {
  const svg = document.getElementById("curve");
  svg.replaceChildren();
  if (!curve || curve.length === 0) return;

  const width = Math.max(svg.clientWidth || 640, 320);
  const height = 168;
  const pad = { top: 12, right: 14, bottom: 22, left: 62 };
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const values = curve.map((p) => Number(p.balance));
  const times = curve.map((p) => new Date(p.ts).getTime());
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (max - min < 1) { min -= 5; max += 5; }
  const padY = (max - min) * 0.12;
  min -= padY; max += padY;

  const t0 = times[0];
  const t1 = times[times.length - 1];
  const spanX = Math.max(t1 - t0, 1);
  const x = (t) => pad.left + ((t - t0) / spanX) * (width - pad.left - pad.right);
  const y = (v) => height - pad.bottom - ((v - min) / (max - min)) * (height - pad.top - pad.bottom);

  // ölçek çizgileri (geri planda kalan hairline'lar)
  const ticks = 3;
  for (let i = 0; i <= ticks; i++) {
    const v = min + ((max - min) * i) / ticks;
    const yy = y(v);
    svg.append(svgEl("line", {
      class: "grid-line", x1: pad.left, x2: width - pad.right, y1: yy, y2: yy,
    }));
    const label = svgEl("text", { class: "tick", x: pad.left - 8, y: yy + 4, "text-anchor": "end" });
    label.textContent = money(v);
    svg.append(label);
  }

  const points = curve.map((p, i) => [x(times[i]), y(Number(p.balance))]);
  const line = points.map(([px, py], i) => `${i === 0 ? "M" : "L"}${px.toFixed(1)},${py.toFixed(1)}`).join(" ");

  const baseY = height - pad.bottom;
  svg.append(svgEl("path", {
    class: "area",
    d: `${line} L${points[points.length - 1][0].toFixed(1)},${baseY} L${points[0][0].toFixed(1)},${baseY} Z`,
  }));
  svg.append(svgEl("path", { class: "series", d: line }));
  svg.append(svgEl("line", { class: "axis-line", x1: pad.left, x2: width - pad.right, y1: baseY, y2: baseY }));

  const firstLabel = svgEl("text", { class: "tick", x: pad.left, y: height - 6 });
  firstLabel.textContent = dateTimeFmt().format(new Date(t0));
  svg.append(firstLabel);
  const lastLabel = svgEl("text", { class: "tick", x: width - pad.right, y: height - 6, "text-anchor": "end" });
  lastLabel.textContent = dateTimeFmt().format(new Date(t1));
  svg.append(lastLabel);

  // hover katmanı: dikey imleç + nokta + tooltip
  const cursor = svgEl("line", { class: "cursor", y1: pad.top, y2: baseY, x1: -10, x2: -10, opacity: 0 });
  const dot = svgEl("circle", { class: "dot", r: 4.5, cx: -10, cy: -10, opacity: 0 });
  svg.append(cursor, dot);

  const tooltip = document.getElementById("tooltip");
  const overlay = svgEl("rect", {
    x: pad.left, y: pad.top, width: Math.max(width - pad.left - pad.right, 1),
    height: Math.max(baseY - pad.top, 1), fill: "transparent",
  });
  overlay.addEventListener("mousemove", (event) => {
    const box = svg.getBoundingClientRect();
    const scale = width / box.width;
    const mx = (event.clientX - box.left) * scale;
    let best = 0;
    for (let i = 1; i < points.length; i++) {
      if (Math.abs(points[i][0] - mx) < Math.abs(points[best][0] - mx)) best = i;
    }
    const [px, py] = points[best];
    cursor.setAttribute("x1", px); cursor.setAttribute("x2", px); cursor.setAttribute("opacity", 1);
    dot.setAttribute("cx", px); dot.setAttribute("cy", py); dot.setAttribute("opacity", 1);
    const row = curve[best];
    tooltip.innerHTML =
      `<strong>${money(row.balance)}</strong><br><span class="muted">${dateTimeFmt().format(new Date(row.ts))}</span>`;
    tooltip.style.opacity = "1";
    tooltip.style.left = `${event.clientX + 14}px`;
    tooltip.style.top = `${event.clientY - 10}px`;
  });
  overlay.addEventListener("mouseleave", () => {
    cursor.setAttribute("opacity", 0);
    dot.setAttribute("opacity", 0);
    tooltip.style.opacity = "0";
  });
  svg.append(overlay);

  renderCurveTable(curve);
}

function renderCurveTable(curve) {
  const host = document.getElementById("curve-table");
  host.hidden = false;
  const rows = curve.slice(-25).reverse();
  const details = el("details", {},
    el("summary", { class: "hint", style: "cursor:pointer" }, "Veri tablosu"),
    el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Zaman"), el("th", { class: "num" }, "Bakiye"))),
      el("tbody", {}, rows.map((r) =>
        el("tr", {},
          el("td", {}, dateTimeFmt().format(new Date(r.ts))),
          el("td", { class: "num" }, money(r.balance))))),
    ),
  );
  host.replaceChildren(details);
}

/* -------------------------------------------------------- yaklaşan koşular */

function assessmentTable(race) {
  const picks = new Set((race.bets || []).map((b) => b.number));
  return el("table", {},
    el("thead", {}, el("tr", {},
      el("th", {}, "No"),
      el("th", {}, "At"),
      el("th", { class: "num" }, "Ganyan"),
      el("th", { class: "num" }, "Model"),
      el("th", { class: "num" }, "Piyasa"),
      el("th", { class: "num" }, "EV"))),
    el("tbody", {}, (race.assessments || []).map((a) =>
      el("tr", { class: picks.has(a.number) ? "pick" : "" },
        el("td", {}, el("span", { class: "no" }, a.number)),
        el("td", { class: "name" }, a.horse),
        el("td", { class: "num" }, a.odds ? a.odds.toFixed(2) : "—"),
        el("td", { class: "num" }, pct(a.model_prob * 100)),
        el("td", { class: "num" }, pct(a.market_prob * 100)),
        el("td", { class: `num ${a.ev > 0 ? "pos" : ""}` }, `${a.ev > 0 ? "+" : ""}${(a.ev * 100).toFixed(0)}%`)))),
  );
}

function betsBlock(race) {
  const bets = race.bets || [];
  if (bets.length === 0) {
    const reason = race.skip_reason
      || (race.locked ? "Kilitlendi — değer bulunamadı, bahis oynanmadı." : "Şu an EV eşiğini geçen at yok.");
    return el("div", { class: "bets" },
      el("div", { class: "no-bet" }, `${race.locked ? "Kilitlendi · " : ""}${reason}`));
  }
  return el("div", { class: "bets" },
    bets.map((b) => {
      const odds = b.odds ?? b.odds_at_lock;
      const ev = Number(b.ev) * 100;
      return el("div", { class: "bet-line" },
        el("span", { class: "horse" },
          el("span", { class: "no" }, b.number), " ", b.horse),
        el("span", {},
          `${money(b.stake)} @ ${Number(odds).toFixed(2)} · EV +${ev.toFixed(0)}%`));
    }));
}

function renderUpcoming(races, lockMinutes) {
  const host = document.getElementById("upcoming");
  host.replaceChildren();
  if (!races || races.length === 0) {
    host.append(el("div", { class: "empty" }, "Yaklaşan koşu yok."));
    return;
  }

  for (const race of races) {
    const locked = race.locked;
    const card = el("div", { class: `race ${locked ? "locked" : ""}`, "data-race": race.id },
      el("div", { class: "race-head" },
        el("span", { class: "title" }, `${race.track} · ${race.race_no}. koşu`),
        el("span", { class: "spacer" }),
        el("span", { class: `badge ${locked ? "locked" : "live"}` }, locked ? "KİLİTLİ" : "CANLI")),
      el("div", { class: "race-meta" },
        [
          timeFmt().format(new Date(race.post_time)),
          race.distance ? `${race.distance} m` : null,
          race.surface,
          race.race_class,
          `${race.field_size} at`,
        ].filter(Boolean).join(" · ")),
      el("div", { class: "countdown", "data-post": race.post_time, "data-lock": lockMinutes }, "—"),
      assessmentTable(race),
      betsBlock(race),
    );
    host.append(card);
  }
  tickCountdowns();
}

function tickCountdowns() {
  const now = Date.now();
  for (const node of document.querySelectorAll(".countdown")) {
    const post = new Date(node.dataset.post).getTime();
    const lockMs = Number(node.dataset.lock || 5) * 60000;
    const toPost = (post - now) / 1000;
    const toLock = (post - lockMs - now) / 1000;
    node.textContent = toLock > 0
      ? `Kilide ${countdown(toLock)} · start ${countdown(toPost)}`
      : `Kilitlendi · start ${countdown(toPost)}`;
  }
}

/* ------------------------------------------------------------- sonuçlar */

function statusChip(status) {
  if (status === "won") return el("span", { class: "status won" }, el("span", { class: "icon" }, "✓"), "KAZANDI");
  if (status === "lost") return el("span", { class: "status lost" }, el("span", { class: "icon" }, "✕"), "KAYBETTİ");
  return el("span", { class: "status void" }, el("span", { class: "icon" }, "—"), "İADE");
}

function renderResults(results) {
  const host = document.getElementById("results");
  host.replaceChildren();
  if (!results || results.length === 0) {
    host.append(el("div", { class: "empty" }, "Henüz sonuçlanan tahmin yok."));
    return;
  }

  const rows = [];
  for (const race of results) {
    if (race.bets.length === 0) {
      rows.push(el("tr", {},
        el("td", {}, dateTimeFmt().format(new Date(race.post_time))),
        el("td", { class: "name" }, `${race.track} ${race.race_no}.`),
        el("td", { class: "name" }, "—"),
        el("td", { class: "num" }, "—"),
        el("td", { class: "num" }, "—"),
        el("td", {}, el("span", { class: "status void" }, el("span", { class: "icon" }, "—"), "BAHİS YOK")),
        el("td", { class: "num" }, "—"),
        el("td", { class: "name" }, (race.finish_order || []).slice(0, 3).join(" - "))));
      continue;
    }
    for (const bet of race.bets) {
      const profit = Number(bet.profit || 0);
      rows.push(el("tr", {},
        el("td", {}, dateTimeFmt().format(new Date(race.post_time))),
        el("td", { class: "name" }, `${race.track} ${race.race_no}.`),
        el("td", { class: "name" }, el("span", { class: "no" }, bet.number), ` ${bet.horse}`),
        el("td", { class: "num" }, Number(bet.odds_at_lock).toFixed(2)),
        el("td", { class: "num" }, money(bet.stake)),
        el("td", {}, statusChip(bet.status)),
        el("td", { class: `num ${profit > 0 ? "pos" : profit < 0 ? "neg" : ""}` },
          `${profit > 0 ? "+" : ""}${money(profit)}`),
        el("td", { class: "name" }, (race.finish_order || []).slice(0, 3).join(" - "))));
    }
  }

  host.append(el("table", {},
    el("thead", {}, el("tr", {},
      el("th", {}, "Start"),
      el("th", {}, "Koşu"),
      el("th", {}, "Tahmin"),
      el("th", { class: "num" }, "Oran"),
      el("th", { class: "num" }, "Bahis"),
      el("th", {}, "Durum"),
      el("th", { class: "num" }, "Kazanç"),
      el("th", {}, "İlk 3"))),
    el("tbody", {}, rows)));
}

/* ------------------------------------------------------------------ akış */

async function refresh() {
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state = await response.json();
    tz = state.timezone || tz;

    document.getElementById("provider-chip").textContent = `kaynak: ${state.provider}`;
    document.getElementById("lock-min").textContent = state.lock_minutes;
    document.getElementById("updated").textContent =
      `güncellendi ${timeFmt().format(new Date(state.now))}`;

    renderTiles(state.stats);
    renderCurve(state.stats.curve);
    renderUpcoming(state.upcoming, state.lock_minutes);
    renderResults(state.results);
  } catch (err) {
    document.getElementById("updated").textContent = `bağlantı hatası: ${err.message}`;
  }
}

document.getElementById("sync-btn").addEventListener("click", async (event) => {
  const btn = event.currentTarget;
  btn.disabled = true;
  btn.textContent = "senkronize ediliyor…";
  try {
    await fetch("/api/sync", { method: "POST" });
    await refresh();
  } finally {
    btn.disabled = false;
    btn.textContent = "Şimdi senkronize et";
  }
});

setInterval(() => {
  document.getElementById("clock").textContent = timeFmt().format(new Date());
  tickCountdowns();
}, 1000);

window.addEventListener("resize", () => {
  if (state) renderCurve(state.stats.curve);
});

refresh();
setInterval(refresh, REFRESH_MS);
