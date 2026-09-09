"""Restart the control worker independently of the Ingress UI."""
import os
import signal
import subprocess
import sys
import time


def main():
    stopping = False
    child = None

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        child = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(__file__), "mqtt_control.py")],
            start_new_session=True,
        )
        while child.poll() is None and not stopping:
            time.sleep(0.25)
        # Kill any tunnel descendants after worker exit as well as shutdown.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        if stopping:
            break
        print("[gardena-control] Worker exited; restarting in 15 seconds", flush=True)
        for _ in range(60):
            if stopping:
                break
            time.sleep(0.25)


if __name__ == "__main__":
    main()
