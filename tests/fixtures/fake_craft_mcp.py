# Fake craft-anything-cli for tests. The test prepends the settings as globals: MODE, COMPARE
# (the web_pixels_compare reply, or None for a computed one), LOG (a file that records each call).
import json
import os
import sys
import time

MODE = globals().get("MODE", "ok")
COMPARE = globals().get("COMPARE")
LOG = globals().get("LOG")
EVIDENCE = "ev_" + "a" * 64


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def compare(args):
    if COMPARE is not None:
        return COMPARE
    fraction = 0.2 if "frame-1" in args["before"]["path"] else 0.0
    box = {"x": 10, "y": 5, "width": 20, "height": 10} if fraction else None
    result = {"comparable": True, "difference_fraction": fraction, "changed_bounding_box": box}
    return {"id": EVIDENCE, "result": result}


def handle(message):
    if message.get("method") == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0"},
                },
            }
        )
        return
    if message.get("method") != "tools/call":
        return  # notifications get no reply
    name, args = message["params"]["name"], message["params"]["arguments"]
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} {name}\n")
    if MODE == "hang":
        time.sleep(600)
    if MODE == "bigline":
        sys.stdout.write("x" * 5000 + "\n")
        sys.stdout.flush()
        return
    if MODE == "wrongid":
        send({"jsonrpc": "2.0", "id": 999, "result": {}})
    if MODE == "rpcError":
        send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -1, "message": "no"}})
        return
    data = compare(args) if name == "web_pixels_compare" else {"revision": 1}
    if MODE == "isError":
        result = {"isError": True, "content": [{"type": "text", "text": "boom"}]}
    elif MODE == "text":
        result = {"content": [{"type": "text", "text": json.dumps(data)}]}
    else:
        result = {
            "content": [{"type": "text", "text": json.dumps(data)}],
            "structuredContent": data,
        }
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})


if sys.argv[1:] == ["--version"]:
    print("craft-anything-cli 0.6.1")
else:
    for line in sys.stdin:
        handle(json.loads(line))
