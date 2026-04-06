import os
from quart import Quart, request, jsonify, websocket
from quart_cors import cors, route_cors
from livekit import api
from livekit.api import LiveKitAPI, CreateRoomRequest, AccessToken, VideoGrants
from dotenv import load_dotenv
from ws_bridge import handle_esp32_quart

load_dotenv()

app = Quart(__name__)
# ✅ NO aplicar cors a toda la app, solo a rutas específicas

DEFAULT_ROOM = "gafas-test"
_dispatch_created = False



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
                print(f"🗑️ Dispatch eliminado: {d.id}")
            except Exception:
                pass
        print(f"✅ Sala '{DEFAULT_ROOM}' lista.")
    except Exception as e:
        print(f"⚠️ Startup: {e}")
    finally:
        await lk.aclose()



@app.before_serving
async def startup():
    await clean_room_on_startup()


async def _connect_bridge():
    """Pre-conecta el bridge a LiveKit al arrancar."""
    from ws_bridge import connect_to_livekit
    try:
        await connect_to_livekit()
        print("✅ Bridge pre-conectado a LiveKit")
    except Exception as e:
        print(f"⚠️ Bridge startup error: {e}")

@app.route("/getToken")
@route_cors(allow_origin="*")  # ✅ CORS solo en esta ruta
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
            print("✅ Dispatch creado.")
        except Exception as e:
            print(f"⚠️ Error: {e}")
        finally:
            await lk.aclose()
    else:
        print("⚡ Dispatch ya existe.")

    return jsonify({"token": token.to_jwt(), "room": room})


@app.route("/reset")
@route_cors(allow_origin="*")  # ✅ CORS solo en esta ruta
async def reset():
    global _dispatch_created
    _dispatch_created = False
    await clean_room_on_startup()
    return jsonify({"status": "reset ok"})


@app.websocket("/ws")
async def ws():
    # ✅ Deshabilitar compresión — el ESP32 no soporta gzip/deflate
    await websocket.accept(headers={
        "Content-Encoding": "identity",
    })
    await handle_esp32_quart()


if __name__ == "__main__":
    import subprocess, sys
    port = os.getenv("PORT", "8000")  # ✅ Railway asigna PORT
    subprocess.run([
        sys.executable, "-m", "hypercorn", "server:app",
        "--bind", f"0.0.0.0:{port}"  # ✅ usar ese puerto
    ])