"""
Chat API for the weather agent, with Redis memory.

Runs as the agent-api service (uvicorn chat_api:app), published through the nginx
gateway at https://api.ai.internal. Every call except /health needs an API key in the
X-API-Key header. The key decides who you are: memory is tied to the key's user, so a
caller can never read or change someone else's memory.
Create a key on the host: python3 vault/create_api_key.py <user>

  POST   /chat     {"session_id", "message"} -> {"answer", "blocked", "session_id", "latency_s"}
  GET    /memory   what the agent remembers about you (long-term facts)
  DELETE /memory   forget you (same as sending "forget me")
  GET    /health   liveness (no key needed)

  curl -s https://api.ai.internal/chat -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
       -d '{"session_id":"s1","message":"Weather in 77469 today?"}'
"""
import hashlib
import hmac
import threading
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from langfuse import propagate_attributes
from pydantic import BaseModel, Field

import agent_loop
import memory
from vault_client import VaultError, read_secret

ID_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"
KEYS_PATH = "agent/api-keys"     # Vault: {sha256(api key): user_id}
KEYS_CACHE_S = 60                # new or revoked keys take effect within a minute

app = FastAPI(title="Weather agent API", version="1.1",
              description="Guarded weather agent with Redis memory")

# One request at a time: there is one GPU, and the agent keeps per-run state
# (LAST_RUN) that concurrent requests would overwrite.
_agent_lock = threading.Lock()
_keys = {"loaded_at": 0.0, "map": {}}
_keys_lock = threading.Lock()


def _key_map() -> dict:
    with _keys_lock:
        if time.time() - _keys["loaded_at"] > KEYS_CACHE_S:
            try:
                _keys["map"] = read_secret(KEYS_PATH)
            except VaultError as e:
                if "not found" not in str(e):
                    raise HTTPException(status_code=503, detail="Key store unavailable") from e
                _keys["map"] = {}  # no keys created yet: everyone is refused
            _keys["loaded_at"] = time.time()
        return _keys["map"]


def current_user(x_api_key: str | None = Header(default=None)) -> str:
    """Map the X-API-Key header to a user. Only hashes of keys are stored in Vault."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    digest = hashlib.sha256(x_api_key.encode()).hexdigest()
    user = None
    for stored, owner in _key_map().items():
        if hmac.compare_digest(stored, digest):  # constant-time comparison
            user = owner
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user


class ChatRequest(BaseModel):
    session_id: str = Field(pattern=ID_PATTERN, description="ID of this conversation")
    message: str = Field(min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    answer: str
    blocked: bool
    session_id: str
    latency_s: float


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user: str = Depends(current_user)):
    start = time.perf_counter()
    with _agent_lock:
        # user/session show up in Langfuse (Sessions view groups each conversation)
        with propagate_attributes(user_id=user, session_id=req.session_id):
            try:
                answer = agent_loop.run_agent(req.message, user_id=user, session_id=req.session_id)
            except memory.MemoryInputError as e:
                raise HTTPException(status_code=422, detail=str(e))
            finally:
                agent_loop.langfuse.flush()
    return ChatResponse(answer=answer, blocked=answer == agent_loop.REFUSAL,
                        session_id=req.session_id,
                        latency_s=round(time.perf_counter() - start, 2))


@app.get("/memory")
def show_memory(user: str = Depends(current_user)):
    return {"user_id": user, "facts": memory.get_facts(user),
            "facts_expire_after_days": memory.FACTS_TTL // 86400}


@app.delete("/memory")
def forget_me(user: str = Depends(current_user)):
    return {"user_id": user, "keys_removed": memory.forget(user)}
