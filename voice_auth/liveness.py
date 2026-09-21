from __future__ import annotations

import random
import re


class ChallengePhrase:
    PHRASES = (
        "CHIDVI open my private workspace",
        "CHIDVI verify my voice identity",
        "CHIDVI unlock my assistant",
        "CHIDVI this is my secure voice",
    )

    @classmethod
    def generate(cls) -> str:
        return random.choice(cls.PHRASES)

    @staticmethod
    def normalize(text: str) -> str:
        return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()

    @classmethod
    def matches(cls, expected: str, transcript: str) -> bool:
        return cls.normalize(expected) == cls.normalize(transcript)
