"""Sends one command to the Unity editor through the Unity bridge and prints the answer.

    python tools/unity_cmd.py ping
    python tools/unity_cmd.py exportstate
    python tools/unity_cmd.py shaders Endless
    python tools/unity_cmd.py --timeout 600 materials "Shader Graphs/Endless_Shader_NoFade|_BaseColor"

The bridge (unity/Editor/EndstarBridge.cs) must be loaded in the open editor.
A thin wrapper round endstar_unity_mcp.send(), so it wakes a background Unity while it waits and
cancels a command that gets no answer instead of leaving it to run later, out of order.

It returns as soon as the bridge answers, so for makeprop and exportprop use the MCP tools
(endstar_make_prop, endstar_export_prop): makeprop answers "cells pending" before the cells exist, and
exportprop here skips the BUSY check and does not wait for the stages or the upload.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # also found when run with -I or from elsewhere
import endstar_unity_mcp as bridge  # noqa: E402

NO_WAIT = {
    "makeprop": "the cells are not ready yet: wait for unity\\outbox\\makeprop-<name>.ready, or use endstar_make_prop",
    "exportprop": "the export is only started: watch exportstate and unity\\outbox\\export-<name>.log, or use endstar_export_prop",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--no-press", action="store_true", help="leave an 'Export Success!' box alone while waiting")
    ap.add_argument("command", nargs="+")
    a = ap.parse_args()
    try:
        answer = bridge.send(" ".join(a.command), timeout=a.timeout, press_confirm=not a.no_press)
    except TimeoutError as e:
        print(f"NO ANSWER: {e} (is the editor open with the bridge compiled?)", file=sys.stderr)
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # a console code page must not crash on an answer's text
    print(answer)
    word = (" ".join(a.command).split() or [""])[0]   # the command may come as one quoted argument
    if word in NO_WAIT and not answer.startswith("ERROR"):
        print("note: " + NO_WAIT[word], file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
