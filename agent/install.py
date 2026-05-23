"""
Run once on the Windows machine (as Administrator) to install Poimenas.
"""
import sys, shutil, subprocess, winreg
from pathlib import Path

INSTALL_DIR  = Path(r"C:\ProgramData\Microsoft\Windows\DiagnosticCore")
AGENT_TASK   = "Windows Diagnostic Core Service"
AGENT_FILES  = ["agent.pyw", "config.json", "requirements.txt", "watchdog.py"]

def copy_files(src: Path):
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    for f in AGENT_FILES:
        if (src / f).exists():
            shutil.copy(src / f, INSTALL_DIR / f)
            print(f"  copied {f}")

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(INSTALL_DIR / "requirements.txt")], check=True)

def install_agent_task():
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    agent   = INSTALL_DIR / "agent.pyw"
    subprocess.run([
        "schtasks", "/create", "/f",
        "/tn", AGENT_TASK,
        "/tr", f'"{pythonw}" "{agent}"',
        "/sc", "ONLOGON",
        "/rl", "HIGHEST",
        "/delay", "0000:30",
    ], check=True)
    print(f"  task created: {AGENT_TASK}")

def install_watchdog_service():
    watchdog = INSTALL_DIR / "watchdog.py"
    # Register the service
    subprocess.run([sys.executable, str(watchdog), "--startup", "auto", "install"], check=True)
    # Start it immediately
    subprocess.run(["net", "start", "WinTelemetrySyncHost"], check=True)
    print("  watchdog service installed and started")

def set_static_dns(rpi_ip: str):
    subprocess.run([
        "powershell", "-Command",
        f'Get-NetAdapter | Where-Object {{$_.Status -eq "Up" -and '
        f'$_.InterfaceAlias -notlike "*Loopback*"}} | '
        f'Set-DnsClientServerAddress -ServerAddresses ("{rpi_ip}")',
    ], check=True)
    print(f"  DNS set to {rpi_ip}")

def disable_doh():
    """Disable DNS-over-HTTPS in Chrome and Edge via registry policy."""
    policy_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Google\Chrome"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Edge"),
    ]
    for hive, path in policy_paths:
        try:
            key = winreg.CreateKeyEx(hive, path, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "DnsOverHttpsMode", 0, winreg.REG_SZ, "off")
            winreg.CloseKey(key)
        except Exception as e:
            print(f"  warning: could not set DoH policy for {path}: {e}")
    print("  DoH disabled for Chrome and Edge")

def disable_safe_mode():
    """Remove Safe Mode boot option so it can't be used to bypass the agent."""
    try:
        subprocess.run(
            ["bcdedit", "/deletevalue", "{default}", "safeboot"],
            capture_output=True,  # silently fails if not set, that's fine
        )
        print("  Safe Mode boot option removed")
    except Exception as e:
        print(f"  warning: could not modify boot config: {e}")

def main():
    src = Path(__file__).parent

    if not (src / "config.json").exists():
        print("ERROR: copy config.json.example -> config.json and fill it in first.")
        sys.exit(1)

    import json
    with open(src / "config.json") as f:
        cfg = json.load(f)
    rpi_ip = cfg.get("rpi_local_ip", "")
    if not rpi_ip:
        print("ERROR: rpi_local_ip is not set in config.json.")
        sys.exit(1)

    print("\n[1/7] Copying files...")
    copy_files(src)

    print("[2/7] Installing Python dependencies...")
    install_deps()

    print("[3/7] Creating agent scheduled task...")
    install_agent_task()

    print("[4/7] Installing watchdog service...")
    install_watchdog_service()

    print("[5/7] Setting DNS to RPi...")
    set_static_dns(rpi_ip)

    print("[6/7] Disabling DNS-over-HTTPS...")
    disable_doh()

    print("[7/7] Disabling Safe Mode boot option...")
    disable_safe_mode()

    pythonw = Path(sys.executable).parent / "pythonw.exe"
    print(f"\nInstalled to: {INSTALL_DIR}")
    print("Reboot or log out and back in to start the agent.")
    print(f"\nTo start the agent manually now:")
    print(f'  "{pythonw}" "{INSTALL_DIR / "agent.pyw"}"')

if __name__ == "__main__":
    main()
