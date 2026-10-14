# Android phone control

These plugins control the **phone from CHIDVI**. The mobile controller website
controls the **laptop from the phone** and has a separate pairing process.

## What was repaired

`plugins/wireless_control.py` is an HTTP client for a JARVIS Companion app; it
does not use Android developer options. No Companion APK or Android source is
bundled in this repository. Its empty configuration, doubled endpoint slashes,
ignored server errors and broken self-check prevented reliable diagnosis.

`plugins/adb_control.py` is the existing Android developer-options integration.
It previously required `adb` on PATH even when Android Studio had installed it.
Connection management and shared validation now live in `core/phone_control.py`;
the plugins retain their AI tool schemas and readable `run()` result, and expose
structured `execute()` results with `ok`, `message`/`error` and optional `data`.
The game plugin's `_get`/`_post` Companion interfaces remain available.

## Android 11+ wireless debugging (recommended for this computer)

The installed executable was found at
`C:\Users\chidv\AppData\Local\Android\Sdk\platform-tools\adb.exe`.
The audit verified version 37.0.1 and an empty device list. No phone was paired
or controlled. The user elected to connect the phone later.

1. Connect phone and laptop to the same trusted Wi-Fi network.
2. On the phone enable Developer options, then **Wireless debugging**.
3. Select **Pair using pairing code**. Leave that screen open.
4. Run these commands locally in PowerShell, replacing the address and port:

```powershell
Set-Location D:\CHIDVI-556
$adbExe = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
& $adbExe version
& $adbExe pair 'PHONE_IP:PAIRING_PORT'
```

Enter the six-digit code **only at ADB's terminal prompt**. Do not send it in chat,
include it in a command argument, or save it in configuration. The plugin's `pair`
action returns these local instructions; it deliberately does not collect codes
through the AI or claim that instruction generation paired a device.

5. Return to the main Wireless debugging screen. Its **IP address & Port** is
   the connection endpoint and normally differs from the pairing endpoint.
   If the phone did not connect automatically:

```powershell
& $adbExe connect 'PHONE_IP:CONNECTION_PORT'
& $adbExe devices -l
```

The state must be `device`. `unauthorized` requires phone-side authorization;
`offline` requires reconnecting. If the Wi-Fi network/port changes, use its new
connection endpoint. Guest-network isolation can block communication.

6. In CHIDVI Plugin Settings, **Android ADB Control → CHECK DEVICES** diagnoses
   the connection off the UI thread. With multiple authorized phones, specify
   the desired serial in that section or in the command. Never assume the first
   listed phone is the intended target.

```powershell
python -c "from plugins.adb_control import run; print(run({'action': 'devices'}))"
```

Disconnect only the selected network endpoint:

```powershell
& $adbExe disconnect 'PHONE_IP:CONNECTION_PORT'
```

Disconnecting does not revoke pairing. To revoke it, use the phone's Wireless
debugging → Paired devices → this computer → **Forget**, or disable Wireless
debugging. USB remains supported after enabling USB debugging and accepting the
phone's RSA authorization prompt. No USB-to-TCP port opening is automated.

[Official Android wireless debugging instructions](https://developer.android.com/tools/adb#wireless)
and [SDK Platform Tools](https://developer.android.com/tools/releases/platform-tools).

## Discovery and configuration

ADB executable discovery tries `adb_control.adb_path` in Plugin Settings, then
`ADB_PATH`, PATH, `ANDROID_SDK_ROOT`, `ANDROID_HOME`, and the normal Windows SDK
directory under `LOCALAPPDATA`. A configured but missing executable fails clearly
instead of silently choosing another. No permanent PATH or firewall changes are
made. `ANDROID_SERIAL` is supported after an explicit command serial and the
saved plugin serial.

`connect`/`disconnect` require an explicit literal LAN/Tailscale `endpoint` with a
port. Public addresses, shell fragments and malformed ports are refused.
Commands have 10–30 second deadlines. Raw subprocess arguments, typed text,
pairing codes and server error bodies are not included in transport errors.

## Companion HTTP configuration

Use this only if an existing compatible Companion app is installed and running
on the phone. Fill **Wireless Phone Control** settings with its LAN/Tailscale IP,
port (default 8795), and current bearer token, then **TEST CONNECTION**. The token
is mandatory and masked in the settings field. Enable HTTPS only when the phone
server has a certificate trusted by this computer.

Plain HTTP on a LAN is not encrypted: anyone able to observe that traffic can
read its token/commands. Use a trusted Tailscale route or trusted HTTPS for
confidential transport. Do not expose port 8795 publicly. The client refuses
redirects and configured HTTP proxies to avoid forwarding its credentials.
The existing repository configuration file still stores plugin settings locally
in plaintext; protect that file and never commit it.

The Companion must support `/ping`, `/launch`, `/tap`, `/swipe`, `/type`,
`/keyevent`, `/screenshot`, `/back`, `/home`, and `/notifications`. Commands and
ping must acknowledge `{"ok": true}`; failure responses never become successful
actions. Screenshot responses contain base64 `png`; notification responses contain
a `notifications` array. Only the notification count is returned to the agent,
because notification bodies can contain one-time codes. Accessibility, screen
capture, and notification permissions must be granted on the phone as needed.

## Screenshots, input and verification

Screenshots run only for an explicit `screencap` action with a new `.png` path in
an existing directory. An existing file is never overwritten. PNGs are validated
before writing, capped at 12 MiB and 33 million pixels, and ADB captures use
`exec-out` without leaving temporary screenshots on the phone. Nothing captures
continuously. Avoid requesting screenshots on password/authentication screens.

ADB's standard `input text` supports printable ASCII in this implementation;
Unicode, control characters and literal `%s` are rejected instead of silently
corrupting them. Use the phone keyboard or compatible Companion for those inputs.
Phone shell arguments are quoted independently of the Windows host shell.

A completed input command confirms transport completion, not the resulting UI
state. Read the next screen when appropriate and obtain explicit confirmation
before sensitive operations. These transport plugins cannot infer whether an
arbitrary tap is a payment, deletion or message-send action.

## Checks

```powershell
python -m unittest discover -s tests -p test_phone_control.py -v
python -m plugins.wireless_control
python -m plugins.adb_control
git diff --check
```

These tests mock devices/HTTP/subprocesses, cover authorization, ambiguity,
pairing-code handling, timeouts, network/server errors, request validation,
shell quoting and safe screenshot output, and do not need a phone. Real phone
input, Companion compatibility, LAN reachability and firewall behavior remain
unverified until the deferred hardware pairing is completed.
