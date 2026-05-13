"""
Voice (Whisper) + Vision (Phi-3.5-vision) modules — on-device via OpenVINO GenAI.

- Whisper runs on NPU on Intel Core Ultra (verified).
- Phi-3.5-vision int4 prefers NPU, falls back to GPU/CPU.

Both pipelines are lazy-loaded on first request.
"""
from __future__ import annotations

import io
import threading
import time
import wave
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import openvino as ov
    import openvino_genai as ov_genai
    OV_AVAILABLE = True
except Exception as _exc:
    OV_AVAILABLE = False
    print(f"[VOICE_VISION] OpenVINO not available: {_exc}")

BASE_DIR = Path(__file__).resolve().parent
WHISPER_DIR = BASE_DIR / "models" / "whisper-base-ov"
VISION_DIR = BASE_DIR / "models" / "phi35-vision-ov"

_whisper = None
_whisper_device = None
_whisper_lock = threading.Lock()
_whisper_load_lock = threading.Lock()

_vision = None
_vision_device = None
_vision_lock = threading.Lock()
_vision_load_lock = threading.Lock()


def _load_whisper():
    global _whisper, _whisper_device
    if _whisper is not None:
        return
    if not OV_AVAILABLE or not WHISPER_DIR.exists():
        raise RuntimeError("Whisper model not found at models/whisper-base-ov")
    for device in ("NPU", "GPU", "CPU"):
        try:
            t = time.time()
            print(f"[WHISPER] Loading on {device}...")
            _whisper = ov_genai.WhisperPipeline(str(WHISPER_DIR), device)
            _whisper_device = device
            print(f"[WHISPER] Ready on {device} ({time.time()-t:.1f}s)")
            return
        except Exception as e:
            print(f"[WHISPER] {device} failed: {str(e)[:120]}")
    raise RuntimeError("Whisper failed to load on any device")


def whisper_device() -> str | None:
    return _whisper_device


def transcribe_wav(wav_bytes: bytes) -> dict:
    with _whisper_load_lock:
        if _whisper is None:
            _load_whisper()

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())

    if sw != 2:
        raise ValueError(f"WAV must be 16-bit PCM (got {sw*8}-bit)")
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch == 2:
        audio = audio.reshape(-1, 2).mean(axis=1)

    if sr != 16000:
        ratio = 16000 / sr
        new_len = int(len(audio) * ratio)
        idx = np.linspace(0, len(audio) - 1, new_len)
        audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)

    duration_s = len(audio) / 16000.0
    t0 = time.perf_counter()
    with _whisper_lock:
        result = _whisper.generate(audio)
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    text = str(result).strip()
    return {
        "text": text,
        "latency_ms": elapsed_ms,
        "audio_seconds": round(duration_s, 2),
        "hardware": _whisper_device or "unknown",
        "model": "whisper-base",
    }


def _load_vision():
    global _vision, _vision_device
    if _vision is not None:
        return
    if not OV_AVAILABLE or not VISION_DIR.exists():
        raise RuntimeError("Vision model not found at models/phi35-vision-ov")
    for device in ("NPU", "GPU", "CPU"):
        try:
            t = time.time()
            print(f"[VISION] Loading Phi-3.5-vision on {device}...")
            _vision = ov_genai.VLMPipeline(str(VISION_DIR), device)
            _vision_device = device
            print(f"[VISION] Ready on {device} ({time.time()-t:.1f}s)")
            return
        except Exception as e:
            print(f"[VISION] {device} failed: {str(e)[:120]}")
    raise RuntimeError("Vision model failed to load on any device")


def vision_device() -> str | None:
    return _vision_device


def _pil_to_tensor(img: Image.Image) -> "ov.Tensor":
    img = img.convert("RGB")
    w, h = img.size
    cap = 1024
    if max(w, h) > cap:
        scale = cap / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    arr = np.array(img)
    return ov.Tensor(arr)


def analyze_image(image_bytes: bytes, prompt: str, max_new_tokens: int = 320) -> dict:
    with _vision_load_lock:
        if _vision is None:
            _load_vision()

    img = Image.open(io.BytesIO(image_bytes))
    tensor = _pil_to_tensor(img)
    full_prompt = f"<image_1>\n{prompt}"

    t0 = time.perf_counter()
    with _vision_lock:
        try:
            _vision.start_chat()
        except Exception:
            pass
        try:
            result = _vision.generate(
                full_prompt,
                images=[tensor],
                max_new_tokens=max_new_tokens,
            )
        finally:
            try:
                _vision.finish_chat()
            except Exception:
                pass

    text = str(result).strip()
    if len(text) < 40 or text.lower().startswith("<image"):
        with _vision_lock:
            try:
                _vision.start_chat()
            except Exception:
                pass
            try:
                fallback = (
                    "Describe what is happening in this photo, then list any "
                    "safety issues, equipment problems, or notable observations."
                )
                result = _vision.generate(
                    fallback,
                    images=[tensor],
                    max_new_tokens=max_new_tokens,
                )
                text = str(result).strip()
            finally:
                try:
                    _vision.finish_chat()
                except Exception:
                    pass
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    return {
        "text": text,
        "latency_ms": elapsed_ms,
        "hardware": _vision_device or "unknown",
        "model": "phi-3.5-vision",
    }
