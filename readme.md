# ⚙️ CHIDVI 556 (Road to AGI)
A real-time voice AI that can hear, see, understand, and control your computer — on any OS. Supports Windows, macOS, and Linux. Built on the Gemini Live API for native audio streaming, delivering zero subscriptions and total digital autonomy.

---

## ✨ Overview

**CHIDVI 556 is the hands-free & scalable release.** Say **" Arise"** and it wakes; stay quiet and it slips back to sleep on its own — while asleep, your microphone never leaves the machine, so an off-hand *"I'll be right there"* to someone in the room no longer sets it off. Under the hood it now runs on the faster **Gemini 3.1 Flash Live** engine, and the moment you ask for something that takes a beat — analysing a file, searching the web — it answers instantly *("On it — going through that now…")* so you never wonder whether it heard you.

It's also built to grow: every skill — bundled or drop-in — now **describes itself in its own file**, so adding a tool is a one-file operation and the core stays lean.

It's not just an assistant — it's an extension of your digital life.

---

## 🚀 Capabilities

### Core Features
| Feature | Description |
|---|---|
| 🎙️ Wake Word | Local **" Arise"** detection — sleeps until called, auto-sleeps after 2 min of silence, and never streams audio while asleep. Opt-in, one-click download, toggle & manual sleep/wake from the UI |
| ⚡ Instant Acknowledgment | Speaks a short, context-aware reply in **your language** the instant a longer task starts — no more silent waiting |
| 🚀 Faster Live Engine | Runs on **Gemini 3.1 Flash Live** — roughly 2× faster time-to-first-word than the previous model |
| 🧩 Self-Describing Skills | Actions and plugins share one shape (`TOOL` / `PLUGIN` dict + `run()`), auto-discovered at launch — adding or moving a skill is a single file, no core edits |
| 🧠 Recallable Memory | No size limit and nothing silently forgotten — the prompt carries what fits, the rest is looked up on demand from a local search |
| 👁️ Memory Panel | See every fact JARVIS has stored about you, when it learned it, and delete any of it in one click |
| ↩️ Undo | Take back what the assistant did — files it moved, renamed, created or wrote, and settings it changed |
| ⚠️ Real Confirmation | Shutdown, restart and WiFi wait for a button **you** press — the model cannot confirm its own irreversible actions |
| 🎧 Audio Device Picker | Choose the microphone and speakers by name, filtered to the short list your OS shows — and measured, so every entry actually works |
| 🔗 Session Continuity | A dropped connection, a voice change or a device change no longer wipes the conversation |
| 🧩 Plugin System | Drop a single `.py` file into `plugins/` — JARVIS learns a new skill on next launch |
| 🎙️ Real-time Voice | Ultra-low latency conversation in any language via Gemini Live API |
| 🎨 Live Theming | Recolour the entire HUD from a hue wheel or hex — applied instantly across every panel |
| 〰️ Reactive HUD | Waveform and reactor core pulse to real audio — your mic while listening, JARVIS while speaking |
| 🎙️ Voice Picker | Choose from 5 native Gemini voices and switch live from the UI — no restart |
| ♾️ Unlimited Sessions | Sliding-window context compression — one conversation can last for hours |
| 🖥️ System Control | Launch apps, adjust volume/brightness, WiFi, shortcuts, power — all by voice |
| 🧩 Autonomous Tasks | High-level planning for complex multi-step goals via agent mode |
| 👁️ Visual Awareness | Real-time screen capture and webcam vision piped into your main Gemini session |
| 🧠 Persistent Memory | Deeply remembers projects, preferences, and personal context across sessions |
| ⌨️ Hybrid Input | Seamlessly switch between keyboard typing and voice commands |
| 🌅 Morning Briefing | On first boot: greets you, reads the time, recaps yesterday, and fetches live news |
| 🔔 Proactive 2.0 | Time-aware, context-aware check-ins — knows the time of day, your projects, and what you've been discussing |
| 🗓️ Session Memory | Summarises each conversation and mentions it naturally next morning — consumed after use, never repeats |
| 👁️‍🗨️ Background Monitoring | User-configured topic watching — checks for new headlines once a day and alerts naturally |
| 📊 Hardware Monitoring | Continuous CPU, RAM, GPU and temperature telemetry with localized voice alerts |
| 🌤️ Weather Report | Live weather data for your city, personalized from memory |
| 🗺️ Dynamic Content Panel | Scrollable display layer beneath the HUD that renders web results, news, and search data |
| 🔍 Multi-Mode Web Search | `news` / `research` / `price` / `compare` / `search` — Gemini Grounded first, DDG fallback |
| ⏰ Smart Reminders | OS-native scheduled notifications (Windows Task Scheduler / macOS LaunchAgent / Linux systemd) |
| ✈️ Flight Finder | Live flight price and availability lookup |
| 🎮 Game Updater | Checks and triggers game updates on Steam and Epic Games on demand |
| 📂 File Processor | Read, summarize, and answer questions about local files |
| 💻 Code Helper | Inline code review, debugging, and generation |
| 🌐 Browser Control | Open URLs, navigate tabs, and interact with the browser by voice |
| 📨 Send Message | Compose and send messages through WhatsApp, Telegram, and more |
| 🎬 YouTube Control | Search, play, and control YouTube playback by voice |
| 🖱️ Desktop Control | Taskbar, window management, and desktop-level operations |
| 🧑‍💻 Silent Language Memory | Detects spoken language on first use — all future sessions adapt automatically |
| 📱 Remote Dashboard | Control the assistant from your phone via QR code pairing |
| ⚡ Auto-Start on Boot | Registers with the OS startup system (registry / LaunchAgent / .desktop) |
| 📋 Clipboard Intelligence | Copy any text → floating panel with Translate / Summarise / Explain / Fix |
| 🪪 Assistant Customization | Change the assistant name, your name, voice, and colour from the UI — takes effect immediately |
| 🙌 BareHands Mode | The webcam hands interface that floats notesimages over the screen, moved by your bare hands |
---

## 🆕 What's New in CHIDVI 556

CHIDVI 556 is about making JARVIS **hands-free, faster, and easy to extend** — all universal: no hardcoded language, no bundled asset files, works the same on Windows, macOS and Linux.

### 🎙️ Wake Word — " Arise"
JARVIS can now sit quietly until you call it. Turn on **⚙ → WAKE WORD** (a one-click, opt-in download of a tiny local model) and it goes to sleep: the microphone is processed **only on your machine** by a local detector, and nothing is sent to the cloud until it hears **" Arise."** Once awake it listens normally, then **auto-sleeps after 2 minutes** of silence. You can also **sleep/wake it by clicking** in the settings. Because it's a *local* gate, background chatter — *"I'm coming!"* to someone at home — never wakes it. It costs **zero** when off (the model isn't even loaded), and the detection runs in its own thread, so nothing else in the app slows down.

### ⚡ Instant Acknowledgment
No more silent gaps. When you ask for something that takes a moment — reading an uploaded file, a web/research search, building code — JARVIS **immediately** says one short, natural sentence *in your language* (*"Right away — going through that file now."*) and *then* runs the tool. Instant actions (opening an app, volume) stay snappy with no chatter.

### 🚀 Faster Live Engine — Gemini 3.1 Flash Live
The live session moved to **`gemini-3.1-flash-live-preview`**, cutting the time-to-first-word roughly in half while keeping tools, all five voices, transcription, session resumption and sliding-window compression intact.

### 🧩 Self-Describing Skills — a Scalable Core
Every bundled **action** now carries its own `TOOL` declaration in its own file (exactly like a drop-in **plugin's** `PLUGIN` dict), and the core auto-discovers them at launch. `main.py` no longer holds a giant list of tool definitions and dispatch branches — it shrank by hundreds of lines. Adding a new built-in skill, or promoting an `actions/*.py` file into a shareable plugin, is now just… moving a file.

> Built on the CHIDVI LI/  foundation: the **🧩 Plugin System**, **♾️ Unlimited Sessions**, **🎨 Live Theming**, **〰️ Reactive HUD** and **🎙️ Voice Picker** are all still here.

---

## 🔄 The Foundation Update — in every CHIDVI from  

These four landed across **CHIDVI  ,  I, LIV and LV at the same time**, after each of those releases had already shipped. They are not what any one of those versions originally introduced; they are the floor all of them now stand on, so moving up a CHIDVI never costs you something the one below it had.

No new dependencies. No bundled asset files. No hardcoded language, and nothing that assumes one operating system.

### 🧠 A memory that actually remembers

The store was capped at **2,200 characters — the whole memory, not per entry** — because all of it was pasted into the system prompt on every connect, so growing the memory grew every request. When it filled, the oldest entries were deleted and one line was printed to a console nobody reads. An assistant advertised as remembering "projects, preferences and personal context" was in practice a two-page notepad that quietly forgot your sister's name after a few weeks.

Storage and prompt budget are now separate problems:

* **Nothing is deleted.** The cap is a runaway guard normal use never approaches, and if it is ever hit it says so in the activity log instead of on stdout.
* **The prompt carries a core, not a dump.** Identity in full, then the most recently updated facts, budgeted — measured at **971 characters on a memory holding 62 stored facts.** That is *smaller* than the old whole-store cap, so sessions now connect with fewer tokens than before.
* **The rest is fetched on demand.** A `recall_memory` tool searches the full store locally — no network, no second model, well under a millisecond.

The part that is easy to get wrong: **a model cannot look something up if it doesn't know the thing exists.** So the prompt also carries an **index of the keys** it had no room for. Without it, "who is Ayşe?" gets "I don't know" while `ayse_sister` sits on disk unread. That index interleaves categories rather than sorting by recency — sorted like the core, a memory with forty preferences pushed the one entry the index existed for off the end.

⚙ → **🧠 MEMORY** shows every stored fact, when it was learned, and a ✕ to forget it. Everything stays in `memory/long_term.json` on your machine.

### ↩️ Undo — it can take back what it did

JARVIS moves files, renames them, writes to them and changes your settings. None of that had a way back; if it misheard you, the only remedy was to fix it by hand.

Say **"undo"** — in any language — and it reverses its own last action:

| | |
|---|---|
| **Files** | move · rename · create · copy · write · delete · organize desktop |
| **Settings** | volume · brightness · dark mode |

Three things it deliberately does *not* do:

* **It does not guess.** Settings undo reads the current value *before* changing it. Where a platform won't report that value, nothing is registered — an undo that restores a guess is worse than no undo.
* **It does not hoard.** Undoing a write means keeping the old contents in memory, so files over 1 MB are excluded and it says so rather than holding a 200 MB log for the session.
* **It does not delete your files to undo a copy.** The reverse of a copy is removing the copy; the reverse of "create a folder" is removing it *only while it's still empty*.

`organize_desktop` gets special treatment — one command that moves dozens of files, which made it the least reversible thing the assistant could do. It journals every move and puts all of them back in one go, cleaning up the folders it created if they're still empty.

**Undo costs nothing at runtime.** It appends a closure to a list; nothing in it runs unless you ask.

### ⚠️ A confirmation the model can't forge

The old gate read like this:

```python
if action in _DANGEROUS_ACTIONS:            # {"restart", "shutdown"}
    confirmed = str(params.get("confirmed", "")).lower()
```

`confirmed` is a **tool parameter, which means the model fills it in.** Nothing stopped it sending `confirmed=yes` on the first call and nothing checked that a human was ever involved. It was a convention, not a gate. And its coverage was two actions — so `toggle_wifi`, which cuts the assistant's own connection to the Live API and therefore *cannot be asked to undo itself*, went through with no gate at all.

The token is now issued by the interface. Shutdown, restart and WiFi put a banner on the HUD and **return immediately**; the action runs only if you press CONFIRM. Nothing blocks — JARVIS keeps talking while the banner is up — so this is **cheaper than the old gate**, which burned two tool round trips on every power command.

> The split between the two mechanisms is about reversibility, not about how alarming a word sounds. Anything undoable is done at once; only the genuinely irreversible asks. An assistant that checks with you before turning the volume down is one you stop talking to.

### 🎧 It finally asks which microphone

Both audio streams opened with no device argument at all, so they always took whatever the OS called "default" — and on Windows that *moves on its own* the moment you plug a headset in. "JARVIS can't hear me" almost always meant "JARVIS is listening to the webcam".

⚙ → **🎧 AUDIO DEVICES** lets you pick the microphone and the speakers by name. Two things matter more than the dropdown:

**The list is short.** `query_devices()` returns one entry per *device × host API*, not per device — measured on an ordinary Windows machine, **41 entries for what the sound settings show as 4 microphones and 4 speakers.** The same microphone appears four times, under MME, DirectSound, WASAPI and WDM-KS, with nothing to say which is which. That is not a choice, it's a quiz. The picker takes one host API per direction, drops the "Sound Mapper" and "Primary Sound Driver" pseudo-devices that just mean "default", and deduplicates. **41 → 8.**

**Every entry has been measured, not assumed.** The obvious approach is to pick the host API with the nicest names — WASAPI on Windows, which in shared mode **doesn't resample**, so with 16 kHz in and 24 kHz out against 48 kHz hardware every open failed. Adding a rate check and moving to DirectSound passes that test on both sides, and PortAudio's DirectSound **output is a silent sink**: the stream opens, every write returns success in ~0 ms, and not one sample reaches the speakers.

| | write(2.0 s) took | |
|---|---|---|
| MME | **2.02 s** | consumed in real time |
| DirectSound | **0.00 s** | swallowed instantly |

No capability flag reports that. So the app measures it — once per host API per direction, on a background thread at startup, using silence. Two consequences worth stating plainly:

* **Each direction picks its own host API.** On Windows this lands on DirectSound for the microphone and MME for the speakers — a split no amount of reasoning would have produced.
* **The probe runs in the mode the app actually ships.** DirectSound input passes a callback stream and fails a blocking read; probing the wrong mode rejected a microphone that works perfectly.

Your choice is stored **by name, not by index** — indices shift whenever something is plugged in. If the saved device is gone, it falls back to the system default and says so in the log rather than failing to start.

### 🔗 It stops forgetting the conversation when the connection drops

`session_resumption` was switched on in the config and the handle the server sent back was **never read** — so every reconnect started an empty session. A dropped packet, or simply changing the voice, wiped the conversation. "Unlimited sessions" leaked through exactly this hole.

The handle is captured and replayed now. A network blip, or switching your microphone, keeps the conversation intact.

It is held in memory only, deliberately: writing it to disk would make a fresh launch continue yesterday's chat, which sounds appealing but breaks the session-summary flow — a conversation that never ends never produces a summary, and the "yesterday we talked about…" line in the morning briefing silently disappears. Changing the **voice** also starts clean on purpose, since resuming restores the server's session state and would likely bring the old voice back with it.

### 🩹 Fixes that came with it

* **The assistant could die on a log line.** Status lines carry emoji and arrows (`📤 file_controller → Moved: a.txt → Documents/`). On a non-UTF-8 console — cp1254 on a Turkish Windows, cp1251 on a Russian one, cp932 on a Japanese one — printing one raises `UnicodeEncodeError`, and because that print sits *after* the tool's own `try/except`, it escaped into the receive loop and took the session down.
* **Every computer command paid for two model round trips.** `computer_settings` made an *entire second Gemini call, inside the tool*, purely to translate the request into one of its own action names — because the declaration only said "The action to perform", so the model rarely filled it in. When that second call failed, the fallback was `description.lower().replace(" ", "_")`, which turns the Turkish for "turn it down" into `sesi_kis` and straight into "Unknown action". The declaration now names all 56 actions and the rest is spelling tolerance handled locally by `difflib` in microseconds. When nothing matches it suggests real action names instead of dead-ending.
* An unresolvable saved audio device, or one the driver refuses to open, falls back to the system default and says so — on both the microphone and the speakers.
* A rejected session-resumption handle is dropped after one attempt, so an expired handle can never be replayed on every retry and prevent the reconnect it exists to protect.

## ⚡ Quick Start

```bash
git clone https://github.com/chidvielasparepalli/CHIDVI-556.git
cd CHIDVI-556
pip install requirements.txt
python main.py
```

`setup.py` only ever installs what your operating system needs — the Windows-only libraries are skipped automatically on macOS and Linux (and vice-versa). Prefer to do it by hand? `pip install -r requirements.txt` works too.

> ⚠️ **Installation Note:** If you hit a `ModuleNotFoundError` for an OS-specific package, install it with `pip install <module_name>`. The optional **wake word** engine is *not* installed here — grab it in one click from **⚙ → WAKE WORD** inside the app.

## 🗂️ Project Structure

```
CHIDVI-556/
├── .env
├── .gitignore
├── main.py                       # Core loop — Gemini Live session, audio I/O, wake/sleep state, tool dispatch
├── ui.py                         # PyQt6 HUD — waveform, log panel, settings drawer, plugin manager, camera feed
├── setup.py                      # OS-aware installer
├── readme.md
├── requirements.txt
├── actions/                      # Bundled skills — TOOL dict + handler
│   ├── background_monitor.py     # Daily topic watching
│   ├── browser_control.py        # Browser control
│   ├── code_helper.py            # Code review/generation
│   ├── computer_control.py       # KB shortcuts, mouse, windows
│   ├── computer_settings.py      # Volume, brightness, WiFi, power
│   ├── desktop.py                # Desktop & taskbar
│   ├── dev_agent.py              # Dev task agent
│   ├── file_controller.py        # File ops
│   ├── file_processor.py         # Document reading
│   ├── flight_finder.py          # Flight search
│   ├── game_updater.py           # Steam/Epic updates
│   ├── open_app.py               # App launcher
│   ├── proactive.py              # Proactive check-ins
│   ├── reminder.py               # OS notifications
│   ├── screen_processor.py       # Screen & webcam capture
│   ├── send_message.py           # Messaging
│   ├── system_monitor.py         # CPU/RAM/GPU/temp telemetry
│   ├── weather_report.py         # Live weather
│   ├── web_search.py             # Gemini + DDG search
│   └── youtube_video.py          # YouTube control
├── barehands/                    # Desktop-connection engine (plugin source for barehands_control)
│   ├── .gitattributes
│   ├── .gitignore
│   ├── CONTRIBUTING.md
│   ├── LICENSE
│   ├── README.md
│   ├── TROUBLESHOOTING.md
│   ├── barehands.json.example
│   ├── barehands.md
│   ├── run.bat
│   ├── server.py                 # Barehands server
│   ├── stage.html
│   ├── update.bat
│   ├── update.sh
│   ├── bin/
│   │   ├── board-state.sh
│   │   └── board.sh
│   ├── media/
│   │   ├── fx/
│   │   │   ├── README.md
│   │   │   └── fireball.png
│   │   ├── holo/README.md
│   │   ├── misc/
│   │   │   ├── README.md
│   │   │   └── g
│   │   └── models/
│   │       ├── README.md
│   │       ├── a_windy_day.glb
│   │       ├── earth_globe_hologram_2mb_looping_animation.glb
│   │       ├── hulkbuster.glb
│   │       ├── iron-man_mark_85__rigged.glb
│   │       └── rifle__awp_weapon_model_cs2.glb
│   ├── sample-notes/
│   │   ├── Field Guide/
│   │   │   ├── Make It Yours.md
│   │   │   ├── Props and Models.md
│   │   │   └── Streaming and Recording.md
│   │   └── Getting Started/
│   │       ├── The Gestures.md
│   │       ├── Welcome.md
│   │       └── Wire In Your AI.md
│   └── state/README.md
├── config/
│   ├── __init__.py
│   ├── api_keys.j
├── core/
│   ├── __init__.py
│   ├── action_loader.py          # Bundled-action engine
│   ├── audio_devices.py          # Mic/speaker list
│   ├── confirm.py                # Irreversible-action gate
│   ├── installer.py
│   ├── llm_client.py
│   ├── plugin_loader.py          # Plugin engine
│   ├── prompt.txt                # Assistant personality
│   ├── stt.py
│   ├── tts.py
│   ├── undo.py                   # Shared undo stack
│   └── wake_word.py              # "Arise" detector
├── dashboard/
│   ├── __init__.py
│   ├── server.py                 # Remote dashboard server
│   └── static/
│       ├── app.html
│       ├── crypto-js.min.js
│       └── login.html
├── memory/
│   ├── __init__.py
│   ├── config_manager.py         # api_keys.json access
│   ├── long_term.json            # Persistent store
│   └── memory_manager.py         # Load/save long_term.json
├── plugins/
│   ├── __init__.py
│   ├── _template.py              # Copy this to write a plugin
│   ├── adb_control.py
│   ├── barehands_control.py
│   ├── form_agent.py
│   ├── game_control.py
│   └── wireless_control.py        # API key, OS setting, assistant name, user name, voice, UI colour, toggles
```
## 👤 Connect with the Creator

Engineered by a developer building a real-world JARVIS-style assistant.

| Platform | Link |
| --- | --- |
| YouTube | [@chidvielas](https://www.youtube.com/@bysiddx) |
| Instagram | [@chidvielas](https://www.instagram.com/chidvielasparepalli) |
#                          ⚙️ CHIDVI-556
### Road to AGI — A General-Purpose AI Agent Research Prototype

CHIDVI-556 is the next-generation evolution of **CHIDVI-555**.

It is designed as an **AGI research prototype** focused on building a more autonomous, multimodal, extensible, and computer-capable AI agent.

The goal is not to claim that CHIDVI-556 is true AGI.

Instead, CHIDVI-556 is an experimental platform for exploring the capabilities required for increasingly general AI systems:

> **Understand → Plan → Execute → Observe → Evaluate → Correct → Learn → Repeat**

---

# 🧠 CHIDVI Evolution

```text
CHIDVI-555
    │
    │  Personal AI Assistant
    │
    ▼
CHIDVI-556
    │
    │  AGI Research Prototype
    │
    ▼
Advanced AGI System
    │
    │  Future Research
    ▼
ASI Research
🆚 CHIDVI-555 vs CHIDVI-556
Capability	CHIDVI-555	CHIDVI-556
AI Conversation	✅	✅
Voice Interaction	✅	✅ Enhanced
Personality System	✅	✅
Memory	✅	🧠 Persistent + Recallable
Computer Control	✅	🚀 Expanded
Tool Usage	✅	🧩 Extensible
Plugin System	✅	🧩 Self-Describing Plugins
Autonomous Tasks	Basic	🚀 Advanced
Multi-Step Planning	Limited	🧠 Agent-Based
Self-Evaluation	Limited	🔄 Planned / Integrated
Error Correction	Basic	🔄 Autonomous correction
Web Research	✅	🔎 Multi-mode research
Coding	✅	💻 Developer Agent
File Understanding	✅	📂 Enhanced
Vision	Basic	👁️ Screen + Webcam
Wake Word	❌ / Limited	🎙️ Local Wake Word
Real-Time Voice	✅	⚡ Gemini Live
Session Continuity	Limited	🔗 Persistent Sessions
Undo System	Limited	↩️ Action Reversal
Safety Confirmation	Basic	⚠️ Human-issued confirmation
Audio Device Management	Basic	🎧 Device-aware
Remote Control	Limited	📱 Remote Dashboard
Phone Control	Experimental	📱 Plugin Architecture
Bare Hands Interaction	Experimental	🙌 BareHands
Proactive Behavior	Limited	🌅 Proactive System
Hardware Monitoring	Limited	📊 Continuous Monitoring
Plugin Architecture	Basic	🧩 Scalable
Cross-Platform Goal	Limited	🖥️ Windows / macOS / Linux
AGI Research Architecture	❌	🧠 Designed for experimentation
✨ What Is CHIDVI-556?

CHIDVI-556 is a real-time multimodal AI agent designed to interact with the user, understand context, operate computer systems, use tools, remember information, and execute increasingly complex tasks.

Unlike a conventional chatbot, CHIDVI-556 is designed around an agent loop.

                    ┌───────────────┐
                    │     GOAL      │
                    └───────┬───────┘
                            ↓
                    ┌───────────────┐
                    │  UNDERSTAND   │
                    └───────┬───────┘
                            ↓
                    ┌───────────────┐
                    │     PLAN      │
                    └───────┬───────┘
                            ↓
                    ┌───────────────┐
                    │    EXECUTE    │
                    └───────┬───────┘
                            ↓
                    ┌───────────────┐
                    │    OBSERVE    │
                    └───────┬───────┘
                            ↓
                    ┌───────────────┐
                    │   EVALUATE    │
                    └───────┬───────┘
                            ↓
                    ┌───────────────┐
                    │    CORRECT    │
                    └───────┬───────┘
                            ↓
                    ┌───────────────┐
                    │     LEARN     │
                    └───────┬───────┘
                            │
                            └──────────► Repeat

This architecture allows CHIDVI-556 to move beyond simple question-and-answer interaction toward goal-oriented autonomous execution.

🚀 Core Capabilities
🎙️ Real-Time Voice

CHIDVI-556 supports real-time voice interaction.

The system is designed around low-latency conversational interaction rather than traditional:

Record → Send → Wait → Receive

Instead, the architecture aims for:

User Speech
     ↓
Real-Time Audio
     ↓
AI Processing
     ↓
Real-Time Response
🎙️ Local Wake Word

CHIDVI-556 supports a local wake-word architecture.

Example:

"Arise"

When sleeping:

Audio processing remains local.
Audio is not streamed to the AI service.
The wake detector waits for the activation phrase.
The assistant wakes only after detecting the wake word.
The system can automatically return to sleep after inactivity.

This provides a more privacy-conscious hands-free experience.

⚡ Instant Task Acknowledgment

Long-running operations should not leave the user wondering whether CHIDVI heard them.

For example:

User:
"Analyze this document."

CHIDVI:
"On it — analyzing the document now."

        ↓

Document Processing
        ↓

Result

This creates a more natural agent experience.

🧠 Persistent Memory

CHIDVI-556 maintains persistent local memory.

Memory can contain:

User preferences
Projects
Important facts
Previous sessions
Topics
Configurations
Monitoring tasks
Contextual information

Memory is stored locally.

memory/
├── long_term.json
├── memory_manager.py
└── config_manager.py

The architecture separates:

Memory Storage
        +
Memory Retrieval
        +
Context Injection

Instead of loading the entire memory database into every AI request, relevant information can be retrieved when required.

🧠 Memory Panel

The UI is designed to expose stored memory.

Users should be able to:

View stored memories
Inspect when information was learned
Remove individual memories
Control persistent information

The objective is to keep memory transparent and user-controlled.

↩️ Undo System

CHIDVI-556 introduces an action reversal architecture.

Actions that modify the computer can register an undo operation.

Examples:

Move file
Rename file
Create file
Copy file
Write file
Modify settings
Organize desktop

Conceptually:

AI Action
    ↓
Execute
    ↓
Register Undo
    ↓
User says "Undo"
    ↓
Reverse Action

The goal is to make autonomous computer control safer.

⚠️ Human Confirmation

Certain irreversible actions require explicit human confirmation.

Examples include:

Shutdown
Restart
WiFi changes
Other configured destructive actions

The important security principle is:

The AI cannot confirm its own dangerous action.

The confirmation originates from the user interface.

AI requests action
       ↓
UI displays confirmation
       ↓
USER presses CONFIRM
       ↓
Action executes
👁️ Vision

CHIDVI-556 is designed to understand visual information.

Vision capabilities include:

Screen capture
Webcam input
Visual analysis
UI understanding
Document understanding
Object understanding

The vision architecture can eventually support additional capabilities such as:

Food Analysis
Document Analysis
Object Recognition
Screen Understanding
Code Screenshot Analysis
Visual Research
Environment Understanding
🖥️ Computer Control

CHIDVI-556 can interact with the computer through tools.

Examples:

Open applications
Control windows
Mouse operations
Keyboard shortcuts
Volume control
Brightness control
WiFi control
Power operations
Desktop operations
Browser operations
File operations

The objective is to transform natural-language instructions into controlled computer actions.

Example:

User:
"Open VS Code and open my CHIDVI project."

        ↓

AI understands intent

        ↓

open_app
        ↓
desktop_control
        ↓
file_controller

        ↓

Project opened
🧩 Plugin Architecture

CHIDVI-556 is designed around a scalable plugin architecture.

Plugins live inside:

plugins/

A plugin can expose a capability to the AI without requiring large modifications to the core system.

Current plugin architecture includes:

plugins/
├── _template.py
├── adb_control.py
├── barehands_control.py
├── form_agent.py
├── game_control.py
└── wireless_control.py

The goal is:

Create Plugin
      ↓
Add Plugin File
      ↓
Plugin Loader Discovers It
      ↓
CHIDVI Learns the Capability

This keeps the core system smaller and makes experimentation easier.

📱 Phone Control

CHIDVI-556 contains experimental phone-control infrastructure.

The architecture is designed to allow CHIDVI to communicate with a companion Android application.

Potential capabilities include:

Launch applications
Tap
Swipe
Type text
Press buttons
Navigate
Take screenshots
Read notifications
Control media
Execute Android actions

The communication layer can use:

CHIDVI Desktop
       ↓
Wireless Control Plugin
       ↓
Android Companion
       ↓
Phone

Authentication and permissions should be required before exposing sensitive phone operations.

🙌 BareHands

CHIDVI-556 includes a BareHands integration.

The BareHands system provides a visual interaction environment where hand movements can be used to interact with virtual content.

Architecture:

Webcam
   ↓
Hand Tracking
   ↓
BareHands
   ↓
Visual Interaction
   ↓
CHIDVI

The system can display virtual models, effects, and interactive content.

🧑‍💻 Developer Agent

CHIDVI-556 includes a developer-agent architecture.

It is intended to assist with:

Code generation
Debugging
Code analysis
Project understanding
Error investigation
Development tasks

Long-term development goals include:

Understand repository
      ↓
Identify problem
      ↓
Plan solution
      ↓
Modify code
      ↓
Run tests
      ↓
Analyze failures
      ↓
Correct implementation
      ↓
Verify

This is an important component of the AGI research direction.

🔎 Web Research

CHIDVI-556 provides web-search capabilities.

Search modes include:

search
news
research
price
compare

The objective is to allow CHIDVI to gather external information before completing tasks.

Example:

User:
"Research the best GPU for my budget."

        ↓

Search
        ↓
Collect information
        ↓
Compare
        ↓
Evaluate
        ↓
Respond
📂 File Intelligence

CHIDVI-556 can work with local files.

Capabilities include:

Reading documents
Summarization
File analysis
Question answering
File operations

This allows the assistant to work directly with a user's local project environment.

🌐 Browser Control

CHIDVI-556 can interact with web browsers.

Potential operations include:

Open URL
Navigate
Switch tabs
Interact with webpages
Search
Read web content

Browser control enables the agent to perform tasks that require interaction with websites.

📨 Messaging

CHIDVI-556 includes messaging integration infrastructure.

The assistant can be extended to compose and send messages through supported messaging platforms.

The architecture is designed so that external communication can remain permission-controlled.

🎬 YouTube Control

CHIDVI-556 can control YouTube-related operations.

Examples:

Search videos
Play videos
Control playback

Voice commands can therefore control media without requiring manual interaction.

🎮 Game Control

Game-related functionality includes update management and game-control infrastructure.

Supported integrations can include:

Steam
Epic Games
Game Control Plugins
📊 Hardware Monitoring

CHIDVI-556 can monitor system resources.

Examples:

CPU
RAM
GPU
Temperature

The system can eventually provide contextual alerts such as:

"Your GPU temperature is getting high."
🌤️ Weather

The assistant can retrieve live weather information and present it conversationally.

Example:

User:
"What's the weather today?"

CHIDVI:
"Today's weather is..."
⏰ Smart Reminders

CHIDVI-556 supports OS-level reminder infrastructure.

The architecture can integrate with platform scheduling systems.

The goal is to allow:

"Remind me tomorrow at 8 AM."

to become an actual operating-system notification.

🌅 Proactive Intelligence

CHIDVI-556 explores proactive AI behavior.

Instead of always waiting for:

User → Command → AI

the long-term architecture supports:

Context
  ↓
Time
  ↓
Memory
  ↓
Monitoring
  ↓
Reasoning
  ↓
Relevant Notification

Examples:

Morning briefing
Project reminders
System warnings
Tracked topics
Scheduled events

Proactive behavior remains configurable and user-controlled.

📱 Remote Dashboard

CHIDVI-556 includes a remote dashboard architecture.

Phone
  ↓
Remote Dashboard
  ↓
CHIDVI-556

The dashboard can provide remote access to selected assistant functionality.

Security and authentication are required for remote access.

🎨 Reactive HUD

CHIDVI-556 uses a PyQt6-based interface.

The HUD can represent assistant state through:

Waveform
Audio activity
Logs
Status
Camera feed
Settings
Plugin management

The objective is to make the assistant's state visible rather than hiding everything behind a terminal.

🎧 Audio Device Management

CHIDVI-556 includes dedicated audio-device management.

Users can select:

Microphone
Speaker

The system attempts to resolve devices reliably rather than depending entirely on the operating system's current default device.

🔊 Voice System

The voice architecture supports conversational audio interaction.

The system contains dedicated components for:

STT
TTS
Audio devices
Wake word
Live AI interaction

These components are separated from the main application to make the audio system easier to maintain.

🧩 Architecture
                    CHIDVI-556
                         │
        ┌────────────────┼────────────────┐
        │                │                │
       UI              Core             Memory
        │                │                │
     PyQt6          Agent Runtime     Long-Term
        │                │                │
        │          ┌─────┴─────┐          │
        │          │           │          │
        │         LLM        Tools        │
        │          │           │          │
        │          └─────┬─────┘          │
        │                │                │
        └────────────────┼────────────────┘
                         │
                    Plugins / Actions
                         │
       ┌─────────────────┼──────────────────┐
       │                 │                  │
   Computer            Browser            Phone
       │                 │                  │
       ├── Files         ├── Search        ├── ADB
       ├── Desktop       ├── Navigation    └── Wireless
       ├── Settings      └── Interaction
       └── Apps
🗂️ Project Structure
CHIDVI-556/
│
├── .env
├── .gitignore
├── main.py
├── ui.py
├── setup.py
├── readme.md
├── requirements.txt
│
├── actions/
│   ├── background_monitor.py
│   ├── browser_control.py
│   ├── code_helper.py
│   ├── computer_control.py
│   ├── computer_settings.py
│   ├── desktop.py
│   ├── dev_agent.py
│   ├── file_controller.py
│   ├── file_processor.py
│   ├── flight_finder.py
│   ├── game_updater.py
│   ├── open_app.py
│   ├── proactive.py
│   ├── reminder.py
│   ├── screen_processor.py
│   ├── send_message.py
│   ├── system_monitor.py
│   ├── weather_report.py
│   ├── web_search.py
│   └── youtube_video.py
│
├── barehands/
│   ├── server.py
│   ├── stage.html
│   ├── barehands.json.example
│   ├── README.md
│   ├── TROUBLESHOOTING.md
│   ├── media/
│   ├── sample-notes/
│   ├── bin/
│   └── state/
│
├── config/
│   ├── __init__.py
│   └── api_keys.json
│
├── core/
│   ├── __init__.py
│   ├── action_loader.py
│   ├── audio_devices.py
│   ├── confirm.py
│   ├── installer.py
│   ├── llm_client.py
│   ├── plugin_loader.py
│   ├── prompt.txt
│   ├── stt.py
│   ├── tts.py
│   ├── undo.py
│   └── wake_word.py
│
├── dashboard/
│   ├── __init__.py
│   ├── server.py
│   └── static/
│       ├── app.html
│       ├── crypto-js.min.js
│       └── login.html
│
├── memory/
│   ├── __init__.py
│   ├── config_manager.py
│   ├── long_term.json
│   └── memory_manager.py
│
└── plugins/
    ├── __init__.py
    ├── _template.py
    ├── adb_control.py
    ├── barehands_control.py
    ├── form_agent.py
    ├── game_control.py
    └── wireless_control.py
🧩 Actions vs Plugins

CHIDVI-556 separates built-in capabilities from experimental extensions.

Actions

Located in:

actions/

Actions are bundled capabilities provided by the core project.

Examples:

web_search
file_processor
computer_control
browser_control
system_monitor
code_helper
Plugins

Located in:

plugins/

Plugins provide an extension mechanism for additional functionality.

Examples:

ADB Control
Wireless Phone Control
BareHands
Game Control
Form Agent

This separation allows experimental features to evolve without unnecessarily changing the core architecture.

🔐 Security Philosophy

CHIDVI-556 is designed around controlled autonomy.

An increasingly capable agent must not have unrestricted access by default.

Important principles include:

Least Privilege

Only provide the permissions required for a task.

Human Approval

Irreversible operations require explicit user confirmation.

Local Processing

Sensitive functionality should remain local whenever possible.

Transparent Actions

The UI should show what the agent is doing.

Undoability

Reversible operations should provide an undo path.

Plugin Isolation

Plugins should not be allowed to silently compromise the core system.

🧠 AGI Research Direction

CHIDVI-556 is not intended to solve AGI through a single model.

The research direction is to combine multiple capabilities:

Reasoning
   +
Memory
   +
Planning
   +
Tool Use
   +
Vision
   +
Computer Interaction
   +
Learning
   +
Self-Evaluation
   +
Multi-Agent Coordination
   +
Safety

The long-term architecture is intended to investigate how these components can work together.

🧪 Future Research Modules

Potential future modules include:

Advanced Planner
Task Decomposer
Self-Evaluator
Reflection Engine
Knowledge Graph
Semantic Memory
Episodic Memory
Skill Learning
Multi-Agent System
Experiment Manager
Hypothesis Generator
Long-Term Goal Manager
Autonomous Coding Agent
Environment Model
Personal Knowledge Engine

These are research directions and do not imply that CHIDVI-556 currently possesses human-level general intelligence.

🔄 Target Agent Loop

The long-term CHIDVI-556 agent should operate approximately like this:

                    USER GOAL
                       │
                       ▼
                 UNDERSTAND GOAL
                       │
                       ▼
                  CHECK MEMORY
                       │
                       ▼
                  CREATE PLAN
                       │
                       ▼
                SELECT TOOLS
                       │
                       ▼
                 EXECUTE STEP
                       │
                       ▼
                   OBSERVE
                       │
                       ▼
                  EVALUATE
                  /        \
             SUCCESS       FAILURE
                │             │
                ▼             ▼
              FINISH       CORRECT
                              │
                              ▼
                            RETRY
                              │
                              ▼
                           LEARN
                              │
                              └──────► NEXT STEP
🛠️ Technology Stack

CHIDVI-556 is built around a Python desktop architecture.

Core
Python
PyQt6
Gemini
AI
LLM
Real-Time Voice
Vision
Tool Calling
Agent Workflows
Computer Interaction
Keyboard
Mouse
Desktop APIs
Browser Automation
File System
OS APIs
Memory
Local JSON Storage
Memory Manager
Context Retrieval
Web
Web Search
Research
News
Price Search
Comparison
Extensions
Plugin System
ADB
Wireless Control
BareHands
Game Control
Form Agent
🚀 Installation

Clone the repository:

git clone https://github.com/chidvielasparepalli/CHIDVI-556.git
cd CHIDVI-556

Create a virtual environment:

python -m venv .venv

Activate it on Windows:

.venv\Scripts\Activate.ps1

Install dependencies:

pip install -r requirements.txt

Run:

python main.py
⚙️ Configuration

Configuration is stored under:

config/

Environment secrets should be kept outside the source code.

Never commit private API keys, authentication tokens, or device credentials.

📱 Phone Integration

Phone-control plugins are experimental.

The general architecture is:

CHIDVI-556
      │
      ▼
wireless_control.py
      │
      ▼
Android Companion
      │
      ▼
Android Device

The companion application must authenticate requests before allowing device-control operations.

🧪 Development Philosophy

CHIDVI-556 is intentionally being developed incrementally.

Each major capability should follow:

Design
  ↓
Implement
  ↓
Test
  ↓
Observe
  ↓
Evaluate
  ↓
Fix
  ↓
Document

New features should not simply be added because they sound impressive.

Every capability should contribute toward one or more of:

Generalization
Autonomy
Reasoning
Memory
Learning
Tool Use
Multimodal Understanding
Reliability
Safety
🗺️ Development Roadmap
Phase 1 — Stable Agent Foundation
 Core voice interaction
 Memory
 Computer control
 Plugin architecture
 Web research
 File intelligence
 Vision
 Developer tools
 Safety confirmation
 Undo system
Phase 2 — Autonomous Agent
 Multi-step task execution
 Advanced task planner
 Goal decomposition
 Task state tracking
 Automatic retry
 Self-evaluation
 Error recovery
Phase 3 — Cognitive Architecture
 Episodic memory
 Semantic memory
 Knowledge graph
 Long-term goals
 Reflection system
 Skill learning
 Context modeling
Phase 4 — Multi-Agent Intelligence
 Research agent
 Coding agent
 Planning agent
 Vision agent
 Verification agent
 Agent coordinator

Example:

                 CHIDVI
                    │
             Agent Coordinator
                    │
       ┌────────────┼────────────┐
       │            │            │
   Research       Coding       Vision
     Agent         Agent        Agent
       │            │            │
       └────────────┼────────────┘
                    │
                Evaluator
Phase 5 — Learning & Adaptation
 Experience tracking
 Failure analysis
 Skill improvement
 Preference adaptation
 Task optimization
 Knowledge consolidation
Phase 6 — Advanced AGI Research
 General task benchmark
 Cross-domain reasoning
 Long-horizon planning
 Autonomous experimentation
 Hypothesis generation
 Environment modeling
 Advanced self-evaluation
 Multi-agent research
 Stronger safety architecture
🎯 First Major Objective

The first major objective of CHIDVI-556 is not:

"Create AGI."

The first objective is:

Create a reliable autonomous agent that can take a complex goal, decompose it into steps, use tools, observe results, recover from failures, and complete the task with appropriate human approval.

Example:

User:

"Research the best laptop for my requirements,
compare the options, create a report,
and save it to my project folder."

CHIDVI-556:

Understand
    ↓
Recall user requirements
    ↓
Plan
    ↓
Research
    ↓
Compare
    ↓
Evaluate
    ↓
Generate report
    ↓
Save file
    ↓
Verify
    ↓
Report completion

This is the foundation for increasingly general autonomous systems.

⚠️ Reality Check

CHIDVI-556 is an AGI research prototype.

It is not a claim of:

Human-level intelligence
True AGI
Artificial superintelligence
Consciousness
Autonomous self-improvement beyond its programmed architecture

Current AI systems have significant limitations.

CHIDVI-556 therefore focuses on engineering increasingly capable agent systems rather than claiming to have solved AGI.

🌌 Long-Term Vision
              CHIDVI-555
                   │
          Personal Assistant
                   │
                   ▼
              CHIDVI-556
                   │
          AGI Research Platform
                   │
                   ▼
        Advanced Autonomous Agent
                   │
                   ▼
          Advanced AGI Research
                   │
                   ▼
            Future ASI Research

The objective is to build the system one reliable capability at a time.

👤 Creator

Built by Chidvi as an ongoing AI research and engineering project.

CHIDVI-556 represents the transition from a traditional personal assistant toward a more autonomous, multimodal, tool-using AI architecture.

⭐ Project Philosophy

Don't build a chatbot that only answers questions.

Build an agent that understands goals, takes actions, learns from outcomes, and knows when humans should remain in control.

CHIDVI-556

Road to AGI.

Research. Build. Test. Learn. Repeat. 🚀
