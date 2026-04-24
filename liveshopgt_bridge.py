#!/usr/bin/env python3
"""
LiveShopGT Bridge v3
====================
Conecta un scraper local (ej. fb_live_listener.py) con LiveShopGT API.

Mejoras principales vs v2:
- Soporta formato WS del listener actual: {"type":"comment","data":{...}}
- Ignora eventos no-comentario (hello/debug/sync/info)
- Dedupe local (TTL) para evitar re-envíos accidentales
- Reintento robusto con reconexión al WS
- Métricas y logs más claros

Requisitos:
  pip install websockets aiohttp

Uso:
  python liveshopgt_bridge.py \
    --session-key TU_SESSION_KEY \
    --api-url https://gabolive.replit.app \
    --ws-url ws://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import websockets
except ImportError:
    print("ERROR: instala websockets con: pip install websockets")
    sys.exit(1)

try:
    import aiohttp
except ImportError:
    print("ERROR: instala aiohttp con: pip install aiohttp")
    sys.exit(1)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("liveshopgt_bridge")


SKIP_PATTERNS = [
    "está en vivo ahora",
    "se unió al live",
    "reaccionó con",
    "compartió el video",
    "sigue a la página",
    "joined the live",
    "is now live",
    "started a live video",
]

UI_NOISE_WORDS = {
    "fijar", "fijado", "pin", "pinned", "comentario", "comment"
}

GREETING_PATTERNS = [
    "buenos dias", "buen día", "buen dia", "buenas tardes", "buenas noches",
    "hola", "holi", "saludos", "bendiciones", "feliz noche", "feliz dia", "feliz día"
]

SALE_HINTS = {
    "quiero", "precio", "cuanto", "cuánto", "talla", "color", "envio", "envío",
    "pago", "factura", "pedido", "apartame", "apártame", "disponible", "stock"
}


class Stats:
    def __init__(self):
        self.total_received = 0
        self.total_sent = 0
        self.auto_registered = 0
        self.pending_review = 0
        self.errors = 0
        self.duplicates_dropped = 0
        self.by_intent: Dict[str, int] = defaultdict(int)
        self.start_time = time.time()

    def summary(self) -> str:
        elapsed = int(time.time() - self.start_time)
        mins = elapsed // 60
        secs = elapsed % 60
        intents = " | ".join(
            f"{k}={v}" for k, v in sorted(self.by_intent.items()) if v > 0
        )
        base = (
            f"[{mins:02d}:{secs:02d}] Recibidos={self.total_received} "
            f"Enviados={self.total_sent} Auto-reg={self.auto_registered} "
            f"Revision={self.pending_review} Duplicados={self.duplicates_dropped} "
            f"Errores={self.errors}"
        )
        return base + (f" | {intents}" if intents else "")


stats = Stats()


class DedupeCache:
    def __init__(self, ttl_sec: int = 180, max_items: int = 10000):
        self.ttl_sec = ttl_sec
        self.max_items = max_items
        self._order = deque()  # (ts, key)
        self._set = set()

    def _prune(self, now: float):
        cutoff = now - self.ttl_sec
        while self._order and (self._order[0][0] < cutoff or len(self._order) > self.max_items):
            _, key = self._order.popleft()
            self._set.discard(key)

    def seen_or_add(self, key: str, now: Optional[float] = None) -> bool:
        now = now or time.time()
        self._prune(now)
        if key in self._set:
            return True
        self._set.add(key)
        self._order.append((now, key))
        return False


def normalize_comment_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""

    # Quita ruido de UI incrustado en el texto (ej: "Fijar").
    t = re.sub(r"\b(?:fijar|fijado|pin(?:ned)?)\b", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip(" -|:\t\n\r")
    return t


def is_tag_only_comment(text: str) -> bool:
    t = normalize_comment_text(text)
    if not t:
        return True

    low = t.lower()
    if any(h in low for h in SALE_HINTS):
        return False
    if re.search(r"\d", t):
        return False

    # Quita @ y separadores para analizar tokens.
    base = re.sub(r"[@#]", "", t)
    tokens = [tok for tok in re.split(r"\s+", base) if tok]
    if not tokens:
        return True

    # Si son pocas palabras y parecen solo nombres etiquetados, se descarta.
    if len(tokens) <= 6:
        cleaned = [tok.strip(",.;:!¡?¿()[]{}\"'") for tok in tokens]
        cleaned = [tok for tok in cleaned if tok]
        if cleaned and all(re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", tok) for tok in cleaned):
            connectors = {"de", "del", "la", "el", "y", "e", "a", "con", "para", "por", "en"}
            non_connectors = [tok for tok in cleaned if tok.lower() not in connectors]
            # 1-4 palabras útiles, sin verbos/señales de compra: típico comentario de solo etiqueta.
            if 1 <= len(non_connectors) <= 4:
                return True

    return False


def is_emoji_or_greeting_comment(text: str) -> bool:
    t = normalize_comment_text(text)
    if not t:
        return True

    low = t.lower()
    if any(h in low for h in SALE_HINTS):
        return False

    # Saludos y frases sociales cortas.
    if any(p in low for p in GREETING_PATTERNS):
        return True

    # Solo emojis/símbolos/puntuación (sin letras ni números).
    if not re.search(r"[a-záéíóúüñ0-9]", low, flags=re.IGNORECASE):
        return True

    return False


def should_skip(text: str) -> bool:
    t = normalize_comment_text(text).lower()
    if not t:
        return True
    if any(p in t for p in SKIP_PATTERNS):
        return True
    if is_emoji_or_greeting_comment(t):
        return True
    if is_tag_only_comment(t):
        return True
    return False


def parse_products(products_arg: Optional[str]) -> List[Dict[str, Any]]:
    """
    Formato simple: "Brillante:75,VinoM:50,Celeste:25"
    JSON completo: '[{"code":"BRI","description":"Brillante","price":75}]'
    """
    if not products_arg:
        return []

    try:
        data = json.loads(products_arg)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    products = []
    for item in products_arg.split(","):
        item = item.strip()
        if ":" not in item:
            continue
        name, price_raw = item.rsplit(":", 1)
        name = name.strip()
        try:
            price = float(price_raw.strip())
        except ValueError:
            log.warning(f"Precio inválido para producto: {item}")
            continue
        products.append(
            {
                "code": name.replace(" ", "").upper()[:10],
                "description": name,
                "price": price,
            }
        )
    return products


def _mk_external_id(author: str, text: str, ts_unix: int, platform: str) -> str:
    raw = f"{platform}|{author}|{text}|{ts_unix}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()


def parse_ws_message(raw: str, platform: str = "facebook") -> Optional[Dict[str, Any]]:
    """
    Acepta:
      1) listener actual: {"type":"comment","data":{"author":"...","text":"...","ts_unix":...}}
      2) JSON plano:     {"author":"...","text":"...","ts_unix":...}
      3) texto plano:    "Nombre: comentario"
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            ev_type = str(data.get("type", "")).lower().strip()

            # Ignorar eventos del listener que no son comentario.
            if ev_type and ev_type != "comment":
                return None

            body = data.get("data") if isinstance(data.get("data"), dict) else data
            author = body.get("author") or body.get("name") or body.get("user") or ""
            text = body.get("text") or body.get("message") or body.get("comment") or ""
            ts = body.get("ts_unix") or body.get("timestamp") or int(time.time())

            author = str(author).strip()
            text = str(text).strip()
            if not author or not text:
                return None

            ts_i = int(ts)
            return {
                "author": author,
                "text": text,
                "ts_unix": ts_i,
                "platform": platform,
                "source": "python-bridge",
                "externalId": _mk_external_id(author, text, ts_i, platform),
            }
    except json.JSONDecodeError:
        pass
    except Exception:
        return None

    if ": " in raw:
        author, text = raw.split(": ", 1)
        author = author.strip()
        text = text.strip()
        if author and text:
            ts_i = int(time.time())
            return {
                "author": author,
                "text": text,
                "ts_unix": ts_i,
                "platform": platform,
                "source": "python-bridge",
                "externalId": _mk_external_id(author, text, ts_i, platform),
            }

    return None


def build_ingest_endpoint(api_url: str) -> str:
    base = api_url.rstrip("/")
    if base.endswith("/api"):
        return f"{base}/live-listener/ingest"
    return f"{base}/api/live-listener/ingest"


async def send_batch(
    session: aiohttp.ClientSession,
    api_url: str,
    session_key: str,
    comments: List[Dict[str, Any]],
    products: List[Dict[str, Any]],
    auto_register: bool,
):
    if not comments:
        return

    payload = {
        "sessionKey": session_key,
        "comments": comments,
        "autoRegister": auto_register,
        "context": {"products": products} if products else {},
    }

    endpoint = build_ingest_endpoint(api_url)

    try:
        async with session.post(
            endpoint,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            body_text = await resp.text()

            if resp.status == 200:
                try:
                    data = json.loads(body_text) if body_text else {}
                except Exception:
                    data = {}

                stats.total_sent += int(data.get("received", len(comments)) or len(comments))

                # Compat: versión previa (autoRegistered/pendingReview) y nueva (ordersRegistered)
                auto_reg = int(data.get("autoRegistered", data.get("ordersRegistered", 0)) or 0)
                pending = int(data.get("pendingReview", 0) or 0)

                stats.auto_registered += auto_reg
                stats.pending_review += pending

                for order in data.get("orders", []) or []:
                    stats.by_intent["auto_order"] += 1
                    log.info(
                        f"✅ REGISTRADO: {order.get('author','?')} → "
                        f"{order.get('description','?')} Q{order.get('unitPrice','?')}"
                    )

                if pending > 0:
                    log.info(f"⚡ {pending} posible(s) pedido(s) — confirma en el portal")

            elif resp.status == 404:
                log.error("Session Key inválido o sesión no encontrada en el portal")
                stats.errors += 1
            elif resp.status == 401:
                log.error("No autorizado — verifica sesión activa/permisos")
                stats.errors += 1
            elif resp.status == 400:
                log.warning(f"API 400: {body_text}")
                stats.errors += 1
            else:
                log.warning(f"API HTTP {resp.status}: {body_text[:300]}")
                stats.errors += 1

    except aiohttp.ClientConnectorError:
        log.error(f"No se puede conectar a {api_url} — verifica URL/red")
        stats.errors += 1
    except asyncio.TimeoutError:
        log.warning("Timeout al enviar — reintenta en próximo ciclo")
        stats.errors += 1
    except Exception as e:
        log.error(f"Error al enviar batch: {e}")
        stats.errors += 1


async def bridge_loop(args):
    products = parse_products(args.products)
    if products:
        log.info(f"Productos configurados: {[p.get('description') for p in products]}")
    else:
        log.warning("Sin productos configurados. IA inferirá desde comentarios.")

    log.info(f"Conectando a scraper WS: {args.ws_url}")
    log.info(f"API LiveShopGT: {args.api_url}")
    log.info(f"Ingest endpoint: {build_ingest_endpoint(args.api_url)}")
    log.info(f"Session Key: {args.session_key}")
    log.info(
        "Auto-registro: " +
        ("ACTIVADO (confianza >=0.85)" if args.auto_register else "DESACTIVADO — revisión manual")
    )
    log.info(f"Batch: cada {args.batch_interval}s o al acumular {args.min_batch} comentarios")
    log.info("-" * 60)

    dedupe = DedupeCache(ttl_sec=args.dedupe_ttl)
    buffer: List[Dict[str, Any]] = []

    async with aiohttp.ClientSession() as http:
        while True:
            try:
                async with websockets.connect(
                    args.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                    max_size=2**20,
                ) as ws:
                    log.info("✅ Conectado al scraper local — esperando comentarios...")

                    async def flush():
                        nonlocal buffer
                        if not buffer:
                            return
                        to_send = buffer.copy()
                        buffer.clear()
                        log.info(f"→ Enviando {len(to_send)} comentario(s) al API...")
                        await send_batch(
                            http,
                            args.api_url,
                            args.session_key,
                            to_send,
                            products,
                            args.auto_register,
                        )
                        log.info(stats.summary())

                    async def timer_flush():
                        while True:
                            await asyncio.sleep(args.batch_interval)
                            if buffer:
                                await flush()

                    flush_task = asyncio.create_task(timer_flush())
                    try:
                        async for message in ws:
                            comment = parse_ws_message(str(message), args.platform)
                            if not comment:
                                continue

                            comment["text"] = normalize_comment_text(comment.get("text", ""))
                            if should_skip(comment["text"]):
                                continue

                            # Recalcula externalId por si el texto fue normalizado.
                            comment["externalId"] = _mk_external_id(
                                comment["author"], comment["text"], comment["ts_unix"], comment["platform"]
                            )

                            if dedupe.seen_or_add(comment["externalId"], now=time.time()):
                                stats.duplicates_dropped += 1
                                continue

                            stats.total_received += 1
                            buffer.append(comment)

                            ts_str = datetime.fromtimestamp(comment["ts_unix"]).strftime("%H:%M:%S")
                            log.info(f"[{ts_str}] {comment['author']}: {comment['text'][:120]}")

                            if len(buffer) >= args.min_batch:
                                await flush()
                    finally:
                        flush_task.cancel()
                        if buffer:
                            await flush()

            except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                log.warning(f"WS desconectado: {e}. Reintentando en 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                log.error(f"Error inesperado: {e}. Reintentando en 10s...")
                await asyncio.sleep(10)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="LiveShopGT Bridge v3 — Conector de live chat con portal de ventas"
    )
    parser.add_argument("--session-key", required=True, help="Session Key del live")
    parser.add_argument(
        "--api-url",
        default="https://liveshopgt.replit.app/api",
        help="URL base API LiveShopGT (acepta con o sin /api)",
    )
    parser.add_argument(
        "--ws-url",
        default="ws://127.0.0.1:8765",
        help="WebSocket del scraper local",
    )
    parser.add_argument(
        "--auto-register",
        action="store_true",
        default=False,
        help="Permitir auto-registro de pedidos de alta confianza",
    )
    parser.add_argument(
        "--batch-interval",
        type=float,
        default=5.0,
        help="Segundos entre envíos al API",
    )
    parser.add_argument(
        "--min-batch",
        type=int,
        default=3,
        help="Enviar al acumular N comentarios",
    )
    parser.add_argument(
        "--products",
        default=None,
        help=(
            'Productos del live. "Brillante:75,VinoM:50" o '
            "JSON: '[{\"code\":\"BRI\",\"description\":\"Brillante\",\"price\":75}]'"
        ),
    )
    parser.add_argument(
        "--platform",
        default="facebook",
        choices=["facebook", "tiktok", "youtube"],
        help="Plataforma del live",
    )
    parser.add_argument(
        "--dedupe-ttl",
        type=int,
        default=180,
        help="TTL (s) para dedupe local de mensajes",
    )
    parser.add_argument("--verbose", action="store_true", help="Logs debug")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    print("=" * 60)
    print(" LiveShopGT Bridge v3")
    print(" Ctrl+C para detener")
    print("=" * 60)

    try:
        asyncio.run(bridge_loop(args))
    except KeyboardInterrupt:
        print("\nDetenido.")
        print(f"Resumen final: {stats.summary()}")


if __name__ == "__main__":
    main()
