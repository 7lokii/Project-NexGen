/* =========================================================
   Project NexGen — common.js
   JS partilhado por TODAS as páginas da dashboard:
   helpers, áudio, boot sequence, WebSocket, chrome global
   (header badges, statusbar, terminais, toasts) e ações comuns.
   Cada página define `pageRender(s)` para o seu conteúdo.
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

/* ------------------------------------------- som sintetizado (Web Audio) */
let audioCtx = null;
function ensureAudio() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    return audioCtx;
  } catch (e) { return null; }
}
function beep(freq, dur, type, delay, gain) {
  const ctx = ensureAudio();
  if (!ctx) return;
  try {
    const t0 = ctx.currentTime + (delay || 0);
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = type || "square";
    osc.frequency.setValueAtTime(freq, t0);
    g.gain.setValueAtTime(gain || 0.045, t0);
    g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
    osc.connect(g).connect(ctx.destination);
    osc.start(t0);
    osc.stop(t0 + dur + 0.02);
  } catch (e) { /* sem áudio disponível */ }
}
/* clique de teclado curto (linhas do boot) */
function tick() {
  const ctx = ensureAudio();
  if (!ctx) return;
  try {
    const t0 = ctx.currentTime;
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = "square";
    osc.frequency.setValueAtTime(1700 + Math.random() * 500, t0);
    g.gain.setValueAtTime(0.022, t0);
    g.gain.exponentialRampToValueAtTime(0.001, t0 + 0.035);
    osc.connect(g).connect(ctx.destination);
    osc.start(t0);
    osc.stop(t0 + 0.05);
  } catch (e) {}
}
/* sons por tipo de notificação */
function soundTradeOpen() { beep(660, .09, "square"); beep(990, .12, "square", .1); }
function soundTradeClose() { beep(520, .1, "square"); }
function soundRisk() { beep(170, .18, "sawtooth"); beep(120, .22, "square", .16); }
function soundMgmt() { beep(440, .08, "square"); beep(440, .08, "square", .12); }
function soundBlip() { beep(880, .06, "square", 0, .028); }
const NOTIF_SOUNDS = { trade_open: soundTradeOpen, trade_close: soundTradeClose, risk: soundRisk, management: soundMgmt, summary: soundBlip, system: soundBlip };
function playNotifSound(kind) { const fn = NOTIF_SOUNDS[kind]; if (fn) fn(); }
/* primeiro gesto do utilizador desbloqueia o áudio (autoplay policy) */
document.addEventListener("pointerdown", () => { try { ensureAudio(); } catch (e) {} });

/* ------------------------------------------- boot sequence da dashboard */
const dashBoot = $("dashBoot");
const dashBootText = $("dashBootText");
let _bootBaseDone = false;
let _bootSnapshotDone = false;
let _pendingComplete = null;
function dashBootLine(txt) { dashBootText.textContent += txt + "\n"; tick(); }
function finishDashBoot() {
  // esconde sempre: com snapshot (linhas reais) ou no fallback de ~3s
  // (sem snapshot — ex.: WebSocket bloqueado) o boot tem de sair do ecrã
  if (dashBoot) {
    setTimeout(() => dashBoot.classList.add("hide"), 400);
    setTimeout(() => { dashBoot.style.display = "none"; }, 950);
  }
}
function completeDashBoot(s) {
  if (_bootSnapshotDone) return;
  const build = () => {
    _bootSnapshotDone = true;
    const exec = s.execution || {};
    if (exec.mode && exec.mode.startsWith("mt5")) {
      dashBootLine(exec.connected ? "> MT5 BRIDGE ........... OK" : "> MT5 BRIDGE ........... OFFLINE");
      if (exec.connected) dashBootLine(exec.trade_allowed ? "> ALGO TRADING ......... OK" : "> ALGO TRADING ......... OFF");
      dashBootLine("> MODO ................. " + (exec.mode === "mt5-real" ? "REAL (DEMO)" : exec.mode.toUpperCase()));
    } else {
      dashBootLine("> SIM ENGINE ........... OK");
    }
    dashBootLine("> DASHBOARD ONLINE .....");
    finishDashBoot();
  };
  if (_bootBaseDone) build();
  else _pendingComplete = build;
}
function runDashBoot() {
  if (!dashBoot) return;
  // o boot só corre UMA vez por sessão (login → index): ao trocar de página
  // (teste/diagnóstico/help) o overlay some logo e o conteúdo aparece direto
  let bootDone = false;
  try { bootDone = sessionStorage.getItem("nexgenBootDone") === "1"; } catch (err) { /* ignore */ }
  if (bootDone) {
    dashBoot.style.display = "none";
    _bootSnapshotDone = true;  // o snapshot do WS não precisa de linhas de boot
    _bootBaseDone = true;
    return;
  }
  try { sessionStorage.setItem("nexgenBootDone", "1"); } catch (err) { /* ignore */ }
  const lines = ["> INIT PROJECT NEXGEN ...", "> ESTABLISHING UPLINK ...", "> TELEGRAM LISTENER .... OK"];
  let i = 0;
  const step = () => {
    if (i < lines.length) { dashBootLine(lines[i]); i++; setTimeout(step, 320); }
    else {
      _bootBaseDone = true;
      if (_pendingComplete) { _pendingComplete(); _pendingComplete = null; }
      else setTimeout(() => { if (!_bootSnapshotDone) { dashBootLine("> DASHBOARD ONLINE ....."); finishDashBoot(); } }, 3000);
    }
  };
  step();
}

function connect() {
  // HTTPS (túnel/VPS) exige wss:// — ws:// num site https é bloqueado
  // pelo navegador (mixed content) e o snapshot nunca chega
  const scheme = location.protocol === "https:" ? "wss://" : "ws://";
  const ws = new WebSocket(`${scheme}${location.host}/ws`);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "snapshot") { render(m.data); completeDashBoot(m.data); }
    if (m.type === "notification") {
      if (typeof prependFeed === "function") prependFeed(m);
      if (m.kind === "revived") toast("🔄 " + m.text.replace(/^🔄\s*/, ""), "info");
    }
  };
  ws.onclose = () => {
    // se a sessão expirou, o servidor fecha o WS — confirmar e ir para o login
    fetch("/api/status").catch(() => {});
    setTimeout(connect, 2000);
  };
}

/* ---------------------------------------------------------------- render */

/* a barra de tabs cola por baixo do header — a altura do header muda com o
   zoom do navegador (e quando os elementos envolvem), por isso mede-se ao
   vivo e expõe-se em --hdr-h; o zoom dispara resize no browser */
function updateHdrH() {
  const h = document.querySelector("header");
  document.documentElement.style.setProperty("--hdr-h", (h ? h.offsetHeight : 52) + "px");
}
window.addEventListener("resize", updateHdrH);
updateHdrH();

/* estratégia ativa + corrida paper (aviso se um teste ganha à principal) */
const STRATEGY_NAMES = { nova: "TP1..TP5", nova_gestao: "TP1..TP5 + gestão", tp2: "TP1→BE + sair no TP2", nova_gestao_3: "TP1..TP3 + gestão", tp2_gestao: "Sair no TP2 + gestão", tp2_protect: "Sair no TP2 + protect only", auto_2: "Auto 2 TPs", auto_3: "Auto 3 TPs", auto_5: "Auto 5 TPs", auto_escada_3: "Auto 3 TPs + escada", auto_escada_3_hibrida: "Auto 3 TPs + escada + protect híbrido", auto_escada_5: "Auto 5 TPs + escada + confirmação" };

/* períodos do bloco "desde o início": todo o histórico (0), formato atual
   (2026-07-28, data da 1ª mensagem do formato atual) e só o mês corrente
   (1º dia do mês — dinâmico para não ficar preso a agosto) */
const _ALL_FIRST = (() => {
  const d = new Date();
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-01";
})();
const _ALL_MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];
let _allPeriod = "0"; /* valor do seletor de período (since ou "0") — partilhado pelas páginas de teste, persistido na config */
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
    : `<span class="badge-main">♟ ${esc(name)}</span><span class="badge-sub">${esc(pt)}</span>`;
  b.title = orphan
    ? `⚠️ A estratégia "${esc(sid)}" da config já não existe no registo — a conta principal está a correr com ${esc(p.nome || "nova_gestao")} (fallback). Corrige a config (promove outra estratégia).`
    : `Estratégia ativa da conta principal — execução real com: ${pt}`;
  b.style.display = "";
  if (_prevStrategy !== null && _prevStrategy !== sid) {
    b.classList.add("flash");
    toast((orphan ? "⚠️ Config órfã: " : "♟ Estratégia ativa mudou: ") + name + " — " + pt, orphan ? "err" : "ok");
  }
  _prevStrategy = sid;
}

function renderExecBadge(exec) {
  const b = $("execBadge");
  if (!exec.mode) { b.style.display = "none"; return; }
  const acct = exec.account || {};
  const bal = `${fmt(acct.balance ?? 0)} ${acct.currency || ""}`.trim();
  if (exec.mode === "mt5-real" && exec.connected) {
    if (!exec.trade_allowed) {
      b.textContent = `⚠️ MT5 real · ${acct.login || ""} · AutoTrading OFF`;
      b.title = `O terminal MT5 está com a negociação algorítmica DESLIGADA — liga o botão "Algo Trading" na barra de ferramentas.\nconta ${acct.login} · saldo ${bal} · equity ${fmt(acct.equity ?? 0)} ${acct.currency || ""}`;
      b.className = "badge mt5-off";
    } else {
      b.textContent = `🔌 MT5 real · ${acct.login || ""} · ${bal}`.trim();
      b.title = `conta ${acct.login} · equity ${fmt(acct.equity ?? 0)} ${acct.currency || ""} · alavancagem 1:${acct.leverage ?? "?"}`;
      b.className = "badge mt5-real";
    }
  } else if (exec.mode === "mt5-dry" && exec.connected) {
    b.textContent = `🧪 MT5 dry-run · ${acct.login || ""} · ${bal}`.trim();
    b.title = `conta ${acct.login} · equity ${fmt(acct.equity ?? 0)} ${acct.currency || ""} · ordens registadas, nunca enviadas`;
    b.className = "badge mt5-dry";
  } else if (exec.mode.startsWith("mt5")) {
    b.textContent = "⚠️ MT5 sem terminal";
    b.title = "Abre o terminal MT5 e faz login para o bot conseguir executar.";
    b.className = "badge mt5-off";
  } else {
    b.textContent = "🧪 simulação";
    b.className = "badge manual";
  }
  b.style.display = "";
}

const TICKER_LABEL = { XAUUSD: "GOLD", XAGUSD: "SILVER" };
/* cores das estratégias na corrida paralela / páginas de teste / HELP */
const RACE_COLORS = { nova: "#3b82f6", nova_gestao: "#9A4FE8", tp2: "#f5a524", nova_gestao_3: "#22d3ee", tp2_gestao: "#f97316", tp2_protect: "#10b981", auto_2: "#fb7185", auto_3: "#22d3ee", auto_5: "#64748b", auto_escada_3: "#a3e635", auto_escada_3_hibrida: "#c084fc", auto_escada_5: "#f59e0b" };

/* risco % e saldo inicial do sim das páginas de teste (estudos). O saldo
   default é 1000 (o saldo próprio da conta paper da estratégia) — nunca o
   saldo da conta MT5. */
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
/* cor do preço conforme a variação vs o valor anterior (verde = subiu,
   vermelho = desceu, neutro = igual/1ª leitura) — movimento ao vivo */
let _tickPrev = {};
function renderPriceStrip(market) {
  const strip = $("priceStrip");
  if (!strip) return;
  const sources = market.sources || {};
  const prices = market.prices || {};
  const syms = Object.keys(prices);
  if (!syms.length) { strip.innerHTML = ""; return; }
  strip.innerHTML = syms.map(sym => {
    const src = sources[sym] || "sim";
    const v = prices[sym];
    const label = src === "real" ? "preço real" : "simulado";
    const cls = src === "real" ? "real" : "sim";
    const show = TICKER_LABEL[sym] || sym;
    const val = v === null || v === undefined ? "—" :
      (v >= 100 ? fmt(v) : fmt(v, 4));
    let dir = "";
    if (v !== null && v !== undefined) {
      const prev = _tickPrev[sym];
      if (prev !== undefined) dir = v > prev ? " up" : (v < prev ? " down" : "");
      _tickPrev[sym] = v;
    }
    // variação % do dia (chartPreviousClose do Yahoo) — só quando há valor
    const chg = (market.changes || {})[sym];
    let chgH = "";
    if (chg !== undefined && chg !== null) {
      const chgN = Number(chg);
      const chgCls = chgN > 0 ? "up" : chgN < 0 ? "down" : "";
      // barrinha da magnitude (2% = largura total) com tooltip a explicar
      const mag = Math.max(4, Math.round(Math.min(Math.abs(chgN) / 2.0, 1) * 100));
      const bCol = chgN > 0 ? "var(--green)" : chgN < 0 ? "var(--red)" : "var(--muted)";
      const tip = `Variação do dia: ${chgN > 0 ? "+" : ""}${chgN.toFixed(2)}% vs o fecho de ontem (chartPreviousClose)`;
      chgH = `<span class="pchg ${chgCls}">${chgN > 0 ? "+" : ""}${chgN.toFixed(2)}%</span>` +
        `<span class="pbar ${chgCls}" title="${tip}"><i style="width:${mag}%;background:${bCol}"></i></span>`;
    }
    const tip = `${esc(sym)} (${show}) · ${label}${chg ? ` · variação do dia ${chg > 0 ? "+" : ""}${Number(chg).toFixed(2)}% (barrinha vs fecho de ontem)` : ""}`;
    return `<span class="price-chip ${cls}" title="${tip}">${esc(show)} <span class="pr"><span class="p${dir}" data-text="${esc(val)}">${val}</span>${chgH}</span></span>`;
  }).join("");
}

/* ---------------------------------------------------- status bar / auto preview */
function renderStatusBar(s) {
  const dot = $("stDot"), txt = $("stText");
  const c = s.config, ex = s.execution || {};
  const kill = !!c.kill_switch;
  if (kill) { dot.className = "dot bad"; txt.innerHTML = "<b style='color:var(--red)'>PAUSADO</b> · kill switch ativo — nada abre até reativares"; $("stLast").textContent = ""; return; }
  if (c.mode === "auto") {
    const wait = (s.signals || []).find(x => x.status === "awaiting_edit");
    if (wait) {
      dot.className = "dot warn";
      txt.innerHTML = `Modo <b style='color:var(--green)'>auto</b> · sinal <b>${esc(wait.symbol || "")}</b> na janela de edição — executa em <b class='cd' id='stCount'>${Math.max(0, Math.ceil((new Date(wait.edit_deadline) - Date.now()) / 1000))}s</b>`;
    } else {
      const hasOpen = s.account.open_trades > 0;
      dot.className = "dot " + (hasOpen ? "warn" : "ok");
      txt.innerHTML = `Modo <b style='color:var(--green)'>auto</b> · ${ex.connected && ex.trade_allowed ? "execução real ligada" : "sem execução (ver badge MT5)"}${hasOpen ? ` · <b>${s.account.open_trades}</b> trade(s) aberta(s) a ser geridas` : " · a aguardar próximo sinal"}`;
    }
  } else {
    dot.className = "dot ok";
    txt.innerHTML = `Modo <b style='color:var(--blue)'>manual</b> · sinais ficam <b>pendentes</b> — aprova-os na tab Sinais`;
  }
  const mgmt = (s.signals || []).find(x => x.status === "management");
  $("stLast").textContent = mgmt ? "Última do grupo: " + when(mgmt.received_at) + " " + String(mgmt.raw_text || "").replace(/\*\*/g, "").replace(/\s+/g, " ").trim().slice(0, 70) : "";
}

const _persistToasts = {};
const _toastSnooze = {};
function persistentToast(key, msg, kind) {
  // dispensado há menos de 5 min? o utilizador já viu — volta só se persistir
  if (_toastSnooze[key] && Date.now() - _toastSnooze[key] < 5 * 60 * 1000) return;
  const box = document.getElementById("toasts") || (() => {
    const el = document.createElement("div"); el.id = "toasts"; document.body.appendChild(el); return el;
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
    persistentToast("mt5", "⚠️ MT5 sem terminal — a ligação ao MetaTrader caiu. Abre o terminal e faz login, senão nada executa.", "err");
  } else if (!on) {
    persistentToast("mt5", "⚠️ AutoTrading DESLIGADO no terminal MT5 — liga o botão \"Algo Trading\" na barra de ferramentas, senão as ordens serão recusadas.", "err");
  } else {
    clearPersistentToast("mt5");
  }
  if (_lastConnected !== null && conn !== _lastConnected && conn && on) { toast("✅ MT5 reconectado", "ok"); soundTradeOpen(); }
  if (_lastTradeAllowed !== null && on !== _lastTradeAllowed && on) { toast("✅ AutoTrading ligado — trades reais ativas", "ok"); soundTradeOpen(); }
  if (_lastConnected !== null && conn !== _lastConnected && !conn) soundRisk();
  if (_lastTradeAllowed !== null && on !== _lastTradeAllowed && !on) soundRisk();
  _lastTradeAllowed = on;
  _lastConnected = conn;
}

let _lastListenerAlive = null;
function watchListener(lis) {
  if (!lis) { _lastListenerAlive = null; clearPersistentToast("listener"); return; }
  const alive = !!lis.alive;
  if (!alive && !lis.need_code) {
    persistentToast("listener", "⚠️ Listener do Telegram OFFLINE — o servidor vai tentar reiniciá-lo automaticamente. Se precisar de código de login, aparece aqui.", "err");
  } else if (alive) {
    clearPersistentToast("listener");
  }
  if (_lastListenerAlive !== null && alive !== _lastListenerAlive) {
    if (!alive) soundRisk();
    else { toast("✅ Listener do Telegram online", "ok"); soundTradeOpen(); }
  }
  _lastListenerAlive = alive;
}
function renderCodePrompt(lis) {
  const box = document.getElementById("toasts") || (() => {
    const el = document.createElement("div"); el.id = "toasts"; document.body.appendChild(el); return el;
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
    const el = document.createElement("div"); el.id = "toasts"; document.body.appendChild(el); return el;
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
  // novo login → volta a mostrar o boot (a animação é por sessão)
  try { sessionStorage.removeItem("nexgenBootDone"); } catch (err) { /* ignore */ }
  location.href = "/login";
};

/* ------------------------------------------------------- chrome global */
function render(s) {
  last = s;
  const a = s.account, c = s.config;
  CURRENCY = (a.currency || "USD") === "EUR" ? "€" : (a.currency || "USD") + " ";
  updateHdrH();  // mantém o sticky das tabs alinhado com o header atual

  const ou = $("onlineUsers");
  if (ou) {
    const users = (s.online_users || []).map(u => u.user);
    const n = users.length;
    if (n === 0) { ou.textContent = "👥 0"; ou.title = "Ninguém com a dashboard aberta"; }
    else if (n === 1) { ou.textContent = "👥 " + users[0]; ou.title = users[0] + " ligado"; }
    else {
      ou.textContent = "👥 " + users.join(" · ");
      ou.title = n + " utilizadores ligados: " + users.join(", ");
    }
  }

  const badge = $("modeBadge");
  badge.textContent = c.kill_switch ? "pausado" : (c.mode === "auto" ? "auto" : "manual");
  badge.className = "badge " + (c.kill_switch ? "paused" : c.mode);
  $("killBadge").style.display = c.kill_switch ? "" : "none";
  $("btnKill").textContent = c.kill_switch ? "▶ Reativar" : "🛑 Kill switch";
  $("btnKill").className = c.kill_switch ? "green" : "danger";

  const mb = $("marketBadge");
  const execMode = (s.execution || {}).mode;
  if (execMode === "mt5-real" && (s.execution || {}).connected) {
    mb.textContent = "⚡ execução real (demo MT5)";
    mb.className = "badge mt5-real";
  } else if (execMode === "mt5-dry" && (s.execution || {}).connected) {
    mb.textContent = "🧪 dry-run (demo MT5)";
    mb.className = "badge mt5-dry";
  } else {
    mb.textContent = (s.market && s.market.real_data) ? "📈 preços reais" : "🎲 simulação";
    mb.className = "badge " + ((s.market && s.market.real_data) ? "auto" : "manual");
  }
  renderExecBadge(s.execution || {});
  renderStrategyBadge(c.strategy, s);
  const acctF = $("acctFooter");
  if (acctF) {
    const ex = s.execution || {}, ac = ex.account || {};
    acctF.textContent = ac.login || "—";
    const srv = $("acctServerFooter");
    if (srv && ac.server) srv.textContent = ac.server;
  }
  renderPriceStrip(s.market || {});
  watchAutoTrading(s.execution || {});
  watchListener(s.listener);
  renderCodePrompt(s.listener);

  const risk = $("riskSlider");
  if (risk) {
    if (risk.value !== String(c.risk_pct)) risk.value = c.risk_pct;
    $("riskVal").textContent = fmt(c.risk_pct, 1) + "%";
    $("limitsLbl").textContent = `perda diária ${fmt(c.daily_loss_limit_pct, 1)}% · máx ${c.max_positions} posições`;
    const ew = $("editWinSlider"), ev = $("editWinVal");
    if (ew) { ew.value = c.edit_window_seconds; if (ev) ev.textContent = fmt(c.edit_window_seconds, 0) + "s"; }
  }
  const td = $("testeDays");
  if (td && document.activeElement !== td) td.value = String(c.teste_days ?? 30);
  const prh = $("paperRaceHours");
  if (prh && document.activeElement !== prh) prh.value = String(c.paper_race_hours ?? 24);
  const tap = $("testeAllPeriod");
  if (tap) {
    if (!tap.options.length) tap.innerHTML = _allPeriodOptions(_allPeriod); // 1ª vez
    if (document.activeElement !== tap) {
      _allPeriod = String(c.all_period ?? _allPeriod ?? "0");
      tap.value = _allPeriod;
    }
  }
  const er = $("testeRisk"), eb = $("testeBalance");
  if (er && document.activeElement !== er) er.value = String(c.edge_risk ?? "");
  if (eb && document.activeElement !== eb) eb.value = String(c.edge_bal ?? "") || "1000";

  renderStatusBar(s);
  $("btnLogout").style.display = (s.auth && s.auth.enabled) ? "" : "none";

  // cada página desenha o seu conteúdo no snapshot
  if (typeof pageRender === "function") pageRender(s);
}

/* --------------------------------------------------- navegação (terminal) */
/* ordem = ordem do DOM: PRINCIPAL, as 5 estratégias do dropdown, SISTEMA.
   Consolidação 19/08: tp2/tp2_gestao/nova_gestao_3/auto_2 removidos (perdem),
   nova/auto_5 ocultas do menu (contrafactual/comparação no backtest+paper). */
const TERM_ORDER = ["principal",
  "teste-nova_gestao", "teste-tp2_protect", "teste-auto_3",
  "teste-auto_escada_3", "teste-auto_escada_3_hibrida", "teste-auto_escada_5",
  "diagnostico", "help"];
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
  // se o item ativo viver dentro do dropdown de estratégias, abre-o (a
  // navegação por teclado não fica "presa" em itens colapsados)
  const drop = $("dropEstrategias");
  const tgl = $("dropEstrategiasToggle");
  const items = termItems();
  if (drop && tgl && items[termIndex] && drop.contains(items[termIndex])) {
    drop.classList.add("open");
    tgl.classList.add("open");
  }
}

/* dropdown ▚ ESTRATÉGIAS: toggle no clique, estado persistido em
   localStorage (default ABERTO — o utilizador vê todas as estratégias),
   fecha ao clicar fora. O painel é position:fixed posicionado a partir do
   toggle (getBoundingClientRect) — o nav tem overflow-x:auto (zoom) e
   cortava o dropdown; fixed escapa ao clipping e funciona no overlay móvel. */
const dropToggle = $("dropEstrategiasToggle");
if (dropToggle) {
  const drop = $("dropEstrategias");
  function positionDrop() {
    if (!dropToggle || !drop || !drop.classList.contains("open")) return;
    const tr = dropToggle.getBoundingClientRect();
    const dw = drop.offsetWidth || 300;
    let left = tr.left;
    if (left + dw > window.innerWidth - 8) left = Math.max(8, window.innerWidth - dw - 8);
    drop.style.left = left + "px";
    drop.style.top = (tr.bottom + 4) + "px";
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
    try { localStorage.setItem("dropEstrategiasOpen", drop.classList.contains("open") ? "1" : ""); } catch (err) { /* ignore */ }
  };
  try {
    const saved = localStorage.getItem("dropEstrategiasOpen");
    setOpen(saved === null ? true : saved === "1");
  } catch (err) { setOpen(true); }
  document.addEventListener("click", (e) => {
    if (e.target !== dropToggle && !dropToggle.contains(e.target)
        && !drop.contains(e.target)) setOpen(false);
  });
  window.addEventListener("resize", () => positionDrop());
  // re-posiciona ao abrir o menu móvel (☰) e ao navegar por teclado
  window.addEventListener("scroll", () => positionDrop(), true);
}

/* marca a estratégia vencedora (histórico completo) no menu de simulação.
   O winner do /api/analysis/compare é um RÓTULO ("new"/"manage"/"tp2") —
   não um id de estratégia: "manage" = TP1..TP5 + gestão = a principal
   (nova_gestao), "new" = a antiga nova (fora do menu) e "tp2" = a tp2
   (HIDDEN) — por isso só o "manage" tem página para marcar (o PRINCIPAL). */
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
      item.querySelector(".best").textContent = "✓ melhor";
    }
  } catch (e) { /* silencioso: sem marcação se a API falhar */ }
}
markBestStrategy();

/* menu móvel: hambúrguers no header abrem/fecham; clicar fora fecha */
const btnMenu = $("btnMenu");
if (btnMenu) btnMenu.onclick = (e) => {
  e.stopPropagation();
  $("termMenu").classList.toggle("open");
  // o dropdown (fixed) precisa de re-posicionar quando o menu móvel abre
  requestAnimationFrame(() => { if (window._positionStrategyDrop) window._positionStrategyDrop(); });
};
const btnMenuStatus = $("btnMenuStatus");
if (btnMenuStatus) btnMenuStatus.onclick = (e) => { e.stopPropagation(); $("termRight").classList.toggle("open"); };
/* colapsar o bloco STATE do menu (‹ ›) — persistido no localStorage */
const btnTermCollapse = $("btnTermCollapse");
if (btnTermCollapse) btnTermCollapse.onclick = (e) => {
  e.stopPropagation();
  const collapsed = $("termSide").classList.toggle("collapsed");
  btnTermCollapse.textContent = collapsed ? "›" : "‹";
  try { localStorage.setItem("termSideCollapsed", collapsed ? "1" : ""); } catch (err) { /* ignore */ }
};
(() => {
  let collapsed = false;
  try { collapsed = localStorage.getItem("termSideCollapsed") === "1"; } catch (err) { /* ignore */ }
  if (collapsed && $("termSide")) {
    $("termSide").classList.add("collapsed");
    if (btnTermCollapse) btnTermCollapse.textContent = "›";
  }
})();
/* colapsar o terminal direito (conta/sessão) — persistido no localStorage */
const btnTermRightCollapse = $("btnTermRightCollapse");
if (btnTermRightCollapse) btnTermRightCollapse.onclick = (e) => {
  e.stopPropagation();
  const collapsed = $("termRight").classList.toggle("collapsed");
  document.body.classList.toggle("term-r-collapsed", collapsed);
  btnTermRightCollapse.textContent = collapsed ? "›" : "‹";
  try { localStorage.setItem("termRightCollapsed", collapsed ? "1" : ""); } catch (err) { /* ignore */ }
};
(() => {
  let collapsed = false;
  try { collapsed = localStorage.getItem("termRightCollapsed") === "1"; } catch (err) { /* ignore */ }
  if (collapsed && $("termRight")) {
    $("termRight").classList.add("collapsed");
    document.body.classList.add("term-r-collapsed");
    if (btnTermRightCollapse) btnTermRightCollapse.textContent = "›";
  }
})();
document.addEventListener("click", (e) => {
  const mm = $("termMenu");
  if (mm && mm.classList.contains("open") && !mm.contains(e.target) && e.target !== btnMenu) {
    mm.classList.remove("open");
  }
  const tr = $("termRight");
  if (tr && tr.classList.contains("open") && !tr.contains(e.target) && e.target !== btnMenuStatus) {
    tr.classList.remove("open");
  }
});

// setas ↑↓ + Enter no menu; também funcionam em qualquer lado da página
// quando o menu está visível
window.addEventListener("keydown", (e) => {
  const menu = document.getElementById("termMenu");
  if (e.key === "Escape") {
    if (menu) menu.classList.remove("open");
    const tr = document.getElementById("termRight");
    if (tr) tr.classList.remove("open");
    return;
  }
  if (!menu || menu.offsetParent === null) return;
  if (e.key === "ArrowDown") { e.preventDefault(); setTermIndex(termIndex + 1, true); }
  else if (e.key === "ArrowUp") { e.preventDefault(); setTermIndex(termIndex - 1, true); }
  else if (e.key === "Enter") {
    const item = termItems()[termIndex];
    if (item) { e.preventDefault(); location.href = item.getAttribute("href") || (item.dataset.page === "principal" ? "/" : "/" + item.dataset.page.replace("teste-", "teste?s=")); }
  }
});

setTermIndex(0);

/* ------------------------------------------------------------------ misc */
setInterval(() => { const cl = $("clock"); if (cl) cl.textContent = new Date().toLocaleTimeString("pt-PT"); }, 1000);
setInterval(() => {
  const el = $("stCount");
  if (!el) return;
  const n = parseInt(el.textContent, 10);
  if (n > 0) el.textContent = (n - 1) + "s";
}, 1000);
// arranque: só quando o DOM está pronto — o script inline de cada página já
// definiu o `pageRender` (conteúdo específico) antes do primeiro snapshot
window.addEventListener("DOMContentLoaded", () => {
  runDashBoot();
  connect();
});
