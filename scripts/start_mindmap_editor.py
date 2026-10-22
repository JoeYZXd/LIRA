"""Start the local mindmap editor without opening a service console window."""
from pathlib import Path
import json
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from mindmap_editor import BridgeConfig


def main():
    root = Path(__file__).resolve().parents[1]
    html = root / "docs/plans/2026-10-15-0923-feat-developer-console-mindmap-plan.html"
    config = json.loads(BridgeConfig(html.read_text(encoding="utf-8")).config)

    def connected():
        try:
            request = Request(config["base"] + "/api/requests", headers={"X-Mindmap-Token": config["token"]})
            with urlopen(request, timeout=1) as response:
                return response.status == 200
        except (OSError, ValueError):
            return False

    if not connected():
        subprocess.Popen([sys.executable, str(root / "scripts/mindmap_editor.py"), "--html", str(html)],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            time.sleep(0.15)
            if connected():
                break
        else:
            raise SystemExit("Local editor could not start. Check whether the port is occupied.")
    print("Local editor ready: " + config["base"] + "/")
    print("Paste the copied request notification into the current chat after submitting.")


if __name__ == "__main__":
    main()
