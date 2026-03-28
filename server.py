import os
from quart import Quart, request, jsonify
from quart_cors import cors
from livekit import api
from livekit.api import LiveKitAPI, CreateRoomRequest, AccessToken, VideoGrants
from dotenv import load_dotenv

load_dotenv()

app = Quart(__name__)
app = cors(app, allow_origin="*")

DEFAULT_ROOM = "gafas-test"
_dispatch_created = False


async def clean_room_on_startup():
    """Limpia todos los dispatches viejos al arrancar el servidor."""
    lk = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    try:
        await lk.room.create_room(CreateRoomRequest(name=DEFAULT_ROOM))
        existing = await lk.agent_dispatch.list_dispatch(room_name=DEFAULT_ROOM)
        dispatches = getattr(existing, "agent_dispatches", [])
        for d in dispatches:
            try:
                await lk.agent_dispatch.delete_dispatch(
                    api.DeleteAgentDispatchRequest(dispatch_id=d.id, room=DEFAULT_ROOM)
                )
                print(f"🗑️ Dispatch viejo eliminado al inicio: {d.id}")
            except Exception as e:
                print(f"⚠️ No se pudo eliminar {d.id}: {e}")
        print(f"✅ Sala '{DEFAULT_ROOM}' limpia y lista.")
    except Exception as e:
        print(f"⚠️ Startup cleanup: {e}")
    finally:
        await lk.aclose()


@app.before_serving
async def startup():
    """Se ejecuta UNA vez cuando arranca el servidor."""
    await clean_room_on_startup()


@app.route("/getToken")
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
            print(f"⚠️ Error creando dispatch: {e}")
        finally:
            await lk.aclose()
    else:
        print("⚡ Dispatch ya existe, omitiendo.")

    return jsonify({"token": token.to_jwt(), "room": room})


@app.route("/reset")
async def reset():
    global _dispatch_created
    _dispatch_created = False
    await clean_room_on_startup()
    return jsonify({"status": "reset ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)