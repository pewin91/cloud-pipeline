"""
🤖 AI Pipeline Bot — Versión Cloud (Railway)
Modo AUTO: monitorea pewintest@gmail.com buscando emails con fotos.
Modo MANUAL: usuario envía foto por Telegram directamente.
Dashboard: https://<tu-dominio>.railway.app/

Comandos Telegram:
  start      → Activar pipeline
  stop       → Desactivar pipeline
  auto       → Modo automático (Gmail)
  manual     → Modo manual (Telegram foto)
  both       → Ambos modos activos
  status     → Ver estado
  help       → Lista de comandos
"""
import os, time, threading, json, logging, asyncio, base64
import imaplib, email as email_lib
from collections import Counter, deque
from flask import Flask, jsonify, request as flask_request
import urllib.request, urllib.parse, urllib.error

# ─── Log buffer (para el dashboard) ──────────────────────
LOG_BUFFER = deque(maxlen=300)

class _BufferHandler(logging.Handler):
    COLORS = {"INFO": "info", "WARNING": "warn", "ERROR": "error", "DEBUG": "debug"}
    def emit(self, record):
        LOG_BUFFER.append({
            "t":     time.strftime("%H:%M:%S"),
            "level": record.levelname,
            "cls":   self.COLORS.get(record.levelname, "info"),
            "msg":   record.getMessage(),
        })

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-5s │ %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("pipeline")
_bh = _BufferHandler()
_bh.setLevel(logging.INFO)
logger.addHandler(_bh)

# ─── Configuración ────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN",        "")
CHAT_IDS       = [int(x) for x in os.getenv("CHAT_IDS", "7396447561,8578286357").split(",")]
GEMINI_KEY     = os.getenv("GEMINI_API_KEY",    "")
OPENAI_KEY     = os.getenv("OPENAI_API_KEY",    "")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL",      "gemini-2.5-flash")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL",      "gpt-4o")
CLAUDE_MODEL   = os.getenv("CLAUDE_MODEL",      "claude-sonnet-4-20250514")
RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
PORT           = int(os.getenv("PORT", 8080))
GMAIL_USER     = os.getenv("GMAIL_USER",         "pewintest@gmail.com")
GMAIL_PASS     = os.getenv("GMAIL_APP_PASSWORD",  "")
GMAIL_POLL_INT = int(os.getenv("GMAIL_POLL_INT",  "8"))
IMAP_HOST      = "imap.gmail.com"
SELF_PING_INT  = 270
OCR_TIMEOUT    = 25

# ─── Prompt de alineación personalizado ──────────────────
# Pon tu instrucción aquí o en la variable de entorno ALIGNMENT_PROMPT.
# Se añade al inicio del prompt de respuesta directa.
ALIGNMENT_PROMPT = os.getenv("ALIGNMENT_PROMPT", "")
# Ejemplo: "This is a nurse licensing exam. Prioritize patient safety answers."

# ─── Estado global ────────────────────────────────────────
_state = {
    "active":     True,
    "mode":       "auto",
    "debug":      False,
    "processing": False,
    "processed":  0,
    "last":       None,
    "last_src":   None,
    "start_time": time.time(),
    "tg_offset":  0,
    "errors":     0,
}

# ─── Watchdog state ───────────────────────────────────────
_wd = {
    "active":            False,
    "target_url":        os.getenv("WD_TARGET_URL", ""),
    "check_interval":    int(os.getenv("WD_INTERVAL", "120")),
    "stuck_timeout":     int(os.getenv("WD_STUCK", "120")),
    "consecutive_fails": 0,
    "processing_since":  None,
    "last_check":        0,
    "last_status":       "—",
    "last_check_str":    "nunca",
}

# ─── Dashboard HTML ───────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Pipeline — Dashboard</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d1117;color:#c9d1d9;font-family:'Courier New',monospace;font-size:13px}
  header{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;align-items:center;gap:16px}
  header h1{font-size:16px;color:#58a6ff}
  .dot{width:10px;height:10px;border-radius:50%;display:inline-block}
  .dot.green{background:#3fb950}.dot.red{background:#f85149}.dot.yellow{background:#d29922}
  .grid{display:grid;grid-template-columns:260px 1fr 230px;gap:0;height:calc(100vh - 50px)}
  .sidebar{background:#161b22;border-right:1px solid #30363d;padding:14px;display:flex;flex-direction:column;gap:12px;overflow-y:auto}
  .flowpanel{background:#161b22;border-left:1px solid #30363d;padding:14px;overflow-y:auto;display:flex;flex-direction:column;gap:0}
  .card{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px}
  .card h3{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
  .stat{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #21262d}
  .stat:last-child{border-bottom:none}
  .stat .label{color:#8b949e}.stat .value{color:#e6edf3;font-weight:bold}
  .btn-group{display:flex;flex-direction:column;gap:6px}
  .btn{padding:7px 12px;border:1px solid #30363d;border-radius:5px;background:#21262d;color:#c9d1d9;
       cursor:pointer;font-family:inherit;font-size:12px;text-align:left;transition:all .15s}
  .btn:hover{background:#30363d;border-color:#58a6ff}
  .btn.danger{border-color:#da3633}.btn.danger:hover{background:#da3633;color:#fff}
  .btn.success{border-color:#2ea043}.btn.success:hover{background:#2ea043;color:#fff}
  .console{background:#0d1117;padding:12px;overflow-y:auto;height:calc(100vh - 50px);font-size:12px;line-height:1.6}
  .log-line{padding:1px 0;border-bottom:1px solid #161b22;white-space:pre-wrap;word-break:break-all}
  .log-line.error{color:#f85149}.log-line.warn{color:#d29922}.log-line.info{color:#c9d1d9}
  .log-line .ts{color:#484f58;margin-right:6px}.log-line .lvl{margin-right:8px;font-weight:bold}
  .log-line.error .lvl{color:#f85149}.log-line.warn .lvl{color:#d29922}.log-line.info .lvl{color:#3fb950}
  #answer-flash{display:none;background:#1f6feb;color:#fff;padding:10px;border-radius:6px;
                text-align:center;font-size:18px;font-weight:bold;margin-bottom:8px}
  /* ── Flow diagram ── */
  .flow{display:flex;flex-direction:column;align-items:center;gap:0;padding:8px 0}
  .fbox{width:100%;border:2px solid #30363d;border-radius:8px;background:#0d1117;
        padding:10px 8px;text-align:center;font-size:11px;line-height:1.4;transition:all .3s;position:relative}
  .fbox .ftitle{font-weight:bold;font-size:12px;margin-bottom:4px}
  .fbox .fsub{color:#8b949e;font-size:10px}
  .fbox.active{border-color:#58a6ff;background:#0d2137;box-shadow:0 0 12px #1f6feb55}
  .fbox.done{border-color:#2ea043;background:#0d1f12}
  .fbox.fail{border-color:#da3633;background:#1f0d0d}
  .fbox.warn{border-color:#d29922;background:#1f1a0d}
  .farrow{color:#484f58;font-size:18px;text-align:center;line-height:1;margin:2px 0}
  .fbranch{display:flex;gap:6px;width:100%;margin:2px 0}
  .fbranch .fbox{font-size:10px;padding:7px 4px}
  .fbox .fbadge{display:inline-block;margin-top:4px;padding:2px 6px;border-radius:8px;
                font-size:10px;font-weight:bold;background:#21262d;color:#c9d1d9}
  .fp-title{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;
            margin-bottom:10px;text-align:center;padding-bottom:6px;border-bottom:1px solid #30363d}
</style>
</head>
<body>
<header>
  <span class="dot" id="hdr-dot"></span>
  <h1>🤖 AI Pipeline Bot</h1>
  <span id="hdr-status" style="color:#8b949e;font-size:12px"></span>
  <span style="margin-left:auto;color:#484f58;font-size:11px" id="hdr-uptime"></span>
</header>
<div class="grid">

  <!-- ── SIDEBAR ── -->
  <div class="sidebar">
    <div id="answer-flash"></div>
    <div class="card">
      <h3>Estado</h3>
      <div class="stat"><span class="label">Pipeline</span><span class="value" id="s-status">—</span></div>
      <div class="stat"><span class="label">Procesados</span><span class="value" id="s-processed">0</span></div>
      <div class="stat"><span class="label">Última resp.</span><span class="value" id="s-last">—</span></div>
      <div class="stat"><span class="label">Errores</span><span class="value" id="s-errors">0</span></div>
    </div>
    <div class="card">
      <h3>Control</h3>
      <div class="btn-group">
        <button class="btn success" onclick="ctrl('start')">▶ Activar pipeline</button>
        <button class="btn danger"  onclick="ctrl('stop')">■ Desactivar pipeline</button>
      </div>
    </div>
    <div class="card">
      <h3>Fuente</h3>
      <div style="color:#3fb950;font-size:11px;padding:4px 0">🟢 AUTO — Gmail</div>
      <div style="color:#484f58;font-size:10px">pewintest@gmail.com · poll 8s</div>
    </div>
    <div class="card">
      <h3>🐕 Watchdog</h3>
      <div class="stat"><span class="label">Estado</span><span class="value" id="wd-status">🔴</span></div>
      <div class="stat"><span class="label">Check</span><span class="value" id="wd-last">nunca</span></div>
      <div class="stat"><span class="label">PC</span><span class="value" id="wd-pc">—</span></div>
      <div class="stat"><span class="label">Fallos</span><span class="value" id="wd-fails">0</span></div>
      <div style="margin-top:8px">
        <input id="wd-url-input" type="text" placeholder="URL a monitorear"
          style="width:100%;background:#0d1117;border:1px solid #30363d;border-radius:4px;
                 padding:5px 7px;color:#c9d1d9;font-family:inherit;font-size:10px;margin-bottom:5px">
        <div class="btn-group">
          <button class="btn success" onclick="wdCtrl('start')" style="font-size:11px">▶ Activar</button>
          <button class="btn danger"  onclick="wdCtrl('stop')"  style="font-size:11px">■ Detener</button>
          <button class="btn" onclick="wdCtrl('check_now')"     style="font-size:11px;border-color:#8b949e">⚡ Check ya</button>
        </div>
      </div>
    </div>
    <div class="card">
      <h3>Consola</h3>
      <div class="btn-group">
        <button class="btn" onclick="clearLogs()">🗑 Limpiar</button>
        <button class="btn" id="btn-scroll" onclick="toggleScroll()">📌 Scroll: ON</button>
      </div>
    </div>
  </div>

  <!-- ── CONSOLE ── -->
  <div id="console" class="console"></div>

  <!-- ── FLOW DIAGRAM ── -->
  <div class="flowpanel">
    <div class="fp-title">📊 Diagrama de Flujo</div>
    <div class="flow">

      <div class="fbox" id="f-email">
        <div class="ftitle">📧 Gmail</div>
        <div class="fsub">pewintest@gmail.com<br>Poll cada 8s</div>
      </div>
      <div class="farrow">↓</div>

      <div class="fbox" id="f-recv">
        <div class="ftitle">📎 Imagen recibida</div>
        <div class="fsub">Adjunto detectado<br>bytes descargados</div>
      </div>
      <div class="farrow">↓</div>

      <div class="fbox" id="f-phase1">
        <div class="ftitle">🖼 FASE 1</div>
        <div class="fsub">Verificación encuadre<br>
          <span class="fbadge">Gemini</span>
          <span class="fbadge" style="color:#8b949e">fallback→</span>
          <span class="fbadge">GPT</span>
          <span class="fbadge">Claude</span>
        </div>
      </div>
      <div class="farrow">↓</div>

      <div class="fbranch">
        <div class="fbox fail" id="f-invalid" style="flex:1">
          <div class="ftitle" style="color:#f85149">❌ Inválido</div>
          <div class="fsub">###Horizontal<br>###Vert Arriba<br>###Vert Abajo<br>###No veo</div>
        </div>
        <div class="fbox done" id="f-ok" style="flex:1">
          <div class="ftitle" style="color:#3fb950">✅ Ok</div>
          <div class="fsub">4 lados<br>visibles</div>
        </div>
      </div>
      <div class="farrow">↓</div>

      <div class="fbox" id="f-phase2">
        <div class="ftitle">🎯 FASE 2</div>
        <div class="fsub">Consenso paralelo<br>
          <span class="fbadge">Gemini</span>
          <span class="fbadge">GPT-4o</span>
          <span class="fbadge">Claude</span>
        </div>
      </div>
      <div class="farrow">↓</div>

      <div class="fbox" id="f-vote">
        <div class="ftitle">🗳 Votación</div>
        <div class="fsub">Mayoría gana<br><span id="f-vote-detail" style="color:#58a6ff">—</span></div>
      </div>
      <div class="farrow">↓</div>

      <div class="fbox" id="f-result">
        <div class="ftitle">📲 Resultado</div>
        <div class="fsub">→ Telegram<br><span id="f-last-ans" style="color:#3fb950;font-size:14px;font-weight:bold">—</span></div>
      </div>

    </div>
  </div>

</div>
<script>
let logOffset=0, autoScroll=true, lastProcessed=0, wasProcessing=false;

function ctrl(a){fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})}).then(r=>r.json()).then(()=>fetchStatus());}
function wdCtrl(a){const u=document.getElementById('wd-url-input').value.trim();fetch('/api/watchdog/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a,url:u})}).then(r=>r.json()).then(()=>fetchWatchdog());}
function clearLogs(){document.getElementById('console').innerHTML='';logOffset=0;}
function toggleScroll(){autoScroll=!autoScroll;document.getElementById('btn-scroll').textContent='📌 Scroll: '+(autoScroll?'ON':'OFF');}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function setFlow(active, last, processing){
  const ids=['f-email','f-recv','f-phase1','f-invalid','f-ok','f-phase2','f-vote','f-result'];
  ids.forEach(id=>{
    const el=document.getElementById(id);
    if(!el)return;
    el.classList.remove('active');
    if(!['f-invalid','f-ok'].includes(id)) el.classList.remove('done','fail','warn');
  });
  if(!active) return;
  if(processing){
    ['f-email','f-recv','f-phase1'].forEach(id=>document.getElementById(id)?.classList.add('active'));
  } else if(last){
    const frameTokens=['Horizontal','Vert Arriba','Vert Abajo','No veo'];
    const isFrame = frameTokens.some(t=>last===t);
    document.getElementById('f-email').classList.add('done');
    document.getElementById('f-recv').classList.add('done');
    document.getElementById('f-phase1').classList.add('done');
    if(isFrame){
      document.getElementById('f-invalid').classList.add('active');
      document.getElementById('f-result').classList.add('warn');
    } else {
      document.getElementById('f-ok').classList.add('done');
      document.getElementById('f-phase2').classList.add('done');
      document.getElementById('f-vote').classList.add('done');
      document.getElementById('f-result').classList.add('done');
    }
    document.getElementById('f-last-ans').textContent='###'+last;
  }
}

function fetchStatus(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    document.getElementById('hdr-dot').className='dot '+(d.active?(d.processing?'yellow':'green'):'red');
    document.getElementById('hdr-status').textContent=d.active?(d.processing?'Procesando...':'Esperando'):'Detenido';
    const u=d.uptime_s,h=Math.floor(u/3600),m=Math.floor((u%3600)/60),s=u%60;
    document.getElementById('hdr-uptime').textContent=`Uptime: ${h}h ${m}m ${s}s`;
    document.getElementById('s-status').textContent=d.active?'🟢 ACTIVO':'🔴 INACTIVO';
    document.getElementById('s-status').style.color=d.active?'#3fb950':'#f85149';
    document.getElementById('s-processed').textContent=d.processed;
    document.getElementById('s-last').textContent=d.last||'—';
    document.getElementById('s-errors').textContent=d.errors;
    if(d.processed>lastProcessed){
      lastProcessed=d.processed;
      const f=document.getElementById('answer-flash');
      f.textContent='###'+d.last;f.style.display='block';
      setTimeout(()=>{f.style.display='none';},3000);
    }
    setFlow(d.active, d.last, d.processing);
  });
}

function fetchWatchdog(){
  fetch('/api/watchdog/status').then(r=>r.json()).then(d=>{
    document.getElementById('wd-status').textContent=d.active?'🟢 ACTIVO':'🔴 INACTIVO';
    document.getElementById('wd-status').style.color=d.active?'#3fb950':'#f85149';
    document.getElementById('wd-last').textContent=d.last_check_str||'nunca';
    document.getElementById('wd-pc').textContent=d.last_status||'—';
    document.getElementById('wd-fails').textContent=d.consecutive_fails;
    if(d.target_url&&!document.getElementById('wd-url-input').value)
      document.getElementById('wd-url-input').value=d.target_url;
  });
}

function fetchLogs(){
  fetch('/api/logs?offset='+logOffset).then(r=>r.json()).then(d=>{
    if(!d.entries.length)return;
    const con=document.getElementById('console');
    d.entries.forEach(e=>{
      const div=document.createElement('div');
      div.className='log-line '+e.cls;
      div.innerHTML=`<span class="ts">${e.t}</span><span class="lvl">${e.level.padEnd(5)}</span>${esc(e.msg)}`;
      con.appendChild(div);
    });
    logOffset+=d.entries.length;
    if(autoScroll)con.scrollTop=con.scrollHeight;
  });
}

fetchStatus();fetchWatchdog();fetchLogs();
setInterval(fetchStatus,2000);
setInterval(fetchWatchdog,5000);
setInterval(fetchLogs,1000);
</script>
</body>
</html>"""

# ─── Flask ────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def dashboard():
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

@flask_app.route("/ping")
def ping():
    return jsonify({"alive": True, "active": _state["active"]}), 200

@flask_app.route("/health")
def health():
    status = "processing" if _state["processing"] else ("waiting" if _state["active"] else "stopped")
    return jsonify({
        "status":    status,
        "processed": _state["processed"],
        "last":      _state["last"],
        "uptime_s":  round(time.time() - _state["start_time"]),
    }), 200

@flask_app.route("/api/status")
def api_status():
    return jsonify({
        "active":     _state["active"],
        "mode":       _state["mode"],
        "processing": _state["processing"],
        "processed":  _state["processed"],
        "last":       _state["last"],
        "last_src":   _state["last_src"],
        "errors":     _state["errors"],
        "uptime_s":   round(time.time() - _state["start_time"]),
    }), 200

@flask_app.route("/api/logs")
def api_logs():
    offset = int(flask_request.args.get("offset", 0))
    entries = list(LOG_BUFFER)[offset:]
    return jsonify({"entries": entries, "total": len(LOG_BUFFER)}), 200

@flask_app.route("/api/control", methods=["POST"])
def api_control():
    action = (flask_request.json or {}).get("action", "")
    if action == "start":
        _state["active"] = True
        logger.info("▶ Pipeline ACTIVADO desde dashboard")
        _broadcast("✅ Pipeline *ACTIVADO* desde el dashboard web.")
    elif action == "stop":
        _state["active"] = False
        logger.info("■ Pipeline DETENIDO desde dashboard")
        _broadcast("😴 Pipeline *DETENIDO* desde el dashboard web.")
    return jsonify({"ok": True}), 200

@flask_app.route("/api/watchdog/status")
def api_wd_status():
    return jsonify({
        "active":            _wd["active"],
        "target_url":        _wd["target_url"],
        "consecutive_fails": _wd["consecutive_fails"],
        "last_check_str":    _wd["last_check_str"],
        "last_status":       _wd["last_status"],
    }), 200

@flask_app.route("/api/watchdog/control", methods=["POST"])
def api_wd_control():
    body   = flask_request.json or {}
    action = body.get("action", "")
    url    = body.get("url", "").strip()

    if url:
        _wd["target_url"] = url

    if action == "start":
        if not _wd["target_url"]:
            return jsonify({"ok": False, "error": "URL requerida"}), 400
        _wd["active"] = True
        logger.info(f"🐕 Watchdog ACTIVADO → {_wd['target_url']}")
    elif action == "stop":
        _wd["active"] = False
        logger.info("🐕 Watchdog DETENIDO")
    elif action == "check_now":
        threading.Thread(target=_wd_check, daemon=True).start()

    return jsonify({"ok": True}), 200

# ─── Telegram helpers ─────────────────────────────────────
def _tg(method: str, params: dict = None) -> dict:
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 409:
            logger.debug(f"TG [{method}]: 409 (otro cliente activo — ignorado)")
        else:
            logger.error(f"TG [{method}]: HTTP {e.code}")
        return {"ok": False}
    except Exception as e:
        logger.error(f"TG [{method}]: {e}")
        return {"ok": False}

def _send(chat_id: int, text: str):
    _tg("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

def _broadcast(text: str):
    for cid in CHAT_IDS:
        _send(cid, text)

def _download_tg_photo(file_id: str) -> bytes | None:
    info = _tg("getFile", {"file_id": file_id})
    if not info.get("ok"):
        return None
    path = info["result"]["file_path"]
    try:
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    except Exception as e:
        logger.error(f"TG download: {e}")
        return None

# ─── Gmail IMAP ───────────────────────────────────────────
def _imap_connect():
    if not GMAIL_PASS:
        logger.error("GMAIL_APP_PASSWORD no configurada")
        return None
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST)
        conn.login(GMAIL_USER, GMAIL_PASS)
        return conn
    except Exception as e:
        logger.error(f"IMAP connect: {e}")
        return None

def _fetch_new_images() -> list[bytes]:
    images = []
    conn   = _imap_connect()
    if conn is None:
        return images
    try:
        conn.select("INBOX")
        _, data = conn.search(None, "UNSEEN")
        ids = data[0].split()
        if not ids:
            return images
        logger.info(f"📧 {len(ids)} email(s) nuevo(s)")
        for uid in ids:
            _, msg_data = conn.fetch(uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)
            found = False
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype.startswith("image/"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        images.append(payload)
                        logger.info(f"  📎 Imagen ({len(payload)} bytes, {ctype})")
                        found = True
                        break
                elif ctype == "application/octet-stream":
                    disp = str(part.get("Content-Disposition", ""))
                    if "attachment" in disp or "inline" in disp:
                        payload = part.get_payload(decode=True)
                        if payload and len(payload) > 1000:
                            images.append(payload)
                            logger.info(f"  📎 Adjunto binario ({len(payload)} bytes)")
                            found = True
                            break
            conn.store(uid, "+FLAGS", "\\Seen")
            if not found:
                logger.warning(f"  Email sin imagen adjunta — ignorado")
    except Exception as e:
        logger.error(f"IMAP fetch: {e}")
        _state["errors"] += 1
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return images

# ─── Prompt maestro ───────────────────────────────────────
# Una sola pasada: analiza encuadre → si ok, responde pregunta.
# Respuestas válidas: ###Horizontal | ###Vert Arriba | ###Vert Abajo | ###No veo | ###A-D
MASTER_PROMPT = """Analiza solo el marco físico exterior de la pantalla en la foto.

Ignora completamente:
- contenido dentro de la pantalla
- texto, ventanas, íconos, cursor
- fondo, pared, mesa, teclado, base, bisagra
- reflejos internos
- cualquier elemento que no sea el bisel/marco exterior

Definición:
El marco es el borde físico rectangular que rodea la pantalla. Un lado cuenta como visible solo si se distingue realmente como borde físico, con contraste suficiente, de forma continua o mayormente continua.

Evalúa por separado:
- borde superior
- borde inferior
- borde izquierdo
- borde derecho

Regla crítica:
No infieras lados faltantes por perspectiva, simetría o forma general. Si un lado no se distingue claramente, cuenta como NO visible.

Un lado es NO visible si:
- está fuera del encuadre
- está cortado
- se pierde por oscuridad o bajo contraste
- solo se adivina
- es demasiado parcial para confirmarlo
- está bloqueado por objeto o sombra

Prioridad de salida:
1. ###No veo
2. ###Horizontal
3. ###Vert Arriba
4. ###Vert Abajo
5. OCR y respuesta A-D

Reglas de clasificación:
- Si un objeto, sombra, mano u obstáculo oculta parcialmente más de 2 lados del marco y además cubre parte de la pantalla: `###No veo`
- Si el borde izquierdo o derecho NO es visible: `###Horizontal`
- Si el borde superior NO es visible: `###Vert Arriba`
- Si el borde inferior NO es visible: `###Vert Abajo`
- Si los 4 lados del marco son claramente visibles y cierran el rectángulo: proceder a analizar el contenido

Desempate:
- Si falta un lateral y también arriba o abajo, responde `###Horizontal`

Solo si está Ok visualmente, proceder a hacer OCR:
Detecta si el contenido corresponde a una pregunta de opción múltiple.
Criterios:
- pregunta arriba
- 4 opciones en lista vertical
- si faltan letras, asignarlas en orden A-D
- formato de quiz/test

Salida final:
Devuelve solo una de estas respuestas exactas, sin explicación:
- ###Horizontal
- ###Vert Arriba
- ###Vert Abajo
- ###No veo
- ###A
- ###B
- ###C
- ###D"""

# Tokens válidos que puede devolver el sistema
import re as _re
_TOKEN_RE    = _re.compile(r'###\s*(No veo|Horizontal|Vert Arriba|Vert Abajo|[ABCD])\b', _re.IGNORECASE)
FRAME_TOKENS  = {"No veo", "Horizontal", "Vert Arriba", "Vert Abajo"}
ANSWER_TOKENS = {"A", "B", "C", "D"}

def _parse_token(raw: str) -> str | None:
    """Extrae el token ###X de la respuesta de la IA."""
    if not raw:
        return None
    m = _TOKEN_RE.search(raw)
    if m:
        return m.group(1).strip()
    stripped = raw.strip().upper()
    if stripped in ANSWER_TOKENS:
        return stripped
    return None

# ─── Proveedores AI ───────────────────────────────────────
async def _gemini(img: bytes, prompt: str) -> str | None:
    if not GEMINI_KEY:
        return None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=GEMINI_KEY)
        r = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Part.from_bytes(data=img, mime_type="image/jpeg"), prompt]
        )
        t = r.text.strip()
        return t if t and "[OCR_FAILED]" not in t else None
    except Exception as e:
        logger.error(f"Gemini: {e}")
        return None

async def _openai(img: bytes, prompt: str) -> str | None:
    if not OPENAI_KEY:
        return None
    try:
        from openai import AsyncOpenAI
        b64 = base64.b64encode(img).decode()
        c   = AsyncOpenAI(api_key=OPENAI_KEY)
        r   = await c.chat.completions.create(
            model=OPENAI_MODEL, max_tokens=512,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]}]
        )
        t = r.choices[0].message.content.strip()
        return t if t and "[OCR_FAILED]" not in t else None
    except Exception as e:
        logger.error(f"OpenAI: {e}")
        return None

async def _claude(img: bytes, prompt: str) -> str | None:
    if not ANTHROPIC_KEY:
        return None
    try:
        from anthropic import AsyncAnthropic
        b64 = base64.b64encode(img).decode()
        c   = AsyncAnthropic(api_key=ANTHROPIC_KEY)
        r   = await c.messages.create(
            model=CLAUDE_MODEL, max_tokens=512,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": prompt}
            ]}]
        )
        t = r.content[0].text.strip()
        return t if t and "[OCR_FAILED]" not in t else None
    except Exception as e:
        logger.error(f"Claude: {e}")
        return None

_PROVIDERS = [("GEMINI", _gemini), ("OPENAI", _openai), ("CLAUDE", _claude)]

# ─── Debug helper ─────────────────────────────────────────
def _dbg(text: str):
    """Envía mensaje por Telegram solo si debug mode está activo."""
    if _state.get("debug"):
        _broadcast(text)

# ─── Pipeline — dos fases ─────────────────────────────────
async def _pipeline(img: bytes, source: str = "gmail"):
    """
    Fase 1: Gemini verifica el encuadre (fallback OpenAI → Claude).
            Si el encuadre es inválido → broadcast y termina.
    Fase 2: Los 3 en paralelo determinan la respuesta (consenso).
    """
    t0 = time.time()
    _state["processing"] = True
    logger.info(f"⚡ Procesando [{source}]...")
    _dbg(f"⚡ *Pipeline iniciado* — fuente: `{source}`")

    try:
        # ── FASE 1: Verificación de encuadre (secuencial) ────
        _dbg("🖼 *Fase 1* — verificando encuadre (Gemini primero)...")
        frame_token    = None
        frame_provider = None

        for name, fn in _PROVIDERS:
            logger.info(f"🖼 Encuadre → [{name}]")
            _dbg(f"  ⏳ `{name}` analizando...")
            try:
                raw   = await asyncio.wait_for(fn(img, MASTER_PROMPT), timeout=OCR_TIMEOUT)
                token = _parse_token(raw)
                if token:
                    frame_token    = token
                    frame_provider = name
                    logger.info(f"  ✓ [{name}] → {token}")
                    _dbg(f"  ✅ `{name}` → *{token}*")
                    break
                else:
                    logger.warning(f"  ✗ [{name}] sin token — siguiente...")
                    _dbg(f"  ❌ `{name}` → sin respuesta, fallback...")
            except asyncio.TimeoutError:
                logger.error(f"  ✗ [{name}] timeout")
                _dbg(f"  ⏱ `{name}` → timeout, fallback...")
            except Exception as e:
                logger.error(f"  ✗ [{name}] {e}")
                _dbg(f"  ❌ `{name}` → error, fallback...")

        elapsed = round(time.time() - t0, 1)

        if frame_token is None:
            _broadcast(f"⚠️ Sin respuesta de ningún proveedor ({elapsed}s)")
            _state["errors"] += 1
            return

        # ── Encuadre inválido → responder y salir ────────────
        if frame_token in FRAME_TOKENS:
            result = f"###{frame_token}"
            logger.info(f"★ ENCUADRE: {result} [{elapsed}s]")
            _dbg(f"🖼 *Encuadre inválido* → `{result}` — pipeline detenido")
            _broadcast(result)
            _state["last"]      = frame_token
            _state["last_src"]  = source
            _state["processed"] += 1
            return

        # ── FASE 2: Encuadre Ok → consenso en paralelo ───────
        _dbg(
            f"✅ *Encuadre Ok* — `{frame_provider}` confirmó\n"
            f"🎯 *Fase 2* — consultando 3 IAs en paralelo..."
        )
        logger.info(f"✓ Encuadre Ok [{frame_provider}] — iniciando consenso")

        # Voto de fase 1 ya cuenta
        votes: dict = {frame_provider: frame_token}

        async def _ask(name, fn):
            try:
                raw   = await asyncio.wait_for(fn(img, MASTER_PROMPT), timeout=OCR_TIMEOUT)
                token = _parse_token(raw)
                if token in ANSWER_TOKENS:
                    votes[name] = token
                    logger.info(f"  ✓ [{name}] → {token}")
                    _dbg(f"  ✅ `{name}` → *{token}*")
                else:
                    votes[name] = None
                    _dbg(f"  ⚠️ `{name}` → `{token}` (descartado)")
            except Exception as e:
                votes[name] = None
                logger.error(f"  ✗ [{name}] {e}")
                _dbg(f"  ❌ `{name}` → error")

        others = [(n, f) for n, f in _PROVIDERS if n != frame_provider]
        await asyncio.gather(*[_ask(n, f) for n, f in others])

        elapsed = round(time.time() - t0, 1)
        valid   = [v for v in votes.values() if v in ANSWER_TOKENS]

        if not valid:
            _broadcast(f"⚠️ Sin consenso de respuesta ({elapsed}s)")
            _state["errors"] += 1
            return

        winner, count = Counter(valid).most_common(1)[0]
        logger.info(f"🗳 Consenso: {winner} ({count}/{len(valid)}) — {votes}")
        votes_str = " | ".join(f"`{k}`→{v or '?'}" for k, v in votes.items())
        _dbg(f"🗳 *Consenso: {winner}* ({count}/{len(valid)} votos)\n{votes_str}")

        result = f"###{winner}"
        _dbg(f"✅ *Completo* en {elapsed}s — enviando `{result}`")
        _broadcast(result)
        _state["last"]      = winner
        _state["last_src"]  = source
        _state["processed"] += 1
        logger.info(f"★ RESPUESTA: {result} [{elapsed}s] via {source}")

    finally:
        _state["processing"] = False

# ─── Watchdog logic ───────────────────────────────────────
def _pc(path: str, method="GET", timeout=8):
    """Llama al target del watchdog."""
    try:
        req = urllib.request.Request(
            f"{_wd['target_url']}{path}",
            data=(b"" if method == "POST" else None),
            method=method
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None

def _wd_check():
    health = _pc("/health")
    now    = time.time()
    _wd["last_check"]     = now
    _wd["last_check_str"] = time.strftime("%H:%M:%S")

    if health is None:
        _wd["consecutive_fails"] += 1
        _wd["last_status"] = "sin respuesta"
        fails = _wd["consecutive_fails"]
        logger.warning(f"🐕 Target sin respuesta (fallo #{fails})")
        if fails in (3, 6, 10):
            _broadcast(
                f"🚨 *Watchdog — ALERTA*\n"
                f"Target no responde tras *{fails}* intentos.\n"
                f"`{_wd['target_url']}`"
            )
        return

    _wd["consecutive_fails"] = 0
    status  = health.get("status", "unknown")
    elapsed = health.get("elapsed", 0)
    _wd["last_status"] = status
    logger.info(f"🐕 Target [{status}] elapsed={elapsed}s")

    if status == "processing":
        if _wd["processing_since"] is None:
            _wd["processing_since"] = now
        elif now - _wd["processing_since"] > _wd["stuck_timeout"]:
            logger.warning("🐕 Target colgado — enviando abort")
            result = _pc("/abort", method="POST")
            _wd["processing_since"] = None
            ok = result and result.get("ok")
            _broadcast(
                f"⚡ *Watchdog → Abort enviado*\n"
                f"{'Abortado ✅' if ok else 'Sin respuesta ❌'}"
            )
    else:
        _wd["processing_since"] = None

def _watchdog_loop():
    logger.info("🐕 Watchdog loop iniciado")
    while True:
        if _wd["active"] and _wd["target_url"]:
            try:
                _wd_check()
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
        time.sleep(_wd["check_interval"])

# ─── Gmail loop ───────────────────────────────────────────
def _gmail_loop():
    logger.info(f"📧 Gmail polling iniciado ({GMAIL_POLL_INT}s) → {GMAIL_USER}")
    while True:
        try:
            if _state["active"] and not _state["processing"]:
                images = _fetch_new_images()
                for img in images:
                    asyncio.run(_pipeline(img, "gmail"))
        except Exception as e:
            logger.error(f"Gmail loop: {e}")
        time.sleep(GMAIL_POLL_INT)

# ─── Telegram loop ────────────────────────────────────────
def _telegram_loop():
    logger.info("📨 Telegram polling iniciado")
    while True:
        try:
            res = _tg("getUpdates", {
                "offset":          _state["tg_offset"],
                "timeout":         25,
                "allowed_updates": ["message"],
            })
            if not res.get("ok"):
                time.sleep(5)
                continue
            for upd in res.get("result", []):
                _state["tg_offset"] = upd["update_id"] + 1
                msg     = upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                if chat_id not in CHAT_IDS:
                    continue

                # ── Foto (modo manual) ────────────────────
                # ── Comandos de texto ─────────────────────
                text = msg.get("text", "").strip().lower()
                if not text:
                    continue

                if text in ("debug", "/debug", "debug on"):
                    _state["debug"] = True
                    _send(chat_id,
                        "🔬 *Modo DEBUG activado*\n"
                        "Recibirás cada paso en tiempo real.\n"
                        "Envía `debug off` para desactivar."
                    )

                elif text in ("debug off", "/debugoff"):
                    _state["debug"] = False
                    _send(chat_id, "🔬 Modo DEBUG *desactivado*.")

                elif text in ("start", "/start", "activar"):
                    _state["active"] = True
                    _send(chat_id, "✅ Pipeline *ACTIVADO* — monitoreando Gmail.")

                elif text in ("stop", "/stop", "parar"):
                    _state["active"] = False
                    _send(chat_id, "😴 Pipeline *DESACTIVADO*.")

                elif text in ("status", "/status", "estado"):
                    uptime = round((time.time() - _state["start_time"]) / 60)
                    estado = "🟢 ACTIVO" if _state["active"] else "🔴 INACTIVO"
                    debug  = "🔬 ON" if _state["debug"] else "OFF"
                    _send(chat_id,
                        f"🤖 *Pipeline Status*\n"
                        f"Estado:    {estado}\n"
                        f"Debug:     {debug}\n"
                        f"Uptime:    {uptime} min\n"
                        f"Procesados:{_state['processed']}\n"
                        f"Última:    `{_state['last'] or 'ninguna'}` ({_state['last_src'] or '—'})\n"
                        f"Dashboard: https://{RAILWAY_DOMAIN}"
                    )

                elif text in ("help", "/help", "ayuda"):
                    _send(chat_id,
                        "🤖 *AI Pipeline — Comandos*\n\n"
                        "`start`     — Activar pipeline\n"
                        "`stop`      — Desactivar\n"
                        "`debug`     — Activar depuración paso a paso\n"
                        "`debug off` — Desactivar depuración\n"
                        "`status`    — Ver estado\n"
                        "`help`      — Esta ayuda"
                    )

        except Exception as e:
            logger.error(f"Telegram loop: {e}")
            time.sleep(5)

# ─── Self-ping ────────────────────────────────────────────
def _self_ping_loop():
    if not RAILWAY_DOMAIN:
        logger.warning("RAILWAY_PUBLIC_DOMAIN no definida — self-ping desactivado")
        return
    url = f"https://{RAILWAY_DOMAIN}/ping"
    logger.info(f"🏓 Self-ping → {url}")
    while True:
        time.sleep(SELF_PING_INT)
        try:
            urllib.request.urlopen(url, timeout=8)
            logger.debug("🏓 ping OK")
        except Exception as e:
            logger.warning(f"Self-ping fallo: {e}")

# ─── Main ─────────────────────────────────────────────────
def main():
    if not GMAIL_PASS:
        logger.warning("⚠ GMAIL_APP_PASSWORD no configurada")

    logger.info(f"🤖 AI Pipeline Bot arrancando — modo: {_state['mode']}")
    logger.info(f"📊 Dashboard: https://{RAILWAY_DOMAIN}/")

    _broadcast(
        f"🤖 *AI Pipeline Bot online*\n"
        f"Modo: `{_state['mode'].upper()}`\n"
        f"Dashboard: https://{RAILWAY_DOMAIN}\n"
        "Envía `help` para comandos."
    )

    threading.Thread(target=_gmail_loop,     daemon=True, name="Gmail").start()
    threading.Thread(target=_watchdog_loop,  daemon=True, name="Watchdog").start()
    threading.Thread(target=_telegram_loop,  daemon=True, name="Telegram").start()
    threading.Thread(target=_self_ping_loop, daemon=True, name="SelfPing").start()
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
