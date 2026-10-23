"""
List the failing cases of an eval run, with why each one failed.

  docker exec agent-orchestrator python show_failures.py coder-hard-v1
  docker exec agent-orchestrator python show_failures.py baseline-coder-sdk4 --dataset guardrails-eval
"""
import argparse
import json
import warnings

warnings.filterwarnings("ignore", message="API key will be sent over plaintext HTTP")

from vault_client import load_langfuse_env

load_langfuse_env()

from langfuse import get_client

SCORES = ("correct", "tool_args_correct", "no_hallucination", "has_both_units", "used_knowledge",
          "memory_correct")


def as_dict(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"value": value}
    return value if isinstance(value, dict) else {}


def _guardrail(trace):
    """Which rail blocked this run: read from the agent span's metadata, else from the
    rail generations' replies ('Yes' = block)."""
    for obs in getattr(trace, "observations", None) or []:
        meta = getattr(obs, "metadata", None) or {}
        if isinstance(meta, dict) and meta.get("guardrail"):
            return {"input_blocked": "input rail", "output_blocked": "output rail"}.get(
                meta["guardrail"], meta["guardrail"])
    for obs in getattr(trace, "observations", None) or []:
        name = getattr(obs, "name", "") or ""
        if name.startswith("rail-llm:") and str(getattr(obs, "output", "")).strip().lower().startswith("yes"):
            return "input rail" if name.endswith("input") else "output rail"
    return "unknown"


def main():
    parser = argparse.ArgumentParser(description="Show failing cases of an eval run")
    parser.add_argument("run_name")
    parser.add_argument("--dataset", default="guardrails-eval-hard")
    args = parser.parse_args()

    lf = get_client()
    items = {i.id: i for i in lf.get_dataset(args.dataset).items}
    run = lf.api.datasets.get_run(dataset_name=args.dataset, run_name=args.run_name)

    failed = 0
    for ri in run.dataset_run_items:
        scores = lf.api.scores.get_many(trace_id=ri.trace_id, limit=50).data
        misses = [s for s in scores if getattr(s, "name", None) in SCORES and getattr(s, "value", None) == 0]
        if not misses:
            continue
        failed += 1
        item = items.get(ri.dataset_item_id)
        inp = as_dict(item.input) if item else {}
        case = (item.metadata or {}).get("case", ri.dataset_item_id) if item else ri.dataset_item_id
        trace = lf.api.trace.get(ri.trace_id)
        out = as_dict(trace.output)
        blocked_by = _guardrail(trace)

        print(f"\n=== {inp.get('suite', '?')}:{case}")
        if inp.get("turns"):
            for i, t in enumerate(inp["turns"], 1):
                print(f"  turn {i}:   [{t['user']}/{t['session']}] {t['message']}")
        else:
            print(f"  input:    {inp.get('prompt') or inp.get('answer')}")
        for s in misses:
            print(f"  FAILED {s.name}: {getattr(s, 'comment', '') or ''}")
        if out.get("tool_calls"):
            print(f"  tools:    {out['tool_calls']}")
        if out.get("decision") == "blocked":
            print(f"  blocked:  {blocked_by}")
        answer = (out.get("answer") or "").replace("\n", " ")
        print(f"  answer:   {answer[:300]}{'...' if len(answer) > 300 else ''}")

    print(f"\n{failed} of {len(run.dataset_run_items)} cases had a failing score in run '{args.run_name}'.")


if __name__ == "__main__":
    main()
