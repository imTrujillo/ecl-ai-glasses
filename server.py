import logging
import mimetypes
import os
from pathlib import Path

from quart import Quart, jsonify, request, send_file, send_from_directory, websocket
from quart_cors import route_cors
from livekit import api
from livekit.api import LiveKitAPI, CreateRoomRequest, AccessToken, VideoGrants
from dotenv import load_dotenv
from ws_bridge import handle_esp32_quart, handle_phone_quart

load_dotenv()

logger = logging.getLogger("navi-server")

# Ruta absoluta a static/ (no depende del directorio desde donde ejecutas main.py)
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = Quart(__name__)
DEFAULT_ROOM = "gafas-test"
_dispatch_created = False

MIME_OVERRIDES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/png",
    ".html": "text/html; charset=utf-8",
}


def _static_path(*parts: str) -> Path:
    return STATIC_DIR.joinpath(*parts)


async def _send_static(rel_path: str):
    """Sirve un archivo de static/ con MIME correcto (evita CSS sin estilo)."""
    full = _static_path(rel_path)
    if not full.is_file():
        logger.warning("static 404: %s", rel_path)
        return jsonify({"error": "not found", "path": rel_path}), 404
    suffix = full.suffix.lower()
    mime = MIME_OVERRIDES.get(suffix) or mimetypes.guess_type(full.name)[0]
    resp = await send_file(full, mimetype=mime)
    # Desarrollo local: evita que el navegador quede con CSS/HTML viejos
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


async def clean_room_on_startup():
    lk = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    try:
        await lk.room.create_room(CreateRoomRequest(name=DEFAULT_ROOM))
        existing = await lk.agent_dispatch.list_dispatch(room_name=DEFAULT_ROOM)
        for d in getattr(existing, "agent_dispatches", []):
            try:
                await lk.agent_dispatch.delete_dispatch(
                    api.DeleteAgentDispatchRequest(dispatch_id=d.id, room=DEFAULT_ROOM)
                )
                print(f"[lk] Dispatch eliminado: {d.id}")
            except Exception:
                pass
        print(f"[lk] Sala '{DEFAULT_ROOM}' lista.")
    except Exception as e:
        print(f"[lk] Startup LiveKit: {e}")
    finally:
        await lk.aclose()


@app.before_serving
async def startup():
    css = _static_path("css", "navi.css")
    print(f"[navi] STATIC_DIR = {STATIC_DIR}")
    print(f"[navi] CSS existe: {css.is_file()} -> {css}")
    try:
        await clean_room_on_startup()
    except Exception as e:
        print(f"[navi] LiveKit al arrancar (la web sigue): {e}")


@app.route("/api/navi/config")
@route_cors(allow_origin="*")
async def navi_config():
    """Sala única LiveKit + URL para que todos los paneles se sincronicen."""
    use_lk = os.getenv("NAVI_USE_LIVEKIT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    lk_url = os.getenv("LIVEKIT_URL", "").strip()
    return jsonify({
        "room": DEFAULT_ROOM,
        "livekit_url": lk_url,
        "livekit_enabled": use_lk and bool(lk_url) and bool(os.getenv("LIVEKIT_API_KEY")),
    })


@app.route("/getToken")
@route_cors(allow_origin="*")
async def get_token():
    global _dispatch_created
    name = request.args.get("name", "guest")
    room = request.args.get("room", DEFAULT_ROOM)

    token = (
        AccessToken(
            os.getenv("LIVEKIT_API_KEY"),
            os.getenv("LIVEKIT_API_SECRET"),
        )
        .with_identity(name)
        .with_name(name)
        .with_grants(VideoGrants(room_join=True, room=room))
    )

    if not _dispatch_created:
        lk = LiveKitAPI(
            url=os.getenv("LIVEKIT_URL"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
        )
        try:
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(agent_name="smart-glasses", room=room)
            )
            _dispatch_created = True
            print("[lk] Dispatch creado.")
        except Exception as e:
            print(f"[lk] Error: {e}")
        finally:
            await lk.aclose()
    else:
        print("[lk] Dispatch ya existe.")

    return jsonify({"token": token.to_jwt(), "room": room})


@app.route("/reset")
@route_cors(allow_origin="*")
async def reset():
    global _dispatch_created
    _dispatch_created = False
    await clean_room_on_startup()
    return jsonify({"status": "reset ok"})


@app.route("/health")
async def health():
    css_path = _static_path("css", "navi.css")
    css_ok = css_path.is_file()
    css_build = ""
    if css_ok:
        try:
            css_build = css_path.read_text(encoding="utf-8").split("\n", 1)[0][:80]
        except OSError:
            pass
    return jsonify({
        "status": "ok",
        "service": "navi-bridge",
        "static_dir": str(STATIC_DIR),
        "css_ok": css_ok,
        "css_build": css_build,
    })


@app.route("/favicon.ico")
async def favicon():
    return await _send_static("assets/favicon.png")


@app.route("/")
async def landing():
    return await _send_static("index.html")


@app.route("/app")
async def app_page():
    return await _send_static("app.html")


@app.route("/assets/<path:filename>")
async def assets(filename):
    return await _send_static(f"assets/{filename}")


@app.route("/css/<path:filename>")
async def css_files(filename):
    # Compat: HTML viejo cacheado puede pedir app-panel.css (ya unificado en navi.css).
    if filename.split("?")[0].endswith("app-panel.css"):
        return await _send_static("css/navi.css")
    return await _send_static(f"css/{filename}")


@app.route("/js/<path:filename>")
async def js_files(filename):
    return await _send_static(f"js/{filename}")


@app.websocket("/ws")
async def ws_esp32():
    await websocket.accept()
    await handle_esp32_quart()


@app.websocket("/ws/phone")
async def ws_phone():
    await websocket.accept()
    await handle_phone_quart()
