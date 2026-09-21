from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import sounddevice as sd


def record_owner_samples(
    app,
    *,
    sample_rate: int = 16000,
    seconds: float = 4.0,
    count: int = 5,
) -> list[np.ndarray]:
    """Show a one-time Qt enrollment dialog and record samples from the selected mic."""
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

    done = threading.Event()
    result: dict[str, object] = {"samples": None, "error": None}

    def launch() -> None:
        dialog = QDialog()
        dialog.setWindowTitle("CHIDVI • Voice Authorization")
        dialog.setModal(True)
        dialog.resize(520, 260)

        title = QLabel("VOICE AUTHORIZATION")
        title.setStyleSheet("font-size:22px;font-weight:700;")
        instruction = QLabel(
            "This is a one-time setup. Record five natural voice samples.\n"
            "Use your normal voice and stay close to the microphone."
        )
        instruction.setWordWrap(True)
        status = QLabel("Sample 1 of 5 — press RECORD and speak for 4 seconds.")
        record = QPushButton("RECORD")
        record.setMinimumHeight(48)
        cancel = QPushButton("CANCEL")

        layout = QVBoxLayout(dialog)
        layout.addWidget(title)
        layout.addWidget(instruction)
        layout.addWidget(status)
        layout.addWidget(record)
        layout.addWidget(cancel)

        samples: list[np.ndarray] = []
        busy = {"value": False}

        def finish(samples_value=None, error=None):
            result["samples"] = samples_value
            result["error"] = error
            done.set()
            dialog.close()

        def cancel_setup():
            finish(None, RuntimeError("Voice authorization setup was cancelled."))

        def capture():
            if busy["value"]:
                return
            busy["value"] = True
            record.setEnabled(False)
            cancel.setEnabled(False)
            status.setText(
                f"Recording sample {len(samples) + 1} of {count}…"
            )
            dialog.repaint()
            try:
                audio = sd.rec(
                    int(sample_rate * seconds),
                    samplerate=sample_rate,
                    channels=1,
                    dtype="float32",
                )
                sd.wait()
                audio = np.asarray(audio, dtype=np.float32).reshape(-1)
                if float(np.max(np.abs(audio))) < 0.01:
                    raise ValueError("The recording was too quiet. Please try again.")
                samples.append(audio)
                if len(samples) >= count:
                    status.setText("Voice profile captured. Saving…")
                    finish(samples.copy(), None)
                    return
                status.setText(
                    f"Sample {len(samples) + 1} of {count} — press RECORD."
                )
            except Exception as exc:
                status.setText(f"Recording failed: {exc}")
            finally:
                busy["value"] = False
                record.setEnabled(True)
                cancel.setEnabled(True)

        record.clicked.connect(capture)
        cancel.clicked.connect(cancel_setup)
        dialog.finished.connect(
            lambda _code: (
                None
                if done.is_set()
                else finish(None, RuntimeError("Voice authorization setup was cancelled."))
            )
        )
        dialog.show()

    QTimer.singleShot(0, launch)
    done.wait()

    if result["error"] is not None:
        raise result["error"]
    return result["samples"]  # type: ignore[return-value]
