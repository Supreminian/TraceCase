"""Run a recoverable TraceCase Agent worker independently of browser sessions."""
import os
from pathlib import Path
import signal
import sys
import threading


def main():
    from sdlc.testing_agent.worker import run_forever
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    root = Path(__file__).resolve().parents[1]
    run_forever(os.environ.get('SDLC_DB_PATH', str(root / 'data/sdlc.sqlite3')), stop=stop)


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
