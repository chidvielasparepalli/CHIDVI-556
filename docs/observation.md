# Optional desktop awareness

`core/observation.py` adds local window awareness to the existing CHIDVI agent.
It does not create another AI agent, read form values, capture screenshots, send
images to a model, or record a history to disk. The desktop's explicit screen
analysis tools are separate and require their own capture permissions.

Monitoring starts **stopped**, with **AC only** enabled and **Offer help** disabled.
The desktop monitoring controls start, pause, stop, and clear the current context.
A persistent status reports when monitoring is running or temporarily suspended.
Pause/stop immediately clear the retained context; results already in flight are
discarded after a stop or permission change. Clear also forgets website/application
greeting history. There are no saved observations or screenshots to delete.

Only the current foreground application, window identity, sanitized window title,
and explicitly connected browser page metadata are retained. Query strings,
fragments, URL credentials, sensitive authentication paths and likely secrets
are excluded. Password, authentication, checkout, payment and lock contexts pause
observation. This is conservative metadata filtering, not a guarantee that every
application correctly labels its sensitive UI. No text fields or screenshot pixels
are read by this service.

When AC only is selected, an unplugged battery suspends observation; reconnecting
power resumes it. Systems without a battery are treated as desktops on AC power.
Unavailable power diagnostics pause observation until they recover or AC only is
disabled. CPU use above 85% or memory use above 90% also defers observations.

On Windows, a dedicated `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` message thread
marks foreground changes for the worker. Pause/stop posts a quit message; the
hook is removed on the same thread that installed it. A bounded fallback checks
metadata at adaptive 1–15 second intervals. Event bursts can trigger at most two
metadata reads per second. Errors back off up to 60 seconds. No screen capture or
inference runs on the Qt main thread. No GPU workload is introduced by this service.

Browser navigation events are accepted through
`actions.browser_control.bind_navigation(observation.notify_navigation)` for
the connected/controlled page. Monitoring being off makes the callback discard
its inputs. Unsupported browsers and browser windows without a supported
connection still have foreground metadata; they do not gain automatic DOM access.

Offer help emits one intent question per application or website in the current
bounded session history. It suppresses greetings during tasks or speech, applies
a global cooldown, and ignores minor path changes on the same website. The
returned greeting goes through the current CHIDVI voice/personality. It never
executes an action or grants permission. When a task is active, changed context
returns a status update instead of a new greeting.

Optional configuration before starting CHIDVI:

```powershell
Set-Location D:\CHIDVI-556
$env:CHIDVI_PROACTIVE_COOLDOWN = '120'
$env:CHIDVI_PROACTIVE_APPS = 'chrome.exe,msedge.exe,code.exe,blender.exe'
python main.py
```

Cooldown defaults to 120 seconds and is bounded to 15–3600 seconds. The app list
is comma separated, matches executable names case-insensitively, and only affects
help offers. An empty list disables help offers for all applications. Defaults
include common browsers, Explorer, Notepad, VS Code, Blender and Office apps.

Worker integration:

```python
observation = ObservationController(on_status=ui_status_signal.emit)
observation.configure(ac_only=True, proactive=False)
observation.set_mode('running')
# Call in the existing worker, supplying live task and playback states:
event = observation.poll(busy=task_running, speaking=voice_playing)
status = observation.snapshot()
observation.set_mode('stopped')  # application shutdown
```

`poll()` returns `None` when unchanged/suspended, otherwise a dict with `context`,
`status`, optional `greeting`, and optional `task_status`. `snapshot()` returns only
sanitized state; it does not perform observation.

Validation:

```powershell
python -m unittest discover -s tests -p test_observation.py -v
```

The unit tests use fake windows, power/resource readings and clocks. The native
Windows event thread's start/stop was smoke-tested without collecting window
metadata or screenshots. Actual UI indicator/voice behavior is verified by the
application integration checks, not by these unit tests.

Windows implementation references:
[SetWinEventHook](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook),
[UnhookWinEvent](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-unhookwinevent),
[PostThreadMessageW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-postthreadmessagew).
