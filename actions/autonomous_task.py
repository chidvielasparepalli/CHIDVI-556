
"""Expose long-horizon autonomous execution as a CHIDVI action."""
from pathlib import Path
from core.long_horizon_agent import LongHorizonAgent
BASE_DIR = Path(__file__).resolve().parents[1]

def _log(message, player=None, speak=None):
    if speak:
        try: speak(message)
        except Exception: pass
    if player and hasattr(player, "write_log"):
        try: player.write_log(message)
        except Exception: pass
    print(message)

def run(parameters: dict, player=None, speak=None, **kwargs) -> str:
    objective = str(parameters.get("objective") or "").strip()
    max_steps = min(int(parameters.get("max_steps") or 32), 50)
    if not objective:
        return "I need an objective to work on."
    logger = lambda msg: _log(msg, player, speak)
    agent = LongHorizonAgent(BASE_DIR, context={"player": player, "speak": speak}, max_steps=max_steps, logger=logger)
    return agent.run(objective)

TOOL = {"name":"autonomous_task","description":"Give CHIDVI a goal and let it plan, execute, observe results, and continue until the goal is verified or the bounded step limit is reached.","parameters":{"type":"OBJECT","properties":{"objective":{"type":"STRING"},"max_steps":{"type":"INTEGER","description":"Maximum execution steps, capped at 50."}},"required":["objective"]},"handler":run}
=======
"""High-level long-horizon autonomous task entry point for CHIDVI-556."""
from __future__ import annotations

from core.long_horizon_agent import LongHorizonAgent

PLUGIN = None

TOOL = {
    "name": "autonomous_task",
    "description": (
        "Execute a long-horizon objective autonomously across CHIDVI's real tools. "
        "Use for multi-step browser, portal, research, computer, coding, verification, "
        "and other workflows. Observe each result and continue until the objective is verified."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "objective": {"type": "STRING", "description": "The complete outcome the user wants, including constraints."},
            "max_steps": {"type": "INTEGER", "description": "Maximum autonomous tool iterations. Default 32; hard cap 50."}
        },
        "required": ["objective"]
    },
    "handler": None,
}


def run(parameters: dict, **ctx) -> str:
    objective = str(parameters.get("objective", "")).strip()
    if not objective:
        return "No autonomous objective was supplied."
    try:
        max_steps = int(parameters.get("max_steps", 32))
    except (TypeError, ValueError):
        max_steps = 32

    try:
        from main import BASE_DIR
    except Exception:
        from pathlib import Path
        BASE_DIR = Path(__file__).resolve().parents[1]

    def log(message: str) -> None:
        text = str(message)
        speaker = ctx.get("speak")
        if callable(speaker):
            try:
                speaker(text)
            except Exception:
                pass
        player = ctx.get("player")
        writer = getattr(player, "write_log", None)
        if callable(writer):
            try:
                writer(text)
            except Exception:
                pass
        print(text)

    agent = LongHorizonAgent(base_dir=BASE_DIR, context=ctx, max_steps=max_steps, logger=log)
    return str(agent.run(objective))


TOOL["handler"] = run
