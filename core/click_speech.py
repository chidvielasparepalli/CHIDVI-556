"""Exact local click speech, serialized with the existing audio output."""
import threading
import time


class ClickSpeech:
    def __init__(self, voice, output_device, playback_lock, speaking, log):
        self.voice = voice
        self.output_device = output_device
        self.playback_lock = playback_lock
        self.speaking = speaking
        self.log = log
        self.stopped = threading.Event()
        self._fallback_reported = False

    def speak(self, text):
        if text != "I am clicking here.":
            raise ValueError("Unexpected click confirmation text")
        if self.stopped.is_set():
            raise RuntimeError("Click cancelled")
        with self.playback_lock:
            self.speaking(True)
            try:
                voice = self.voice()
                if voice is not None:
                    pcm = voice.synthesize(text)
                    if not pcm:
                        raise RuntimeError("Click speech synthesis produced no audio")
                    import sounddevice as sd
                    with sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16",
                                            device=self.output_device()) as stream:
                        for start in range(0, len(pcm), 4800):
                            if self.stopped.is_set():
                                raise RuntimeError("Click cancelled")
                            stream.write(pcm[start:start+4800])
                else:
                    self._sapi(text)
            finally:
                self.speaking(False)
        return not self.stopped.is_set()

    def _sapi(self, text):
        import pythoncom
        import win32com.client
        if not self._fallback_reported:
            self.log("SYS: Click confirmation uses the local Windows voice when no reference voice is configured.")
            self._fallback_reported = True
        pythoncom.CoInitialize()
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            speaker.Speak(text, 1 | 16)  # Async, plain text; cancellation stays responsive.
            deadline = time.monotonic() + 30
            while not speaker.WaitUntilDone(50):
                if self.stopped.is_set() or time.monotonic() > deadline:
                    speaker.Speak("", 2)
                    raise RuntimeError("Click speech cancelled or timed out")
        finally:
            pythoncom.CoUninitialize()
