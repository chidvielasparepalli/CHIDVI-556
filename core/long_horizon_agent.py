
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
=======
"""Long-horizon autonomous agent for CHIDVI-556.

Turns one user objective into a bounded observe -> decide -> act -> evaluate
loop over the actions already exposed by CHIDVI. The planner is model-backed,
while execution is delegated to the real action registry; nothing is simulated.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from core.action_loader import ActionRegistry, discover_actions


class LongHorizonAgent:
    def __init__(self, *, base_dir: Path, context: dict[str, Any] | None = None,
                 max_steps: int = 32, logger=print):
        self.base_dir = base_dir
        self.context = context or {}
        self.max_steps = max(1, min(int(max_steps), 50))
        self.logger = logger
        self.history: list[dict[str, Any]] = []
        self.registry: ActionRegistry = discover_actions(base_dir / "actions", logger=logger)
        # The orchestrator must never recursively select itself as a tool.
        self.registry._actions.pop("autonomous_task", None)
        self.client = genai.Client(api_key=self._api_key())

    def _api_key(self) -> str:
        path = self.base_dir / "config" / "api_keys.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        key = str(data.get("gemini_api_key", "")).strip()
        if not key:
            raise RuntimeError("Gemini API key is missing from config/api_keys.json")
        return key

    def _planner_prompt(self, objective: str) -> str:
        tools = self.registry.get_tool_declarations()
        history = json.dumps(self.history[-12:], ensure_ascii=False, default=str)
        return f"""You are CHIDVI-556's long-horizon task planner.

USER OBJECTIVE:
{objective}

AVAILABLE REAL TOOLS:
{json.dumps(tools, ensure_ascii=False)}

EXECUTION HISTORY:
{history}

Plan ONE next action only. Observe its result before choosing the next action.
Continue until the objective is actually verified complete, not merely attempted.
Never claim an action happened unless its tool result says it happened.
Never invent personal information, credentials, answers, dates, or portal data.
Use browser_control for websites and real browser interaction. Use memory/recall
when personal context is needed. Narrate meaningful progress through the tool
context when possible. If a required personal fact is missing, ask the user
rather than guessing. Do not bypass CAPTCHA, OTP, MFA, passwords, or access
controls. Do not answer or submit graded exams/assessments on the user's behalf;
for those, navigate, explain, organize, or assist only as permitted by the
portal/course rules.

Return JSON only with exactly this shape:
{{
  "done": false,
  "summary": "short status",
  "tool": "tool_name",
  "arguments": {{}},
  "reason": "why this is the next action"
}}
When verified complete, return:
{{"done": true, "summary": "verified outcome"}}
"""

    def _plan(self, objective: str) -> dict[str, Any]:
        response = self.client.models.generate_content(
            model="gemini-2.5-flash",
            contents=self._planner_prompt(objective),
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        raw = getattr(response, "text", "") or ""
        decision = json.loads(raw)
        if not isinstance(decision, dict):
            raise ValueError("Planner returned a non-object JSON response")
        return decision

    def run(self, objective: str) -> dict[str, Any]:
        objective = str(objective).strip()
        if not objective:
            return {"ok": False, "summary": "No objective supplied.", "steps": []}

        self.history = []
        for step in range(1, self.max_steps + 1):
            try:
                decision = self._plan(objective)
            except Exception as exc:
                self.history.append({"step": step, "tool": "planner", "ok": False,
                                     "output": f"{type(exc).__name__}: {exc}"})
                return {"ok": False, "summary": "Planner failed; no further action was taken.",
                        "steps": self.history}

            if decision.get("done") is True:
                return {"ok": True, "summary": decision.get("summary", "Task completed."),
                        "steps": self.history}

            name = str(decision.get("tool", "")).strip()
            args = decision.get("arguments") or {}
            if not name or not isinstance(args, dict):
                self.history.append({"step": step, "tool": name, "ok": False,
                                     "output": "Planner produced an invalid action."})
                continue

            if not self.registry.has(name):
                self.history.append({"step": step, "tool": name, "ok": False,
                                     "output": "Unknown tool."})
                continue

            self.logger(f"[Autonomy {step}/{self.max_steps}] {decision.get('reason', 'Executing next action.')}" )
            output = self.registry.run(name, args, self.context)
            text = str(output)
            ok = not text.startswith("Tool '") and not text.endswith("is not available.")
            self.history.append({"step": step, "tool": name, "ok": ok, "output": output})

        return {"ok": False,
                "summary": f"Stopped after {self.max_steps} steps before the objective was verified complete.",
                "steps": self.history}
