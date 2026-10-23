"""
Agent memory in Redis (database 2 on agent-memory; Langfuse uses database 0).

Short-term: the recent turns of one conversation, so "and tomorrow?" has context.
  key  mem:u:<user>:s:<session>   list of {"role", "content"}, last 6 turns, expires 24 h

Long-term: a few facts about a user that carry across conversations.
  key  mem:u:<user>:facts          hash, expires 30 days after the last update
  home_zip  set only when the user states it ("my zip is 77469", "I live in 77469")
  last_zip  the last ZIP that returned a real forecast

Safety rules:
  - every key is namespaced by user ID, and IDs are validated, so one user can never
    read or overwrite another's memory
  - only turns that passed both guardrails are stored (blocked attempts are not)
  - facts are extracted with fixed patterns (5-digit ZIPs only), never free text,
    so nothing a user types can be planted into another conversation's instructions
  - everything expires; forget(user) deletes all of a user's memory at once
"""
import json
import os
import re
from functools import lru_cache

import redis
from langfuse import observe

REDIS_HOST = os.environ.get("REDIS_HOST", "agent-memory")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
MEMORY_DB = int(os.environ.get("AGENT_MEMORY_DB", "2"))
SESSION_TTL = int(os.environ.get("MEMORY_SESSION_TTL", str(24 * 3600)))
FACTS_TTL = int(os.environ.get("MEMORY_FACTS_TTL", str(30 * 24 * 3600)))
MAX_TURNS = 6            # turns kept per conversation (user + assistant = 1 turn)
MAX_CHARS = 2000         # per stored message

ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
HOME_ZIP_RES = [
    re.compile(r"\bmy\s+(?:home\s+)?zip(?:\s*code)?\s*(?:is|=|:)\s*(\d{5})\b", re.I),
    re.compile(r"\bI\s+live\s+in\s+(?:zip(?:\s*code)?\s*)?(\d{5})\b", re.I),
]
FORGET_RE = re.compile(
    r"^\s*(?:please\s+)?(?:forget\s+me|forget\s+everything(?:\s+about\s+me)?|"
    r"delete\s+my\s+(?:data|memory))\W*$", re.I)


class MemoryInputError(ValueError):
    pass


@lru_cache(maxsize=1)
def _redis():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=MEMORY_DB,
                       password=os.environ.get("REDIS_PASSWORD") or None,  # from .env via compose
                       decode_responses=True, socket_timeout=2, socket_connect_timeout=2)


def check_id(kind: str, value: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise MemoryInputError(f"{kind} must be 1-64 characters of letters, digits, '.', '_' or '-'")
    return value


def _session_key(user, session):
    return f"mem:u:{check_id('user_id', user)}:s:{check_id('session_id', session)}"


def _facts_key(user):
    return f"mem:u:{check_id('user_id', user)}:facts"


@observe(name="memory:load")
def load(user: str, session: str) -> dict:
    """{"history": [{"role", "content"}...], "facts": {"home_zip": ..., ...}}"""
    pipe = _redis().pipeline()
    pipe.lrange(_session_key(user, session), 0, -1)
    pipe.hgetall(_facts_key(user))
    raw_history, facts = pipe.execute()
    history = []
    for item in raw_history:
        try:
            msg = json.loads(item)
        except json.JSONDecodeError:
            continue
        if msg.get("role") in ("user", "assistant") and isinstance(msg.get("content"), str):
            history.append({"role": msg["role"], "content": msg["content"]})
    return {"history": history, "facts": facts}


def facts_from_turn(user_message: str, tool_calls: list, tool_outputs: list) -> dict:
    """Facts worth keeping from one approved turn (fixed patterns only)."""
    facts, verified = {}, []
    for call, out in zip(tool_calls, tool_outputs):
        if call.get("name") != "get_weather_forecast":
            continue
        try:
            data = json.loads(out)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("forecast"), dict) \
                and re.fullmatch(r"\d{5}", str(data.get("zipcode", ""))):
            verified.append(str(data["zipcode"]))
    if verified:
        facts["last_zip"] = verified[-1]
    for rx in HOME_ZIP_RES:
        m = rx.search(user_message or "")
        # Only keep a stated home ZIP that actually returned a forecast this turn,
        # so typos and invalid ZIPs ("my zip is 00000") are never remembered.
        if m and m.group(1) in verified:
            facts["home_zip"] = m.group(1)
            break
    return facts


@observe(name="memory:save")
def save_turn(user: str, session: str, user_message: str, answer: str, facts: dict) -> None:
    skey, fkey = _session_key(user, session), _facts_key(user)
    pipe = _redis().pipeline()
    pipe.rpush(skey,
               json.dumps({"role": "user", "content": user_message[:MAX_CHARS]}),
               json.dumps({"role": "assistant", "content": answer[:MAX_CHARS]}))
    pipe.ltrim(skey, -2 * MAX_TURNS, -1)
    pipe.expire(skey, SESSION_TTL)
    if facts:
        pipe.hset(fkey, mapping=facts)
        pipe.expire(fkey, FACTS_TTL)
    pipe.execute()


def get_facts(user: str) -> dict:
    return _redis().hgetall(_facts_key(user))


@observe(name="memory:forget")
def forget(user: str) -> int:
    """Delete every memory key for this user. Returns how many keys were removed."""
    pattern = f"mem:u:{check_id('user_id', user)}:*"
    r = _redis()
    keys = list(r.scan_iter(match=pattern, count=200))
    return r.delete(*keys) if keys else 0


def facts_prompt(facts: dict) -> str:
    """System-prompt text describing what we remember (values are validated ZIPs)."""
    parts = []
    if re.fullmatch(r"\d{5}", facts.get("home_zip", "")):
        parts.append(f"their home ZIP is {facts['home_zip']}")
    if re.fullmatch(r"\d{5}", facts.get("last_zip", "")) and facts.get("last_zip") != facts.get("home_zip"):
        parts.append(f"the last ZIP they asked about was {facts['last_zip']}")
    if not parts:
        return ("You have no remembered location for this user: if they don't give one, "
                "ask for their 5-digit ZIP code instead of guessing.")
    return ("From earlier conversations you remember that " + " and ".join(parts) + ". "
            "If the user doesn't name a location, use their home ZIP (or else the last ZIP) "
            "and say which one you used.")
