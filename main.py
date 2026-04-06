import asyncio
import subprocess
import sys
import os

async def run_agent():
    mode = "start" if os.getenv("RAILWAY_ENVIRONMENT") else "dev"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "agent.py", mode,
        stdout=sys.stdout, stderr=sys.stderr
    )
    await proc.wait()

async def run_server():
    port = os.getenv('PORT', '8000')
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "hypercorn", "server:app",
        "--bind", f"0.0.0.0:{port}",
        "--no-websocket-per-message-deflate",  # ✅ deshabilitar compresión WS
        stdout=sys.stdout, stderr=sys.stderr
    )
    await proc.wait()

async def main():
    await asyncio.gather(
        run_server(),
        run_agent(),
    )

if __name__ == "__main__":
    asyncio.run(main())
