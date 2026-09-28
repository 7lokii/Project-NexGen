/* =========================================================
   Project NexGen — common.js
   JS partilhado por TODAS as páginas da dashboard:
   helpers, WebSocket, render do chrome global (barra superior:
   navegação + estado + preços), badges, statusbar, toasts e
   ações comuns. Cada página define `pageRender(s)`.
   ========================================================= */

let last = null;
let _prevBalance = null;

// sessão expirada / sem login -> ir para a página de login
const _fetch = window.fetch;
window.fetch = function (url, opts) {
  return _fetch(url, opts).then((r) => {
    if (r.status === 401 && !location.pathname.startsWith("/login")) {
      location.href = "/login";
      throw new Error("unauthorized");
    }
    return r;
  });
};

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v === null || v === undefined) ? "—" : Number(v).toFixed(d);
const cls = (v) => v > 0 ? "pos" : (v < 0 ? "neg" : "");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
/* hora amigável: hoje -> "14:03", ontem -> "ontem 14:03", senão "12/08 14:03"
   (sempre em hora local — os históricos do Telegram chegam em UTC) */
const _p2 = (n) => String(n).padStart(2, "0");
function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  const hhmm = _p2(d.getHours()) + ":" + _p2(d.getMinutes());
  const t0 = new Date(); t0.setHours(0, 0, 0, 0);
  const d0 = new Date(d); d0.setHours(0, 0, 0, 0);
  const days = Math.round((t0 - d0) / 86400000);
  if (days <= 0) return hhmm;
  if (days === 1) return "ontem " + hhmm;
  return _p2(d.getDate()) + "/" + _p2(d.getMonth() + 1) + " " + hhmm;
}
const tpsCell = (o) => {
  const arr = [];
  for (let i = 1; i <= 5; i++) {
    const tp = o["take_profit_" + i];
    if (tp) arr.push(tp);
  }
  return arr.length ? arr.join(" · ") : "—";
};
const lotsTotal = (t) => {
  let s = 0;
  for (let i = 1; i <= 5; i++) s += Number(t["lots_" + i] || 0);
  return s ? s : "—";
};
/* "0.03+0.03+0.03" por perna (TP1..TP5) com tooltip — em vez de só o total */
const lotsPerLeg = (t) => {
  const legs = [];
  for (let i = 1; i <= 5; i++) {
    const v = Number(t["lots_" + i] || 0);
    if (v > 0) legs.push({ leg: i, v });
  }
  if (!legs.length) return { html: "—", title: "" };
  return {
    html: legs.map(l => `<span class="cd">${l.v}</span>`).join("<span style='color:var(--muted)'>+</span>"),
    title: "Lotes por perna: " + legs.map(l => `TP${l.leg} ${l.v}`).join(" · ") + ` (total ${legs.reduce((a, l) => a + l.v, 0)})`,
  };
};
const dirLabel = (d) => d === "buy" ? "▲ compra" : d === "sell" ? "▼ venda" : (d || "—");
const TRADE_STATUS = { open: "aberta", closed: "fechada", partial: "parcial" };
const tradeStatusLabel = (st) => TRADE_STATUS[st] || st || "—";
const ddmm = (day) => { const p = String(day || "").split("-"); return p.length === 3 ? p[2] + "/" + p[1] : (day || "—"); };
let CURRENCY = "$";
const cur = (v) => CURRENCY + fmt(v);
const money = (v) => (v > 0 ? "+" : "") + cur(v);

/* ------------------------- cache de assinatura de conteúdo (render diffs)
   contentChanged(key, payload) devolve true quando o payload mudou desde a
   última chamada — permite aos renderers saltar rebuilds de DOM em snapshots
   que não mudaram (o snapshot chega ~1×/s). */
const _sigCache = {};
function contentChanged(key, payload) {
  let s;
  try { s = JSON.stringify(payload); } catch (e) { s = String(payload); }
  if (_sigCache[key] === s) return false;
  _sigCache[key] = s;
  return true;
}
function setActiveTab(id) { window._activeTab = id; }

/* ------------------------ chrome global: barra superior (fonte única)
   Renderizado UMA vez aqui — as páginas só trazem um placeholder vazio
   <div id="appChrome"></div>. A barra tem duas linhas: navegação + estado
   (badges/relógio/kill/sair) e a faixa de preços do mercado. */
const STRATEGY_MENU = [
  { id: "nova_gestao", label: "TP1..TP5 + gestão", sub: "principal · referência" },
  { id: "nova_gestao_fallback", label: "TP1..TP5 + gestão + fallback", sub: "experimental · 5 min" },
  { id: "tp2_protect", label: "Sair no TP2 + protect only", sub: "só protect do grupo" },
  { id: "auto_3", label: "Auto 3 TPs", sub: "3 pernas" },
  { id: "auto_escada_3", label: "Auto 3 TPs + escada", sub: "Fase 2 · escada + trailing" },
  { id: "auto_escada_3_hibrida", label: "Auto 3 TPs + escada + protect híbrido", sub: "paper · proteção mecânica + grupo" },
  { id: "auto_escada_5", label: "Auto 5 TPs + escada + confirmação", sub: "5 pernas + confirmação" },
];
/* ordem = ordem do DOM: PRINCIPAL, as 7 estratégias do dropdown, SISTEMA. */
const TERM_ORDER = ["principal",
  ...STRATEGY_MENU.map(s => "teste-" + s.id),
  "diagnostico", "auditoria", "help"];

function _menuItemHTML(page, label, sub, opts = {}) {
  const href = opts.href || ("/teste?s=" + String(page).replace(/^teste-/, ""));
  return `<a class="term-menu-item${opts.cls ? " " + opts.cls : ""}" data-page="${page}" href="${href}"><span class="cur">▸</span>${label}<span class="sub">${sub}</span>${opts.best ? '<span class="best"></span>' : ""}</a>`;
}

/* re-hidrata o dropdown ▚ ESTRATÉGIAS a partir de /api/strategies — o registo
   em strategies.py é a fonte única; o STRATEGY_MENU acima é só o fallback. */
async function hydrateStrategyMenu() {
  const drop = $("dropEstrategias");
  if (!drop) return;
  try {
    const reg = await fetch("/api/strategies").then(r => r.json());
    const menu = Array.isArray(reg.menu) ? reg.menu : [];
    if (!menu.length) return;
    const current = new URLSearchParams(location.search).get("s");
    drop.innerHTML = menu.map(s =>
      _menuItemHTML("teste-" + s.id, s.label, s.sub || "", { best: true }))
      .join("\n    ");
    if (current) {
      const item = drop.querySelector(`[data-page="teste-${current}"]`);
      if (item) item.classList.add("active");
    }
    if (window._wireStrategyDrop) window._wireStrategyDrop();
    const summary = $("strategyLifecycleSummary");
    if (summary) {
      summary.innerHTML = (reg.strategies || []).map(s => {
        const lc = s.lifecycle || {};
        const state = lc.state || (s.hidden ? "hidden" : "active");
        const cls = state === "retired" ? "loss" : (state === "candidate" ? "warn" : "win");
        return `<span class="strategy-lifecycle-chip ${cls}"><b>${esc(s.nome || s.id)}</b><small>${esc(state)}</small></span>`;
      }).join("");
    }
  } catch (e) { /* fallback local já renderizado */ }
}

/* ------------------------------------------- tema claro/escuro */
/* o atributo data-theme é aplicado antes do paint por um snippet no <head>
   de cada página; o common.js garante o estado aqui para páginas antigas e
   mantém o botão sincronizado. */
const THEME_KEY = "nx_theme";
function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}
function _syncThemeButton(t) {
  const b = $("btnTheme");
  if (!b) return;
  const dark = t === "dark";
  b.textContent = dark ? "☀" : "☾";
  b.title = dark ? "Tema claro" : "Tema escuro";
  b.setAttribute("aria-label", dark ? "Mudar para tema claro" : "Mudar para tema escuro");
  b.setAttribute("aria-pressed", String(dark));
}
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* cookies off */ }
  _syncThemeButton(t);
}
function toggleTheme() { applyTheme(currentTheme() === "dark" ? "light" : "dark"); }
function themeIsDark() { return currentTheme() === "dark"; }
/* --- seguir o SO ao vivo ---
   Enquanto o utilizador NÃO escolher manualmente (sem nx_theme no storage),
   o tema acompanha o prefers-color-scheme do sistema, incluindo mudanças
   ao vivo. Uma escolha explícita no botão passa a mandar (fica persistida)
   e o SO deixa de interferir. */
function hasExplicitTheme() {
  try { return localStorage.getItem(THEME_KEY) !== null; }
  catch (e) { return true; } // sem storage: conservador, não segue o SO
}
function systemTheme() {
  try { return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"; }
  catch (e) { return "light"; }
}
function applySystemTheme() {
  document.documentElement.dataset.theme = systemTheme();
  _syncThemeButton(systemTheme());
}
function followSystemTheme() {
  if (hasExplicitTheme()) return;
  applySystemTheme();
}
(function wireSystemThemeFollow() {
  try {
    const mq = matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", followSystemTheme);
  } catch (e) { /* media query API indisponível — fica com o tema do head */ }
})();
/* paleta dos gráficos canvas — cores de série (vividas) ficam iguais;
   grelha/eixo/fundo adaptam-se ao tema para leitura em ambos. */
function chartPalette() {
  return themeIsDark()
    ? { grid: "#273142", axis: "#8896ab", track: "#101828" }
    : { grid: "#e2e8f0", axis: "#94a3b8", track: "#f6f8fb" };
}

function _appChromeHTML() {
  return `
  <header class="topbar" id="topBar">
    <div class="tb-row1">
      <button id="btnMenu" class="ghost tb-burger" title="Menu" aria-label="Abrir menu">☰</button>
      <a class="tb-brand" href="/">
        <span class="tb-logo">N</span>
        <span class="tb-title">Project NexGen</span>
        <span class="tb-sub">copy-trading · execução real (demo MT5)</span>
      </a>
      <nav class="tb-nav" id="tbNav" aria-label="Navegação">
        ${_menuItemHTML("principal", "PRINCIPAL", "conta MT5 · estratégia ativa", { cls: "main", href: "/", best: true })}
        ${_menuItemHTML("segundaria", "SEGUNDA CONTA", "lane tp2 · 2ª conta demo", { href: "/segundaria" })}
        <span class="term-drop-toggle" id="dropEstrategiasToggle" role="button" tabindex="0" aria-expanded="false">▚ ESTRATÉGIAS <span class="drop-caret">▾</span></span>
        <div class="term-drop" id="dropEstrategias">
          ${STRATEGY_MENU.map(s => _menuItemHTML("teste-" + s.id, s.label, s.sub, { best: true })).join("\n    ")}
        </div>
        ${_menuItemHTML("diagnostico", "DIAGNÓSTICO", "MT5 · listener · BD", { href: "/diagnostico" })}
        ${_menuItemHTML("auditoria", "AUDITORIA", "journal · replay · forward", { href: "/auditoria" })}
        ${_menuItemHTML("help", "HELP", "estratégias · conceitos", { href: "/help" })}
      </nav>
      <div class="tb-state">
        <span class="hdr-badges" id="hdrBadges">
          <span id="modeBadge" class="badge auto">auto</span>
          <span id="strategyBadge" class="badge violet" style="display:none" title="Estratégia ativa da conta principal"></span>
          <span id="execBadge" class="badge mt5-dry" style="display:none"></span>
          <span id="marketBadge" class="badge manual">simulação</span>
          <span id="killBadge" class="badge paused" style="display:none">PAUSADO</span>
        </span>
        <span class="online-users" id="onlineUsers" title="Utilizadores com a dashboard aberta">—</span>
        <span class="clock" id="clock">—</span>
        <button id="btnTheme" class="ghost tb-theme" title="Tema escuro" aria-label="Mudar para tema escuro" aria-pressed="false">☾</button>
        <button id="btnKill" class="danger">Kill switch</button>
        <button id="btnLogout" class="ghost" style="display:none" title="Terminar sessão">Sair</button>
      </div>
    </div>
  </header>`;
}

/* página ativa a partir do URL */
function _currentPageId() {
  const p = location.pathname;
  if (p.startsWith("/teste")) return "teste-" + (new URLSearchParams(location.search).get("s") || "nova_gestao");
  if (p.startsWith("/segundaria")) return "segundaria";
  if (p.startsWith("/diagnostico")) return "diagnostico";
  if (p.startsWith("/auditoria")) return "auditoria";
  if (p.startsWith("/help")) return "help";
  return "principal";
}

/* vista de lane a partir do URL (?lane=tp2|main) — comum a todas as páginas
   que partilham o chrome (o index.html tem a própria LANE_VIEW com o mesmo
   critério) */
function _laneViewId() {
  const v = new URLSearchParams(location.search).get("lane") || "";
  return v === "tp2" || v === "main" ? v : "";
}

/* injeta o chrome no placeholder (as páginas só trazem <div id="appChrome">) */
(() => {
  const host = $("appChrome");
  if (host && !host.childElementCount) host.innerHTML = _appChromeHTML();
})();

/* ------------------------------------------- WebSocket */
function connect() {
  const scheme = location.protocol === "https:" ? "wss://" : "ws://";
  const ws = new WebSocket(`${scheme}${location.host}/ws`);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "snapshot") { render(m.data); }
    if (m.type === "notification") {
      if (typeof prependFeed === "function") prependFeed(m);
      if (m.kind === "revived") toast("🔄 " + m.text.replace(/^🔄\s*/, ""), "info");
    }
  };
  ws.onclose = () => {
    fetch("/api/status").catch(() => {});
    setTimeout(connect, 2000);
  };
}

/* ---------------------------------------------------------------- render */

/* a barra de tabs cola por baixo da barra superior — a altura é medida ao
   vivo e exposta em --hdr-h (muda com o zoom do navegador) */
function updateHdrH() {
  const h = document.querySelector(".topbar") || document.querySelector("header");
  document.documentElement.style.setProperty("--hdr-h", (h ? h.offsetHeight : 96) + "px");
}
window.addEventListener("resize", updateHdrH);
updateHdrH();

/* estratégia ativa + corrida paper (aviso se um teste ganha à principal) */
const STRATEGY_NAMES = { nova: "TP1..TP5", nova_gestao: "TP1..TP5 + gestão", nova_gestao_fallback: "TP1..TP5 + gestão + fallback", tp2_protect: "Sair no TP2 + protect only", auto_3: "Auto 3 TPs", auto_5: "Auto 5 TPs", auto_escada_3: "Auto 3 TPs + escada", auto_escada_3_hibrida: "Auto 3 TPs + escada + protect híbrido", auto_escada_5: "Auto 5 TPs + escada + confirmação" };

/* períodos do bloco "desde o início": todo o histórico (0), formato atual
   (2026-07-28) e só o mês corrente (dinâmico) */
const _ALL_FIRST = (() => {
  const d = new Date();
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-01";
})();
const _ALL_MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];
let _allPeriod = "0";
function _allPeriodLabel(since) {
  if (!since || since === "0") return "no início do grupo";
  if (since === "2026-07-28") return "a 28/07 (formato atual)";
  return "desde " + since.slice(8) + "/" + since.slice(5, 7);
}
function _allPeriodLabelShort(since) {
  if (!since) return "todo o histórico";
  if (since === "2026-07-28") return "formato atual (28/07)";
  return "só " + _ALL_MONTHS[Number(since.slice(5, 7)) - 1] + " (" + since.slice(8) + "/" + since.slice(5, 7) + ")";
}
function _allPeriodOptions(selected) {
  const o = (v, t) => `<option value="${v}"${v === selected ? " selected" : ""}>${t}</option>`;
  return o("0", "todo o histórico (abr–hoje)") +
    o("2026-07-28", "só o formato atual (desde 28/07)") +
    o(_ALL_FIRST, _allPeriodLabelShort(_ALL_FIRST));
}
/* parâmetros de execução da estratégia ativa (max_legs/exit_at_tp/lote/gestão) */
function stratParamsText(p) {
  const parts = [];
  parts.push(p.max_legs ? `máx ${p.max_legs} pernas` : "TP1..TP5 (todas)");
  if (p.exit_at_tp) parts.push(`fecha tudo no TP${p.exit_at_tp}`);
  if (p.lots_override) parts.push(`lote fixo ${p.lots_override}`);
  parts.push(p.manage ? "gestão ON" : "gestão OFF");
  if (p.defensive) parts.push("🛡 modo defensivo");
  return parts.join(" · ");
}
let _prevStrategy = null;
function renderStrategyBadge(sid, s) {
  const b = $("strategyBadge");
  if (!b) return;
  if (!sid) { b.style.display = "none"; return; }
  const name = STRATEGY_NAMES[sid] || sid;
  const p = (s.config || {}).strategy_params || {};
  const pt = stratParamsText(p);
  const orphan = !!p.orphan;
  b.classList.add("stack");
  b.classList.toggle("orphan", orphan);
  b.classList.remove("flash");
  b.innerHTML = orphan
    ? `<span class="badge-main">⚠️ ${esc(sid || "?")} (config órfã)</span><span class="badge-sub">a correr com ${esc(p.nome || "a principal")} — ${esc(pt)}</span>`
    : `<span class="badge-main">${esc(name)}</span><span class="badge-sub">${esc(pt)}</span>`;
  b.title = orphan
    ? `⚠️ A estratégia "${esc(sid)}" da config já não existe no registo — a conta principal está a correr com ${esc(p.nome || "nova_gestao")} (fallback). Corrige a config (promove outra estratégia).`
    : `Estratégia ativa da conta principal — execução real com: ${pt}`;
  b.style.display = "";
  if (_prevStrategy !== null && _prevStrategy !== sid) {
    b.classList.add("flash");
    toast((orphan ? "⚠️ Config órfã: " : "Estratégia ativa mudou: ") + name + " — " + pt, orphan ? "err" : "ok");
  }
  _prevStrategy = sid;
}

function renderExecBadge(exec) {
  const b = $("execBadge");
  if (!exec.mode) { b.style.display = "none"; return; }
  const lanes = (exec.lanes || []).filter(l => l.enabled !== false);
  if (lanes.length > 1) {
    const modeLabel = exec.mode === "mt5-dry" ? "MT5 dry-run"
      : exec.mode === "mt5-real" ? "MT5 real" : "simulação";
    const parts = lanes.map(l => {
      const acct = l.account || {};
      const login = acct.login || "";
      return `${login || l.lane_id} · ${l.strategy_id}`;
    });
    b.textContent = `${modeLabel} · ${parts.join(" + ")}`.trim();
    b.title = lanes.map(l => {
      const acct = l.account || {};
      const bal = `${fmt(acct.balance ?? 0)} ${acct.currency || ""}`.trim();
      return `lane ${l.lane_id} · ${l.strategy_id} (${l.strategy_name || ""}) · conta ${acct.login || "?"} · saldo ${bal}`;
    }).join("\n");
    b.className = "badge " + (exec.mode === "mt5-dry" ? "mt5-dry"
      : exec.mode === "mt5-real" ? "mt5-real" : "manual");
    b.style.display = "";
    return;
  }
  const acct = exec.account || {};
  const bal = `${fmt(acct.balance ?? 0)} ${acct.currency || ""}`.trim();
  if (exec.mode === "mt5-real" && exec.connected) {
    if (!exec.trade_allowed) {
      b.textContent = `⚠️ MT5 real · ${acct.login || ""} · AutoTrading OFF`;
      b.title = `O terminal MT5 está com a negociação algorítmica DESLIGADA — liga o botão "Algo Trading" na barra de ferramentas.\nconta ${acct.login} · saldo ${bal} · equity ${fmt(acct.equity ?? 0)} ${acct.currency || ""}`;
      b.className = "badge mt5-off";
    } else {
      b.textContent = `MT5 real · ${acct.login || ""} · ${bal}`.trim();
      b.title = `conta ${acct.login} · equity ${fmt(acct.equity ?? 0)} ${acct.currency || ""} · alavancagem 1:${acct.leverage ?? "?"}`;
      b.className = "badge mt5-real";
    }
  } else if (exec.mode === "mt5-dry" && exec.connected) {
    b.textContent = `MT5 dry-run · ${acct.login || ""} · ${bal}`.trim();
    b.title = `conta ${acct.login} · equity ${fmt(acct.equity ?? 0)} ${acct.currency || ""} · ordens registadas, nunca enviadas`;
    b.className = "badge mt5-dry";
  } else if (exec.mode.startsWith("mt5")) {
    b.textContent = "⚠️ MT5 sem terminal";
    b.title = "Abre o terminal MT5 e faz login para o bot conseguir executar.";
    b.className = "badge mt5-off";
  } else {
    b.textContent = "simulação";
    b.className = "badge manual";
  }
  b.style.display = "";
}

/* cores das estratégias na corrida paralela / páginas de teste / HELP */
const RACE_COLORS = { nova: "#3b82f6", nova_gestao: "#6d28d9", nova_gestao_fallback: "#16a34a", tp2_protect: "#0d9488", auto_3: "#0891b2", auto_5: "#64748b", auto_escada_3: "#65a30d", auto_escada_3_hibrida: "#9333ea", auto_escada_5: "#d97706" };

/* risco % e saldo inicial do sim das páginas de teste (estudos) */
function testeSimQS() {
  let q = "";
  const rEl = $("testeRisk");
  const r = rEl ? rEl.value : "";
  if (r) q += "&risk_pct=" + r;
  const bEl = $("testeBalance");
  const b = parseFloat(bEl ? bEl.value : "1000");
  q += "&balance=" + (b > 0 ? b : 1000);
  return q;
}
/* ---------------------------------------------------- status bar / health */
function ensureGlobalHealthBanner() {
  if ($("healthBanner")) return $("healthBanner");
  const anchor = document.querySelector(".wrap header, .wrap .statusbar");
  if (!anchor) return null;
  const banner = document.createElement("div");
  banner.id = "healthBanner";
  banner.className = "health-banner";
  banner.setAttribute("role", "status");
  banner.setAttribute("aria-live", "polite");
  banner.innerHTML = '<span class="health-icon" aria-hidden="true">●</span><div><b id="healthTitle">A ligar…</b><span id="healthDetail" class="muted">A verificar serviços…</span></div><span id="healthUpdated" class="health-updated">—</span>';
  anchor.insertAdjacentElement("afterend", banner);
  return banner;
}

function renderHealthBanner(s) {
  const banner = ensureGlobalHealthBanner();
  if (!banner) return;
  const ex = s.execution || {}, lis = s.listener || {};
  const issues = [];
  if (ex.mode && ex.mode.startsWith("mt5") && !ex.connected) issues.push("MT5 offline");
  if (ex.mode && ex.mode.startsWith("mt5") && ex.connected && !ex.trade_allowed) issues.push("AutoTrading desligado");
  if (lis && lis.alive === false && !lis.need_code) issues.push("listener Telegram offline");
  if (s.config && s.config.kill_switch) issues.push("kill switch ativo");
  const critical = issues.some(x => x.includes("offline") || x.includes("desligado"));
  banner.classList.toggle("healthy", issues.length === 0);
  banner.classList.toggle("attention", issues.length > 0 && !critical);
  banner.classList.toggle("critical", critical);
  const title = $("healthTitle"), detail = $("healthDetail"), updated = $("healthUpdated");
  if (title) title.textContent = issues.length ? (critical ? "Atenção necessária" : "Operação pausada") : "Operação normal";
  if (detail) detail.textContent = issues.length ? issues.join(" · ") : "MT5 · AutoTrading · listener Telegram verificados";
  if (updated) updated.textContent = "atualizado " + new Date().toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function renderStatusBar(s) {
  const dot = $("stDot"), txt = $("stText");
  const c = s.config, ex = s.execution || {};
  const kill = !!c.kill_switch;
  if (kill) { dot.className = "dot bad"; txt.innerHTML = "<b style='color:var(--red)'>PAUSADO</b> · kill switch ativo — nada abre até reativares"; $("stLast").textContent = ""; return; }
  {
    const wait = (s.signals || []).find(x => x.status === "awaiting_edit");
    if (wait) {
      dot.className = "dot warn";
      txt.innerHTML = `Modo <b style='color:var(--green)'>auto</b> · sinal <b>${esc(wait.symbol || "")}</b> na janela de edição — executa em <b class='cd' id='stCount'>${Math.max(0, Math.ceil((new Date(wait.edit_deadline) - Date.now()) / 1000))}s</b>`;
    } else {
      const hasOpen = s.account.open_trades > 0;
      dot.className = "dot " + (hasOpen ? "warn" : "ok");
      txt.innerHTML = `Modo <b style='color:var(--green)'>auto</b> · ${ex.connected && ex.trade_allowed ? "execução real ligada" : "sem execução (ver badge MT5)"}${hasOpen ? ` · <b>${s.account.open_trades}</b> trade(s) aberta(s) a ser geridas` : " · a aguardar próximo sinal"}`;
    }
  }
  const mgmt = (s.signals || []).find(x => x.status === "management");
  $("stLast").textContent = mgmt ? "Última do grupo: " + when(mgmt.received_at) + " " + String(mgmt.raw_text || "").replace(/\*\*/g, "").replace(/\s+/g, " ").trim().slice(0, 70) : "";
}

const _persistToasts = {};
const _toastSnooze = {};
function persistentToast(key, msg, kind) {
  if (_toastSnooze[key] && Date.now() - _toastSnooze[key] < 5 * 60 * 1000) return;
  const box = document.getElementById("toasts") || (() => {
    const el = document.createElement("div"); el.id = "toasts"; el.setAttribute("role", "region"); el.setAttribute("aria-label", "Avisos"); document.body.appendChild(el); return el;
  })();
  let el = _persistToasts[key];
  if (!el) {
    el = document.createElement("div");
    el.className = "toast " + kind;
    const x = document.createElement("button");
    x.className = "toast-x"; x.textContent = "×"; x.title = "Dispensar (volta se o problema persistir)";
    x.onclick = (ev) => { ev.stopPropagation(); _toastSnooze[key] = Date.now(); clearPersistentToast(key); };
    el.appendChild(x);
    el.appendChild(document.createElement("span"));
    box.appendChild(el);
    _persistToasts[key] = el;
  }
  el.className = "toast " + kind;
  el.lastChild.textContent = msg;
}
function clearPersistentToast(key) {
  const t = _persistToasts[key];
  if (t && t.parentNode) t.remove();
  delete _persistToasts[key];
}
let _lastTradeAllowed = null;
let _lastConnected = null;
function watchAutoTrading(exec) {
  if (!exec.mode || !exec.mode.startsWith("mt5")) {
    clearPersistentToast("mt5");
    _lastTradeAllowed = null; _lastConnected = null;
    return;
  }
  const on = !!exec.trade_allowed;
  const conn = !!exec.connected;
  if (!conn) {
    persistentToast("mt5", "⚠️ MT5 offline · abre o terminal e faz login.", "err");
  } else if (!on) {
    persistentToast("mt5", "⚠️ AutoTrading OFF · liga o Algo Trading no MT5.", "err");
  } else {
    clearPersistentToast("mt5");
  }
  if (_lastConnected !== null && conn !== _lastConnected && conn && on) { toast("✅ MT5 reconectado", "ok"); }
  if (_lastTradeAllowed !== null && on !== _lastTradeAllowed && on) { toast("✅ AutoTrading ligado — trades reais ativas", "ok"); }
  _lastTradeAllowed = on;
  _lastConnected = conn;
}

let _lastListenerAlive = null;
function watchListener(lis) {
  if (!lis) { _lastListenerAlive = null; clearPersistentToast("listener"); return; }
  const alive = !!lis.alive;
  if (!alive && !lis.need_code) {
    persistentToast("listener", "⚠️ Telegram offline · watchdog vai tentar reiniciar.", "err");
  } else if (alive) {
    clearPersistentToast("listener");
  }
  if (_lastListenerAlive !== null && alive !== _lastListenerAlive) {
    if (!alive) { /* silêncio: o toast persistente já avisa */ }
    else { toast("✅ Listener do Telegram online", "ok"); }
  }
  _lastListenerAlive = alive;
}
function renderCodePrompt(lis) {
  const box = document.getElementById("toasts") || (() => {
    const el = document.createElement("div"); el.id = "toasts"; el.setAttribute("role", "region"); el.setAttribute("aria-label", "Avisos"); document.body.appendChild(el); return el;
  })();
  let el = document.getElementById("codeToast");
  if (!(lis && lis.need_code)) {
    if (el) el.remove();
    return;
  }
  if (el) return;
  el = document.createElement("div");
  el.id = "codeToast";
  el.className = "toast err";
  el.style.padding = "10px 14px";
  el.innerHTML = `<b>🔢 Código do Telegram necessário</b><br>
    <span style="font-size:12px">O Telegram enviou um código de login para a app — introduz-o para o listener voltar a ligar.</span><br>
    <input id="codeInput" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="12345"
      style="margin-top:8px;width:110px;background:var(--panel2);border:1px solid var(--blue);color:var(--text);border-radius:6px;padding:6px 10px;font-size:14px;letter-spacing:.2em;outline:none">
    <button id="codeSend" class="small green" style="padding:6px 12px">Enviar</button>`;
  box.appendChild(el);
  const input = el.querySelector("#codeInput");
  const send = el.querySelector("#codeSend");
  const submit = async () => {
    const code = input.value.trim();
    if (!code) { input.focus(); return; }
    send.disabled = true;
    const r = await fetch("/api/listener/code", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) });
    if (r.ok) { toast("🔢 Código enviado — o listener vai ligar...", "ok"); el.remove(); }
    else { send.disabled = false; toast("Erro ao enviar o código", "err"); }
  };
  send.onclick = submit;
  input.onkeydown = (e) => { if (e.key === "Enter") submit(); };
  setTimeout(() => input.focus(), 100);
}

/* ------------------------------------------------------------- actions */
function toast(msg, kind = "info") {
  const box = document.getElementById("toasts") || (() => {
    const el = document.createElement("div"); el.id = "toasts"; el.setAttribute("role", "region"); el.setAttribute("aria-label", "Avisos"); document.body.appendChild(el); return el;
  })();
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  t.onclick = () => t.remove();
  box.appendChild(t);
  setTimeout(() => { if (t.parentNode) t.remove(); }, 5000);
}

async function act(url, opts = {}) {
  let r;
  try {
    r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(opts) });
  } catch (e) {
    toast("Falha de ligação ao servidor", "err");
    throw e;
  }
  const data = await r.json();
  if (data && data.error) toast(String(data.error), "err");
  else if (data && data.action === "blocked") toast("Sinal bloqueado: " + (data.reasons || []).join("; "), "err");
  else if (data && data.action === "execution_failed") toast("Não executado: " + (data.error || "ordem recusada"), "err");
  else if (data && data.action === "executed") toast("✅ Trade aberta no MT5", "ok");
  return data;
}

$("btnKill").onclick = async () => {
  const on = last && last.config.kill_switch;
  if (!on && !confirm("Pausar o trading? Posições abertas continuam a ser geridas, mas nada de novo é aberto.")) return;
  const d = await act("/api/killswitch", { enabled: !on });
  if (!d.error) toast(on ? "Trading reativado" : "Trading PAUSADO (kill switch)", on ? "ok" : "err");
};
$("btnLogout").onclick = async () => {
  await fetch("/api/logout", { method: "POST" });
  location.href = "/login";
};

/* ------------------------------------------------------- chrome global */
function relativeAge(now) {
  if (!now) return "atualização pendente";
  const sec = Math.max(0, Math.round((Date.now() - now) / 1000));
  return sec < 2 ? "atualizado agora" : `atualizado há ${sec}s`;
}

function initGlobalOriginFilter() {
  const el = $("globalOriginFilter");
  if (!el) return;
  try { el.value = localStorage.getItem("nexgenOriginFilter") || "all"; } catch (e) {}
  el.onchange = () => {
    try { localStorage.setItem("nexgenOriginFilter", el.value); } catch (e) {}
    document.body.dataset.originFilter = el.value;
    if (typeof window.onOriginFilterChanged === "function") window.onOriginFilterChanged(el.value);
  };
  document.body.dataset.originFilter = el.value;
}

let _lastSnapshotAt = null;
function render(s) {
  last = s;
  _lastSnapshotAt = Date.now();
  const updated = $("pageUpdatedAt");
  if (updated) updated.textContent = relativeAge(_lastSnapshotAt);
  const a = s.account, c = s.config;
  CURRENCY = (a.currency || "USD") === "EUR" ? "€" : (a.currency || "USD") + " ";
  updateHdrH();

  const ou = $("onlineUsers");
  if (ou) {
    const users = (s.online_users || []).map(u => u.user);
    const n = users.length;
    if (n === 0) { ou.textContent = "—"; ou.title = "Ninguém com a dashboard aberta"; }
    else if (n === 1) { ou.textContent = users[0]; ou.title = users[0] + " ligado"; }
    else {
      ou.textContent = users.join(" · ");
      ou.title = n + " utilizadores ligados: " + users.join(", ");
    }
  }

  const badge = $("modeBadge");
  badge.textContent = c.kill_switch ? "pausado" : "auto";
  badge.className = "badge " + (c.kill_switch ? "paused" : "auto");
  $("killBadge").style.display = c.kill_switch ? "" : "none";
  $("btnKill").textContent = c.kill_switch ? "Reativar" : "Kill switch";
  $("btnKill").className = c.kill_switch ? "green" : "danger";

  const mb = $("marketBadge");
  const execMode = (s.execution || {}).mode;
  if (execMode === "mt5-real" && (s.execution || {}).connected) {
    mb.textContent = "execução real (demo MT5)";
    mb.className = "badge mt5-real";
  } else if (execMode === "mt5-dry" && (s.execution || {}).connected) {
    mb.textContent = "dry-run (demo MT5)";
    mb.className = "badge mt5-dry";
  } else {
    mb.textContent = (s.market && s.market.real_data) ? "preços reais" : "simulação";
    mb.className = "badge " + ((s.market && s.market.real_data) ? "auto" : "manual");
  }
  renderExecBadge(s.execution || {});
  renderStrategyBadge(c.strategy, s);
  const acctF = $("acctFooter");
  if (acctF) {
    const ex = s.execution || {}, ac = ex.account || {};
    // vista de lane (/segundaria → ?lane=tp2): o rodapé mostra a conta da
    // lane, não a da principal
    const laneQ = new URLSearchParams(location.search).get("lane");
    const laneAcct = (laneQ === "tp2" || laneQ === "main")
      ? ((s.lanes || []).find(l => l.lane_id === laneQ) || {}).account
      : null;
    const acShow = laneAcct || ac;
    acctF.textContent = acShow.login || "—";
    const srv = $("acctServerFooter");
    if (srv && acShow.server) srv.textContent = acShow.server;
  }
  watchAutoTrading(s.execution || {});
  watchListener(s.listener);
  renderCodePrompt(s.listener);

  const risk = $("riskSlider");
  if (risk) {
    /* slider por lane: a posição é a BASE da lane (risk_pct:main / risk_pct:tp2) */
    const laneV = _laneViewId();
    const baseByLane = c.risk_pct_by_lane || {};
    const riskBase = laneV
      ? (baseByLane[laneV] != null ? baseByLane[laneV] : c.risk_pct)
      : c.risk_pct;
    if (String(risk.value) !== String(riskBase)) risk.value = riskBase;
    /* na vista de lane o LABEL mostra o risco EFETIVO (pós-drawdown da lane),
       não a base global — o valor que a conta realmente usa */
    const laneEff = laneV
      ? ((s.lanes || []).find(l => l.lane_id === laneV) || {}).effective_risk_pct
      : null;
    if (laneEff != null) {
      $("riskVal").textContent = fmt(laneEff, 1) + "% · efetivo";
      risk.title = `Base da lane ${laneV}: ${fmt(riskBase, 1)}% · risco efetivo (pós-drawdown): ${fmt(laneEff, 1)}%`;
    } else {
      $("riskVal").textContent = fmt(riskBase, 1) + "%";
      risk.title = "Risco por trade (base da conta principal)";
    }
    $("limitsLbl").textContent = `máx ${c.max_positions} posições`;
    const dl = $("dailyLossSlider"), dv = $("dailyLossVal");
    if (dl) {
      dl.value = c.daily_loss_limit_pct;
      if (dv) dv.textContent = Number(c.daily_loss_limit_pct) === 0 ? "off" : fmt(c.daily_loss_limit_pct, 1) + "%";
    }
    const ew = $("editWinSlider"), ev = $("editWinVal");
    if (ew) { ew.value = c.edit_window_seconds; if (ev) ev.textContent = fmt(c.edit_window_seconds, 0) + "s"; }
    const gt = $("entryGuardTolSlider"), gtv = $("entryGuardTolVal");
    if (gt) {
      gt.value = c.entry_guard_tolerance ?? 0.5;
      if (gtv) gtv.textContent = fmt(c.entry_guard_tolerance ?? 0.5, 2);
    }
  }
  const td = $("testeDays");
  if (td && document.activeElement !== td) td.value = String(c.teste_days ?? 30);
  const prh = $("paperRaceHours");
  if (prh && document.activeElement !== prh) prh.value = String(c.paper_race_hours ?? 24);
  const tap = $("testeAllPeriod");
  if (tap) {
    if (!tap.options.length) tap.innerHTML = _allPeriodOptions(_allPeriod);
    if (document.activeElement !== tap) {
      _allPeriod = String(c.all_period ?? _allPeriod ?? "0");
      tap.value = _allPeriod;
    }
  }
  const er = $("testeRisk"), eb = $("testeBalance");
  if (er && document.activeElement !== er) er.value = String(c.edge_risk ?? "");
  if (eb && document.activeElement !== eb) eb.value = String(c.edge_bal ?? "") || "1000";

  renderStatusBar(s);
  renderHealthBanner(s);
  if ($("pageUpdatedAt")) $("pageUpdatedAt").textContent = relativeAge(_lastSnapshotAt);
  $("btnLogout").style.display = (s.auth && s.auth.enabled) ? "" : "none";

  // cada página desenha o seu conteúdo no snapshot
  if (typeof pageRender === "function") pageRender(s);
}

/* --------------------------------------------------- navegação (terminal) */
let termIndex = 0;

function termItems() {
  return Array.from(document.querySelectorAll(".term-menu-item"));
}

function setTermIndex(i, focus = false) {
  termIndex = (i + TERM_ORDER.length) % TERM_ORDER.length;
  termItems().forEach((b, k) => {
    const page = TERM_ORDER[k];
    b.classList.toggle("active", k === termIndex);
    if (k === termIndex && focus) b.focus({ preventScroll: true });
  });
  const drop = $("dropEstrategias");
  const tgl = $("dropEstrategiasToggle");
  const items = termItems();
  if (focus && drop && tgl && items[termIndex] && drop.contains(items[termIndex])) {
    drop.classList.add("open");
    tgl.classList.add("open");
  }
}

/* dropdown ▚ ESTRATÉGIAS: toggle explícito no clique/teclado, fechado por
   defeito em cada página e fechado ao clicar fora. Painel position:fixed
   posicionado a partir do toggle (getBoundingClientRect). */
const dropToggle = $("dropEstrategiasToggle");
if (dropToggle) {
  const drop = $("dropEstrategias");
  function positionDrop() {
    if (!dropToggle || !drop || !drop.classList.contains("open")) return;
    const tr = dropToggle.getBoundingClientRect();
    const dw = drop.offsetWidth || 320;
    let left = tr.left;
    if (left + dw > window.innerWidth - 8) left = Math.max(8, window.innerWidth - dw - 8);
    const dh = drop.offsetHeight || 320;
    const top = Math.min(tr.bottom + 6, Math.max(8, window.innerHeight - dh - 12));
    drop.style.left = left + "px";
    drop.style.top = top + "px";
  }
  window._positionStrategyDrop = positionDrop;
  const setOpen = (open) => {
    drop.classList.toggle("open", open);
    dropToggle.classList.toggle("open", open);
    dropToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) positionDrop();
  };
  dropToggle.onclick = (e) => {
    e.stopPropagation();
    setOpen(!drop.classList.contains("open"));
  };
  dropToggle.onkeydown = (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    e.stopPropagation();
    setOpen(!drop.classList.contains("open"));
  };
  setOpen(false);
  document.addEventListener("click", (e) => {
    if (e.target !== dropToggle && !dropToggle.contains(e.target)
        && !drop.contains(e.target)) setOpen(false);
  });
  window.addEventListener("resize", () => positionDrop());
  window.addEventListener("scroll", () => positionDrop(), true);
  window._wireStrategyDrop = () => { if (drop.classList.contains("open")) positionDrop(); };
}

/* marca a estratégia vencedora (histórico completo) no menu de simulação. */
const BEST_PAGE = { manage: "principal" };
async function markBestStrategy() {
  try {
    const r = await fetch("/api/analysis/compare?group=Golden%20Moves&days=0").then(res => res.json());
    const win = (r.compare || {}).winner;
    if (!win || win === "tie") return;
    termItems().forEach(b => b.classList.remove("best-strategy"));
    const page = BEST_PAGE[win];
    if (!page) return;
    const item = termItems().find(b => b.dataset.page === page);
    if (item) {
      item.classList.add("best-strategy");
      const best = item.querySelector(".best");
      if (best) best.textContent = "✓ melhor";
    }
  } catch (e) { /* silencioso */ }
}
markBestStrategy();
hydrateStrategyMenu();

/* menu móvel: hambúrguer abre/fecha a navegação; clicar fora fecha */
const btnMenu = $("btnMenu");
if (btnMenu) btnMenu.onclick = (e) => {
  e.stopPropagation();
  const nav = $("tbNav");
  if (nav) nav.classList.toggle("open");
};

/* alternar tema claro/escuro (persistido em localStorage) */
const btnTheme = $("btnTheme");
if (btnTheme) {
  btnTheme.onclick = (e) => { e.stopPropagation(); toggleTheme(); };
  // sem escolha explícita, garante o tema do SO no arranque (não grava);
  // com escolha, só sincroniza o ícone com o que o <head> já aplicou
  followSystemTheme();
  _syncThemeButton(currentTheme());
}

// setas ↑↓ + Enter no menu
window.addEventListener("keydown", (e) => {
  const nav = document.getElementById("tbNav");
  if (e.key === "Escape") {
    if (nav) nav.classList.remove("open");
    const drop = document.getElementById("dropEstrategias");
    if (drop) drop.classList.remove("open");
    return;
  }
  if (!nav || nav.offsetParent === null) return;
  if (e.key === "ArrowDown") { e.preventDefault(); setTermIndex(termIndex + 1, true); }
  else if (e.key === "ArrowUp") { e.preventDefault(); setTermIndex(termIndex - 1, true); }
  else if (e.key === "Enter") {
    const item = termItems()[termIndex];
    if (item) { e.preventDefault(); location.href = item.getAttribute("href") || (item.dataset.page === "principal" ? "/" : (item.dataset.page === "segundaria" ? "/segundaria" : "/" + item.dataset.page.replace("teste-", "teste?s="))); }
  }
});
document.addEventListener("click", (e) => {
  const nav = $("tbNav");
  if (nav && nav.classList.contains("open") && !nav.contains(e.target) && e.target !== btnMenu) {
    nav.classList.remove("open");
  }
});

// a página atual começa marcada como ativa (derivada do URL)
setTermIndex(Math.max(0, TERM_ORDER.indexOf(_currentPageId())));

/* ------------------------------------------------------------------ misc */
setInterval(() => { const cl = $("clock"); if (cl) cl.textContent = new Date().toLocaleTimeString("pt-PT"); }, 1000);
setInterval(() => {
  const el = $("stCount");
  if (!el) return;
  const n = parseInt(el.textContent, 10);
  if (n > 0) el.textContent = (n - 1) + "s";
}, 1000);

function setDashboardView(view) {
  const operator = view !== "analyst";
  document.body.classList.toggle("dashboard-operator", operator);
  const op = $("viewOperator"), an = $("viewAnalyst");
  if (op) { op.classList.toggle("active", operator); op.setAttribute("aria-pressed", operator ? "true" : "false"); }
  if (an) { an.classList.toggle("active", !operator); an.setAttribute("aria-pressed", !operator ? "true" : "false"); }
  try { localStorage.setItem("nexgenDashboardView", operator ? "operator" : "analyst"); } catch (e) { /* ignore */ }
}
function initDashboardView() {
  if (!$("viewOperator")) return;
  let view = "operator";
  try { view = localStorage.getItem("nexgenDashboardView") || "operator"; } catch (e) { /* ignore */ }
  $("viewOperator").onclick = () => setDashboardView("operator");
  $("viewAnalyst").onclick = () => setDashboardView("analyst");
  setDashboardView(view);
}
setInterval(() => {
  if (_lastSnapshotAt && $("pageUpdatedAt")) $("pageUpdatedAt").textContent = relativeAge(_lastSnapshotAt);
}, 1000);
window.addEventListener("DOMContentLoaded", () => {
  initGlobalOriginFilter();
  initDashboardView();
  connect();
});
