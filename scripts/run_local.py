"""Run the authenticated gateway and private Streamlit process on this machine."""
import os
from pathlib import Path
import signal
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    # Interruptions are explicit in the durable task ledger; never resume with a stored plaintext token.
    from sdlc.accounts import Accounts
    path = os.environ.get('SDLC_DB_PATH', str(root / 'data/sdlc.sqlite3'))
    store = Accounts(path)
    with store.db(write=True) as db:
        db.execute("UPDATE generation_jobs SET status='failed',error='服务已重启，请重试；已有版本保持不变。' WHERE status IN ('queued','running')")
        db.execute("UPDATE runs SET status='failed',error='服务重启，生成中断；已有响应保留。' WHERE status='generating'")
    child = subprocess.Popen([sys.executable, '-m', 'streamlit', 'run', str(root / 'app.py'),
                              '--server.address', '127.0.0.1', '--server.port', os.environ.get('SDLC_PRIVATE_PORT', '8502'),
                              '--server.enableXsrfProtection', 'true', '--server.enableCORS', 'true'])
    def shutdown(*_):
        child.terminate()
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, shutdown)
    try:
        import uvicorn
        from sdlc.gateway import app
        uvicorn.run(app, host='127.0.0.1', port=int(os.environ.get('SDLC_GATEWAY_PORT', '8501')), log_level='warning')
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


if __name__ == '__main__':
    # Running a script places scripts/ first; allow the project modules to be imported.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
