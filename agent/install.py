"""
Run once on the Windows machine (as Administrator) to install the agent.
"""
import sys, shutil, subprocess
from pathlib import Path

INSTALL_DIR = Path(r"C:\ProgramData\Microsoft\Windows\DiagnosticCore")
TASK_NAME   = "Windows Diagnostic Core Service"
FILES       = ["agent.pyw", "config.json", "requirements.txt"]

def main():
    src = Path(__file__).parent

    # Validate config exists
    if not (src / "config.json").exists():
        print("ERROR: config.json not found. Copy config.json.example -> config.json and fill it in.")
        sys.exit(1)

    # Install files
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        if (src / f).exists():
            shutil.copy(src / f, INSTALL_DIR / f)
            print(f"Copied {f}")

    # Install Python deps
    pip = Path(sys.executable).parent / "pip.exe"
    subprocess.run([str(pip), "install", "-r", str(INSTALL_DIR / "requirements.txt")], check=True)

    # Create scheduled task (runs at logon, highest privileges, no window)
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    agent   = INSTALL_DIR / "agent.pyw"

    subprocess.run([
        "schtasks", "/create", "/f",
        "/tn", TASK_NAME,
        "/tr", f'"{pythonw}" "{agent}"',
        "/sc", "ONLOGON",
        "/rl", "HIGHEST",
        "/delay", "0000:30",   # 30s delay after logon for network to settle
    ], check=True)

    print(f"\nInstalled to: {INSTALL_DIR}")
    print(f"Task created: {TASK_NAME}")
    print("\nAgent will start automatically on next login.")
    print("To start it now without rebooting:")
    print(f'  "{pythonw}" "{agent}"')

if __name__ == "__main__":
    main()
