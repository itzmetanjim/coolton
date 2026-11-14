"""Replay real requests through coolton's first step, to compare prompts or models.

For each case (a Slack message coolton was asked something in), this builds the turn the
way a real one is built (run_agent: system prompt, tools, Jev's preload, the thread's
earlier messages as they were then) and stops right after the model's FIRST response,
before any tool runs: nothing is posted, no sandbox is touched, Slack is only read.
It records what that first step did (which tools, how many at once, any text), its input
tokens and how long it took. Comparing two checkouts' results (say, before and after a
system prompt change) shows whether coolton still reaches for the right tools, and how
fast; --judge also has a stronger model rate each first step.

    python -m evals.first_step --cases ~/coolton-evals/cases.json --out before.json [--tag dsv4.1]
    python -m evals.first_step --compare before.json after.json

Cases are a JSON list of {"channel", "ts", "thread_ts", "user", "text"}; keep them out of
the repo (they're real people's messages). Run it from a scratch copy of the repo with the
server's .env loaded: it writes coolton's usual state files into the working directory.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import statistics
import sys
import time

from slack_sdk import WebClient
from slack_sdk.web.slack_response import SlackResponse

READ_METHODS = ("users.", "conversations.info", "conversations.replies", "conversations.history",
                "conversations.members", "bots.info", "auth.test", "team.info", "emoji.list", "files.info")


class ReadOnlyClient(WebClient):
    """Slack reads go through; anything else is answered with a fake ok, never sent."""

    def api_call(self, api_method: str, **kwargs):  # type: ignore[override]
        if api_method.startswith(READ_METHODS):
            return super().api_call(api_method, **kwargs)
        return SlackResponse(client=self, http_verb="POST", api_url=api_method, req_args={},
                             data={"ok": True, "ts": "0.0", "channel": kwargs.get("json", {}).get("channel", "")},
                             headers={}, status_code=200)


class _FirstStepDone(BaseException):
    """Ends the run after the first model response (BaseException: past every retry)."""


_record: dict = {}


def _record_first_steps() -> None:
    """Wrap agent.agent.chain_model (once) so each run's first model response is recorded in
    _record and the run ends there."""
    coolton = importlib.import_module("agent.agent")  # `agent.agent` the attribute is the Agent
    from pydantic_ai.models.wrapper import WrapperModel

    real_chain_model = coolton.chain_model

    class FirstStep(WrapperModel):
        async def request(self, messages, model_settings, model_request_parameters):
            started = time.perf_counter()
            response = await super().request(messages, model_settings, model_request_parameters)
            _record.update(response=response, seconds=time.perf_counter() - started, model=self.model_name)
            raise _FirstStepDone

    coolton.chain_model = lambda config, http_client=None: FirstStep(real_chain_model(config, http_client))


def _history_until(client, case: dict):
    """The thread's messages from before this case, as coolton would have seen them."""
    import thread_context.thread_history as th

    real = th._fetch_replies

    def before(client, channel, thread_ts):
        messages = real(client, channel, thread_ts) or []
        return [m for m in messages if float(m.get("ts", "0")) < float(case["ts"])]

    th._fetch_replies = before
    try:
        if case["thread_ts"] == case["ts"]:
            return None
        return th.build_thread_context(client, case["channel"], case["thread_ts"], exclude_ts=case["ts"])
    finally:
        th._fetch_replies = real


def run_case(case: dict, tag: str | None) -> dict:
    from agent.agent import run_agent
    from agent.deps import AgentDeps
    from agent.tool_preload import start_preload

    client = ReadOnlyClient(token=os.environ["SLACK_BOT_TOKEN"])
    history = _history_until(client, case)
    deps = AgentDeps(client=client, user_id=case["user"], channel_id=case["channel"],
                     thread_ts=case["thread_ts"], message_ts=case["ts"],
                     user_token=os.environ.get("SLACK_USER_TOKEN"), provider_tag_filter=tag)
    deps.tool_preload = start_preload(case["text"], history)
    _record.clear()
    record = _record
    try:
        run_agent(case["text"], deps, message_history=history)
    except _FirstStepDone:
        pass
    except Exception as e:  # a failed case is a result too
        return {"ts": case["ts"], "error": f"{type(e).__name__}: {e}"[:300]}
    response = record.get("response")
    if response is None:
        return {"ts": case["ts"], "error": "no model response recorded"}
    calls = [{"tool": p.tool_name, "args": p.args if isinstance(p.args, (dict, str)) else str(p.args)}
             for p in response.parts if p.part_kind == "tool-call"]
    text = "".join(p.content for p in response.parts if p.part_kind == "text")
    return {"ts": case["ts"], "model": record.get("model"), "seconds": round(record["seconds"], 2),
            "input_tokens": response.usage.input_tokens, "calls": calls, "text": text}


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if "error" not in r]
    tools = [[c["tool"] for c in r["calls"]] for r in ok]
    alone = sum(1 for t in tools if t == ["add_emoji_reaction"])
    with_work = sum(1 for t in tools if "add_emoji_reaction" in t and len(t) > 1)
    return {
        "cases": len(results), "errors": len(results) - len(ok),
        "median_input_tokens": statistics.median(r["input_tokens"] for r in ok) if ok else None,
        "median_seconds": round(statistics.median(r["seconds"] for r in ok), 2) if ok else None,
        "reaction_alone": alone, "reaction_with_work": with_work,
        "mean_calls_in_first_step": round(statistics.mean(len(t) for t in tools), 2) if ok else None,
    }


def compare(a_path: str, b_path: str) -> None:
    a = {r["ts"]: r for r in json.load(open(a_path))["results"]}
    b = {r["ts"]: r for r in json.load(open(b_path))["results"]}
    print("before:", json.dumps(summarize(list(a.values()))))
    print("after: ", json.dumps(summarize(list(b.values()))))
    for ts in a:
        before = sorted(c["tool"] for c in a[ts].get("calls", []))
        after = sorted(c["tool"] for c in b.get(ts, {}).get("calls", []))
        if set(before) - {"add_emoji_reaction", "send_message"} != set(after) - {"add_emoji_reaction", "send_message"}:
            print(f"{ts}: {before} -> {after}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay real requests through coolton's first step.")
    parser.add_argument("--cases")
    parser.add_argument("--out")
    parser.add_argument("--tag", default=None, help="a [!WITH:tag] to pin the model")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args()
    if args.compare:
        compare(*args.compare)
        return
    cases = json.load(open(os.path.expanduser(args.cases)))
    _record_first_steps()
    results = []
    for i, case in enumerate(cases, 1):
        result = run_case(case, args.tag)
        results.append(result)
        tools = [c["tool"] for c in result.get("calls", [])]
        print(f"[{i}/{len(cases)}] {case['ts']} {result.get('error') or tools}", file=sys.stderr, flush=True)
    summary = summarize(results)
    json.dump({"summary": summary, "results": results}, open(args.out, "w"), indent=1, default=str)
    print(json.dumps(summary))


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
