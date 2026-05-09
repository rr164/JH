#!/usr/bin/env python3
"""
JobHunter AI — Startup Script
Run this file to start the agent.
"""
import os
import sys
import subprocess
import webbrowser
import time
from pathlib import Path

def check_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("\n⚠️  ANTHROPIC_API_KEY not set!")
        print("   Get your key at: https://console.anthropic.com")
        key = input("   Paste your API key here: ").strip()
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
            # Save to .env
            env_path = Path(".env")
            with open(env_path, "a") as f:
                f.write(f"\nANTHROPIC_API_KEY={key}\n")
            print("   ✅ Key saved to .env\n")
        else:
            print("   ❌ No key provided. Resume generation will be disabled.")
    else:
        print(f"   ✅ API key found ({key[:8]}...)")

def install_deps():
    req = Path("backend/requirements.txt")
    if not req.exists():
        return
    print("\n📦 Installing dependencies...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req), "-q"], check=True)
    print("   ✅ Dependencies installed")

def start_server():
    print("\n🚀 Starting JobHunter AI server...")
    print("   Dashboard: http://localhost:8000\n")
    print("━" * 50)

    # Change to script directory
    script_dir = Path(__file__).parent
    os.chdir(script_dir)

    # Mount frontend as static
    server = subprocess.Popen([
        sys.executable, "-m", "uvicorn",
        "backend.main:app",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--reload",
        "--log-level", "warning"
    ])

    time.sleep(2)
    webbrowser.open("http://localhost:8000")

    try:
        server.wait()
    except KeyboardInterrupt:
        print("\n\n👋 JobHunter AI stopped.")
        server.terminate()

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════╗
║         JobHunter AI  v2.0                   ║
║   Autonomous Career Engine · H1-B Radar      ║
╚══════════════════════════════════════════════╝
    """)
    check_api_key()
    install_deps()
    start_server()
