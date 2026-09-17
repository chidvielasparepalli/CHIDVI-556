"""Gemini image generation action with narration while the request is running."""
from __future__ import annotations
import base64, json, threading, time
from pathlib import Path
from google import genai
from google.genai import types
BASE_DIR=Path(__file__).resolve().parents[1]
OUT_DIR=BASE_DIR/"generated_images"

def _key():
    data=json.loads((BASE_DIR/"config"/"api_keys.json").read_text(encoding="utf-8"))
    return data.get("gemini_api_key") or data.get("GEMINI_API_KEY")

def _say(text, player=None, speak=None):
    if speak:
        try: speak(text)
        except Exception: pass
    if player and hasattr(player,"write_log"):
        try: player.write_log(text)
        except Exception: pass
    print(text)

def run(parameters:dict, player=None, speak=None, **kwargs)->str:
    prompt=str(parameters.get("prompt") or "").strip()
    if not prompt: return "I need an image prompt."
    ratio=str(parameters.get("aspect_ratio") or "1:1")
    size=str(parameters.get("image_size") or "1K")
    OUT_DIR.mkdir(parents=True,exist_ok=True)
    stamp=time.strftime("%Y%m%d_%H%M%S")
    path=OUT_DIR/f"image_{stamp}.png"
    _say(f"Absolutely. I'm creating that image now in {ratio} format.",player,speak)
    _say("I'm sending the visual brief to the image generator. I'll keep you updated while it renders.",player,speak)
    stop=threading.Event()
    def progress():
        messages=["The image is rendering now.","I'm still working on the generation — keeping an eye on the result.","The render is still in progress; I'll let you know as soon as it's ready."]
        i=0
        while not stop.wait(7):
            _say(messages[i%len(messages)],player,speak); i+=1
    t=threading.Thread(target=progress,daemon=True); t.start()
    try:
        client=genai.Client(api_key=_key())
        response=client.models.generate_content(model="gemini-2.5-flash-image",contents=prompt,config=types.GenerateContentConfig(response_modalities=["TEXT","IMAGE"]))
        for part in response.candidates[0].content.parts:
            if getattr(part,"inline_data",None):
                data=part.inline_data.data
                if isinstance(data,str): data=base64.b64decode(data)
                path.write_bytes(data); break
        if not path.exists(): return "The image model returned no image data."
        _say(f"The image is finished. I'm saving it now as {path.name}.",player,speak)
        _say("Done. Your image is ready.",player,speak)
        return f"Image generated successfully: {path}"
    finally:
        stop.set()

TOOL={"name":"create_image","description":"Generate an image from a prompt with narrated progress and save it locally.","parameters":{"type":"OBJECT","properties":{"prompt":{"type":"STRING"},"aspect_ratio":{"type":"STRING"},"image_size":{"type":"STRING"}},"required":["prompt"]},"handler":run}
