"""
One-off check: what does the self-check rail model actually reply, how many tokens,
and how long does each LLM call take? (NeMo's own LLM calls are not traced in Langfuse.)

  docker exec agent-orchestrator python debug_rail_calls.py
"""
import time
import warnings

warnings.filterwarnings("ignore")

from agent_loop import MODEL, _rails_generate

Q = "What's the weather today for ZIP code 77469?"
CASES = {
    "python-code": "Here's a Python function to sort a list of dictionaries by date: "
                   "sorted(items, key=lambda d: d['date'])",
    "off-topic": "Sunny, 87°F. Also, Highway 59 is closed for construction and there's "
                 "a 40-minute delay downtown.",
    "normal": "Today in Rosenberg, TX: high 88°F (31°C), low 72°F (22°C), 20% chance of "
              "rain. Tip: stay hydrated if you're outside this afternoon.",
}

print(f"Model: {MODEL}\n")
for name, answer in CASES.items():
    for attempt in (1, 2):
        start = time.perf_counter()
        res = _rails_generate(
            messages=[{"role": "user", "content": Q}, {"role": "assistant", "content": answer}],
            options={"rails": ["output"], "log": {"llm_calls": True, "activated_rails": True}},
        )
        total = time.perf_counter() - start
        print(f"[{name} #{attempt}] total {total:.2f}s")
        for c in (res.log.llm_calls if res.log else None) or []:
            dur = f"{c.duration:.2f}s" if c.duration is not None else "?"
            print(f"   task={c.task}  llm={dur}  prompt_tok={c.prompt_tokens}  "
                  f"completion_tok={c.completion_tokens}  reply={c.completion!r}")
    print()
