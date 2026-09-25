# ⚡ CHIDVI-556

<p align="center"><img src="assets/chidvi-556-readme-hq.jpg" alt="CHIDVI-556 interface"></p>

> **AI Companion • Autonomous AI Command Center • AGI Research Prototype**

CHIDVI-556 is the next evolution of CHIDVI-555 — a persistent AI system designed to move beyond simple question-and-answer interaction toward **goal-driven execution, memory, tool use, observation, verification, and recovery**.

> **Research note:** CHIDVI-556 is an evolving prototype exploring autonomous-agent and AGI-oriented architecture. It does not claim to be AGI or ASI.

---

## 🧠 Vision

The long-term direction is to build an AI operating layer that can understand a high-level objective, plan the work, use available tools, observe what happened, correct failures, learn useful context, and continue until the task is complete or requires human input.

```text
Goal
  ↓
Understand
  ↓
Plan
  ↓
Execute
  ↓
Evaluate
  ↓
Correct
  ↓
Learn
  ↓
Repeat
```

The core idea is simple: **don't just answer the user — help accomplish the objective.**

---

## 🚀 Current Capabilities

| System | Capability |
|---|---|
| 🎙️ Voice | Conversational voice interaction and narrated actions |
| 🧠 Memory | Persistent long-term memory across sessions |
| 🤖 Autonomy | Bounded goal-driven task execution |
| 🌐 Browser | Browser and computer interaction |
| 🔌 Plugins | Auto-discovered plugin architecture |
| 🛠️ Actions | Auto-discovered executable tools |
| 📝 Forms | Memory-aware form assistance |
| 🖼️ Images | Gemini-powered image generation |
| 📱 Android | Wireless phone control through companion HTTP service |
| 🎮 Desktop | Application and desktop control |
| 🧊 Blender | Blender / 3D scene automation integration |

---

## 🤖 Autonomous Agent

CHIDVI-556 can accept an **objective** instead of only a single command.

For example:

> "Open this website, find the registration form, fill the fields you already know, and tell me what information is missing."

The autonomous runner follows a bounded execution loop:

```text
Objective
   ↓
Plan next action
   ↓
Execute tool
   ↓
Observe result
   ↓
Evaluate progress
   ↓
Re-plan
   ↓
Verify completion
```

Execution is intentionally bounded to prevent uncontrolled loops and recursive self-invocation.

---

## 🧠 Persistent Memory

CHIDVI-556 includes a long-term memory layer for reusable context such as:

- Identity information
- User preferences
- Projects
- Relationships
- Wishes and goals
- Useful notes
- Session summaries

Memory is intended to make the agent more consistent across conversations and autonomous tasks.

The system should **never invent personal information** when memory does not contain a reliable answer.

---

## 📝 Memory-Aware Form Agent

The form-agent prototype combines browser interaction with long-term memory.

Workflow:

```text
Open URL
   ↓
Inspect form
   ↓
Understand fields
   ↓
Search memory
   ↓
Fill confidently known fields
   ↓
Ask user for missing / ambiguous information
   ↓
Remember reusable answers
   ↓
Verify form state
   ↓
Request confirmation for sensitive submission
```

The goal is to make repetitive web forms feel more like a conversation with an assistant than manual data entry.

---

## 🌐 Browser & Computer Control

CHIDVI-556 can use browser/computer actions as part of larger tasks rather than treating each action as an isolated command.

The architecture is designed around discoverable tools so new capabilities can be added without rewriting the entire agent.

---

## 🔌 Plugin & Action Architecture

CHIDVI-556 automatically discovers capabilities from the project structure.

```text
plugins/
   ├── plugin_a.py
   ├── plugin_b.py
   └── ...

actions/
   ├── action_a.py
   ├── action_b.py
   └── ...
```

Each capability exposes structured metadata and a handler that can be selected by the agent.

This makes the system modular and allows new tools to become available to the autonomous layer without hard-coding every capability into the core loop.

---

## 🖼️ Gemini Image Generation

The `create_image` action provides Gemini-powered image generation with:

- Multiple aspect ratios
- Multiple output sizes
- Local image saving
- Progress narration
- Optional opening of generated images

Example:

> "Create a cinematic 16:9 futuristic AI command center."

Generated assets are stored under `generated_images/`.

---

## 📱 Wireless Android Control

CHIDVI-556 includes a wireless-control plugin designed to communicate with an Android companion service over a local network or Tailscale connection.

Supported operations include:

- Connection / device checks
- App launching
- Tap
- Swipe
- Text input
- Key events
- Screenshots
- Home / Back
- Notifications

```text
CHIDVI Agent
     ↓
wireless_control.py
     ↓
HTTP / authenticated bridge
     ↓
Android Companion
     ↓
Phone
```

---

## 🧊 Blender & 3D Control

CHIDVI-556 also includes an integration path for controlling Blender programmatically.

The intended architecture is:

```text
CHIDVI Agent
     ↓
Blender Control Plugin
     ↓
Local Authenticated Bridge
     ↓
Blender Add-on
     ↓
bpy / Blender Scene
```

The integration is designed for operations such as scene inspection, primitive creation, transforms, materials, lights, cameras, animation, modifiers, rendering, and saving `.blend` files.

Destructive operations and arbitrary Python execution are intended to remain behind explicit safety controls.

---

## 🎙️ Voice & Interaction

Voice is a first-class interaction layer for CHIDVI-556.

The system can combine conversational responses with meaningful action narration so the user can understand what the agent is doing without watching every internal step.

The goal is not constant narration — only useful progress updates when an autonomous task is taking multiple steps.

---

## 🛡️ Safety Principles

Autonomy should remain useful **without becoming uncontrolled**.

CHIDVI-556 follows boundaries such as:

- Do not invent personal data.
- Do not bypass CAPTCHA, OTP, MFA, passwords, or access controls.
- Do not silently perform high-consequence actions.
- Require confirmation before sensitive or irreversible submissions where appropriate.
- Do not submit graded assessments on behalf of the user.
- Keep autonomous execution bounded.
- Avoid recursive autonomous-tool invocation.

Human confirmation remains part of the system whenever the consequences of an action warrant it.

---

## 🏗️ System Architecture

```text
                         ┌──────────────────────┐
                         │       CHIDVI UI      │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │ Conversation / Voice │
                         └──────────┬───────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │       Agent / Orchestrator     │
                    └───────────────┬───────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
        ┌─────▼─────┐        ┌──────▼──────┐       ┌─────▼─────┐
        │   Memory  │        │    Tools    │       │  Planner  │
        └───────────┘        └──────┬──────┘       └───────────┘
                                    │
                ┌───────────────────┼───────────────────┐
                │                   │                   │
          ┌─────▼─────┐       ┌─────▼─────┐       ┌────▼─────┐
          │   Browser │       │   Android │       │ Blender  │
          └───────────┘       └───────────┘       └──────────┘
                                    │
                              ┌─────▼─────┐
                              │   Image   │
                              │ Generation│
                              └───────────┘
```

---

## 📁 Project Structure

```text
CHIDVI-556/
├── actions/              # Executable agent actions
├── core/                 # Agent, loaders and orchestration logic
├── config/               # Runtime configuration
├── memory/               # Persistent memory storage and manager
├── plugins/              # Modular integrations and capabilities
├── blender_addon/        # Blender-side integration
├── generated_images/     # Generated image assets
├── main.py               # Application entry point
├── ui.py                 # User interface
├── requirements.txt      # Python dependencies
└── README.md             # Project documentation
```

---

## 🧩 Development Philosophy

CHIDVI-556 is being built around a few core principles:

**Modular** — capabilities should be replaceable and independently extensible.

**Tool-driven** — the agent should interact with the real world through explicit tools.

**Observable** — meaningful actions and outcomes should be inspectable.

**Bounded** — autonomous execution must have limits and escape conditions.

**Memory-aware** — useful context should persist instead of being rediscovered repeatedly.

**Human-controlled** — the user should remain in control of sensitive decisions and irreversible actions.

**Incremental** — each version should improve the architecture without pretending the research problem is already solved.

---

## 🔭 Roadmap

The broader direction includes:

- More reliable long-horizon planning
- Better observation and state tracking
- Automatic failure recovery
- Stronger task verification
- Improved memory retrieval and consolidation
- More capable browser/computer agents
- Deeper Android and desktop integration
- Richer Blender / 3D automation
- Better cross-session task continuity
- More robust agent evaluation

The goal is to gradually move from **command execution → task execution → persistent autonomous workflows**.

---

## 🧬 CHIDVI-555 → CHIDVI-556

CHIDVI-555 established the foundation for the assistant.

CHIDVI-556 focuses on extending that foundation toward a more autonomous architecture:

```text
CHIDVI-555
Assistant + Tools + Personality
            ↓
CHIDVI-556
Assistant + Memory + Tools + Planning + Observation
            ↓
Long-term direction
Persistent Goal-Driven AI Operating Layer
```

---

## 📌 Current Status

CHIDVI-556 is an **active, evolving research/development project**.

Some components are production-like utilities, while others are experimental prototypes. The architecture is intentionally being developed in stages so individual capabilities can be tested, verified, and improved before increasing the level of autonomy.

> **Build the capability. Verify it. Then make the agent more autonomous.**

---

## ⚡ CHIDVI-556

**A persistent AI command center evolving toward autonomous, tool-using, memory-aware intelligence.**


---

## 🏗️ Autonomous App Builder

CHIDVI-556 now includes an **App Builder** action that turns a natural-language product idea into a real full-stack web project.

Example:

> "Build a SaaS dashboard for AI resume analysis with authentication, uploads, analytics, and a responsive premium UI."

The builder follows:

```text
Idea
 ↓
Understand requirements
 ↓
Ask blocking questions / credentials
 ↓
Plan architecture + design system
 ↓
Generate project files
 ↓
Install dependencies
 ↓
Build
 ↓
Open in browser
 ↓
Inspect UI + console errors
 ↓
Repair bounded failures
 ↓
Verify
 ↓
Deliver
```

### What it does

- Generates a dedicated project outside the CHIDVI repository.
- Starts with a structured architecture and design plan.
- Asks for missing information instead of guessing.
- Handles credentials through the generated project's local `.env.local`.
- Narrates meaningful progress through the existing CHIDVI voice/log pipeline.
- Generates Next.js + TypeScript projects by default.
- Runs `npm install` and `npm run build`.
- Uses Playwright to open the real application.
- Captures a real browser screenshot and checks console/page errors.
- Uses a bounded repair loop instead of claiming success after a failed build.
- Persists builder state under the generated project's `.chidvi/` directory so interrupted work can be resumed.
- Intentionally pushes generated UI away from generic AI boilerplate and toward a product-specific design system.

### Tool

The auto-discovered action is:

`app_builder`

Supported actions:

```text
start   → create a new application
resume  → continue after a blocking user answer
status  → inspect an existing builder task
cancel  → stop a builder task
```

If Playwright's Chromium binary is missing, the builder attempts a one-time local `playwright install chromium` bootstrap before verification.

