# Wednesday reference voice: inspection and integration

Status: the approved installation recovery on 2026-10-13 **succeeded and produced one validated reference-conditioned WAV**. The app's saved voice configuration remains unchanged and disabled; subjective voice similarity and live-app playback are not verified. The earlier failed downloads and logs remain preserved on D:. No paid call, reference upload, or training was performed. The original MP3 remains unchanged. The user confirmed permission to adapt this recording on 2026-10-13.

## Recording inspection

The original is `A:/New folder/wednesday-addams-2026-10-07-22-01-Depending-on-what-you-mean-by-a-Wednesday-voice.mp3`.

| Property | Measured value |
| --- | --- |
| File size | 186,895 bytes |
| SHA-256 | `5954ec4fa2d2ce92792f138a2f8033c0d73d2ea200cc685998c83cc282c94e4b` |
| Codec | MPEG-1 Layer III, 128 kbps constant bitrate |
| Decoded duration | 11.676734694 seconds |
| Sample rate / channels | 44,100 Hz / mono |
| Decoded samples | 514,944 |
| Peak / RMS | -8.30 dBFS / -27.91 dBFS |
| Full-scale clipping | None detected |
| Near-silence | 132 of 584 20-ms windows below -45 dBFS |
| Leading / trailing near-silence | Approximately 0.08 / 0.04 seconds |

Existing PyQt6 `QAudioDecoder` and its bundled FFmpeg decoded the entire file in memory without errors. Independent parsing found 447 complete MP3 frames and no truncated frames or trailing bytes. The recording was not modified; no processed copy was needed. The ID3 metadata contains a `fish.audio task` field, which suggests a generated source but does not identify or authenticate its speaker.

This is an intact lossy recording with ample peak headroom. Objective measurements do not establish perceived similarity, intelligibility, absence of background music/noise, or a single speaker. Subjective listening and a synthesis comparison remain necessary. No claim is made that this is an actor's authentic voice.

## Recommended model workflow

Use **Chatterbox Turbo**, an English reference-conditioned TTS model. The supplied clip exceeds the implementation's strict five-second reference minimum. It needs no reference transcript and produces 24 kHz audio. Its loader decodes and resamples the reference internally; denoising, normalization of the original, and a separate training dataset are unnecessary at this stage. Conditioning an existing model is inference, not training or fine-tuning. The model's audio watermark is retained.

The available hardware is an RTX 4060 Laptop GPU with approximately 8 GiB VRAM and 23.7 GiB system RAM. The local Windows/Python 3.12.14 trial passed installation, CUDA loading, conditioning, and synthesis; its measured latency and memory use are recorded below. Voice similarity remains unassessed. Upstream documents testing on Python 3.11/Debian 11. This adapter is English-only and does not implement profile pitch/rate/style controls.

Pinned upstream references:

- [Model API and reference processing](https://github.com/resemble-ai/chatterbox/blob/5de7a54aa4e5e2baadb0182dde554908b48b85c2/src/chatterbox/tts_turbo.py)
- [Package dependencies](https://github.com/resemble-ai/chatterbox/blob/5de7a54aa4e5e2baadb0182dde554908b48b85c2/pyproject.toml)
- [Official README and MIT licensing](https://github.com/resemble-ai/chatterbox/blob/5de7a54aa4e5e2baadb0182dde554908b48b85c2/README.md)
- [Model artifact revision](https://huggingface.co/ResembleAI/chatterbox-turbo/tree/749d1c1a46eb10492095d68fbcf55691ccf137cd)

The minimum local artifact set is approximately **2.99 GB**, excluding Python/PyTorch/CUDA dependencies:

| Required artifact | Approximate size |
| --- | --- |
| `ve.safetensors` | 5.7 MB |
| `t3_turbo_v1.safetensors` | 1.92 GB |
| `s3gen_meanflow.safetensors` | 1.06 GB |
| `tokenizer_config.json`, `vocab.json`, `merges.txt`, `added_tokens.json`, `special_tokens_map.json` | Small tokenizer files |

The upstream `from_pretrained()` path can download additional redundant weights; the adapter never calls it. PerTh's local watermark resources must also be present in the installed package. Install packages and obtain these artifacts only after explicit approval. Prefer an isolated Python environment so Chatterbox's pinned dependencies do not replace the assistant's runtime dependencies.

The inspected package declares PyTorch/Torchaudio 2.6.0 for Python below 3.14, Transformers 5.2.0, Librosa 0.11.0, Safetensors 0.5.3, and NumPy below 2.0 for Python below 3.13, plus PerTh and the other dependencies listed in the pinned package file. Provision matching CUDA-enabled PyTorch wheels in that separate environment; the repository's default requirements were not changed.

## Local configuration and activation

`Personality/wednesday.py` is still dynamically discovered. Its optional `voice.reference_config` points to `config/voices/wednesday.json`. That directory is ignored by Git because it can contain private recordings, local paths, settings, and model files. Wednesday's existing ElevenLabs voice-ID metadata is retained for the separate legacy TTS factory; this integration does not create or call an ElevenLabs voice.

The prepared local JSON records authorization, the exact source path and hash, `device: "cuda"`, and an empty `model_dir`. **An empty model directory deliberately leaves the new voice unavailable.** The app logs this explicitly and keeps its configured Gemini voice. No model is silently installed or downloaded.

After approval and installation:

1. Put the complete approved artifact set in a local directory.
2. Set `model_dir` in the ignored JSON to that directory. Set `python_executable` to the dedicated environment's Python executable if it differs from the app's interpreter.
3. Restart the app, or switch to another personality and then explicitly select Wednesday, to rebuild the session configuration.
4. Listen to a short neutral test sentence and measure startup time, response latency and GPU memory. Compare intelligibility and similarity before considering the voice ready.

An isolated child process loads local weights with Hugging Face/Transformers offline flags set before imports. It validates the authorized reference and hash, prepares conditioning once, and returns mono 24 kHz signed 16-bit PCM. Missing dependencies, incomplete artifacts, or inference failures are reported instead of triggering setup/downloads. No extra pitch shifting or postprocessing is applied.

## Runtime behavior

The existing live app uses Gemini native audio; `core/tts.py` is not called by that path. Changing profile metadata alone would not change the actual voice.

For an available local reference configuration, the live pipeline suppresses Gemini's generated PCM, collects its output transcription, synthesizes completed responses in a background worker, and feeds PCM into the existing speaker queue. The microphone input, Gemini tools, output device, waveform display, and session resumption stay on their existing paths. Gemini still generates audio upstream, so this approach adds turn-level latency and does not reduce Gemini audio usage.

Confirmed selection follows the existing personality callback and reconnects with conversation context retained. Each session captures its own voice backend. Escape while the selector is open emits no selection callback, so it changes neither personality nor voice. Interruptions invalidate queued/in-flight synthesis; a late result from an old personality is discarded. Native Gemini playback remains the default for other profiles. If a configured model fails during synthesis, the text response remains available and an error is logged.

## Validation and remaining checks

Focused tests mock model/subprocess/TTS operations and exercise configuration validation, offline loading, confirmed selection, Escape cancellation, native PCM passthrough, replacement audio, and stale-audio rejection. Tests do not contact services, load real model weights, play audio, or modify the reference recording.

Validation on 2026-10-13: **41 focused tests passed** (31 personality/selection/runtime tests and 10 reference-backend tests). `git diff --check` passed. The original MP3 and preexisting README SHA-256 hashes remained unchanged; nothing was staged. Commands run with `QT_QPA_PLATFORM=offscreen`:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_personality*.py' -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_reference_voice.py' -v
```

Still pending after the successful recovery test: subjective reference/output listening, speaker-device playback, similarity/intelligibility assessment, live-app startup and selection testing with real TTS, and warm-run latency measurement. The saved configuration has not been activated. The unmodified original MP3 and existing README work remain separate from these code changes. Nothing is committed or pushed.

## Initial approved installation attempt: 2026-10-13

**Outcome of the first attempt: stopped after download integrity failures; no synthesis was run and no generated audio existed at that stop.** Reference conditioning, synthesis time, peak inference memory, intelligibility, and voice similarity were therefore unverified at that point.

Before any download, D: had 211.1 GiB free. The conservative installation budget was 25 GiB, including dependencies, the 2,987,511,142-byte required model set, caches, and temporary files. Space was sufficient. The GPU reported 8,188 MiB total VRAM and 6,675 MiB free before installation; this is not an inference memory measurement.

The separate environment is `D:/CHIDVI-556/config/voices/chatterbox-turbo-20261013/venv`, using Python 3.12.14 with system site packages disabled. `environment.ps1` routes pip, Hugging Face, Torch, CUDA, Numba, XDG caches, and temporary files into that same isolated directory. No global or project `.venv` packages were installed. At the stop, the new environment contained only its bootstrap `pip==25.0.1`.

The official CUDA 12.4 installation of `torch==2.6.0` and `torchaudio==2.6.0` encountered read timeouts. Downloads advanced to subsequent packages with incomplete wheel files:

| Artifact | Advertised download size | Retained bytes | Archive check |
| --- | --- | --- | --- |
| `torch-2.6.0+cu124-cp312-cp312-win_amd64.whl` | Approximately 2,532.3 MB | 624,033,657 | Invalid ZIP |
| `torchaudio-2.6.0+cu124-cp312-cp312-win_amd64.whl` | Approximately 4.2 MB | 1,441,792 | Invalid ZIP |

An independent read-only ZIP check established the integrity failure; pip had not yet reported its final validation result. Only this task's identified installer and model-downloader processes were stopped, following the user's stop-on-failure instruction. No automatic retry, alternate package version, destructive cleanup, or environment replacement was attempted. The underlying cause of the truncated transfers is not established.

The parallel model download verified `added_tokens.json` and `merges.txt` against the pinned repository's Git blob hashes. `s3gen_meanflow.safetensors.partial` contains 157,286,400 bytes and is incomplete/unverified. The other required weights were not downloaded. The model metadata confirms the approved revision is public, ungated, and MIT licensed; its published sizes and hashes are archived under `sources/`.

Approximately 0.76 GiB of environment files, metadata, logs, and partial downloads remain; D: had approximately 210.3 GiB free at the stop. The local Wednesday JSON still has an empty `model_dir`, so this incomplete environment is not activated. SHA-256 checks confirmed that the original recording, all preexisting modified/new implementation files, README, local voice configuration, and Git HEAD/index were unchanged before this report update.

Evidence is under the isolated directory's `logs/`: `torch-install.log`, `model-download.log`, `installation-result.json`, `download-integrity.json`, `preservation-check.json`, `storage-after-stop.json`, and `installed-packages-after-stop.json`. `sources/required-model-manifest.json` records the exact eight-file model set. The prepared `synthesis_test.py` would run exactly one sentence through the existing Wednesday worker, measure load/conditioning and generation separately, record peak CUDA/RAM use, and save `outputs/wednesday-test.wav`; it has **not** been run. Its syntax, single-synthesis-call structure, and Windows memory measurement were checked independently.

At that stop, the proposed recovery was to obtain complete official wheel/model downloads, verify their hashes, and finish provisioning before running the single offline test. The parent `install_inference.ps1` recorded the pinned English inference packages and source revisions but was not executed. It deliberately omits the upstream demo UI (`gradio`), lazy multilingual packages (`spacy-pkuseg`, `pykakasi`), and development hook (`pre-commit`); these omissions must be distinguished from unexpected missing dependencies when checking the completed environment. PerTh's bundled watermark resources must be verified before inference. The retained original partial files are not usable packages or models.

## Successful installation recovery: 2026-10-13

The subsequent user-approved recovery completed using the same dedicated environment, with new scripts and logs under `D:/CHIDVI-556/config/voices/chatterbox-turbo-20261013/recovery-20261013`. D: had **210.3 GiB free** at its start against the **25 GiB** conservative budget. The completed installation tree contains **11.187 GiB** of files, including retained partials and caches; D: had **198.517 GiB free** after the test. The project `.venv`, application code, Wednesday profile/configuration, README, Git HEAD/index, and original MP3 were preserved. Only this report was updated outside the ignored installation directory. An unrelated untracked `karting_reel_Astra.mp4` appeared during the task and was left untouched.

### Download diagnosis and recovery

The old wheel files were incomplete: they retained valid ZIP beginnings but lacked the complete archives. Their sizes, hashes, and sampled byte ranges were checked against the official package servers before recovery. Copies of the retained bytes were resumed into new recovery files; the original failed wheel files remain byte-for-byte unchanged. Both completed wheels then matched the official full-file SHA-256 hashes and passed every ZIP CRC, wheel-tag, and Python-version check. This confirms valid recovered packages; the underlying cause of the earlier truncated network transfers remains unproven.

| Recovered official wheel | Exact size | SHA-256 |
| --- | --- | --- |
| [Torch 2.6.0+cu124, CPython 3.12/Windows](https://download-r2.pytorch.org/whl/cu124/torch-2.6.0%2Bcu124-cp312-cp312-win_amd64.whl) | 2,532,302,369 bytes | `3313061c1fec4c7310cf47944e84513dcd27b6173b72a349bb7ca68d0ee6e9c0` |
| [Torchaudio 2.6.0+cu124, CPython 3.12/Windows](https://download-r2.pytorch.org/whl/cu124/torchaudio-2.6.0%2Bcu124-cp312-cp312-win_amd64.whl) | 4,157,942 bytes | `004ff6bcee0ac78747253c09db67d281add4308a9b87a7bf1769da5914998639` |

The official indexes advertised range support. HTTPS `curl --continue-at -` transfers used bounded retries and a 900-second per-attempt limit. Torch needed three timed resumptions before completing on attempt four. The decoder needed one timed resumption; the speech model needed two. All retained progress was reused. These timeout exits are recorded, and **no completed recovery artifact failed integrity validation**. A Python HEAD probe received HTTP 403; a normal curl HEAD request to the same official URL succeeded without changing authentication or TLS checks.

Only the dedicated environment's pip was upgraded, from 25.0.1 to **26.2.1**, using a verified official PyPI wheel. Its retry/resume support and a fresh private pip cache were used for dependency installation. Existing installation caches and failed partials were retained. All caches and temporary paths stayed under the D: installation directory.

All eight model files from revision `749d1c1a46eb10492095d68fbcf55691ccf137cd` passed their published SHA-256 or Git-blob checks, totaling **2,987,511,142 bytes**. The downloader used the official pinned URLs of the form `https://huggingface.co/ResembleAI/chatterbox-turbo/resolve/749d1c1a46eb10492095d68fbcf55691ccf137cd/<filename>`. No additional model or `conds.pt` was obtained. The local inference path uses the bundled speech-tokenizer weights and PerTh resources; it does not invoke the upstream pretrained download helpers.

### Installation and preflight

`recovery-20261013/install_inference.ps1` completed successfully. The principal versions are Python 3.12.14, Torch/Torchaudio 2.6.0+cu124, NumPy 1.26.4, Chatterbox 0.1.7, PerTh 1.1.0, S3Tokenizer 0.3.0, Transformers 5.2.0, Diffusers 0.29.0, Librosa 0.11.0, and Safetensors 0.5.3. Chatterbox and PerTh use the documented immutable GitHub revisions. The complete inventory of 75 installed distributions, source archive hashes, and pip installation reports are retained in the recovery logs.

`preflight.py` passed before model construction. It checked the dedicated interpreter, every installed package's active requirements and Python compatibility, exact pins, model hashes, CUDA availability, source revisions, and the three bundled watermark resources against installed wheel RECORD hashes. Only the four previously documented demo/multilingual/development dependencies are intentionally omitted. The test rechecked the same snapshot before loading any model. Neither validation nor synthesis downloaded resources.

### One reference-conditioned synthesis

The existing production worker loaded the verified local model and called `prepare_conditionals()` on the unchanged original MP3. The test overrode `model_dir` and the interpreter path **in memory only**. It made exactly one generation call with seed 556 and this sentence:

> The evening is quiet, and everything is exactly where it should be.

Output: `D:/CHIDVI-556/config/voices/chatterbox-turbo-20261013/recovery-20261013/outputs/wednesday-test.wav`.

| Measurement | Result |
| --- | --- |
| Model loading and reference conditioning | 97.825 seconds |
| Synthesis, including worker PCM conversion | 11.750 seconds |
| Produced audio | 3.680 seconds; mono; 24,000 Hz; PCM signed 16-bit |
| Real-time factor | 3.193; generation was slower than playback duration |
| Peak Torch GPU allocation / reservation | 2.769 / 2.816 GiB |
| Peak process working-set RAM | 4.635 GiB |
| Output bytes / frames | 176,684 / 88,320 |
| Peak / RMS level | -10.96 / -28.72 dBFS |
| Full-scale samples | 0 |
| Output SHA-256 | `b0e0da1263048971ba405433ffffbb32c0aea5e9d53bf460aa5b64050c714710` |

The benchmark's separate validation/import phase took 39.940 seconds; total process work measured 149.688 seconds. GPU figures describe the PyTorch allocator, not the entire process/desktop GPU footprint. No out-of-memory or synthesis error occurred. Diffusers emitted a nonfatal `LoRACompatibleLinear` deprecation warning; no optional PEFT package was installed in response. A restricted Windows CIM inventory query was unavailable; inference RAM measurement succeeded through `GetProcessMemoryInfo` instead.

The saved WAV was independently decoded with both Python `wave` and libsndfile/SoundFile. Both returned identical, non-silent PCM with the expected dimensions, duration, and hash. This proves successful local reference-conditioned generation and an intact output file. It does **not** establish perceptual similarity, correct pronunciation, an authentic character voice, or watermark detection accuracy. No processed reference copy or training was needed.

### Evidence and next step

Recovery logs include `official-wheel-manifest.json`, per-wheel `*.verified.json`, every `torch-resume-*` and `model-*` attempt, `model-verification.json`, pip JSON reports, `installed-packages.txt`, `preflight-result.json`, `synthesis-result.json`, `audio-validation.json`, and preservation reports. The original installation-attempt logs and incomplete originals are retained separately in the parent directory.

Listen to the generated WAV beside the reference before approving activation. Then validate the actual worker startup, explicit Wednesday selection, and Escape cancellation with real audio. The 97.825-second first load/conditioning phase is already close to the app's 120-second worker-response timeout, and production startup also includes imports; this direct-worker benchmark did not test that timeout. Review startup behavior before enabling the configuration. Warm-cache performance was not measured, and the recorded synthesis latency does not establish real-time conversational readiness. The app's saved `model_dir` remains empty, and nothing was committed, staged, or pushed.
