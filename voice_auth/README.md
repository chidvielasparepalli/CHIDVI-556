# CHIDVI-556 Voice Authentication

Local speaker verification for the CHIDVI-556 desktop assistant.

## Current design

- SpeechBrain ECAPA-TDNN speaker embeddings
- 16 kHz mono microphone audio
- Five-sample owner enrollment
- Cosine-similarity verification against a locally stored centroid
- Short pre-roll and bounded utterance capture
- Verification happens locally before microphone audio is sent to Gemini Live
- The optional "answer everyone" override is only accepted after an already-authenticated utterance
- Voiceprint storage is anchored to the application directory under `config/voice_auth/`
- Raw recordings and embeddings are ignored by Git

## Install

```bash
pip install speechbrain torch torchaudio soundfile
```

The pretrained ECAPA model is downloaded by SpeechBrain on first use and cached locally.

## Enrollment flow

1. CHIDVI opens the voice-authorization setup dialog on first launch.
2. Record five natural samples using the configured microphone.
3. CHIDVI builds a centroid speaker embedding and stores it locally.
4. Future utterances are verified locally before Gemini receives them.

## Important security limitation

Speaker verification alone is **not replay-attack resistant**. A recording or synthesized voice may still present the same speaker characteristics. Standards for biometric presentation-attack detection treat replay/spoof resistance as a separate security property, and challenge/nonces are a common way to provide freshness. citeturn613304search0turn613304search4

For high-consequence actions, CHIDVI should therefore use an additional confirmation or a future challenge-response/PAD layer rather than treating the voice score as a standalone high-assurance authenticator.

## Configuration

```text
CHIDVI_VOICE_THRESHOLD=0.62
CHIDVI_VOICE_MIN_SECONDS=1.5
CHIDVI_VOICE_MAX_SECONDS=12
CHIDVI_VOICE_ENROLL_SAMPLES=5
```

The threshold is a deployment parameter and should be calibrated with real owner/non-owner samples; it is not a universal security constant. SpeechBrain's current speaker-verification APIs likewise treat the threshold as a decision parameter rather than a universal fixed value. citeturn731319search0
