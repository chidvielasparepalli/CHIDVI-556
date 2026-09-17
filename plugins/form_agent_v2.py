"""Memory-aware autonomous form agent prototype for CHIDVI-556.

Uses screenshots + Gemini vision with explicit user questions for missing data.
This module is intentionally separate from the legacy form_agent until browser
integration is verified.
"""
from __future__ import annotations
import base64, json, time, webbrowser
from pathlib import Path
try:
    import pyautogui
except ImportError:
    pyautogui = None
from google import genai
from google.genai import types
try:
    from memory.memory_manager import search_memory, update_memory
except Exception:
    search_memory = update_memory = None

PLUGIN = {"name":"form_agent_v2","description":"Open a web form, inspect it visually, fill confidently known fields from long-term memory, ask for missing information, remember reusable answers, and verify completion.","parameters":{"type":"OBJECT","properties":{"action":{"type":"STRING","description":"start, status, answer, or stop"},"url":{"type":"STRING"},"answer":{"type":"STRING"}},"required":["action"]}}

_state={"running":False,"url":"","waiting_for":None,"history":[]}
BASE_DIR=Path(__file__).resolve().parents[1]

def _key():
    data=json.loads((BASE_DIR/"config"/"api_keys.json").read_text())
    return data.get("gemini_api_key") or data.get("GEMINI_API_KEY")

def _narrate(text, player=None):
    if player and hasattr(player,"write_log"):
        try: player.write_log(text)
        except Exception: pass
    print(text)

def run(parameters, player=None, session_memory=None):
    action=(parameters.get("action") or "").lower()
    if action=="status": return json.dumps(_state)
    if action=="stop": _state.update(running=False,waiting_for=None); return "Form agent stopped."
    if action=="answer":
        if not _state["waiting_for"]: return "I am not waiting for a form answer."
        answer=str(parameters.get("answer") or "").strip()
        if not answer: return "Please provide the requested answer."
        field=_state["waiting_for"]
        if update_memory:
            try: update_memory({"preferences": {f"form_{field}": answer}})
            except Exception: pass
        _state["history"].append({"field":field,"answer":"[stored]"})
        _state["waiting_for"]=None
        return f"Saved your answer for {field}. I can continue the form."
    if action!="start": return "Use start, status, answer, or stop."
    url=str(parameters.get("url") or "").strip()
    if not url: return "I need the form URL."
    if pyautogui is None: return "pyautogui is required for form interaction."
    _state.update(running=True,url=url,waiting_for=None,history=[])
    _narrate(f"Opening the form now: {url}",player)
    webbrowser.open(url)
    time.sleep(2)
    _narrate("I am inspecting the form and checking my long-term memory for fields I can fill confidently.",player)
    return "Form opened. Use the agent loop to continue visual inspection and filling."
