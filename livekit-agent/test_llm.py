import os, sys, asyncio, time
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
load_dotenv(".env")

from livekit.plugins import openai
from livekit.agents.llm import ChatContext

async def test():
    llm = openai.LLM(
        model=os.getenv("LLM_CHOICE", "google/gemini-2.5-flash"),
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENAI_API_KEY")
    )
    chat_ctx = ChatContext()
    chat_ctx.add_message(role="system", content="Reply in 1 sentence in Hindi.")
    chat_ctx.add_message(role="user", content="hello")
    
    t0 = time.time()
    stream = llm.chat(chat_ctx=chat_ctx)
    
    count = 0
    full = ""
    async for chunk in stream:
        count += 1
        # The LiveKit wrapper might put content in choices[0].delta.content
        text = ""
        if hasattr(chunk, "choices") and len(chunk.choices) > 0:
            text = getattr(chunk.choices[0].delta, "content", "") or ""
        # Also check delta directly
        if hasattr(chunk, "delta") and hasattr(chunk.delta, "content"):
            text = chunk.delta.content or ""
            
        full += text
        print(f"  [{time.time()-t0:.1f}s] Chunk {count}: {text!r}")
        
    print(f"Total chunks: {count} in {time.time()-t0:.1f}s")
    print(f"Full response: {full}")

asyncio.run(test())
