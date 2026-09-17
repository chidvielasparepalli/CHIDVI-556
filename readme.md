# ⚡ CHIDVI-556

> **AI Companion • AGI Research Prototype**

CHIDVI-556 is the next-generation evolution of CHIDVI-555: an experimental general-purpose AI system focused on autonomous reasoning, planning, memory, learning, tool use, computer interaction, and multi-step task execution.

> ⚠️ **Research prototype:** CHIDVI-556 does not claim to be true AGI or ASI.

---

## 🧠 Vision

The long-term goal is to explore increasingly capable general-purpose AI systems.

```text
CHIDVI-555 → CHIDVI-556 → Advanced AGI Research → Future ASI Research

Learn → Build → Explore → Evolve
```

## 🚀 Core Intelligence Loop

```text
GOAL → UNDERSTAND → PLAN → EXECUTE → OBSERVE
                                      ↓
                         EVALUATE → CORRECT → LEARN
                                      ↓
                                    REPEAT
```

## ✨ Capabilities

| Capability | Status |
|---|---|
| 🤖 LLM Interaction | 🟢 Active |
| 🎙️ Voice Input | 🟢 Active |
| 🔊 Text-to-Speech | 🟢 Active |
| 🎭 Personality System | 🟢 Active |
| 🔌 Plugin System | 🟢 Active |
| 🧠 Short-Term Memory | 🟢 Active |
| 💾 Long-Term Memory | 🟡 Developing |
| 🖥️ Computer Control | 🟡 Developing |
| 📱 Phone Control | 🟡 Developing |
| 🧊 Blender / 3D Control | 🟡 Developing |
| 🖼️ Image Generation | 🟢 Active |
| 🌐 Web Research | 🟡 Developing |
| 💻 Coding & Debugging | 🟡 Developing |
| 📊 Data Analysis | 🟡 Developing |
| 🤖 Long-Horizon Autonomy | 🟢 Active |
| 🧩 Multi-Agent System | 🔵 Planned |
| 🔄 Self-Evaluation | 🟡 Developing |
| 🧠 Adaptive Learning | 🔵 Planned |

---

## 🤖 Long-Horizon Autonomous Agent

CHIDVI-556 now has a model-backed autonomous execution loop. A high-level goal can be broken into real tool actions and evaluated after each step.

```text
USER GOAL
   ↓
UNDERSTAND
   ↓
PLAN ONE ACTION
   ↓
EXECUTE REAL TOOL
   ↓
OBSERVE RESULT
   ↓
RE-PLAN
   ↓
VERIFY OBJECTIVE
   ↓
DONE / CORRECT / CONTINUE
```

The `autonomous_task` action supports up to 50 bounded iterations. It discovers the real actions in `actions/`, records execution history, and refuses to invent personal information or bypass CAPTCHA, OTP, MFA, passwords, or access controls.

### Example

> “Open the website, fill everything you already know, ask me for anything missing, and verify the result.”

The agent can chain browser, memory, computer, phone, image, coding, and other registered capabilities where available.

---

## 🧠 Memory-Aware Form Agent

The form-agent prototype connects form execution to CHIDVI's existing long-term memory rather than creating a second memory store.

```text
URL
 ↓
INSPECT FORM
 ↓
SEARCH MEMORY
 ↓
KNOWN → FILL
 ↓
MISSING / AMBIGUOUS → ASK USER
 ↓
REMEMBER ANSWER
 ↓
RESUME
 ↓
VERIFY
```

It narrates meaningful actions and pauses rather than guessing when required information is missing. High-consequence final submissions require user confirmation.

---

## 🖼️ Image Generation

CHIDVI-556 includes a native `create_image` action powered by Gemini image generation.

```text
USER PROMPT
    ↓
CHIDVI UNDERSTANDS REQUEST
    ↓
CREATE_IMAGE
    ↓
GENERATE / NARRATE PROGRESS
    ↓
SAVE TO generated_images/
    ↓
OPTIONALLY OPEN IMAGE
```

The image action supports common aspect ratios and resolution choices and narrates while the generation is in progress instead of remaining silent.

Example:

> “Create a futuristic AI command center in 16:9.”

---

## 🛠️ Tool & Plugin Architecture

Every `actions/*.py` module exposing a `TOOL` definition can be discovered automatically. Plugins under `plugins/` provide additional capabilities and configuration-backed integrations.

```text
                 CHIDVI CORE
                     │
             Action / Plugin Loader
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼
   Computer        Phone         Blender
       │             │             │
       ▼             ▼             ▼
   Browser        Android       3D Scene
```

## 📱 Phone Control

`wireless_control.py` provides the PC-side foundation for controlling an Android phone over Wi-Fi/Tailscale through the JARVIS Companion HTTP API.

Supported operations include:

- 📲 Launch applications
- 👆 Tap
- 🖐️ Swipe
- ⌨️ Type text
- 🔙 Back / 🏠 Home
- 🔊 Hardware key events
- 📸 Screenshots
- 🔔 Notifications
- 📡 Connection / device status

The phone companion must be running and configured with the phone address and authentication token. The controller does not bypass Android security mechanisms.

## 🧊 Blender / 3D Control

CHIDVI-556 is designed to control a local Blender instance through a dedicated local bridge rather than depending on Claude Code's private MCP protocol.

```text
CHIDVI Agent
    ↓
blender_control.py
    ↓
Local HTTP + HMAC Bridge
    ↓
Blender Add-on
    ↓
Blender Python API (bpy)
    ↓
3D Scene / Materials / Animation / Render
```

The intended bridge is local to `127.0.0.1`, authenticated, and designed around explicit Blender operations. Destructive or heavy actions should remain confirmation-gated.

---

## 🎙️ Voice System

```text
Microphone → Speech Input → LLM / Agent → Tool / Plugin → Result → TTS
```

Autonomous operations should narrate meaningful progress through the same voice/logging layer used by normal CHIDVI interactions.

## 🔄 Self-Evaluation

```text
EXECUTE → OBSERVE → EVALUATE
                    │
              ┌─────┴─────┐
              ▼           ▼
           CORRECT      COMPLETE
              │
              ▼
            RETRY
```

## 🛡️ Safety & Permissions

Autonomy is bounded and observable. The agent should never assume unlimited access. Sensitive operations should use explicit confirmation and permission boundaries. Authentication barriers, CAPTCHA, OTP, MFA, passwords, and access controls must not be bypassed.

## 📁 Project Structure

```text
CHIDVI-556/
├── actions/
│   ├── autonomous_task.py
│   └── image_generator.py
├── core/
│   ├── action_loader.py
│   ├── autonomous_orchestrator.py
│   └── long_horizon_agent.py
├── config/
│   └── mcp_servers.json
├── memory/
├── plugins/
│   ├── blender_control.py
│   ├── form_agent.py
│   ├── form_agent_v2.py
│   ├── form_agent_memory.md
│   └── wireless_control.py
├── generated_images/
├── main.py
├── ui.py
├── requirements.txt
└── readme.md
```

## 🧭 Development Roadmap

### Phase 1 — Foundation
LLM, voice, personality, memory, plugin architecture, and computer capabilities.

### Phase 2 — Agent Core
Goal management, long-horizon planning, multi-step execution, observation, verification, correction, and recovery.

### Phase 3 — Tool Intelligence
Computer control, phone control, Blender/3D control, web research, coding, debugging, image generation, and file intelligence.

### Phase 4 — Multi-Agent Intelligence
Specialized agents, coordination, delegation, communication, and parallel execution.

### Phase 5 — Learning & Knowledge
Experience memory, feedback, successful-solution retrieval, failure memory, knowledge management, and experiment tracking.

### Phase 6 — Advanced AGI Research
Persistent task state, longer-running goals, robust recovery, world models, improved reasoning, autonomous experimentation, and general-purpose evaluation.

---

## ⚙️ Development Philosophy

```text
Learn → Build → Explore → Evolve
```

CHIDVI-556 is an evolving research project. Architecture and capabilities may change as new experiments are completed.
