from __future__ import annotations
import io
import uuid
import edge_tts
from livekit.agents import tts as agents_tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions


class EdgeTTS(agents_tts.TTS):
    def __init__(self, voice: str = "es-PY-TaniaNeural"):
        super().__init__(
            capabilities=agents_tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._voice = voice

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> agents_tts.ChunkedStream:
        return EdgeTTSStream(
            tts=self,
            input_text=text,
            voice=self._voice,
            conn_options=conn_options,
        )


class EdgeTTSStream(agents_tts.ChunkedStream):
    def __init__(self, *, tts: EdgeTTS, input_text: str, voice: str, conn_options: APIConnectOptions):
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._voice = voice

    async def _run(self, output_emitter: agents_tts.AudioEmitter) -> None:
        # 1. Sintetizar todo el audio con edge-tts (devuelve MP3)
        communicate = edge_tts.Communicate(self._input_text, self._voice)
        audio_buffer = io.BytesIO()

        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_buffer.write(chunk["data"])

        audio_data = audio_buffer.getvalue()

        # 2. ✅ Inicializar el emitter con el formato correcto
        #    El SDK decodifica el MP3 internamente con mime_type="audio/mpeg"
        output_emitter.initialize(
            request_id=str(uuid.uuid4()),
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/mpeg",   # edge-tts produce MP3
        )

        # 3. Enviar los bytes — _main_task llama end_input() automáticamente después
        output_emitter.push(audio_data)