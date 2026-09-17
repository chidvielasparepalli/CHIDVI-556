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
