"""
🤖 AI Pipeline Bot — Versión Cloud (Railway)
Monitorea pewintest@gmail.com buscando emails con fotos.
Procesa la imagen → envía ###A/B/C/D por Telegram.

Comandos Telegram:
  start   → Activar pipeline
  stop    → Desactivar pipeline
  status  → Ver estado
  help    → Lista de comandos
"""
import os, time, threading, json, logging, asyncio, base64
import imaplib, email as email_lib
import urllib.request, urllib.parse, urllib.error
from collections import Counter
from flask import Flask, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-5s │ %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("pipeline")

# ─── Configuración (variables de entorno en Railway) ──────
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

# ─── Gmail IMAP ───────────────────────────────────────────
GMAIL_USER     = os.getenv("GMAIL_USER",         "pewintest@gmail.com")
GMAIL_PASS     = os.getenv("GMAIL_APP_PASSWORD",  "")   # App Password de Google
GMAIL_POLL_INT = int(os.getenv("GMAIL_POLL_INT",  "8")) # Segundos entre checks
IMAP_HOST      = "imap.gmail.com"

SELF_PING_INT  = 270   # 4.5 min — evita sleep de Railway
OCR_TIMEOUT    = 25    # segundos por proveedor

# ─── Estado global ────────────────────────────────────────
_state = {
    "active":     True,
    "processing": False,
    "processed":  0,
    "last":       None,
    "start_time": time.time(),
    "tg_offset":  0,
}

# ─── Flask (Railway requiere puerto abierto) ──────────────
flask_app = Flask(__name__)

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

# ─── Telegram helpers ─────────────────────────────────────
def _tg(method: str, params: dict = None) -> dict:
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        logger.error(f"TG [{method}]: {e}")
        return {"ok": False}

def _send(chat_id: int, text: str):
    _tg("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

def _broadcast(text: str):
    for cid in CHAT_IDS:
        _send(cid, text)

# ─── Gmail IMAP ───────────────────────────────────────────
def _imap_connect() -> imaplib.IMAP4_SSL | None:
    """Conecta a Gmail IMAP. Retorna None si falla."""
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
    """
    Busca emails NO LEÍDOS en INBOX con adjuntos de imagen.
    Los marca como leídos y devuelve los bytes de cada imagen.
    """
    images = []
    conn   = _imap_connect()
    if conn is None:
        return images

    try:
        conn.select("INBOX")
        # Buscar emails no leídos
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
            parts_found = []
            for part in msg.walk():
                ctype = part.get_content_type()
                parts_found.append(ctype)
                if ctype.startswith("image/"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        images.append(payload)
                        logger.info(f"  📎 Imagen encontrada ({len(payload)} bytes, {ctype})")
                        found = True
                        break  # una imagen por email
                # También intentar extraer imágenes de partes multipart/related
                elif ctype in ("application/octet-stream",):
                    disp = str(part.get("Content-Disposition", ""))
                    if "attachment" in disp or "inline" in disp:
                        payload = part.get_payload(decode=True)
                        if payload and len(payload) > 1000:
                            images.append(payload)
                            logger.info(f"  📎 Adjunto binario ({len(payload)} bytes)")
                            found = True
                            break

            # Marcar como leído independientemente de si había imagen
            conn.store(uid, "+FLAGS", "\\Seen")

            if not found:
                logger.info(f"  ⚠ Email sin imagen — partes MIME: {parts_found}")

    except Exception as e:
        logger.error(f"IMAP fetch: {e}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return images

# ─── Prompts ──────────────────────────────────────────────
OCR_PROMPT = (
    "You are an OCR system. Extract ALL text from this image exactly as shown. "
    "This is a multiple-choice question. Return the question and ALL options (A,B,C,D). "
    "Preserve formatting. Do NOT answer. If unreadable: [OCR_FAILED]"
)
ANSWER_PROMPT = (
    "Look at this multiple-choice question image. Determine the correct answer. "
    "Respond with ONLY the letter (A, B, C, or D). Nothing else."
)

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

# ─── Pipeline ─────────────────────────────────────────────
async def _ocr(img: bytes) -> str | None:
    """OCR secuencial — primer proveedor que responde gana."""
    for name, fn in _PROVIDERS:
        logger.info(f"🔍 OCR → [{name}]")
        try:
            result = await asyncio.wait_for(fn(img, OCR_PROMPT), timeout=OCR_TIMEOUT)
            if result:
                logger.info(f"✓ OCR [{name}] OK ({len(result)} chars)")
                return result
            logger.warning(f"  ✗ [{name}] sin resultado — siguiente...")
        except asyncio.TimeoutError:
            logger.error(f"  ✗ [{name}] timeout")
        except Exception as e:
            logger.error(f"  ✗ [{name}] {e}")
    return None


async def _answer(img: bytes) -> str | None:
    """Respuesta directa en paralelo — consenso de mayoría."""
    votes = {}

    async def _ask(name, fn):
        try:
            r = await asyncio.wait_for(fn(img, ANSWER_PROMPT), timeout=OCR_TIMEOUT)
            if r:
                for ch in r.upper():
                    if ch in "ABCD":
                        votes[name] = ch
                        logger.info(f"  ✓ [{name}] → {ch}")
                        return
        except Exception:
            pass
        votes[name] = None

    await asyncio.gather(*[_ask(n, f) for n, f in _PROVIDERS])

    valid = [v for v in votes.values() if v]
    if not valid:
        return None
    winner, count = Counter(valid).most_common(1)[0]
    logger.info(f"🗳 Consenso: {winner} ({count}/{len(valid)} votos) — {votes}")
    return winner


async def _pipeline(img: bytes):
    t0 = time.time()
    _state["processing"] = True
    try:
        text = await _ocr(img)
        if not text:
            _broadcast("❌ No se pudo leer el texto de la imagen.")
            return

        resp    = await _answer(img)
        elapsed = round(time.time() - t0, 1)

        if resp:
            _state["last"]      = resp
            _state["processed"] += 1
            msg = f"###{resp}"
            _broadcast(msg)
            logger.info(f"★ RESPUESTA: {resp} [{elapsed}s]")
        else:
            _broadcast(f"⚠️ No se pudo determinar respuesta. ({elapsed}s)")
    finally:
        _state["processing"] = False

# ─── Gmail polling loop ───────────────────────────────────
def _gmail_loop():
    logger.info(f"📧 Gmail polling iniciado ({GMAIL_POLL_INT}s interval) → {GMAIL_USER}")
    while True:
        try:
            if _state["active"] and not _state["processing"]:
                images = _fetch_new_images()
                for img in images:
                    logger.info("⚡ Procesando imagen del correo...")
                    asyncio.run(_pipeline(img))
            elif _state["processing"]:
                logger.debug("⏳ Aún procesando — saltando check")
        except Exception as e:
            logger.error(f"Gmail loop: {e}")
        time.sleep(GMAIL_POLL_INT)

# ─── Telegram command loop ────────────────────────────────
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
                text    = msg.get("text", "").strip().lower()

                if chat_id not in CHAT_IDS:
                    continue

                if text in ("start", "/start", "activar"):
                    _state["active"] = True
                    _send(chat_id, "✅ Pipeline *ACTIVADO* — monitoreando correo para nuevas fotos.")

                elif text in ("stop", "/stop", "parar"):
                    _state["active"] = False
                    _send(chat_id, "😴 Pipeline *DESACTIVADO*.")

                elif text in ("status", "/status", "estado"):
                    uptime = round((time.time() - _state["start_time"]) / 60)
                    estado = "🟢 ACTIVO" if _state["active"] else "🔴 INACTIVO"
                    _send(chat_id,
                        f"🤖 *Pipeline Status*\n"
                        f"Estado:      {estado}\n"
                        f"Uptime:      {uptime} min\n"
                        f"Procesados:  {_state['processed']}\n"
                        f"Última resp: `{_state['last'] or 'ninguna'}`\n"
                        f"Fuente:      📧 `{GMAIL_USER}`"
                    )

                elif text in ("help", "/help", "ayuda"):
                    _send(chat_id,
                        "🤖 *AI Pipeline Bot — Comandos*\n\n"
                        f"📧 Fuente: `{GMAIL_USER}`\n"
                        "Envía una foto al correo → el bot responde con `###B`\n\n"
                        "`start`   — Activar pipeline\n"
                        "`stop`    — Desactivar pipeline\n"
                        "`status`  — Ver estado\n"
                        "`help`    — Esta ayuda"
                    )

        except Exception as e:
            logger.error(f"Telegram loop: {e}")
            time.sleep(5)


def _self_ping_loop():
    if not RAILWAY_DOMAIN:
        logger.warning("RAILWAY_PUBLIC_DOMAIN no definida — self-ping desactivado")
        return
    url = f"https://{RAILWAY_DOMAIN}/ping"
    logger.info(f"🏓 Self-ping activo → {url}")
    while True:
        time.sleep(SELF_PING_INT)
        try:
            urllib.request.urlopen(url, timeout=8)
            logger.debug("🏓 ping OK")
        except Exception as e:
            logger.warning(f"Self-ping fallo: {e}")


def main():
    if not GMAIL_PASS:
        logger.warning("⚠ GMAIL_APP_PASSWORD no configurada — el pipeline no procesará emails")

    logger.info("🤖 AI Pipeline Bot arrancando en Railway...")
    _broadcast(
        "🤖 *AI Pipeline Bot online*\n"
        f"Monitoreando 📧 `{GMAIL_USER}`\n"
        "Envía una foto al correo y te respondo con la letra correcta.\n"
        "Envía `help` para ver los comandos."
    )

    threading.Thread(target=_gmail_loop,    daemon=True, name="Gmail").start()
    threading.Thread(target=_telegram_loop, daemon=True, name="Telegram").start()
    threading.Thread(target=_self_ping_loop, daemon=True, name="SelfPing").start()
    flask_app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
