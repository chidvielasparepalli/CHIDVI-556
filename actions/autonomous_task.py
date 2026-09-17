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
