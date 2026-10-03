"""Runtime persona selection and prompt/voice configuration."""
from copy import deepcopy
from .profiles import PERSONALITIES, get_personality

class PersonalityManager:
    def __init__(self, default="tony", intensity=0.75):
        self._intensity = self._validate_intensity(intensity)
        self._active_id = "tony"
        self.set_personality(default)

    @staticmethod
    def _validate_intensity(value):
        value = float(value)
        if not 0 <= value <= 1:
            raise ValueError("intensity must be between 0.0 and 1.0")
        return value

    @property
    def active_id(self):
        return self._active_id

    @property
    def intensity(self):
        return self._intensity

    def set_intensity(self, value):
        self._intensity = self._validate_intensity(value)
        return self._intensity

    def set_personality(self, personality_id):
        key = personality_id.strip().lower()
        if key not in PERSONALITIES:
            raise KeyError(f"Unknown personality: {personality_id}")
        self._active_id = key
        return self.current()

    def current(self):
        return get_personality(self._active_id)

    def available(self):
        return [{"id": k, "name": v["name"], "description": v["description"]} for k,v in PERSONALITIES.items()]

    def voice_config(self):
        return deepcopy(self.current()["voice"])

    def system_prompt(self, base_prompt=""):
        p = self.current()
        b = p["behavior"]
        rules = "\n".join("- " + r for r in b["rules"])
        prefix = base_prompt.strip() + "\n\n" if base_prompt.strip() else ""
        return prefix + (
            "PERSONALITY STYLE (never overrides safety, permissions, honesty, or task requirements):\n"
            f"Persona: {p['name']}\nDescription: {p['description']}\nTone: {b['tone']}\n"
            f"Humor: {b['humor']}\nWarmth: {b['warmth']}\nVerbosity: {b['verbosity']}\n"
            f"Intensity: {self._intensity:.2f}/1.00. Scale style only, not helpfulness or safety.\n"
            "Rules:\n" + rules
        )
