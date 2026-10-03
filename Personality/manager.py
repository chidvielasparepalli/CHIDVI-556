"""Persona selection with dynamic discovery and configuration access."""
from copy import deepcopy
from .loader import discover_personalities

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
        if key not in discover_personalities():
            raise KeyError(f"Unknown personality: {personality_id}")
        self._active_id = key
        return self.current()

    def current(self):
        return deepcopy(discover_personalities()[self._active_id])

    def available(self):
        return [{"id": k, "name": v["name"], "description": v["description"]}
                for k, v in discover_personalities().items()]

    def voice_config(self):
        return deepcopy(self.current()["voice"])

    def theme_config(self):
        return deepcopy(self.current().get("theme", {}))

    def greeting(self):
        return self.current().get("greeting", "")

    def system_prompt(self, base_prompt=""):
        p = self.current()
        b = p.get("behavior", p.get("interaction", {}))
        rules = "\n".join("- " + rule for rule in b.get("rules", []))
        prefix = base_prompt.strip() + "\n\n" if base_prompt.strip() else ""
        return prefix + (
            "PERSONALITY STYLE (never overrides safety, permissions, honesty, or task requirements):\n"
            f"Persona: {p['name']}\nDescription: {p['description']}\n"
            f"Tone: {b.get('tone', b.get('style', 'natural'))}\n"
            f"Humor: {b.get('humor', 'as appropriate')}\n"
            f"Warmth: {b.get('warmth', 'helpful')}\n"
            f"Verbosity: {b.get('verbosity', 'as needed')}\n"
            f"Intensity: {self._intensity:.2f}/1.00. Scale style only, not helpfulness or safety.\n"
            "Rules:\n" + rules
        )
