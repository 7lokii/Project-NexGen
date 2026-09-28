/* Project NexGen — bridge WhatsApp (Baileys).
 *
 * Mantém uma sessão WhatsApp Web (pareada 1x por QR code, persistida em
 * whatsapp/session/) e expõe um mini-servidor HTTP em 127.0.0.1:8765 que o
 * servidor Python usa para publicar resultados de trades num grupo:
 *
 *   POST /send    {jid, text}   -> envia a mensagem para o grupo/chat
 *   GET  /status                -> {connected, paired, phone, qr, last_error}
 *   GET  /qr                    -> {qr, dataUrl}  (QR atual, PNG base64)
 *   GET  /groups                -> {groups: [{jid, name}]}
 *
 * O bridge só escuta em loopback (nunca exposto — o Caddy não faz proxy para
 * ele). Falhas de envio devolvem {ok:false, error} e nunca derrubam o
 * processo; a ligação religa sozinha no evento connection.update.
 *
 * Setup (1x, na VPS):
 *   cd whatsapp && npm install && node bridge.js
 * Pareia com o QR (Diagnóstico > WhatsApp) e define WHATSAPP_GROUP_JID no .env.
 */

const path = require("path");
const http = require("http");
const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
} = require("@whiskeysockets/baileys");
const qrcodeTerminal = require("qrcode-terminal");
const QRCode = require("qrcode");
const pino = require("pino");

const PORT = parseInt(process.env.WHATSAPP_BRIDGE_PORT || "8765", 10);
const SESSION_DIR = process.env.WHATSAPP_SESSION_DIR ||
  path.join(__dirname, "session");

const state = {
  connected: false,
  paired: false,
  phone: null,
  qr: null,          // string crua do QR (para dataUrl)
  last_error: null,
  connecting: false,
  started_at: null,  // arranque do processo (uptime no Diagnóstico)
  sent_count: 0,     // mensagens enviadas com sucesso
  last_send_at: null,// ISO da última mensagem enviada
  last_send_jid: null,
};

let sock = null;

/* ---------------------------------------------------------------- Baileys */

// O Baileys 6.7+ espera um logger compatível com pino (chama `logger.child`);
// um objeto mudo simples rebentava com "logger.child is not a function".
// Nível "error" mantém os erros reais no bridge.log sem o ruído info.
const logger = pino({ level: "error" });

async function connect() {
  if (state.connecting) return;
  state.connecting = true;
  try {
    const { state: authState, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
    const { version } = await fetchLatestBaileysVersion();
    sock = makeWASocket({
      version,
      auth: authState,
      printQRInTerminal: false,
      logger,
      markOnlineOnConnect: false, // não deixar "online" permanente no telemóvel
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
      const { connection, lastDisconnect, qr } = update;
      if (qr) {
        state.qr = qr;
        state.paired = false;
        state.connected = false;
        qrcodeTerminal.generate(qr, { small: true });
        console.log(
          "[whatsapp] QR gerado — abre o telemóvel: WhatsApp > Dispositivos ligados > Ligar dispositivo"
        );
      }
      if (connection === "open") {
        state.connected = true;
        state.paired = true;
        state.qr = null;
        state.last_error = null;
        const me = sock && sock.user;
        state.phone = me ? String(me.id || "").split(":")[0] : null;
        console.log("[whatsapp] ligado como", state.phone || "(desconhecido)");
      } else if (connection === "close") {
        const code = lastDisconnect && lastDisconnect.error &&
          lastDisconnect.error.output && lastDisconnect.error.output.statusCode;
        state.connected = false;
        if (code === DisconnectReason.loggedOut) {
          // Sessão terminada pelo utilizador — requer re-pareamento.
          state.paired = false;
          state.phone = null;
          state.last_error = "logged_out";
          console.log(
            "[whatsapp] sessão terminada (logged out) — apaga whatsapp/session/ e volta a parear"
          );
        } else {
          state.last_error = code != null ? String(code) : "connection_closed";
          console.log("[whatsapp] ligação fechada (código", code + ") — a religar em 3s...");
          setTimeout(connect, 3000);
        }
      }
    });
  } catch (err) {
    state.last_error = String(err && err.message || err);
    console.error("[whatsapp] erro ao ligar:", state.last_error);
    setTimeout(connect, 5000);
  } finally {
    state.connecting = false;
  }
}

/* --------------------------------------------------------------- HTTP util */

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    req.on("data", (chunk) => {
      chunks.push(chunk);
      total += chunk.length;
      if (total > 1e6) {
        reject(new Error("body demasiado grande"));
        req.destroy();
      }
    });
    req.on("end", () => {
      try {
        // Decodificar o corpo COMPLETO uma vez (UTF-8): concatenar strings
        // chunk a chunk corrompia carateres multibyte (emoji/acentos)
        // partidos ao meio de um TCP chunk.
        const raw = Buffer.concat(chunks).toString("utf8");
        resolve(raw.trim() ? JSON.parse(raw) : {});
      } catch (err) {
        reject(err);
      }
    });
    req.on("error", reject);
  });
}

function json(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function waitConnected(timeoutMs) {
  // Espera a socket voltar a estar "open" (o bridge religa sozinho em ~3s
  // quando o WhatsApp cai). Sem isto um envio que chega na janela em que a
  // ligação está a religar falha logo (503) — e perdia-se a mensagem.
  return new Promise((resolve, reject) => {
    if (state.connected && sock) return resolve(true);
    const started = Date.now();
    const timer = setInterval(() => {
      if (state.connected && sock) {
        clearInterval(timer);
        resolve(true);
      } else if (Date.now() - started > timeoutMs) {
        clearInterval(timer);
        reject(new Error("bridge não ligado ao WhatsApp"));
      }
    }, 250);
  });
}

function sendWithTimeout(jid, text) {
  // Espera a socket (janela de religação) e envia com timeout de 15s;
  // se a socket morrer a meio do envio ("Connection Closed"), tenta uma
  // vez no próximo ciclo em vez de devolver 500.
  return new Promise((resolve, reject) => {
    (async () => {
      try {
        await waitConnected(8000);
      } catch (err) {
        reject(err);
        return;
      }
      const attempt = () => new Promise((res, rej) => {
        const timer = setTimeout(
          () => rej(new Error("timeout ao enviar (15s)")),
          15000
        );
        sock
          .sendMessage(jid, { text })
          .then((v) => { clearTimeout(timer); res(v); })
          .catch((e) => { clearTimeout(timer); rej(e); });
      });
      try {
        resolve(await attempt());
      } catch (err) {
        const msg = String(err && err.message || "");
        if (msg.includes("Connection Closed") || msg === "Connection Closed") {
          try {
            await waitConnected(5000);
            resolve(await attempt());
          } catch (err2) {
            reject(err2);
          }
        } else {
          reject(err);
        }
      }
    })();
  });
}

/* ------------------------------------------------------------------ HTTP */

const server = http.createServer(async (req, res) => {
  const url = (req.url || "").split("?")[0];
  try {
    if (req.method === "GET" && url === "/status") {
      json(res, 200, state);
      return;
    }
    if (req.method === "GET" && url === "/qr") {
      if (!state.qr) {
        json(res, 200, { qr: null, dataUrl: null, paired: state.paired });
        return;
      }
      const dataUrl = await QRCode.toDataURL(state.qr, { width: 260, margin: 1 });
      json(res, 200, { qr: state.qr, dataUrl, paired: false });
      return;
    }
    if (req.method === "GET" && url === "/groups") {
      if (!state.connected || !sock) {
        json(res, 200, { groups: [], connected: false });
        return;
      }
      const meta = await sock.groupFetchAllParticipating();
      const groups = Object.values(meta || {}).map((g) => ({
        jid: g.id,
        name: g.subject || "(sem nome)",
      }));
      groups.sort((a, b) => a.name.localeCompare(b.name));
      json(res, 200, { groups, connected: true });
      return;
    }
    if (req.method === "POST" && url === "/send") {
      const body = await readBody(req);
      const jid = String(body.jid || "").trim();
      const text = String(body.text || "").trim();
      if (!jid || !text) {
        json(res, 400, { ok: false, error: "jid e text são obrigatórios" });
        return;
      }
      // 503 instantâneo quando a socket está a religar — em vez disso
      // espera até ~8s pela janela em que o WhatsApp volta a estar ligado.
      await sendWithTimeout(jid, text);
      state.sent_count += 1;
      state.last_send_at = new Date().toISOString();
      state.last_send_jid = jid;
      console.log("[whatsapp] enviado para", jid, "—", JSON.stringify(text));
      json(res, 200, { ok: true });
      return;
    }
    if (req.method === "POST" && url === "/reconnect") {
      // Religação pedida pela dashboard (sem reiniciar o processo): fechar
      // a socket faz o connection.update 'close' religar em ~3s. Se não há
      // socket ativa, liga diretamente.
      if (state.connected && sock) {
        try {
          sock.end();
        } catch (e) {
          console.error("[whatsapp] erro ao fechar socket no reconnect:", e);
        }
      } else if (!state.connecting) {
        connect();
      }
      console.log("[whatsapp] reconnect pedido pela dashboard");
      json(res, 200, { ok: true });
      return;
    }
    json(res, 404, { ok: false, error: "endpoint desconhecido" });
  } catch (err) {
    const msg = String(err && err.message || err);
    console.error("[whatsapp] erro HTTP:", msg);
    json(res, 500, { ok: false, error: msg });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  state.started_at = new Date().toISOString();
  console.log("[whatsapp] bridge a escutar em http://127.0.0.1:" + PORT);
  connect();
});

process.on("unhandledRejection", (err) => {
  console.error("[whatsapp] unhandledRejection:", err);
});
process.on("uncaughtException", (err) => {
  console.error("[whatsapp] uncaughtException:", err);
});
