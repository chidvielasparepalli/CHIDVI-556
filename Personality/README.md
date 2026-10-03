# CHIDVI-556 Personality System

Modular, provider-independent persona profiles and runtime prompt/voice configuration.

Profiles include Tony-inspired, Hinata-inspired, Levi-inspired, Gojo-inspired, scientific analyst, calm sage, chaotic comedian, detective analyst, deadpan wit, and energetic optimist.

Voice IDs are deliberately unconfigured. Use only voice models or recordings you are authorized to use.

## Quick start

    from Personality import PersonalityManager
    manager = PersonalityManager(default="tony", intensity=0.8)
    prompt = manager.system_prompt(base_prompt="You are CHIDVI, a helpful AI assistant.")
    voice = manager.voice_config()
    manager.set_personality("hinata")

## Integration status

This package provides the profile registry and manager. The host application still needs to call system_prompt() for model requests, apply voice_config() to its TTS provider, and persist active_id/intensity using its settings layer. It is not yet wired into the UI, LLM request flow, persistence, or audio playback.
