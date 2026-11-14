"""A scripted stand-in for the Gemini API (#5216), so a real Gemini CLI shows its real screens and writes its real chat
log with no account and no model (README.md: how to run it). Arguments: the port, a log file of the requests, and
the demo folder (calc.py in it). The reply depends on the last user turn:

- a functionResponse: "Done: <tool name> finished." (text)
- "make a file": a run_shell_command call that writes (touch notes.txt), so Gemini asks first
- "list files": a run_shell_command call (ls), which Gemini runs with no question
- "edit": a replace call on calc.py
- "slow": a text reply in three parts, 30 seconds apart
- anything else: a short text reply
A request with a JSON schema (next speaker, routing) gets a JSON answer.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = sys.argv[2] if len(sys.argv) > 2 else None
FOLDER = sys.argv[3] if len(sys.argv) > 3 else "."


def log(*a):
    if LOG:
        with open(LOG, "a") as f:
            f.write(" ".join(str(x) for x in a) + "\n")


def usage():
    return {"promptTokenCount": 1200, "candidatesTokenCount": 20, "totalTokenCount": 1220,
            "cachedContentTokenCount": 0, "thoughtsTokenCount": 0}


def chunk(parts, finish=None):
    c = {"content": {"role": "model", "parts": parts}, "index": 0}
    if finish:
        c["finishReason"] = finish
    return {"candidates": [c], "usageMetadata": usage(), "modelVersion": "gemini-2.5-flash"}


def plan(body):
    contents = body.get("contents") or []
    last = contents[-1] if contents else {}
    parts = last.get("parts") or []
    if any("functionResponse" in p for p in parts):
        name = next(p["functionResponse"].get("name") for p in parts if "functionResponse" in p)
        return [[{"text": f"Done: {name} finished."}]], 0
    text = " ".join(p.get("text", "") for p in parts).lower()
    if "make a file" in text:
        return [[{"functionCall": {"name": "run_shell_command",
                                   "args": {"command": "touch notes.txt", "description": "Make an empty file."}}}]], 0
    if "list files" in text:
        return [[{"functionCall": {"name": "run_shell_command",
                                   "args": {"command": "ls", "description": "List the files in the folder."}}}]], 0
    if "edit" in text:
        return [[{"functionCall": {"name": "replace", "args": {
            "file_path": f"{FOLDER}/calc.py", "instruction": "Rename the function add to plus.",
            "old_string": "def add(a, b):", "new_string": "def plus(a, b):"}}}]], 0
    if "slow" in text:
        return [[{"text": "Thinking about it. "}], [{"text": "Still here. "}], [{"text": "That took a while."}]], 30
    return [[{"text": "Hello. This folder has a small Python file and a README."}]], 0


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        log("GET", self.path)
        self._json({})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        cfg = body.get("generationConfig") or {}
        schema = cfg.get("responseJsonSchema") or cfg.get("responseSchema")
        log("POST", self.path, "schema" if schema else "", json.dumps(body.get("contents", [])[-1:])[:300])
        if "countTokens" in self.path:
            return self._json({"totalTokens": 1200})
        if schema:
            props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
            ans = {}
            if "next_speaker" in props:
                ans = {"reasoning": "The model answered.", "next_speaker": "user"}
            elif "model_choice" in props:
                ans = {"reasoning": "Simple.", "model_choice": "flash"}
            else:
                ans = {k: "" for k in props}
            return self._json(chunk([{"text": json.dumps(ans)}], "STOP"))
        steps, delay = plan(body)
        if "streamGenerateContent" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i, p in enumerate(steps):
                if i:
                    time.sleep(delay)
                last = i == len(steps) - 1
                self.wfile.write(b"data: " + json.dumps(chunk(p, "STOP" if last else None)).encode() + b"\r\n\r\n")
                self.wfile.flush()
            return
        self._json(chunk([q for p in steps for q in p], "STOP"))

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
