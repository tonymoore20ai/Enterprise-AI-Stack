"""
Weather agent: Qwen (vLLM) decides to call get_weather_forecast for a ZIP code,
the tool runs, and the model writes the final answer.

Tracing:
  - Langfuse  -> LLM calls, tool calls, the whole run as one trace
  - OTel      -> same spans also go to agent-otel -> ClickHouse/Grafana

Run inside the container:
  docker exec agent-orchestrator python agent_loop.py
  docker exec -e ZIPCODE=77002 agent-orchestrator python agent_loop.py
"""
import os
import re
import json
import asyncio
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import qdrant_client
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

# ---------------------------------------------------------------------------
# 1. OpenTelemetry -> your collector (set up BEFORE Langfuse so both share it)
# ---------------------------------------------------------------------------
resource = Resource.create({"service.name": "agent-orchestrator"})
provider = TracerProvider(resource=resource)
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="http://agent-otel:4318/v1/traces"))
)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("agent-tracer")

# ---------------------------------------------------------------------------
# 2. Langfuse + traced OpenAI client pointed at vLLM
# ---------------------------------------------------------------------------
# Langfuse API keys live in Vault (secret/agent/langfuse), not in docker-compose.
# Load them via AppRole before anything from langfuse is imported or created.
from vault_client import load_langfuse_env, load_litellm_keys

load_langfuse_env()
LITELLM_KEYS = load_litellm_keys()  # virtual keys for the LiteLLM gateway (agent + guardrails)

from langfuse import get_client, observe

import memory  # Redis short-term history + long-term facts
from langfuse.openai import OpenAI  # wraps every chat call as a Langfuse generation

langfuse = get_client()  # LANGFUSE_HOST from compose; PUBLIC/SECRET_KEY from Vault (above)


def set_trace_info(**kwargs):
    """Set trace input/output/metadata on any SDK version.
    Older SDKs have update_current_trace; newer ones put this on the root span."""
    fn = getattr(langfuse, "update_current_trace", None)
    if fn:
        fn(**kwargs)
    else:
        langfuse.update_current_span(
            **{k: v for k, v in kwargs.items() if k in ("input", "output", "metadata")}
        )

MODEL = "weather-agent"  # alias defined in litellm/config.yaml; change the real model there
ZIPCODE = os.environ.get("ZIPCODE", "77469")
llm = OpenAI(
    base_url=os.environ.get("LLM_GATEWAY_URL", "http://litellm:4000/v1"),
    api_key=LITELLM_KEYS["agent_key"],
)

# ---------------------------------------------------------------------------
# 3. Tool: wraps your existing weather_tool.get_weather_forecast
# ---------------------------------------------------------------------------
from weather_tool import get_weather_forecast as _weather_impl


def geocode_zip(zipcode: str):
    """US ZIP -> (lat, lon, 'City, ST') using the free zippopotam.us API."""
    r = requests.get(f"https://api.zippopotam.us/us/{zipcode}", timeout=10)
    r.raise_for_status()
    place = r.json()["places"][0]
    return (
        float(place["latitude"]),
        float(place["longitude"]),
        f'{place["place name"]}, {place["state abbreviation"]}',
    )


@observe(name="tool:get_weather_forecast")
def get_weather_for_zip(zipcode: str, days_ahead: int = 0) -> str:
    """ZIP -> lat/lon -> weather_tool.get_weather_forecast."""
    zipcode = str(zipcode).strip()
    if not re.fullmatch(r"\d{5}", zipcode):
        return json.dumps({"zipcode": zipcode, "error": "not a 5-digit US ZIP code"})
    lat, lon, place = geocode_zip(zipcode)
    forecast = _weather_impl(lat=lat, lon=lon, days_ahead=days_ahead)
    return json.dumps({"zipcode": zipcode, "location": place, "forecast": forecast})


@observe(name="tool:search_knowledge", as_type="retriever")
def search_knowledge(query: str) -> str:
    """Search the weather-safety knowledge base (NWS/EPA guidance) in Qdrant."""
    import knowledge  # lazy: loads the embedding model on first use
    try:
        hits = knowledge.search(query, k=3)
    except Exception as e:
        return json.dumps({"error": f"knowledge base unavailable: {e}"})
    if not hits:
        return json.dumps({"results": [], "note": "No matching safety guidance found."})
    return json.dumps({"results": hits})


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather_forecast",
            "description": "Get the daily weather forecast for a US ZIP code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "zipcode": {"type": "string", "description": "5-digit US ZIP code, e.g. 77469"},
                    "days_ahead": {
                        "type": "integer",
                        "description": "0 = today (default), 1 = tomorrow, up to 6",
                        "minimum": 0,
                        "maximum": 6,
                    },
                },
                "required": ["zipcode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "Search official weather-safety guidance (National Weather Service / EPA) on heat, "
                "lightning and thunderstorms, flooding, cold, tornadoes, hurricanes, air quality, "
                "UV and sun, and fog. Use it for safety questions and to base safety tips on."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "the safety question, in plain words"},
                },
                "required": ["query"],
            },
        },
    },
]
TOOL_FUNCS = {"get_weather_forecast": get_weather_for_zip, "search_knowledge": search_knowledge}

# Tool calls and outputs of the most recent run_agent() call, read by run_eval.py
# to score tool arguments and check the answer against the real forecast.
LAST_RUN = {"tool_calls": [], "tool_outputs": []}


def extract_text_tool_calls(content: str):
    """Fallback for when the model writes a tool call as plain text
    (bare JSON, ```json fences, or <tool_call> tags) and vLLM doesn't parse it.
    Returns a list of OpenAI-style tool_call dicts, or [] if it's a normal answer."""
    if not content:
        return []
    text = content.strip()
    text = text.replace("<tool_call>", "").replace("</tool_call>", "")
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
    text = text.strip()

    candidates = []
    for chunk in (text, *text.splitlines()):
        try:
            obj = json.loads(chunk)
        except (json.JSONDecodeError, TypeError):
            continue
        items = obj if isinstance(obj, list) else [obj]
        candidates = [o for o in items if isinstance(o, dict) and o.get("name") in TOOL_FUNCS]
        if candidates:
            break

    calls = []
    for i, c in enumerate(candidates):
        args = c.get("arguments", c.get("parameters", {}))
        if isinstance(args, str):
            args = json.loads(args or "{}")
        calls.append(
            {
                "id": f"call_text_{i}",
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(args)},
            }
        )
    return calls

# ---------------------------------------------------------------------------
# 3b. NeMo Guardrails: input rail before the loop, output rail after it
# ---------------------------------------------------------------------------
os.environ.setdefault("OPENAI_API_KEY", "EMPTY")  # vLLM needs no key; some clients insist
from nemoguardrails import LLMRails, RailsConfig

GUARDRAILS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guardrails")
rails = LLMRails(RailsConfig.from_path(GUARDRAILS_DIR))
REFUSAL = "Sorry, I can only help with weather forecasts. Try asking about a US ZIP code."
# Fixed reply when every forecast lookup failed (invalid ZIP, non-US postcode...).
# It is constant text, so it cannot leak anything and skips the output rail.
FORGOTTEN = "Done - I've deleted everything I remembered about you."
NOT_FOUND = ("I couldn't find weather for that location. I can look up US locations by "
             "5-digit ZIP code - please try again with a valid ZIP, for example 77469.")

# NeMo's sync generate() refuses to run inside an asyncio event loop, and the
# Langfuse experiment runner (run_eval.py) calls our code from inside one. So all
# rail checks run on one long-lived event loop in a background thread instead.
# This works the same from a plain script and from inside async code.
_rails_loop = asyncio.new_event_loop()
threading.Thread(target=_rails_loop.run_forever, name="nemo-rails-loop", daemon=True).start()


def _rails_generate(**kwargs):
    future = asyncio.run_coroutine_threadsafe(rails.generate_async(**kwargs), _rails_loop)
    return future.result(timeout=120)


# NeMo calls vLLM through its own client, so Langfuse can't see those calls on its
# own. Each rail check is recorded as a generation under its guardrail span, using
# the LLM-call log NeMo returns (model reply, token counts).
RAILS_MODEL = rails.config.models[0].model if rails.config.models else MODEL


def _record_rail_llm_calls(generation, res):
    calls = (res.log.llm_calls if res.log else None) or []
    usage = {}
    for key, attr in (("input", "prompt_tokens"), ("output", "completion_tokens"),
                      ("total", "total_tokens")):
        vals = [getattr(c, attr, None) for c in calls]
        if any(v is not None for v in vals):
            usage[key] = sum(v or 0 for v in vals)
    last = calls[-1] if calls else None
    generation.update(
        input=getattr(last, "prompt", None),
        output=getattr(last, "completion", None),
        usage_details=usage or None,
        metadata={"tasks": [getattr(c, "task", None) for c in calls],
                  "llm_calls": len(calls)},
    )


def _rails_blocked(messages, which):
    """Run only the input or output rails. Returns (blocked, names of rails that ran)."""
    generation = langfuse.start_observation(
        name=f"rail-llm:self_check_{which}", as_type="generation", model=RAILS_MODEL,
    )
    try:
        res = _rails_generate(
            messages=messages,
            options={"rails": [which], "log": {"activated_rails": True, "llm_calls": True}},
        )
        _record_rail_llm_calls(generation, res)
    except Exception as e:
        generation.update(level="ERROR", status_message=f"{type(e).__name__}: {e}")
        raise
    finally:
        generation.end()
    activated = (res.log.activated_rails if res.log else None) or []
    blocked = any(getattr(r, "stop", False) for r in activated)
    # Backstop: an allowed check echoes back the message it was given unchanged
    returned = (res.response[0].get("content") if res.response else "") or ""
    if returned.strip() != messages[-1]["content"].strip():
        blocked = True
    return blocked, [r.name for r in activated]


@observe(name="guardrail:input")
def check_input(user_request: str) -> dict:
    blocked, ran = _rails_blocked([{"role": "user", "content": user_request}], "input")
    return {"blocked": blocked, "rails": ran}


@observe(name="guardrail:output")
def check_output(user_request: str, answer: str) -> dict:
    blocked, ran = _rails_blocked(
        [{"role": "user", "content": user_request}, {"role": "assistant", "content": answer}],
        "output",
    )
    return {"blocked": blocked, "rails": ran}

# ---------------------------------------------------------------------------
# 4. Supporting context (Vault + Qdrant), traced as their own spans
# ---------------------------------------------------------------------------
def load_context(span):
    # Secrets were already loaded from Vault at startup (AppRole); just record it.
    span.set_attribute("vault.auth", "approle:agent-engine")
    print("[*] Vault: Langfuse keys loaded via AppRole.")

    try:
        import knowledge  # one place for the Qdrant connection (API key included)
        qdrant = knowledge.client()
        collections = [c.name for c in qdrant.get_collections().collections]
        span.set_attribute("vector_db.collections", str(collections))
        print(f"[*] Qdrant collections: {collections}")
    except Exception as e:
        print(f"[!] Qdrant lookup warning: {e}")

def date_context() -> str:
    """Today's date and the days_ahead value for each of the next 7 days, so the model
    never has to guess what 'Saturday' or 'tomorrow' means."""
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    days = ", ".join(f"{i} = {(today + timedelta(days=i)):%A %b %d}" for i in range(7))
    return f"Today is {today:%A, %B %d, %Y} (US Central time). days_ahead values: {days}."


# ---------------------------------------------------------------------------
# 5. Agent loop: model -> tool calls -> model ... until a final answer
# ---------------------------------------------------------------------------
@observe(name="weather-agent")
def _remember(user_id, session_id, user_request, answer, with_facts=True):
    """Store an approved turn (and any facts) in Redis; never let memory break a reply."""
    if not user_id:
        return
    try:
        facts = memory.facts_from_turn(user_request, LAST_RUN["tool_calls"],
                                       LAST_RUN["tool_outputs"]) if with_facts else {}
        memory.save_turn(user_id, session_id, user_request, answer, facts)
    except Exception as e:  # Redis down etc.: answer anyway
        print(f"[!] Memory save failed: {e}")


def run_agent(user_request: str, max_turns: int = 5, user_id: str | None = None,
              session_id: str | None = None) -> str:
    """One turn. With user_id (+ session_id) the agent uses Redis memory; without it,
    it behaves exactly as before (that is how the eval's single-turn cases run)."""
    LAST_RUN["tool_calls"].clear()
    LAST_RUN["tool_outputs"].clear()
    session_id = session_id or ("default" if user_id else None)
    set_trace_info(
        tags=["weather", "mesh-agents"],
        metadata={"zipcode": ZIPCODE, "model": MODEL, "tags": "weather,mesh-agents",
                  "memory": "on" if user_id else "off"},
        input=user_request,
    )

    # "forget me" is a fixed command: no model involved, so the rails can't refuse it
    if user_id and memory.FORGET_RE.match(user_request):
        removed = memory.forget(user_id)
        print(f"[*] Memory: forgot user ({removed} keys removed)")
        set_trace_info(output=FORGOTTEN, metadata={"memory": "forgotten"})
        return FORGOTTEN

    mem = {"history": [], "facts": {}}
    if user_id:
        try:
            mem = memory.load(user_id, session_id)
        except memory.MemoryInputError:
            raise
        except Exception as e:  # Redis down: carry on without memory
            print(f"[!] Memory load failed, continuing without it: {e}")

    # Input rail: stop before any tool call or model turn if the request breaks policy
    verdict = check_input(user_request)
    if verdict["blocked"]:
        print(f"[!] Input rail blocked the request ({', '.join(verdict['rails'])})")
        set_trace_info(output=REFUSAL, metadata={"guardrail": "input_blocked"})
        return REFUSAL
    print("[*] Input rail: allowed")

    with tracer.start_as_current_span("agent_execution_loop") as span:
        span.set_attribute("agent.zipcode", ZIPCODE)
        load_context(span)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a concise weather assistant. Always call get_weather_forecast "
                    "to get real data before answering. Report temperatures in both °F and °C, "
                    "the rain chance, and one practical tip for the day. "
                    "Pass whatever location the user gave to get_weather_forecast, even if it does "
                    "not look like a valid ZIP code; the tool checks it. "
                    "Whenever the user asks how to stay safe or protect someone, call search_knowledge, "
                    "even if the forecast looks mild. Also call it when the forecast suggests a hazard "
                    "(heat, storms, flooding, cold, poor air, strong sun, fog), and "
                    "base your advice on what it returns, naming the source (e.g. 'National Weather "
                    "Service'). Do not invent safety advice that search_knowledge did not return. "
                    "If get_weather_forecast returns an error, say you couldn't find that location "
                    "and ask for a valid 5-digit US ZIP code; never guess a forecast. "
                    + date_context()
                    + (" " + memory.facts_prompt(mem["facts"]) if user_id else "")
                ),
            },
            *mem["history"],  # earlier turns of this conversation (already rail-approved)
            {"role": "user", "content": user_request},
        ]

        forecast_ok = forecast_failed = False
        for turn in range(max_turns):
            resp = llm.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
                max_tokens=512,
                name=f"agent-turn-{turn + 1}",  # label shown in Langfuse
            )
            msg = resp.choices[0].message

            if msg.tool_calls:
                tool_calls = [tc.model_dump() for tc in msg.tool_calls]
                content = msg.content or ""
            else:
                tool_calls = extract_text_tool_calls(msg.content)
                content = ""  # the text WAS the tool call
                if tool_calls:
                    print("[*] Parsed tool call from model text (hermes parser missed it)")

            if not tool_calls:
                if forecast_failed and not forecast_ok:
                    # Every forecast lookup failed: give the fixed not-found reply
                    print("[*] Location not found: fixed reply (output rail not needed)")
                    set_trace_info(output=NOT_FOUND, metadata={"location": "not_found"})
                    _remember(user_id, session_id, user_request, NOT_FOUND, with_facts=False)
                    return NOT_FOUND
                answer = msg.content or ""
                # Output rail: check the answer before the user sees it
                verdict = check_output(user_request, answer)
                if verdict["blocked"]:
                    print(f"[!] Output rail blocked the answer ({', '.join(verdict['rails'])})")
                    set_trace_info(output=REFUSAL, metadata={"guardrail": "output_blocked"})
                    return REFUSAL
                print("[*] Output rail: allowed")
                set_trace_info(output=answer)
                _remember(user_id, session_id, user_request, answer)
                return answer

            # Record the assistant's tool request, then run each tool
            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            for tc in tool_calls:
                name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"] or "{}")
                print(f"[*] LLM requested tool: {name}({args})")
                try:
                    output = TOOL_FUNCS[name](**args)
                except Exception as e:
                    output = json.dumps({"error": str(e)})
                print(f"[*] Tool output: {output}")
                LAST_RUN["tool_calls"].append({"name": name, "args": args})
                LAST_RUN["tool_outputs"].append(output)
                if name == "get_weather_forecast":
                    if '"forecast"' in output:
                        forecast_ok = True
                    else:
                        forecast_failed = True
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": output})

        return "[!] Agent stopped: too many tool-call turns without a final answer."


if __name__ == "__main__":
    try:
        answer = run_agent(f"What's the weather today for ZIP code {ZIPCODE}?")
        print(f"\n[Final LLM Response]:\n{answer}")
    finally:
        langfuse.flush()
        provider.force_flush()
        provider.shutdown()
