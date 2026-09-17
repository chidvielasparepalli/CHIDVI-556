"""Bounded long-horizon autonomous goal runner for CHIDVI-556."""
from __future__ import annotations
import json
from pathlib import Path
from google import genai
from google.genai import types
from core.action_loader import discover_actions

class LongHorizonAgent:
    def __init__(self, base_dir: Path, context=None, max_steps=32, logger=print):
        self.base_dir = Path(base_dir)
        self.context = context or {}
        self.max_steps = min(max(int(max_steps), 1), 50)
        self.logger = logger
        self.registry = discover_actions(self.base_dir / "actions", logger=logger)
        self.registry._actions.pop("autonomous_task", None)
        key = self._load_key()
        self.client = genai.Client(api_key=key)
        self.model = "gemini-2.5-flash"
        self.history = []

    def _load_key(self):
        p = self.base_dir / "config" / "api_keys.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        key = data.get("gemini_api_key") or data.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("Gemini API key is not configured.")
        return key

    def _plan(self, objective):
        tools = self.registry.get_tool_declarations()
        prompt = f'''You are CHIDVI-556's autonomous planner. Work toward this user objective: {objective}
Available tools: {json.dumps(tools)}
Recent execution history: {json.dumps(self.history[-12:])}
Return ONLY JSON with keys action, parameters, reason, done. Choose exactly one available action per step.
Observe tool results before planning the next step. Use memory for known personal context; never invent personal facts, credentials, answers, dates or portal data. Ask the user when required information is missing or ambiguous. Use browser automation for websites. Never bypass CAPTCHA, OTP, MFA, passwords or access controls. Do not submit graded exams/assessments for the user. Set done=true only when the objective is actually verified complete.'''
        response = self.client.models.generate_content(model=self.model, contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1))
        return json.loads(response.text)

    def run(self, objective):
        if not objective or not objective.strip():
            return "I need an objective to work on."
        self.logger(f"Autonomous task started: {objective}")
        for step in range(1, self.max_steps + 1):
            decision = self._plan(objective)
            if decision.get("done"):
                self.logger("Autonomous task verified complete.")
                return "Objective completed and verified."
            name = decision.get("action")
            params = decision.get("parameters") or {}
            if not name or not self.registry.has(name):
                self.logger(f"Planner selected unavailable action: {name}")
                return f"I stopped because the planner selected an unavailable action: {name}"
            self.logger(f"Autonomy step {step}: {name} — {decision.get('reason', '')}")
            result = self.registry.run(name, params, self.context)
            self.history.append({"step": step, "action": name, "parameters": params, "result": result})
            self.logger(f"Result: {result}")
        self.logger(f"Autonomous task reached the {self.max_steps}-step limit.")
        return f"I reached the {self.max_steps}-step safety limit before verifying completion."
