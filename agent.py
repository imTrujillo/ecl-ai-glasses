from __future__ import annotations
import asyncio
import logging
import os
import httpx
from livekit.agents import (
    AutoSubscribe, JobContext, WorkerOptions, cli, Agent, AgentSession
)
from livekit.rtc import ParticipantKind
from livekit.plugins import groq, silero
from edge_tts_plugin import EdgeTTS
from dotenv import load_dotenv
from api import all_tools
from prompts import INSTRUCTIONS, MODE_OCR, MODE_DESCRIBE, MODE_ASSISTANT

load_dotenv()
logger = logging.getLogger("smart-glasses-agent")
logger.setLevel(logging.INFO)

_last_tts_sent = {"text": "", "ts": 0.0}


async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)

    logger.info("⏳ Esperando participante humano...")
    participant = None
    try:
        participant = await asyncio.wait_for(
            ctx.wait_for_participant(),
            timeout=30.0
        )
    except asyncio.TimeoutError:
        logger.warning("⏳ Timeout esperando participante humano")
        return
    except RuntimeError as e:
        # Evita crash-loop cuando la sala se cae durante el warmup
        logger.warning(f"⚠️ Sala desconectada esperando participante: {e}")
        return
    except Exception as e:
        logger.error(f"❌ Error esperando participante: {e}")
        return

    logger.info(f"✅ Got participant: {participant.identity}")

    if participant is None:
        logger.warning("⏳ Nadie se conectó en 120s")
        return
    
    logger.info(f"✅ Participante conectado: {participant.identity}")

    current_mode  = {"value": "assistant"}
    image_chunks  = {"parts": [], "total": 0, "mode": "describe"}
    mode_prompts  = {
        "ocr":       MODE_OCR,
        "describe":  MODE_DESCRIBE,
        "assistant": MODE_ASSISTANT,
    }

    agent   = Agent(instructions=INSTRUCTIONS, tools=all_tools())
    session = AgentSession(
        vad=silero.VAD.load(min_silence_duration=0.6, activation_threshold=0.6),
        stt=groq.STT(model="whisper-large-v3-turbo", language="es"),
        llm=groq.LLM(model="llama-3.3-70b-versatile"),
        tts=EdgeTTS(voice="es-PY-TaniaNeural"),
    )

    async def _say_and_send(text: str):
        session.say(text)
        try:
            await ctx.room.local_participant.publish_data(
                f"TTS:{text}".encode(), reliable=True
            )
            _last_tts_sent["text"] = text
            _last_tts_sent["ts"] = asyncio.get_event_loop().time()
        except Exception as e:
            logger.error(f"Error enviando TTS data: {e}")

    async def _process_image(image_b64: str, mode: str):
        instruction = mode_prompts.get(mode, MODE_DESCRIBE)
        logger.info(f"[IMG] 🔍 Procesando — modo={mode}")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}"},
                    json={
                        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": instruction},
                                {"type": "image_url", "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_b64}"
                                }}
                            ]
                        }],
                        "max_tokens": 300
                    }
                )
            result = response.json()
            logger.info(f"[IMG] Groq response keys: {list(result.keys())}")
            if "choices" not in result:
                logger.error(f"[IMG] ❌ Sin choices — respuesta: {result}")
                await _say_and_send("No pude procesar la imagen.")
                return
            description = result["choices"][0]["message"]["content"]
            logger.info(f"[IMG] ✅ Vision OK — modo={mode}")
            await _say_and_send(description)
        except Exception as e:
            logger.error(f"Vision error: {e}")
            await _say_and_send("No pude procesar la imagen.")

    @session.on("agent_speech_committed")
    def on_speech_committed(msg, *args, **kwargs):
        text = msg.content if hasattr(msg, "content") else str(msg)
        if text.strip():
            now = asyncio.get_event_loop().time()
            if (
                text == _last_tts_sent["text"]
                and (now - _last_tts_sent["ts"]) < 2.0
            ):
                return
            _last_tts_sent["text"] = text
            _last_tts_sent["ts"] = now
            asyncio.ensure_future(
                ctx.room.local_participant.publish_data(
                    f"TTS:{text}".encode(), reliable=True
                )
            )
            
    @ctx.room.on("data_received")
    def on_data_received(packet, *args, **kwargs):
        try:
            message = packet.data.decode("utf-8")

            if message.startswith("MODE:"):
                mode = message.split(":")[1].strip().lower()
                if mode in mode_prompts:
                    current_mode["value"] = mode
                    logger.info(f"[MODE] Cambio a '{mode}' (TTS lo envía el bridge)")

            elif message.startswith("IMG_START:"):
                # formato: IMG_START:modo:0
                parts = message.split(":")
                mode = parts[1] if len(parts) > 2 else current_mode["value"]
                image_chunks["parts"] = []
                image_chunks["total"] = 0
                image_chunks["mode"]  = mode
                logger.info(f"[IMG] 📥 Recibiendo imagen — modo={mode}")

            elif message.startswith("IMG_CHUNK:"):
                _, idx, data = message.split(":", 2)
                image_chunks["parts"].append((int(idx), data))
                logger.info(f"[IMG] 📦 Chunk {idx} recibido — {len(data)} chars")

            elif message == "IMG_END":
                image_chunks["parts"].sort(key=lambda x: x[0])
                full_b64 = "".join(part for _, part in image_chunks["parts"])
                mode     = image_chunks["mode"]
                logger.info(f"[IMG] ✅ Imagen completa — {len(full_b64)} chars b64 modo={mode}")
                asyncio.ensure_future(_process_image(full_b64, mode))

            elif message.startswith("USER_UTTERANCE:"):
                utt = message.split(":", 1)[1].strip()
                if not utt:
                    return
                snippet = utt[:4000]
                logger.info(f"[VOICE] usuario dijo: '{snippet[:80]}'")

                async def _reply_voice_text(user_text):
                    """Groq LLM directo: más fiable que session.generate_reply."""
                    try:
                        async with httpx.AsyncClient(timeout=25) as client:
                            r = await client.post(
                                "https://api.groq.com/openai/v1/chat/completions",
                                headers={
                                    "Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}"
                                },
                                json={
                                    "model": "llama-3.3-70b-versatile",
                                    "messages": [
                                        {"role": "system", "content": INSTRUCTIONS},
                                        {"role": "user", "content": user_text},
                                    ],
                                    "max_tokens": 200,
                                    "temperature": 0.5,
                                },
                            )
                        data = r.json()
                        if "choices" not in data:
                            logger.error(f"[VOICE] Groq sin choices: {data}")
                            await _say_and_send("No pude responder, intenta de nuevo.")
                            return
                        reply = data["choices"][0]["message"]["content"].strip()
                        if not reply:
                            reply = "No tengo respuesta para eso."
                        logger.info(f"[VOICE] LLM → '{reply[:80]}'")
                        await _say_and_send(reply)
                    except Exception as ex:
                        logger.error(f"[VOICE] error: {ex}", exc_info=True)
                        await _say_and_send("Hubo un error procesando tu pregunta.")

                asyncio.ensure_future(_reply_voice_text(snippet))

        except Exception as e:
            logger.error(f"DataChannel error: {e}")

    await session.start(room=ctx.room, agent=agent)
    logger.info("✅ Agente listo — bienvenida y TTS de modo los envía el bridge al ESP32")

if __name__ == "__main__":
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="smart-glasses"
    ))