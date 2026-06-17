#!/usr/bin/env python
"""
대시보드를 실행하고 웹브라우저에서 자동으로 엽니다.
Cross-platform launcher for the auction monitor dashboard
"""
import os
import sys
import time
import webbrowser
import subprocess
from pathlib import Path


def main():
    # 프로젝트 루트 디렉토리
    root = Path(__file__).resolve().parent
    os.chdir(root)
    
    # 가상환경 확인
    if sys.platform == "win32":
        python_exe = root / ".venv" / "Scripts" / "python.exe"
    else:
        python_exe = root / ".venv" / "bin" / "python"
    
    if not python_exe.exists():
        print("❌ 가상환경이 없습니다.")
        print("다음 명령어를 먼저 실행하세요:")
        if sys.platform == "win32":
            print("  python -m venv .venv")
            print("  .venv\\Scripts\\pip install -r requirements.txt")
        else:
            print("  python -m venv .venv")
            print("  source .venv/bin/activate")
            print("  pip install -r requirements.txt")
        sys.exit(1)
    
    print("🚀 경매·공매 대시보드를 시작하고 있습니다...")
    print("📱 웹브라우저가 자동으로 열립니다 (http://localhost:8501)")
    print("🛑 종료하려면 이 창을 닫으세요.\n")
    
    # 2초 후 브라우저 열기
    def open_browser():
        time.sleep(2)
        webbrowser.open("http://localhost:8501")
    
    import threading
    threading.Thread(target=open_browser, daemon=True).start()
    
    # Streamlit 실행
    cmd = [
        str(python_exe),
        "-I",
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--logger.level=warning"
    ]
    
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print("\n👋 대시보드를 종료합니다.")
        sys.exit(0)


if __name__ == "__main__":
    main()
