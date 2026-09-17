# CHIDVI-556

> **CHIDVI-556 — an evolving autonomous AI command center.**

CHIDVI-556 combines conversational intelligence, voice interaction, persistent memory, computer/browser actions, plugins, phone control, image generation, and bounded long-horizon autonomy.

## Core capabilities

- 🎙️ Voice-first interaction and narrated actions
- 🧠 Persistent long-term memory
- 🌐 Browser and computer automation
- 🔌 Auto-discovered plugins and actions
- 📱 Wireless Android phone control through the companion HTTP service
- 🖼️ Gemini image generation with spoken progress narration
- 🤖 Goal-driven autonomous execution with observe → act → verify loops
- 🎮 Application and desktop control
- 📝 Memory-aware form assistance

## Autonomous execution

Give CHIDVI an objective rather than a single command. The autonomous runner selects one real action at a time, observes the result, keeps execution history, and continues until the objective is verified or a bounded step limit is reached.

```text
Goal → Plan → Execute → Observe → Re-plan → Verify
```

The runner is intentionally bounded (maximum 50 steps) and excludes itself from its own tool registry to prevent recursive execution.

## Image generation

The `create_image` action generates images with Gemini, saves them under `generated_images/`, and narrates progress while the generation request is running.

Example:

> "Create a cinematic 16:9 futuristic AI command center."

## Form agent

The memory-aware form-agent prototype can open a supplied URL, inspect a form, use confidently known memory, ask for missing information, remember reusable answers, and verify completion. Sensitive or irreversible submissions should require user confirmation.

## Wireless phone control

`plugins/wireless_control.py` communicates with a JARVIS Companion HTTP server on an Android phone over LAN/Tailscale. Supported operations include connection checks, app launching, taps, swipes, text entry, key events, screenshots, home/back, and notifications.

## Safety boundaries

CHIDVI should not invent personal data, bypass CAPTCHA/OTP/MFA/passwords or access controls, or submit graded assessments on the user's behalf. High-consequence actions should be confirmed before final submission.

## Project direction

The long-term goal is a persistent AI operating layer that can accept a high-level objective, use its available tools, recover from intermediate failures, maintain task state, and resume work across sessions.
