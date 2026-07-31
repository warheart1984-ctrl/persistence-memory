import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    host = os.getenv("JARVIS_HOST", "0.0.0.0")
    port = int(os.getenv("JARVIS_PORT", "8001"))
    env = (os.getenv("JARVIS_ENV") or "development").lower()
    reload = env not in {"production", "prod"}
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)
