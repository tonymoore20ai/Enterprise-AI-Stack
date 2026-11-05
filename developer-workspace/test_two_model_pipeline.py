"""
Two-model pipeline test.

  1. The strategist (weather-agent alias, RTX 3090) answers a weather request,
     using the real agent loop: guardrails, tools and tracing included.
  2. The coder (coder alias, RTX 5060 Ti) turns that answer into discrete JSON
     elements, seeing only the answer text.
  3. Each element is checked against the raw forecast tool output.

Run inside the container (the agent's own key can't call coder, so pass one that can):
  docker exec -e CODER_API_KEY agent-orchestrator python test_two_model_pipeline.py
  docker exec -e CODER_API_KEY -e ZIPCODE=77002 agent-orchestrator python test_two_model_pipeline.py

Exits 0 if every check passes, 1 if any fails.
"""
import json
import os
import sys
import time

from coder_json import CODER_MODEL, SCHEMA, make_client, to_discrete_json

TEMP_TOLERANCE_C = 0.6  # the answer may round the tool's value to a whole degree
TEMP_TOLERANCE_F = 1.5  # room for rounding on both the C and F values

def check_elements(data, forecast: dict, zipcode: str, location: str):
    """Compare each element with the tool output. Returns (name, ok, detail) tuples."""
    results = []

    def add(name, ok, detail):
        results.append((name, bool(ok), detail))

    def close(name, actual, expected, tolerance):
        if actual is None:
            add(name, False, f"null, expected about {expected}")
        else:
            add(name, abs(actual - expected) <= tolerance, f"{actual}, tool data gives {expected}")

    def matches_if_stated(name, actual, expected):
        if actual is None:
            add(name, True, f"null (not stated in the answer; tool data has {expected})")
        else:
            add(name, str(actual).strip().lower() == str(expected).strip().lower(),
                f"{actual}, tool data has {expected}")

    add("all elements present", set(data) == set(SCHEMA["required"]), ", ".join(sorted(data)))
    high_c, low_c = forecast["max_temp_c"], forecast["min_temp_c"]
    close("high_c", data.get("high_c"), high_c, TEMP_TOLERANCE_C)
    close("low_c", data.get("low_c"), low_c, TEMP_TOLERANCE_C)
    close("high_f", data.get("high_f"), round(high_c * 9 / 5 + 32, 1), TEMP_TOLERANCE_F)
    close("low_f", data.get("low_f"), round(low_c * 9 / 5 + 32, 1), TEMP_TOLERANCE_F)
    close("rain_chance_percent", data.get("rain_chance_percent"),
          forecast["precip_prob_percent"], 0)
    matches_if_stated("zipcode", data.get("zipcode"), zipcode)
    # Required: the coder is given the current date, so null or "Today" is a failure
    add("date", data.get("date") == forecast["date"],
        f"{data.get('date')}, tool data has {forecast['date']}")
    # The model may shorten a place name, so either form can contain the other
    place = str(data.get("location") or "").lower()
    if place:
        add("location", place in location.lower() or location.lower() in place,
            f"{data['location']}, tool data has {location}")
    else:
        add("location", True, f"null (not stated in the answer; tool data has {location})")
    tip = data.get("tip")
    add("tip", isinstance(tip, str) and tip.strip(), repr(tip))
    return results


def main() -> int:
    if not os.environ.get("CODER_API_KEY"):
        print("[!] CODER_API_KEY is not set. Create a key that can call 'coder' and pass it in.")
        return 1

    # Imported late: this loads the guardrails and connects to Vault and Langfuse
    import agent_loop

    failed = True
    try:
        request = f"What's the weather today for ZIP code {agent_loop.ZIPCODE}?"
        print(f"\n=== Step 1: strategist ({agent_loop.MODEL}) ===\n{request}\n")
        started = time.time()
        answer = agent_loop.run_agent(request)
        print(f"\n[{time.time() - started:.1f}s] Strategist answer:\n{answer}")

        outputs = []
        for call, raw in zip(agent_loop.LAST_RUN["tool_calls"], agent_loop.LAST_RUN["tool_outputs"]):
            if call["name"] == "get_weather_forecast":
                parsed = json.loads(raw)
                if "forecast" in parsed:
                    outputs.append(parsed)
        if not outputs:
            print("\n[FAIL] The strategist returned no forecast data, so the coder was not called.")
            return 1
        tool_data = outputs[-1]

        print(f"\n=== Step 2: coder ({CODER_MODEL}) ===")
        client = make_client()
        started = time.time()
        raw, resp = to_discrete_json(client, answer)
        finish = resp.choices[0].finish_reason
        print(f"[{time.time() - started:.1f}s] finish_reason={finish}, "
              f"tokens={resp.usage.completion_tokens}")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"\n[FAIL] The coder's output isn't valid JSON: {e}\n{raw}")
            return 1
        if not isinstance(data, dict):
            print(f"\n[FAIL] The coder's output isn't a JSON object:\n{raw}")
            return 1
        print(json.dumps(data, indent=2))

        print("\n=== Step 3: checks against the tool data ===")
        print(f"Tool data: {json.dumps(tool_data)}\n")
        results = check_elements(data, tool_data["forecast"], tool_data["zipcode"],
                                 tool_data["location"])
        results.append(("finish_reason", finish == "stop", finish))
        for name, ok, detail in results:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        failed = not all(ok for _, ok, _ in results)
        print(f"\nRESULT: {'FAIL' if failed else 'PASS'}")
        return 1 if failed else 0
    finally:
        agent_loop.langfuse.flush()
        agent_loop.provider.force_flush()
        agent_loop.provider.shutdown()


if __name__ == "__main__":
    sys.exit(main())
