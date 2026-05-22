# NAVI — servidor local (sin Railway)

## Requisitos

- Python 3.10+
- `ffmpeg` en PATH (TTS)
- Archivo `.env` en `electronica/` con:
  - `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`
  - `GROQ_API_KEY`

## Arrancar (obligatorio para ver estilos)

**No abras** `index.html` con doble clic (`file://`). El CSS solo carga bien con el servidor:

```powershell
cd electronica
pip install -r requirements.txt
python main.py
```

En la consola debe salir algo como:
`CSS existe: True → ...\electronica\static\css\navi.css`

Luego en el navegador (no Live Preview del IDE):

- Landing: http://127.0.0.1:8000/
- Panel: http://127.0.0.1:8000/app
- Comprobar CSS: http://127.0.0.1:8000/css/navi.css (debe verse código CSS, no error)
- Health: http://127.0.0.1:8000/health → `"css_ok": true`

Si la página se ve sin colores: Ctrl+F5 o ventana privada.
- WebSocket ESP32: `ws://TU_IP:8000/ws`
- WebSocket teléfono/web: `ws://TU_IP:8000/ws/phone`

## Arquitectura: una sola sala (`gafas-test`)

Todos los dispositivos comparten **la misma sala LiveKit** (variable `DEFAULT_ROOM` / `.env`):

| Participante | Enlace | Rol |
|--------------|--------|-----|
| **Paneles web** (PC, móvil ngrok) | WebSocket `/ws/phone` + **LiveKit room** | Sync de modo/obstáculo/gafas por la sala; audio/grabación/foto por WS |
| **Bridge Python** | LiveKit + WS | Une la sala con el ESP32 y el agente IA |
| **Agente IA** | LiveKit | Respuestas de voz (TTS en la sala) |
| **ESP32-CAM** | WebSocket `/ws` (no puede usar WebRTC) | Cámara, botones, ultrasonido |

No hay “salas distintas” por dispositivo: si cambias modo en el teléfono, el evento va a la sala y la PC lo recibe al instante.

Si `NAVI_USE_LIVEKIT=0`, no hay sala RTC y el sync queda solo por WebSocket del bridge.

## Dos conexiones WS (no es Bluetooth)

| Qué | Cómo |
|-----|------|
| **Panel / teléfono** | `/app` → **Conectar** → WS + sala LiveKit |
| **Gafas ESP32-CAM** | `WS_BRIDGE_HOST` = IP del PC → `/ws` |

«Gafas no conectadas» = la CAM aún no llegó a `/ws` (revisa IP en el sketch).

Firmware recomendado: `tests/glass_cam_solo/glass_cam_solo.ino`

Defaults ya configurados para tu LAN (cámbialos si tu PC usa otra IP):

```cpp
#define WS_BRIDGE_USE_SSL 0
#define WS_BRIDGE_HOST    "172.20.10.9"    // IP Wi‑Fi del PC en ipconfig (¡SIN puerto!)
#define WS_BRIDGE_PORT    8000
#define WS_BRIDGE_PATH    "/ws"
```

**Errores comunes** que se ven como `[ws] disconnected` en bucle:

- `WS_BRIDGE_HOST` con puerto incluido (ej. `"192.168.0.13:8000"`). NO. El puerto va aparte.
- `WS_BRIDGE_HOST "0.0.0.0:8000"`. `0.0.0.0` no es destino, es la IP que el servidor escucha.
- `WS_BRIDGE_USE_SSL 1` apuntando a un servidor local (que sirve `ws://` plano). Pon `0`.
- PC en una WiFi distinta a la del ESP32 (revisa que sean la misma subred 192.168.0.x).

Cada cambio en estas constantes requiere recompilar y reflashear el ESP32.

## Teléfono en la misma WiFi (o ngrok)

1. Averigua la IP del PC: `ipconfig` → IPv4 (ej. `192.168.0.13`), o usa ngrok: `ngrok http 8000`.
2. En el móvil abre: `http://TU_IP:8000/app` o la URL `https://….ngrok-free.dev/app`.
3. Pulsa **Conectar** en el panel (PC y móvil pueden estar conectados **a la vez**).
4. El modo y el estado de las gafas se **sincronizan** entre dispositivos (un solo estado en el servidor, no salas distintas).
5. En móvil/ngrok las alertas de modo y obstáculo usan **TTS del servidor** (más fiable que la voz del navegador).

## Error LiveKit 429 / 401 al enviar audio

Si en consola aparece `connection minutes limit exceeded` o `401 Unauthorized`:

En `.env` añade:

```
NAVI_USE_LIVEKIT=0
```

Reinicia `python main.py`. El audio del teléfono usará **Groq + TTS** sin abrir más conexiones a LiveKit Cloud.

Sigue siendo obligatorio `GROQ_API_KEY`.

## Probar solo la web (sin gafas)

1. `python main.py`
2. Abre `/app` y pulsa **Conectar**.
3. Pulsa el botón rojo de **Grabar**. Habla. Pulsa otra vez para enviar.
4. Espera la respuesta de Navi (voz).

> El botón de micrófono funciona por **toggle**: un clic empieza a grabar, otro clic
> termina y envía. No es necesario mantener pulsado.

## Caché del navegador

Si tras actualizar el frontend ves errores `app-panel.css 404` o el diseño viejo:

- Recarga con **Ctrl + Shift + R** (Windows) / **Cmd + Shift + R** (Mac).
- O abre una pestaña en modo incógnito.
- El servidor ya envía `Cache-Control: no-cache` y sirve `navi.css` aunque pidan `app-panel.css`.

## Health check

http://127.0.0.1:8000/health
