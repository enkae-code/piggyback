# Fake craft-anything-cli for tests. The test prepends settings as globals: MODE, COMPARE, LOG
# (each call: "pid name json-args"), ENVLOG and PIDFILE (files this fake writes at start).
import json
import os
import sys
import time

MODE = globals().get("MODE", "ok")
COMPARE = globals().get("COMPARE")
LOG = globals().get("LOG")
ENVLOG = globals().get("ENVLOG")
PIDFILE = globals().get("PIDFILE")
JOURNAL = globals().get("JOURNAL")
OUTSIDE = globals().get("OUTSIDE")
EVIDENCE = "ev_" + "a" * 64


class Refused(Exception):
    pass


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def compare(args):
    before, after = args["before"]["path"], args["after"]["path"]
    if not before.endswith("-arrival.png") or after.endswith("-arrival.png"):
        raise Refused()  # the arrival frame goes first, the late frame second
    if COMPARE is not None:
        return COMPARE
    fraction = 0.2 if after.endswith("frame-1.png") else 0.0
    box = {"x": 10, "y": 5, "width": 20, "height": 10} if fraction else None
    result = {"comparable": True, "difference_fraction": fraction, "changed_bounding_box": box}
    return {"id": EVIDENCE, "result": result}


def tool_data(name, args):
    if name == "web_pixels_compare":
        return compare(args)
    return {"revision": 1}


def make_result(data):
    if MODE == "isError":
        return {"isError": True, "content": [{"type": "text", "text": "boom"}]}
    if MODE == "text5":
        return {"content": [{"type": "text", "text": 5}]}
    if MODE == "textdict":
        return {"content": [{"type": "text", "text": {}}]}
    if MODE == "text":
        return {"content": [{"type": "text", "text": json.dumps(data)}]}
    return {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}


def link_into_project():
    # A link the child leaves in its own project folder: the caller must refuse it.
    project = sys.argv[sys.argv.index("--project") + 1]
    os.makedirs(project, exist_ok=True)
    link = os.path.join(project, "evil.json")
    if not os.path.lexists(link):
        os.symlink("/etc/hostname", link)


def answer_call(message):
    name, args = message["params"]["name"], message["params"]["arguments"]
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} {name} {json.dumps(args)}\n")
    if MODE == "hang":
        time.sleep(600)
    if MODE == "bigline":
        sys.stdout.write("x" * 5000 + "\n")
        sys.stdout.flush()
        return
    if MODE == "bigbytes":
        for _ in range(200):
            sys.stdout.write("x" * (1024 * 1024))
            sys.stdout.flush()
        return
    if MODE == "deep":
        sys.stdout.write("[" * 200000 + "\n")
        sys.stdout.flush()
        return
    if MODE == "link":  # leaves a link, then fails
        link_into_project()
        send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -1, "message": "no"}})
        return
    if MODE == "hardlink_out":  # a hard link from inside Craft's tree to a file outside it
        project = sys.argv[sys.argv.index("--project") + 1]
        os.makedirs(project, exist_ok=True)
        link = os.path.join(project, "alias.json")
        if not os.path.lexists(link):
            os.link(OUTSIDE, link)
    if MODE == "wrongid":
        send({"jsonrpc": "2.0", "id": 999, "result": {}})
    if MODE == "rpcError":
        send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -1, "message": "no"}})
        return
    project = sys.argv[sys.argv.index("--project") + 1]
    craft_dir = os.path.dirname(project)
    if MODE == "link_project" and not os.path.lexists(project):
        os.makedirs(craft_dir, exist_ok=True)
        os.symlink(os.path.dirname(OUTSIDE), project)  # the project folder itself is a link out
    if MODE == "link_handoff" and name == "core_handoff_write":
        os.symlink(OUTSIDE, os.path.join(craft_dir, "HANDOFF.md"))  # the handoff is a link out
    try:
        data = tool_data(name, args)
    except Refused:
        send(
            {"jsonrpc": "2.0", "id": message["id"], "result": make_result(None) | {"isError": True}}
        )
        return
    if MODE == "linkok":
        link_into_project()
    send({"jsonrpc": "2.0", "id": message["id"], "result": make_result(data)})


def handle(message):
    if message.get("method") == "initialize":
        if MODE == "hanginit":
            time.sleep(600)
        if MODE == "boolid":
            send({"jsonrpc": "2.0", "id": True, "result": {}})
            return
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
        if MODE == "stall":
            time.sleep(600)  # reads nothing more from stdin
        return
    if message.get("method") == "tools/call":
        answer_call(message)


if sys.argv[1:] == ["--version"]:
    print("craft-anything-cli 0.6.1")
else:
    if PIDFILE:
        with open(PIDFILE, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    if ENVLOG:
        with open(ENVLOG, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(sorted(os.environ)))
    if JOURNAL:  # Craft records each call's params, absolute paths included, in the project
        journal = os.path.join(sys.argv[sys.argv.index("--project") + 1], "journal")
        os.makedirs(journal)
        with open(os.path.join(journal, "2026-11-24.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"params": {"path": journal}}) + "\n")
    for line in sys.stdin:
        handle(json.loads(line))
