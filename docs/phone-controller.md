# Phone controller

The phone website controls the **same running CHIDVI agent** as desktop chat. It is separate from ADB, which controls an Android device from the laptop. No public hosting, tunnel, relay, new firewall rule, or new AI agent is required or enabled automatically.

## What changed

The existing FastAPI dashboard, QR generator, microphone PCM conversion and Gemini relay are reused. Pairing uses a random one-time code in the URL fragment, so the code is not sent in the URL to the server. Pairing expires in ten minutes; generating a replacement or dismissing the desktop QR invalidates the old code. Pairing requires the phone user to press **Pair with laptop**.

The server defaults to **127.0.0.1:8000**. That address is for the laptop only. Remote Control refuses to offer it as a phone URL. To connect an Android browser, explicitly select an assigned private LAN IPv4 address and configure trusted TLS first. The server refuses LAN HTTP and wildcard/public bindings. It never changes Windows network categories or firewall rules.

## Start locally

From PowerShell:

```powershell
Set-Location D:\CHIDVI-556
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

The requirements already include FastAPI, Uvicorn, qrcode, cryptography, python-multipart and psutil. There are no new runtime packages for this controller. Close the desktop app to stop its controller. Authentication and pairing are held in memory, so restarting the app requires pairing again.

## Configure trusted LAN access

1. Connect the phone and laptop to the same trusted Wi-Fi network. Guest networks and client isolation can prevent communication.
2. Inspect the laptop's actual address, network category and port usage. These commands are read-only:

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Format-Table InterfaceAlias,IPAddress
Get-NetConnectionProfile | Format-Table InterfaceAlias,NetworkCategory
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Format-Table LocalAddress,State,OwningProcess
```

3. Set the Wi-Fi interface's displayed address, not the sample value below. Stop an old CHIDVI instance before restarting; do not kill an unrelated process occupying port 8000.

```powershell
$env:CHIDVI_CONTROLLER_HOST = '192.168.1.20' # Replace with your laptop's assigned Wi-Fi IPv4 address.
.\.venv\Scripts\python.exe -m dashboard.certificates $env:CHIDVI_CONTROLLER_HOST
```

The helper creates `config/certs/jarvis.key`, `jarvis.crt` and `controller-ca.crt`. It refuses to overwrite existing files and does not open a network port. The certificate is valid for **30 days** and only for the selected IP. The signing key for the local CA is discarded after generation. Certificates are ignored by Git. Keep `jarvis.key` private and restrict access to the laptop account running CHIDVI; do not upload or share it.

4. Transfer **only `controller-ca.crt`** to the Android phone using a trusted transfer method. Install it as a **CA certificate** through Android's security/credential settings. On Pixel-style Android, search Settings for **Install a certificate**, then choose **CA certificate**. Manufacturer wording varies. Review the certificate name **CHIDVI personal controller CA** and compare its SHA-256 fingerprint with the helper's output. Installing this CA is an explicit phone-side trust change; install only the certificate you generated on your own laptop. If a managed phone disallows user CA certificates, ask its administrator or use an already trusted certificate matching the laptop address.
5. Restart CHIDVI in the same PowerShell window:

```powershell
.\.venv\Scripts\python.exe main.py
```

6. On the phone, open `https://<your-laptop-ip>:8000/login`. Complete certificate trust setup before pairing. **Do not disable Chrome security flags or ignore a mismatched certificate.** If the browser still reports an untrusted connection, verify installation, IP address, clock and certificate expiration before proceeding.
7. In the desktop app, choose **Remote Control**. Scan its QR code with the phone camera and press **Pair with laptop**. Keep the QR window open until pairing finishes. You can also paste its complete code into the pairing screen. The desktop displays connected status and the phone shows connection, personality and laptop-control status.

A different DHCP address or an expired certificate requires replacement certificate files and installing the new CA on the phone. Back up existing files deliberately first; the helper never replaces them. Remove the old CA from Android's user credentials after replacing or retiring the controller.

## Windows firewall and reachability

No firewall rule is added by the application or certificate helper. Check existing rules and ask the laptop owner before changing them:

```powershell
Get-NetFirewallRule -DisplayName '*JARVIS*' -ErrorAction SilentlyContinue | Select-Object DisplayName,Enabled,Profile,Direction,Action
Get-NetFirewallRule -DisplayName '*CHIDVI*' -ErrorAction SilentlyContinue | Select-Object DisplayName,Enabled,Profile,Direction,Action
```

Earlier dashboard versions attempted to grant broad Python access and change Public networks to Private. Those startup actions have been removed; existing Windows rules have not been altered. Review old rules separately before LAN use.

If the phone cannot reach the selected address and the Windows firewall blocks it, the owner can approve a narrowly scoped inbound TCP rule for **port 8000**, the selected laptop address, the **Private** profile, and the trusted local subnet. Do not create a program-wide Python rule, switch every network to Private, or forward the router port. The app does not create that rule for you. Testing the laptop's own URL verifies the service; it does not prove that the phone can reach it.

## Chat, progress and cancellation

Commands carry a request ID into the existing task controller. The website shows queued, planning, waiting for input, executing, verifying and terminal task states. Reconnecting downloads existing task/history snapshots and never submits a command automatically. If delivery is uncertain, **Retry send** uses the same request ID. Reusing that ID for different text is rejected. One session accepts up to 500 unique commands; old IDs are retained for the session to prevent accidental re-execution.

Use **Stop task** or a task's **Cancel** button. Cancelling requests interruption through the shared desktop runtime; it cannot undo an operation that already completed. Sensitive operations still require the desktop's confirmation controls. **Disconnect phone** revokes this session; **Revoke phones** on the laptop revokes all sessions, cancels tasks from those devices, closes their sockets, clears pending phone audio and cancels outstanding pairing.

The website can transfer an inert file of at most **20 MB** into `Downloads\JARVIS Uploads`; it does not execute uploads. File names are sanitized and collision-resistant. Downloads require the paired session and are delivered as attachments. Screenshot preview is intentionally disabled; this implementation does not stream the desktop to the phone.

## Phone microphone

The existing audio path streams 16 kHz PCM into the current Gemini session. Tap **Microphone**, grant the browser's microphone permission, speak, and tap **Stop microphone**. The listening label remains visible. Switching away from the page stops capture. The app handles missing devices and denied permissions; reconnecting does not silently restart the microphone. Use text if trusted HTTPS or microphone access is unavailable.

This phone control does not save audio to disk. Voice audio still goes through the existing Gemini provider. The desktop runtime gates phone input during its own speech. The browser uses the existing PCM conversion and ScriptProcessor capture fallback; future browsers that remove that API will require an AudioWorklet upgrade.

## Configuration and security limits

| Setting | Default | Meaning |
| --- | --- | --- |
| `CHIDVI_CONTROLLER_HOST` | `127.0.0.1` | Exact assigned private IPv4 address for LAN, or loopback. No wildcard/public host. |
| `CHIDVI_CONTROLLER_SESSION_TTL` | `43200` | Session lifetime in seconds; allowed range 60..604800. |
| Certificate files | `config/certs/jarvis.crt`, `jarvis.key` | Required to bind a LAN interface. Certificate trust must be configured on the phone. |
| Pairing lifetime | 600 seconds | `DashboardServer.new_key(expiry_secs=...)` can choose 1..3600 seconds. Desktop currently uses ten minutes. |

Session cookies are HttpOnly and SameSite Strict; LAN sessions also use Secure cookies. Mutations require the exact origin and CSRF token. WebSockets require the session, exact origin and a CSRF handshake. Host checks resist DNS rebinding. Commands, JSON, uploads, audio frames, connection counts and authentication attempts are bounded/rate-limited. Device sessions expire and are revocable. API keys are never put in the frontend. The old AES-over-HTTP security claim and Chrome insecure-origin instructions were removed.

Loopback HTTP is for local development only. LAN requires HTTPS, which protects network transport only when the phone trusts the correct certificate. Pairing grants laptop control for the session, so only pair devices you own. Avoid sensitive values in chat; the shared agent history is visible to paired devices. Logs record device/task identifiers rather than command bodies or credentials. This is a single-user local controller, not an internet deployment or a multi-user permission system.

## Tests and verification boundary

```powershell
python -m unittest discover -s tests -p test_dashboard.py
```

The tests use FastAPI's in-process client and mocked agent tasks: no Android phone, real server, firewall change or Gemini request is required. They cover pairing entropy/expiry/one-time use/cancellation, auth/CSRF/origin/host checks, rate and size bounds, idempotency, shared-task routing, cancellation, WebSocket reconnect and revocation, bounded uploads, and TLS binding rules. Browser rendering can be checked through mocked Playwright routes without opening a port.

Actual Wi-Fi reachability, Android certificate trust and phone microphone behavior require the owner's phone-side steps above. They have not been established by unit tests.
