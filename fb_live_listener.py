#!/usr/bin/env python3
"""
Listener de comentarios para Facebook Live + WebSocket broadcast.

Uso rápido:
  python fb_live_listener.py --url "https://www.facebook.com/..."

Luego conéctate por WebSocket en:
  ws://127.0.0.1:8765

Notas:
- Para leer comentarios del live normalmente necesitas sesión iniciada en Facebook.
- El script intenta extraer comentarios visibles del panel "Chat en vivo".
- Audio opcional: si pasas --audio-out, intenta grabar audio del sistema con ffmpeg.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set

from playwright.async_api import async_playwright, Page, Error as PlaywrightError
import websockets
from websockets.server import WebSocketServerProtocol


@dataclass
class CommentEvent:
    source: str
    author: str
    text: str
    age: str
    ts_unix: float


CLIENTS: Set[WebSocketServerProtocol] = set()

SALE_KEYWORDS = [
    "me lo llevo", "lo quiero", "quiero", "apártame", "apartame", "factura",
    "confirmo", "encargo", "pedido", "pago", "deposito", "transferencia",
    "precio", "cuánto", "cuanto", "talla", "color", "envío", "envio",
    "disponible", "stock", "entrega"
]


def classify_comment(text: str) -> Dict[str, object]:
    t = (text or "").strip().lower()
    if not t:
        return {"kind": "conversation", "saleScore": 0, "signals": []}

    signals = [kw for kw in SALE_KEYWORDS if kw in t]
    has_qty = bool(re.search(r"\b\d+\b", t))
    has_currency = bool(re.search(r"\bq\s?\d+(?:\.\d+)?|\d+(?:\.\d+)?\s?q\b", t))

    score = len(signals)
    if has_qty:
        score += 1
    if has_currency:
        score += 2

    kind = "sale_signal" if score >= 2 else "conversation"
    return {
        "kind": kind,
        "saleScore": score,
        "signals": signals,
        "hasQty": has_qty,
        "hasCurrency": has_currency,
    }


def extract_price_and_quantity(text: str) -> Dict[str, Optional[float]]:
    raw = text or ""

    # Precios: Q199, q 199.50, 199q
    price = None
    m = re.search(r"\bq\s?(\d+(?:\.\d+)?)\b", raw, re.I) or re.search(r"\b(\d+(?:\.\d+)?)\s?q\b", raw, re.I)
    if m:
        try:
            price = float(m.group(1))
        except ValueError:
            price = None

    # Cantidad: x2, 2 unidades, 2 piezas
    quantity = None
    q = re.search(r"\bx\s?(\d+)\b", raw, re.I) or re.search(r"\b(\d+)\s?(?:unidades?|piezas?|uds?)\b", raw, re.I)
    if q:
        try:
            quantity = int(q.group(1))
        except ValueError:
            quantity = None

    return {"unitPrice": price, "quantity": quantity}


async def post_json(url: str, payload: dict, token: str = "", timeout_sec: int = 10):
    def _send():
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url=url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body

    return await asyncio.to_thread(_send)


async def ws_handler(ws: WebSocketServerProtocol):
    CLIENTS.add(ws)
    try:
        await ws.send(json.dumps({"type": "hello", "message": "connected"}, ensure_ascii=False))
        async for _ in ws:
            # Este listener no procesa mensajes entrantes; solo broadcast.
            pass
    finally:
        CLIENTS.discard(ws)


async def broadcast(payload: dict):
    if not CLIENTS:
        return
    data = json.dumps(payload, ensure_ascii=False)
    dead: List[WebSocketServerProtocol] = []
    for ws in CLIENTS:
        try:
            await ws.send(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        CLIENTS.discard(ws)


async def ensure_live_chat_tab(page: Page):
    # Intenta activar "Chat en vivo" si existe el botón.
    for label in ["Chat en vivo", "Live chat"]:
        locator = page.get_by_role("button", name=label)
        if await locator.count() > 0:
            try:
                await locator.first.click(timeout=1500)
                return
            except Exception:
                pass


EXTRACT_JS = r"""
() => {
  const sel = [
    'article',
    '[role="article"]',
    'div[aria-label*="comentario" i]',
    'div[aria-label*="comment" i]'
  ];
  const nodes = Array.from(document.querySelectorAll(sel.join(',')));
  const out = [];

  const uiOnly = /^(Me gusta|Responder|Reaccionar|Ocultar|Reportar|Like|Reply|Resumen|Chat en vivo|Ver conversación|Seguir|Following|Fijar|Fijado|Pin|Pinned comment)$/i;
  const ageOnly = /^\d+\s*(min|m|h|d|s)$/i;
  const justNumber = /^\d+$/;

  for (const a of nodes) {
    const txt = (a.innerText || '').trim();
    if (!txt) continue;

    const lines = txt.split('\n').map(s => s.trim()).filter(Boolean);
    if (lines.length < 2) continue;

    const author = lines[0];
    if (uiOnly.test(author)) continue;

    let age = '';
    for (const ln of lines) {
      if (ageOnly.test(ln)) { age = ln; break; }
    }

    // Candidatos de texto: líneas después del autor, quitando controles/UI.
    const body = lines
      .slice(1)
      .filter(ln => !uiOnly.test(ln))
      .filter(ln => !ageOnly.test(ln))
      .filter(ln => !justNumber.test(ln))
      .filter(ln => ln.length > 1);

    // Evita capturar artículos de "respuestas" sin texto real.
    if (!body.length) continue;

    // Normalmente la primera línea útil es el comentario principal.
    const comment = body[0];

    // Filtro extra: descarta pseudo-textos comunes de UI.
    if (/^(Comentario fijado|Pinned comment)$/i.test(comment)) continue;

    const key = `${author}||${comment}||${age}`;
    out.push({ key, author, text: comment, age });
  }

  return out;
}
"""


async def poll_comments(
    page: Page,
    out_file: Path,
    interval: float = 1.2,
    debug: bool = False,
    sync_endpoint: str = "",
    sync_token: str = "",
    sync_timeout: int = 10,
    sync_sales_only: bool = False,
    sync_mode: str = "live_register",
    default_unit_price: float = 0.01,
    default_quantity: int = 1,
    default_product_code: str = "",
    default_notes: str = "",
    default_source: str = "openclaw",
):
    seen: Set[str] = set()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.touch(exist_ok=True)

    while True:
        try:
            rows = await page.evaluate(EXTRACT_JS)
            if debug:
                print(json.dumps({"type": "debug", "visible_candidates": len(rows)}, ensure_ascii=False), flush=True)
        except Exception as e:
            if debug:
                print(json.dumps({"type": "debug", "eval_error": str(e)}, ensure_ascii=False), flush=True)
            await asyncio.sleep(interval)
            continue

        for row in rows:
            key = row.get("key", "")
            if not key or key in seen:
                continue
            seen.add(key)
            ev = CommentEvent(
                source="facebook_live",
                author=row.get("author", "").strip(),
                text=row.get("text", "").strip(),
                age=row.get("age", "").strip(),
                ts_unix=time.time(),
            )
            analysis = classify_comment(ev.text)
            payload = {"type": "comment", "data": {**asdict(ev), **analysis}}

            # stdout
            print(json.dumps(payload, ensure_ascii=False), flush=True)

            # archivo NDJSON
            with out_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")

            # websocket broadcast
            await broadcast(payload)

            # sync al endpoint cloud (opcional)
            if sync_endpoint and (not sync_sales_only or analysis.get("kind") == "sale_signal"):
                if sync_mode == "live_register":
                    pq = extract_price_and_quantity(ev.text)
                    cloud_payload = {
                        "customerName": ev.author,
                        "description": ev.text,
                        "unitPrice": pq.get("unitPrice") if pq.get("unitPrice") is not None else float(default_unit_price),
                        "productCode": default_product_code or None,
                        "quantity": pq.get("quantity") if pq.get("quantity") is not None else int(default_quantity),
                        "notes": default_notes or f"age={ev.age}; kind={analysis.get('kind')}; saleScore={analysis.get('saleScore')}",
                        "source": default_source,
                    }
                else:
                    cloud_payload = {
                        "platform": "facebook",
                        "channel": "live",
                        "eventType": analysis.get("kind", "conversation"),
                        "author": ev.author,
                        "message": ev.text,
                        "age": ev.age,
                        "tsUnix": ev.ts_unix,
                        "saleScore": analysis.get("saleScore", 0),
                        "signals": analysis.get("signals", []),
                        "source": ev.source,
                        "meta": {
                            "hasQty": analysis.get("hasQty", False),
                            "hasCurrency": analysis.get("hasCurrency", False),
                            "listener": "fb_live_listener.py",
                        },
                    }

                try:
                    status, _ = await post_json(sync_endpoint, cloud_payload, token=sync_token, timeout_sec=sync_timeout)
                    if debug:
                        print(json.dumps({"type": "sync", "status": status, "author": ev.author}, ensure_ascii=False), flush=True)
                except urllib.error.HTTPError as e:
                    print(json.dumps({"type": "sync_error", "status": e.code, "reason": str(e.reason)}, ensure_ascii=False), flush=True)
                except Exception as e:
                    print(json.dumps({"type": "sync_error", "reason": str(e)}, ensure_ascii=False), flush=True)

        await asyncio.sleep(interval)



def start_audio_capture(audio_out: Path, ffmpeg_bin: str = "ffmpeg", device: Optional[str] = None):
    """
    En Windows, graba audio del sistema usando dshow.
    Requiere dispositivo tipo 'Stereo Mix' o equivalente.
    """
    audio_out.parent.mkdir(parents=True, exist_ok=True)

    input_dev = device or "audio=Stereo Mix"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-f", "dshow",
        "-i", input_dev,
        "-ac", "2",
        "-ar", "44100",
        str(audio_out),
    ]

    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def main():
    p = argparse.ArgumentParser(description="Facebook Live comment listener + WebSocket server")
    p.add_argument("--url", required=True, help="URL del live")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--profile-dir", default=".profiles/fb-listener", help="Perfil persistente para mantener sesión")
    p.add_argument("--cdp-url", default="", help="Conectar a Chrome existente por CDP (ej: http://127.0.0.1:9222)")
    p.add_argument("--use-current-tab", action="store_true", help="Si usas --cdp-url, intenta usar la pestaña Facebook ya abierta")
    p.add_argument("--out", default="data/comments.ndjson", help="Archivo de salida NDJSON")
    p.add_argument("--headless", action="store_true", help="Ejecutar sin UI")
    p.add_argument("--audio-out", default="", help="Si se define, intenta grabar audio del sistema a este archivo")
    p.add_argument("--audio-device", default="", help="Dispositivo dshow (ej: 'audio=Stereo Mix')")
    p.add_argument("--debug", action="store_true", help="Muestra métricas de extracción en consola")
    p.add_argument("--sync-endpoint", default="", help="Endpoint HTTP POST para sincronizar comentarios/ventas")
    p.add_argument("--sync-token", default="", help="Bearer token opcional para el endpoint cloud")
    p.add_argument("--sync-timeout", type=int, default=10, help="Timeout HTTP en segundos para sync")
    p.add_argument("--sync-sales-only", action="store_true", help="Sincroniza solo eventos detectados como venta")
    p.add_argument("--sync-mode", choices=["generic", "live_register"], default="live_register", help="Formato de payload para endpoint cloud")
    p.add_argument("--default-unit-price", type=float, default=0.01, help="Precio fallback si no se detecta en comentario")
    p.add_argument("--default-quantity", type=int, default=1, help="Cantidad fallback")
    p.add_argument("--default-product-code", default="", help="Código de producto por defecto")
    p.add_argument("--default-notes", default="", help="Notas por defecto para cada item")
    p.add_argument("--default-source", default="openclaw", help="Campo source para live_register")
    args = p.parse_args()

    out_file = Path(args.out)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.touch(exist_ok=True)
    print(json.dumps({"type": "info", "message": f"Salida NDJSON: {out_file.resolve()}"}, ensure_ascii=False))
    audio_proc = None

    if args.audio_out:
        try:
            audio_proc = start_audio_capture(Path(args.audio_out), device=args.audio_device or None)
            print(json.dumps({"type": "info", "message": f"Audio grabando en {args.audio_out}"}, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({"type": "warn", "message": f"No se pudo iniciar audio: {e}"}, ensure_ascii=False))

    # Si el puerto está ocupado, intenta unos cuantos siguientes.
    ws_server = None
    bound_port = args.port
    last_err = None
    for p_try in range(args.port, args.port + 8):
        try:
            ws_server = await websockets.serve(ws_handler, args.host, p_try)
            bound_port = p_try
            break
        except OSError as e:
            last_err = e
            continue
    if ws_server is None:
        raise last_err or RuntimeError("No se pudo abrir WebSocket")

    print(json.dumps({"type": "info", "message": f"WebSocket listo en ws://{args.host}:{bound_port}"}, ensure_ascii=False))
    if args.sync_endpoint:
        mode = "sales_only" if args.sync_sales_only else "all_comments"
        print(json.dumps({"type": "info", "message": f"Sync cloud activo ({mode}) -> {args.sync_endpoint}"}, ensure_ascii=False))

    try:
        async with async_playwright() as pw:
            if args.cdp_url:
                # Modo recomendado para Facebook: usar tu Chrome real + sesión ya abierta.
                browser = await pw.chromium.connect_over_cdp(args.cdp_url)

                # Busca pestañas en todos los contextos CDP disponibles.
                contexts = list(browser.contexts)
                if not contexts:
                    raise RuntimeError("No se encontraron contextos en Chrome CDP.")

                page = None
                if args.use_current_tab:
                    # Prioriza una URL de video/live de Facebook.
                    preferred = []
                    fallback = []
                    for ctx in contexts:
                        for p0 in ctx.pages:
                            u = (p0.url or "").lower()
                            if "facebook.com" not in u:
                                continue
                            if "/videos/" in u or "/watch/live" in u:
                                preferred.append(p0)
                            else:
                                fallback.append(p0)

                    page = preferred[0] if preferred else (fallback[0] if fallback else None)

                    if page is None:
                        raise RuntimeError(
                            "No encontré una pestaña de Facebook abierta en Chrome. "
                            "Abre manualmente el live en ese Chrome y vuelve a ejecutar."
                        )
                else:
                    # Sin --use-current-tab: toma cualquier página activa y navega.
                    ctx = contexts[0]
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    try:
                        await page.goto(args.url, wait_until="domcontentloaded")
                    except PlaywrightError:
                        # Si la pestaña se cerró, intenta una sola vez con otra.
                        ctx2 = contexts[0]
                        page = ctx2.pages[0] if ctx2.pages else await ctx2.new_page()
                        await page.goto(args.url, wait_until="domcontentloaded")
            else:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=args.profile_dir,
                    headless=args.headless,
                    viewport={"width": 1280, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(args.url, wait_until="domcontentloaded")

            print(json.dumps({"type": "info", "page_url": page.url, "title": await page.title()}, ensure_ascii=False))
            await ensure_live_chat_tab(page)
            await poll_comments(
                page,
                out_file=out_file,
                debug=args.debug,
                sync_endpoint=args.sync_endpoint,
                sync_token=args.sync_token,
                sync_timeout=args.sync_timeout,
                sync_sales_only=args.sync_sales_only,
                sync_mode=args.sync_mode,
                default_unit_price=args.default_unit_price,
                default_quantity=args.default_quantity,
                default_product_code=args.default_product_code,
                default_notes=args.default_notes,
                default_source=args.default_source,
            )
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        if audio_proc:
            audio_proc.terminate()


if __name__ == "__main__":
    if sys.platform != "win32":
        print("[Aviso] El modo de audio dshow está pensado para Windows.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
