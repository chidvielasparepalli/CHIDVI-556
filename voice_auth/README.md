# CHIDVI-556 Voice Authentication

Local speaker verification for the CHIDVI-556 desktop assistant.

## Stack

- SpeechBrain ECAPA-TDNN speaker encoder
- 16 kHz mono microphone audio
- Optional challenge-response phrase verification
- Local voiceprint storage under `config/voice_auth/`

## Install

```bash
pip install speechbrain torch torchaudio soundfile
```

The pretrained model is downloaded by SpeechBrain on first use and cached locally.

## Flow

1. Enroll five clean samples of the owner speaking naturally.
2. Create a centroid speaker embedding.
3. Embed a fresh sample and compare cosine similarity.
4. For sensitive operations, also require a random challenge phrase.

Voiceprints are intentionally ignored by Git. Never commit raw voice recordings or embeddings.
