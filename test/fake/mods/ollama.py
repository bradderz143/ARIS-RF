import json, os
class Client:
    def __init__(self, host=None):
        self.host = host
    def list(self):
        return {"models": [{"model": "qwen2.5:7b"}]}
    def chat(self, model, messages, tools=None, options=None, **kw):
        last = messages[-1]
        if last["role"] == "user":
            t = last["content"].lower()
            if "setup" in t and "ran the" not in t:
                return {"message": {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "get_setup_report", "arguments": {}}}]}}
            ctx = "WITH saved-records context" if "saved records" in t else "no saved-records context"
            hist = [m["content"][:60] for m in messages if m["role"] == "assistant" and m.get("content")]
            return {"message": {"role": "assistant", "content": f"[fake AI] {ctx}; history: {hist}"}}
        return {"message": {"role": "assistant", "content": "[fake AI] tool said: " + last["content"][:400]}}

_base_chat = Client.chat
def _chat2(self, model, messages, tools=None, options=None, **kw):
    last = messages[-1]
    if last["role"] == "user" and "baseline on fm band" in last["content"].lower():
        # what the model sent in the user's screenshot: edges copied from the saved signals
        return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {
            "name": "record_baseline", "arguments": {"start_mhz": 95.3066, "stop_mhz": 98.5078}}}]}}
    if last["role"] == "user" and "what did the baseline find" in last["content"].lower():
        return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {
            "name": "list_baselines", "arguments": {}}}]}}
    return _base_chat(self, model, messages, tools, options)
Client.chat = _chat2


import re as _re
_base2 = Client.chat
def _chat3(self, model, messages, tools=None, options=None, **kw):
    last = messages[-1]
    if last["role"] == "user":
        t = last["content"].lower()
        m = _re.search(r"(\d{2,4}\.\d+)", t)
        if ("investigate" in t or "exactly" in t) and m:
            args = {"frequency_mhz": float(m.group(1))}
            dm = _re.search(r"dongle (\d)", t)
            if dm:
                args["dongle"] = dm.group(1)
            return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {
                "name": "investigate_single_frequency", "arguments": args}}]}}
    return _base2(self, model, messages, tools, options)
Client.chat = _chat3
