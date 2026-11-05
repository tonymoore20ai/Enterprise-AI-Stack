"""
Coder step: turns the strategist's approved answer into discrete JSON elements.
Used by test_two_model_pipeline.py and by run_eval.py --coder.

The agent's own LiteLLM key can't call the coder alias, so the key comes from
CODER_API_KEY. Import agent_loop before calling make_client(): it loads the
Langfuse keys from Vault.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

CODER_MODEL = "coder"  # LiteLLM alias; routes to vLLM's fixed name "coder"

SCHEMA = {
    "type": "object",
    "properties": {
        "zipcode": {"type": ["string", "null"]},
        "location": {"type": ["string", "null"]},
        "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "high_f": {"type": ["number", "null"]},
        "high_c": {"type": ["number", "null"]},
        "low_f": {"type": ["number", "null"]},
        "low_c": {"type": ["number", "null"]},
        "rain_chance_percent": {"type": ["number", "null"]},
        "tip": {"type": ["string", "null"]},
    },
    "required": ["zipcode", "location", "date", "high_f", "high_c", "low_f", "low_c",
                 "rain_chance_percent", "tip"],
    "additionalProperties": False,
}

CODER_SYSTEM_PROMPT = (
    "You convert weather reports into JSON. Output one JSON object with each piece of data "
    "as its own element. Copy values exactly as the report states them: do not calculate, "
    "convert or infer anything, except the date. Write the date as YYYY-MM-DD, turning words "
    "like 'today', 'tomorrow' or a weekday into a real date using the current date you are "
    "given. Use null for anything the report does not state."
)


def make_client():
    from langfuse.openai import OpenAI  # traced client, same as the agent's

    return OpenAI(
        base_url=os.environ.get("LLM_GATEWAY_URL", "http://litellm:4000/v1"),
        api_key=os.environ["CODER_API_KEY"],
    )


def to_discrete_json(client, report: str):
    """Ask the coder to structure the report. Returns (raw text, response)."""
    # Same time zone as weather_tool's forecast dates
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    resp = client.chat.completions.create(
        model=CODER_MODEL,
        messages=[
            {"role": "system", "content": CODER_SYSTEM_PROMPT},
            {"role": "user",
             "content": f"Current date: {today:%Y-%m-%d} ({today:%A})\n\nWeather report:\n{report}"},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "weather_report", "schema": SCHEMA, "strict": True},
        },
        temperature=0,
        max_tokens=1024,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        name="coder-json",  # label shown in Langfuse
    )
    return resp.choices[0].message.content or "", resp
