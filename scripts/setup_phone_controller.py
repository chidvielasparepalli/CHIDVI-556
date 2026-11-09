#!/usr/bin/env python3
"""
Setup JARVIS Phone Controller for LAN access.

This script helps configure the CHIDVI-556 dashboard for phone control over LAN.
It finds your LAN IP, generates TLS certificates, and sets the required environment variable.
"""

import os
import sys
import subprocess
import ipaddress
import socket
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHON = BASE_DIR / ".venv" / "Scripts" / "python.exe"


def get_lan_ips():
    """Get all non-loopback IPv4 addresses assigned to this machine."""
    ips = []
    try:
        # Use psutil if available, otherwise fallback to socket
        import psutil
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    if not ip.startswith("127.") and not ip.startswith("169.254."):
                        ips.append((iface, ip))
    except ImportError:
        # Fallback: get hostname IPs
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.append(("unknown", ip))
    return ips


def is_private_ip(ip_str):
    """Check if IP is in private LAN ranges."""
    ip = ipaddress.IPv4Address(ip_str)
    return any(ip in ipaddress.ip_network(net) for net in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def check_port_in_use(port=8000):
    """Check if port is in use."""
    try:
        import psutil
        for conn in psutil.net_connections():
            if conn.laddr.port == port and conn.status == "LISTEN":
                return conn.laddr.ip, conn.pid
    except (ImportError, psutil.AccessDenied):
        pass
    return None, None


def check_certificates():
    """Check if certificates exist."""
    certs_dir = BASE_DIR / "config" / "certs"
    key_path = certs_dir / "jarvis.key"
    crt_path = certs_dir / "jarvis.crt"
    ca_path = certs_dir / "controller-ca.crt"
    return key_path.exists() and crt_path.exists(), ca_path.exists()


def generate_certificates(ip):
    """Generate TLS certificates using the dashboard helper."""
    try:
        result = subprocess.run(
            [str(VENV_PYTHON), "-m", "dashboard.certificates", ip],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "Certificate generation timed out"
    except FileNotFoundError:
        return False, "", f"Python not found at {VENV_PYTHON}"


def check_firewall():
    """Check for existing JARVIS/CHIDVI firewall rules (Windows only)."""
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-NetFirewallRule -DisplayName '*JARVIS*','*CHIDVI*' -ErrorAction SilentlyContinue | Select-Object DisplayName,Enabled,Profile,Direction,Action | Format-Table -AutoSize"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.stdout.strip().split('\n') if result.stdout.strip() else []
    except Exception:
        return []


def main(lan_ip=None, show_only=False):
    print("=== JARVIS Phone Controller LAN Setup ===\n")

    # Step 1: Find LAN IPs
    print("Step 1: Detecting LAN IP addresses...")
    lan_ips = get_lan_ips()
    if not lan_ips:
        print("ERROR: No LAN IP addresses found. Are you connected to a network?")
        return 1

    for i, (iface, ip) in enumerate(lan_ips):
        print(f"  [{i}] {iface} - {ip}")
    print()

    # Let user choose or auto-select Wi-Fi
    if lan_ip:
        # Validate provided IP
        if lan_ip not in [ip for _, ip in lan_ips]:
            print(f"ERROR: {lan_ip} is not assigned to this machine")
            return 1
        print(f"Using provided LAN IP: {lan_ip}")
    elif len(lan_ips) == 1:
        lan_ip = lan_ips[0][1]
        print(f"Auto-selected: {lan_ip}")
    else:
        # Prefer Wi-Fi interface
        wifi_ips = [(i, ip) for i, (iface, ip) in enumerate(lan_ips) if 'wi-fi' in iface.lower() or 'wifi' in iface.lower()]
        if wifi_ips:
            lan_ip = wifi_ips[0][1]
            print(f"Auto-selected Wi-Fi: {lan_ip}")
        else:
            if not show_only:
                print("Multiple interfaces found. Use --lan-ip to specify.")
                return 1
            lan_ip = lan_ips[0][1]
            print(f"Auto-selected first: {lan_ip}")

    # Validate
    if not is_private_ip(lan_ip):
        print(f"ERROR: {lan_ip} is not a private LAN address")
        return 1

    print(f"Using LAN IP: {lan_ip}\n")

    # Step 2: Check port 8000
    print("Step 2: Checking port 8000...")
    ip_in_use, pid = check_port_in_use(8000)
    if ip_in_use:
        print(f"WARNING: Port 8000 is in use by PID {pid} on {ip_in_use}")
        print("Stop any existing CHIDVI instance before proceeding.")
    else:
        print("Port 8000 is free.")
    print()

    # Step 3: Check certificates
    print("Step 3: Checking existing certificates...")
    has_certs, has_ca = check_certificates()
    certs_dir = BASE_DIR / "config" / "certs"
    if has_certs:
        print("Certificates already exist:")
        print(f"  {certs_dir}/jarvis.key (keep private!)")
        print(f"  {certs_dir}/jarvis.crt")
        if has_ca:
            print(f"  {certs_dir}/controller-ca.crt")
        print("To regenerate, delete these files first.")
    else:
        print("No certificates found. Will generate new ones.")
    print()

    # Step 4: Generate certificates
    if not has_certs:
        print("Step 4: Generating TLS certificates...")
        success, stdout, stderr = generate_certificates(lan_ip)
        if success:
            print("Certificates generated successfully!")
            print(stdout)
        else:
            print(f"ERROR: Certificate generation failed: {stderr}")
            return 1
    else:
        print("Step 4: Skipping certificate generation (already exist)")
    print()

    # Step 5: Verify certificates
    print("Step 5: Verifying certificates...")
    has_certs, has_ca = check_certificates()
    if has_certs and has_ca:
        print("All certificate files verified.")
    else:
        print("WARNING: Some certificate files are missing")
    print()

    # Step 6: Check firewall
    print("Step 6: Checking Windows Firewall rules...")
    fw_rules = check_firewall()
    if fw_rules:
        print("Existing firewall rules found:")
        for line in fw_rules:
            print(f"  {line}")
    else:
        print("No JARVIS/CHIDVI firewall rules found.")
        print("You may need to allow inbound TCP 8000 on Private profile for your subnet.")
    print()

    # Final instructions
    print("=== SETUP COMPLETE ===\n")
    print("To start JARVIS with phone controller:")
    if sys.platform == "win32":
        print(f"  $env:CHIDVI_CONTROLLER_HOST = '{lan_ip}'")
        print(f"  .\\.venv\\Scripts\\python.exe main.py")
    else:
        print(f"  export CHIDVI_CONTROLLER_HOST='{lan_ip}'")
        print(f"  ./.venv/bin/python main.py")
    print()
    print("On your Android phone:")
    print(f"  1. Transfer {certs_dir}/controller-ca.crt to your phone")
    print("  2. Install as CA certificate: Settings > Security > Install certificate > CA certificate")
    print("  3. Verify certificate name: 'CHIDVI personal controller CA'")
    print(f"  4. Open Chrome and go to: https://{lan_ip}:8000/login")
    print("  5. In JARVIS desktop app: Click 'Remote Control' button")
    print("  6. Scan QR code with phone camera, tap 'Pair with laptop'")
    print()
    print("Troubleshooting:")
    print("  - If phone can't connect: Check Windows Firewall (allow TCP 8000 on Private profile)")
    print("  - If certificate error: Verify phone clock is correct, CA is installed, IP matches")
    print("  - If 'local-only' error: Ensure CHIDVI_CONTROLLER_HOST is set BEFORE starting JARVIS")
    print("  - The env var must be set in the SAME terminal that runs JARVIS")

    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Setup JARVIS Phone Controller for LAN access")
    parser.add_argument("--lan-ip", help="LAN IP address to use (e.g., 192.168.1.50)")
    parser.add_argument("--show-only", action="store_true", help="Show instructions without generating certificates")
    args = parser.parse_args()
    sys.exit(main(lan_ip=args.lan_ip, show_only=args.show_only))