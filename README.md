# FB Live Listener + LiveShopGT Bridge

Sistema en Python para capturar comentarios de **Facebook Live**, limpiarlos/filtrarlos en tiempo real y sincronizarlos con **LiveShopGT API** para operación comercial durante transmisiones en vivo.

---

## 🎯 Objetivo del proyecto

Este proyecto automatiza el flujo operativo de un live:

1. **Escuchar comentarios** de Facebook Live.
2. **Clasificar señales de compra** (precio, cantidad, intención).
3. **Filtrar ruido** (mensajes de sistema, saludos, emojis, etiquetas sin contexto de compra).
4. **Enviar lotes de comentarios limpios** al endpoint de LiveShopGT.
5. Facilitar arranque/operación con un **launcher PowerShell**.

En resumen: convierte el chat de Facebook Live en datos accionables para venta.

---

## 🧱 Componentes

### 1) `fb_live_listener.py`
Listener principal.

- Usa **Playwright** para abrir/usar una pestaña del live.
- Extrae comentarios visibles del panel de chat.
- Emite eventos por **WebSocket local** (`ws://127.0.0.1:8765` por defecto).
- Guarda salida en `data/comments.ndjson`.
- Puede sincronizar opcionalmente directo a endpoint HTTP.

### 2) `liveshopgt_bridge.py`
Puente WS → API.

- Se conecta al WS del listener.
- Estandariza payload de comentarios.
- Aplica deduplicación (TTL) y filtros de calidad.
- Envía lotes al endpoint de ingest de LiveShopGT.
- Muestra métricas operativas en consola.

### 3) `activar_listener_live.ps1`
Automatizador operativo.

- Levanta listener + bridge en el orden correcto.
- Resuelve URL de live desde `LiveId` o `LiveUrl`.
- Libera puerto WS si está ocupado.
- Reporta PIDs para detener procesos fácilmente.

---

## ⚙️ Requisitos

- Windows + PowerShell
- Python 3.10+
- Google Chrome (si usarás CDP)

Dependencias Python:

```bash
pip install -r requirements-fb-listener.txt
pip install -r requirements-liveshopgt-bridge.txt
pip install playwright
playwright install chromium
```

---

## 🚀 Ejecución rápida (recomendada)

```powershell
cd C:\Users\yejc2\.openclaw\workspace
.\activar_listener_live.ps1 -LiveId 2116367269219996
```

También puedes pasar URL completa:

```powershell
.\activar_listener_live.ps1 -LiveUrl "https://www.facebook.com/724698819/videos/2116367269219996"
```

---

## 🧪 Ejecución manual

### 1) Listener

```powershell
python .\fb_live_listener.py --url "https://www.facebook.com/724698819/videos/2116367269219996" --port 8765
```

### 2) Bridge

```powershell
python .\liveshopgt_bridge.py --session-key ac4cc282c27945ce --api-url https://gabolive.replit.app --ws-url ws://127.0.0.1:8765 --platform facebook
```

---

## 🧠 Lógica de filtrado implementada

El bridge descarta mensajes no útiles antes de enviar al endpoint:

- Eventos de sistema del live (join/reaction/share).
- Ruido UI (`Fijar`, `Pinned`, etc.).
- Comentarios de **solo etiquetas/nombres** sin intención de compra.
- Comentarios de **solo emojis/símbolos**.
- Saludos genéricos (`hola`, `buenos días`, `buenas noches`, etc.).

Esto reduce ruido operativo y mejora calidad de datos para atención comercial.

---

## 🔌 Integración con LiveShopGT

El bridge envía comentarios en lotes al endpoint:

- Base URL configurable con `--api-url`
- Endpoint final: `/api/live-listener/ingest`
- Sesión comercial mediante `--session-key`

Parámetros útiles:

- `--batch-interval` (default 5s)
- `--min-batch` (default 3)
- `--auto-register` (opcional)
- `--products` (contexto de productos)

---

## 🩺 Troubleshooting

### Error WS desconectado / WinError 1225
Causa común: listener no está corriendo o no abrió el puerto WS.

Verifica:

```powershell
Get-NetTCPConnection -LocalPort 8765
```

### Error CDP `ECONNREFUSED 127.0.0.1:9222`
Si usas `--cdp-url`, Chrome debe abrirse con remote debugging:

```powershell
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\temp\chrome-live"
```

### Bridge no envía al API
- Revisa `--session-key`
- Revisa conectividad con `--api-url`
- Confirma que el listener produce eventos

---

## 📁 Estructura

```text
.
├─ fb_live_listener.py
├─ liveshopgt_bridge.py
├─ activar_listener_live.ps1
├─ requirements-fb-listener.txt
├─ requirements-liveshopgt-bridge.txt
└─ data/
   └─ comments.ndjson
```

---

## ✅ Estado actual

Proyecto funcional para operación en live, con mejoras recientes en:

- Filtrado de ruido y etiquetas
- Descarte de emojis/saludos
- Automatización de arranque en un solo comando

---

## 🔒 Nota operativa

Este repositorio procesa comentarios públicos del live para fines comerciales y operativos. Se recomienda:

- No almacenar datos sensibles innecesarios.
- Mantener credenciales/keys fuera del código (variables de entorno o secrets manager).
- Revisar cumplimiento de términos de la plataforma usada.
