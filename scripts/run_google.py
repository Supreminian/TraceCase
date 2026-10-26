"""Start locally with the user's existing desktop-pet Gemini credential, never copying it to disk."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    if not (os.environ.get('GEMINI_API_KEY', '').strip() or os.environ.get('GOOGLE_API_KEY', '').strip()):
        try:
            result = subprocess.run(
                ['security', 'find-generic-password', '-s', 'local.summerthecat.v30.gemini',
                 '-a', 'api-key', '-w'], capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise SystemExit('无法读取桌宠钥匙串，请在本机设置 GEMINI_API_KEY 后重试。') from None
        if result.returncode or not result.stdout.strip():
            raise SystemExit('桌宠 Gemini 密钥不可读取，请在本机设置 GEMINI_API_KEY 后重试。')
        os.environ['GEMINI_API_KEY'] = result.stdout.decode().strip()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    os.execv(sys.executable, [sys.executable, '-m', 'streamlit', 'run', str(root / 'app.py'),
                             '--server.port', '8501', *sys.argv[1:]])


if __name__ == '__main__':
    main()
