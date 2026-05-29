"""
ASU — On-Device AI NPU Showcase
================================
A Flask demo for Arizona State University's higher education operations,
running entirely on a Copilot+ PC NPU via Microsoft Foundry Local
(Qualcomm Snapdragon X / QNN).

Six personas:
  1. Academic Advisor
  2. Research Assistant
  3. Student Success Coach
  4. Financial Aid Advisor
  5. Career Services Advisor
  6. Campus Operations Manager
Plus a live NPU Dashboard.

Foundry Local discovery pattern adapted from npuneil/holt-cat-npu-demo.
"""

from __future__ import annotations

import sys as _sys
# Force UTF-8 on stdout/stderr so emoji + em-dashes don't crash on Windows
# consoles defaulting to cp1252. Safe no-op on Python <3.7 or non-TTY.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from prompts.personas import (
    SYLLABOT,
    PRACTICE_AI,
    ASSIGNMENT_AI,
    WRITING_GUIDE,
    ONBOARDING_ASSISTANT,
    CONCIERGE_AI,
    KNOWLEDGE_BASE,
    DEVELOPMENT_COACH,
    HYBRID_CONCIERGE,
)

try:
    import voice_vision
    VOICE_VISION_OK = True
except Exception as _vv_exc:
    print(f"[STARTUP] voice_vision unavailable: {_vv_exc}")
    voice_vision = None
    VOICE_VISION_OK = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SERVER_PORT = int(os.environ.get("ASU_PORT", "5007"))

# Set ASU_SKIP_FOUNDRY=1 to bypass the (slow) Foundry NPU preload entirely
# and boot straight into UI-preview mode. Useful for smoke tests and for
# teammates who just want to see the UI before installing Foundry Local.
SKIP_FOUNDRY = os.environ.get("ASU_SKIP_FOUNDRY", "").strip() not in ("", "0", "false", "False")

# Cache the foundry executable path once at import — saves PATH lookups and
# lets us print a single actionable message if it is missing.
FOUNDRY_PATH = shutil.which("foundry")

QNN_NPU_PREFERENCE = [
    "phi-3.5-mini",
    "phi-3-mini-4k",
    "qwen2.5-1.5b",
]
CPU_MODEL_PREFERENCE = [
    "Phi-4-mini-instruct-generic-cpu",
    "Phi-3.5-mini-instruct-generic-cpu",
    "qwen2.5-0.5b-instruct-generic-cpu",
]

# On Qualcomm, QNN NPU models crash the Foundry HTTP API but work perfectly
# via `foundry model run --prompt` CLI.  We use CLI-based inference on Qualcomm.
use_cli_inference = False
cli_model_alias: str | None = None

# Single NPU = single in-flight request. Serialize.
_inference_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Sample data — load once at startup
# ---------------------------------------------------------------------------
def _load_json(name: str) -> dict:
    try:
        return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[STARTUP] Could not load {name}: {exc}")
        return {}


SYLLABI = _load_json("syllabi.json")
PRACTICE = _load_json("practice_topics.json")
ASSIGNMENTS = _load_json("assignments.json")
WRITING = _load_json("writing_samples.json")
ONBOARDING = _load_json("onboarding_tracks.json")
CONCIERGE = _load_json("concierge_kb.json")
KB = _load_json("kb_articles.json")
DEV_GOALS = _load_json("dev_goals.json")
M365 = _load_json("m365_mock.json")


# ---------------------------------------------------------------------------
# Silicon detection
# ---------------------------------------------------------------------------
SILICON = "unknown"


def _detect_silicon() -> str:
    global SILICON
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
            capture_output=True, text=True, timeout=5,
        )
        cpu = result.stdout.strip().lower()
        if "qualcomm" in cpu or "snapdragon" in cpu:
            SILICON = "qualcomm"
        elif "intel" in cpu:
            SILICON = "intel"
        elif "amd" in cpu:
            SILICON = "amd"
    except Exception:
        SILICON = "unknown"
    return SILICON


_detect_silicon()
print(f"[STARTUP] Silicon detected: {SILICON}")


# ---------------------------------------------------------------------------
# Foundry Local discovery
# ---------------------------------------------------------------------------
foundry_ok = False
model_id: str | None = None
foundry_service_url: str | None = None
hardware_label = "CPU"
fallback_reason = ""


def _discover_foundry_port() -> str | None:
    try:
        result = subprocess.run(
            ["foundry", "service", "status"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            m = re.search(r"(https?://[\d.]+:\d+)", line)
            if m:
                return m.group(1)
    except Exception as exc:
        print(f"[STARTUP] foundry CLI not available: {exc}")
    return None


def _foundry_get(path: str, timeout: int = 10):
    try:
        resp = urllib.request.urlopen(f"{foundry_service_url}{path}", timeout=timeout)
        return json.loads(resp.read())
    except Exception:
        return None


def _foundry_post(path: str, body: dict, timeout: int = 120):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{foundry_service_url}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def _device_rank(mid: str) -> int:
    m = mid.lower()
    if any(t in m for t in ("npu", "qnn", "directml", "qualcomm")):
        return 0
    if "gpu" in m:
        return 1
    return 2


def _family_rank(mid: str) -> int:
    m = mid.lower()
    phi4 = "phi-4-mini" in m
    phi3 = "phi-3" in m
    phi = "phi" in m
    # Prefer the runtime that matches the silicon: QNN/NPU variants on
    # Snapdragon X, OpenVINO variants on Intel Core Ultra. Falls through
    # to a neutral score on unknown silicon so model discovery still picks
    # something reasonable.
    qnn = any(t in m for t in ("qnn", "npu", "directml", "qualcomm"))
    ov = "openvino" in m
    sil = (SILICON or "").lower() if "SILICON" in globals() else ""
    if sil in ("qualcomm", "arm64", "snapdragon"):
        runtime_bonus = 0 if qnn else (1 if not ov else 2)
    elif sil in ("intel",):
        runtime_bonus = 0 if ov else (1 if not qnn else 2)
    else:
        runtime_bonus = 0 if (qnn or ov) else 1
    base = 0 if phi4 else (2 if phi3 else (4 if phi else 6))
    return base + runtime_bonus


def _probe(mid: str, timeout: int) -> bool:
    """Probe a model via HTTP API to verify it works."""
    global fallback_reason
    try:
        _foundry_post(
            "/v1/chat/completions",
            {"model": mid, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
            timeout=timeout,
        )
        return True
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")[:600]
        except Exception:
            pass
        print(f"[STARTUP] Probe failed [{mid}] HTTP {e.code}: {body[:240]}")
        if _device_rank(mid) == 0 and not fallback_reason:
            fallback_reason = f"NPU probe failed for {mid.split(':')[0]}: HTTP {e.code}"
        return False
    except Exception as exc:
        print(f"[STARTUP] Probe failed [{mid}]: {exc}")
        return False


def init_foundry() -> None:
    """Discover Foundry Local and select best model for this silicon."""
    global foundry_ok, model_id, foundry_service_url, hardware_label
    global fallback_reason, use_cli_inference, cli_model_alias

    foundry_ok = False
    model_id = None
    foundry_service_url = None
    hardware_label = "CPU"
    fallback_reason = ""

    if SKIP_FOUNDRY:
        print("[STARTUP] ASU_SKIP_FOUNDRY set — skipping Foundry init. UI-preview mode.")
        fallback_reason = "Foundry init skipped via ASU_SKIP_FOUNDRY"
        return

    if FOUNDRY_PATH is None:
        print("[STARTUP] Foundry Local CLI not found on PATH.")
        print("[STARTUP]   Install:  winget install Microsoft.FoundryLocal")
        print("[STARTUP]   Docs:     https://learn.microsoft.com/azure/ai-foundry/foundry-local/")
        print("[STARTUP] Running in UI-preview mode (mock responses).")
        fallback_reason = "Foundry Local CLI not installed — UI-preview mode"
        return

    # ------------------------------------------------------------------
    # Qualcomm path — QNN NPU models crash the HTTP API but work via CLI.
    # We preload via `foundry model run --prompt` and keep using CLI.
    # ------------------------------------------------------------------
    if SILICON == "qualcomm":
        print("[STARTUP] Qualcomm detected — using CLI-based NPU inference")
        for alias in QNN_NPU_PREFERENCE:
            print(f"[STARTUP] Probing NPU model via CLI: {alias} ...")
            try:
                result = subprocess.run(
                    ["foundry", "model", "run", alias, "--device", "NPU",
                     "--prompt", "Reply with only the word OK.",
                     "--retain", "--ttl", "3600"],
                    capture_output=True, timeout=300,
                )
                output = (result.stdout or b"").decode("utf-8", errors="replace") + \
                         (result.stderr or b"").decode("utf-8", errors="replace")
                if "loaded successfully" in output or result.returncode == 0:
                    for line in output.splitlines():
                        if "loaded successfully" in line:
                            m = re.search(r"Model\s+(\S+)\s+loaded", line)
                            if m:
                                model_id = m.group(1)
                    if not model_id:
                        model_id = alias
                    cli_model_alias = alias
                    use_cli_inference = True
                    foundry_ok = True
                    hardware_label = "NPU"
                    print(f"[STARTUP] [OK] NPU model ready via CLI: {model_id}")
                    return
                else:
                    print(f"[STARTUP] CLI probe failed for {alias}: exit {result.returncode}")
            except subprocess.TimeoutExpired:
                print(f"[STARTUP] CLI probe timed out for {alias}")
            except Exception as exc:
                print(f"[STARTUP] CLI probe error for {alias}: {exc}")

        # QNN models all failed — fall through to HTTP/CPU
        print("[STARTUP] No QNN NPU model available via CLI, trying CPU via HTTP...")
        fallback_reason = "QNN NPU models unavailable — falling back to CPU"

    # ------------------------------------------------------------------
    # HTTP API path — for Intel/AMD NPU or CPU fallback
    # ------------------------------------------------------------------
    service_url = _discover_foundry_port()
    if not service_url:
        try:
            subprocess.run(["foundry", "service", "start"], capture_output=True, text=True, timeout=30)
            time.sleep(3)
            service_url = _discover_foundry_port()
        except Exception:
            pass

    if not service_url:
        print("[STARTUP] Foundry Local service not running. UI-preview mode.")
        return

    foundry_service_url = service_url
    print(f"[STARTUP] Foundry Local HTTP service at {service_url}")

    models_data = _foundry_get("/v1/models")
    if not models_data or "data" not in models_data:
        print("[STARTUP] Could not list models from Foundry. UI-preview mode.")
        foundry_service_url = None
        return

    available_ids = [m["id"] for m in models_data["data"]]
    print(f"[STARTUP] Available HTTP models: {available_ids}")

    # On Qualcomm (fallback), skip QNN models for HTTP — they crash the service
    if SILICON == "qualcomm":
        available_ids = [m for m in available_ids if "qnn" not in m.lower()]
        print(f"[STARTUP] Filtered to CPU-safe models: {available_ids}")

    candidates = sorted(available_ids, key=lambda x: (_device_rank(x), _family_rank(x)))
    print(f"[STARTUP] Probe order: {candidates}")

    for mid in candidates:
        tier = _device_rank(mid)
        tier_name = "NPU" if tier == 0 else ("GPU" if tier == 1 else "CPU")
        timeout = 240 if tier <= 1 else 60
        print(f"[STARTUP] Probing {tier_name} model: {mid} (timeout={timeout}s) ...")
        if _probe(mid, timeout):
            model_id = mid
            foundry_ok = True
            hardware_label = tier_name
            print(f"[STARTUP] [OK] Model ready: {model_id} on {tier_name}")
            return
        print(f"[STARTUP] Skipping {mid}; trying next.")

    print("[STARTUP] No model verified. UI-preview mode.")
    foundry_service_url = None


try:
    init_foundry()
except Exception as _init_exc:
    print(f"[STARTUP] init_foundry crashed ({_init_exc}); continuing in UI-preview mode.")
    fallback_reason = f"Foundry init error: {_init_exc}"

if foundry_ok and not use_cli_inference:
    try:
        print("[STARTUP] Warming up model...")
        _foundry_post(
            "/v1/chat/completions",
            {"model": model_id, "messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 8},
            timeout=60,
        )
        print("[STARTUP] Warmup complete.")
    except Exception as exc:
        print(f"[STARTUP] Warmup skipped: {exc}")


def _preload_voice_vision():
    if not VOICE_VISION_OK:
        return

    def _warm_whisper():
        try:
            print("[PRELOAD] Whisper: loading on NPU...")
            voice_vision.transcribe_wav(_make_silent_wav())
            print(f"[PRELOAD] Whisper ready on {voice_vision.whisper_device()}.")
        except Exception as e:
            print(f"[PRELOAD] Whisper preload failed: {e}")

    def _warm_vision():
        try:
            print("[PRELOAD] Vision: loading Phi-3.5-vision...")
            from PIL import Image as _Image
            import io as _io
            buf = _io.BytesIO()
            _Image.new("RGB", (336, 336), (140, 29, 64)).save(buf, format="JPEG")
            voice_vision.analyze_image(
                buf.getvalue(),
                "Briefly describe this image in one sentence.",
                max_new_tokens=32,
            )
            print(f"[PRELOAD] Vision ready on {voice_vision.vision_device()}.")
        except Exception as e:
            print(f"[PRELOAD] Vision preload failed: {e}")

    threading.Thread(target=_warm_whisper, daemon=True, name="whisper-preload").start()
    threading.Thread(target=_warm_vision, daemon=True, name="vision-preload").start()


def _make_silent_wav(seconds: float = 0.4, rate: int = 16000) -> bytes:
    import io as _io
    import wave as _wave
    import struct as _struct
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(_struct.pack("<" + "h" * int(rate * seconds), *([0] * int(rate * seconds))))
    return buf.getvalue()


_preload_voice_vision()


# ---------------------------------------------------------------------------
# Inference helpers
# ---------------------------------------------------------------------------
inference_log: list[dict] = []


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _run_cli_inference(system_prompt: str, user_prompt: str) -> str:
    """Run inference via `foundry model run --prompt` CLI (Qualcomm NPU path)."""
    combined = f"INSTRUCTIONS: {system_prompt}\n\nUSER REQUEST: {user_prompt}"
    try:
        result = subprocess.run(
            ["foundry", "model", "run", cli_model_alias, "--device", "NPU",
             "--prompt", combined, "--retain", "--ttl", "3600"],
            capture_output=True, timeout=120,
        )
        output = (result.stdout or b"").decode("utf-8", errors="replace") + \
                 (result.stderr or b"").decode("utf-8", errors="replace")
        # Parse response — the actual model output follows the 🤖 emoji
        lines = output.splitlines()
        response_lines = []
        capture = False
        for line in lines:
            if "\U0001f916" in line or "🤖" in line or "≡ƒñû" in line:
                text_after = line
                for marker in ["\U0001f916", "🤖", "≡ƒñû"]:
                    if marker in text_after:
                        text_after = text_after.split(marker, 1)[-1]
                response_lines.append(text_after.strip())
                capture = True
            elif capture:
                if any(skip in line for skip in ["Loading model", "Thinking", "loaded successfully", "ERR]", "version of the model"]):
                    continue
                response_lines.append(line)

        text = "\n".join(response_lines).strip()
        if not text:
            # Fallback: grab everything after "Thinking"
            in_response = False
            for line in lines:
                if "Thinking" in line:
                    in_response = True
                    continue
                if in_response and not any(skip in line for skip in ["ERR]", "Loading", "version of"]):
                    text_part = line.strip()
                    if text_part:
                        response_lines.append(text_part)
            text = "\n".join(response_lines).strip()

        return text if text else "[No response from NPU model]"
    except subprocess.TimeoutExpired:
        return "[NPU inference timed out — try a shorter prompt]"
    except Exception as exc:
        return f"[NPU inference error: {exc}]"


def _run_inference(system_prompt: str, user_prompt: str, max_tokens: int = 480, persona: str = "") -> dict:
    if not foundry_ok or not model_id:
        msg = (
            "[Demo mode — Foundry Local is not connected. Install with "
            "`winget install Microsoft.FoundryLocal`, run "
            "`foundry model run phi-3.5-mini --device NPU`, then restart this app.]"
        )
        return {
            "response": msg, "text": msg, "tokens": 0, "latency_ms": 0,
            "cloud_cost_saved": "$0.00", "hardware": hardware_label, "persona": persona,
        }

    t0 = time.perf_counter()

    # Qualcomm CLI-based NPU inference
    if use_cli_inference:
        with _inference_lock:
            text = _run_cli_inference(system_prompt, user_prompt)
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        total_tokens = _estimate_tokens(system_prompt + user_prompt) + _estimate_tokens(text)
        est_cost = round(total_tokens * 0.00001, 6)
        entry = {
            "id": str(uuid.uuid4())[:8],
            "timestamp": datetime.now().isoformat(),
            "persona": persona,
            "tokens": total_tokens,
            "latency_ms": elapsed_ms,
            "cloud_cost_saved": f"${est_cost:.4f}",
            "hardware": "NPU",
        }
        inference_log.append(entry)
        if len(inference_log) > 500:
            del inference_log[: len(inference_log) - 500]
        return {
            "response": text, "text": text, "tokens": total_tokens,
            "latency_ms": elapsed_ms, "cloud_cost_saved": f"${est_cost:.4f}",
            "hardware": "NPU", "persona": persona,
        }

    # HTTP API path (Intel / AMD / CPU fallback)
    safe_max = min(max_tokens, 480)
    body = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": safe_max,
    }

    t0 = time.perf_counter()
    with _inference_lock:
        try:
            result = _foundry_post("/v1/chat/completions", body, timeout=120)
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="ignore")[:400]
            except Exception:
                pass
            print(f"[INFER] Foundry HTTP {e.code}: {err_body}")
            err = f"[Foundry error HTTP {e.code}: {err_body[:200]}]"
            return {
                "response": err, "text": err, "tokens": 0, "latency_ms": 0,
                "cloud_cost_saved": "$0.00", "hardware": hardware_label, "persona": persona,
            }
        except Exception as exc:
            print(f"[INFER] HTTP failed ({hardware_label}): {exc}")
            try:
                init_foundry()
                body["model"] = model_id
                result = _foundry_post("/v1/chat/completions", body, timeout=120)
            except Exception as exc2:
                err = f"[Error reaching Foundry Local — {exc2}]"
                return {
                    "response": err, "text": err, "tokens": 0, "latency_ms": 0,
                    "cloud_cost_saved": "$0.00", "hardware": hardware_label, "persona": persona,
                }
    elapsed_ms = round((time.perf_counter() - t0) * 1000)

    text = ""
    choices = result.get("choices", [])
    if choices:
        msg = choices[0].get("message") or choices[0].get("delta") or {}
        text = msg.get("content", "")

    usage = result.get("usage") or {}
    total_tokens = usage.get("total_tokens", 0) or (
        _estimate_tokens(system_prompt + user_prompt) + _estimate_tokens(text)
    )

    est_cost = round(total_tokens * 0.00001, 6)

    entry = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now().isoformat(),
        "persona": persona,
        "tokens": total_tokens,
        "latency_ms": elapsed_ms,
        "cloud_cost_saved": f"${est_cost:.4f}",
        "hardware": hardware_label,
    }
    inference_log.append(entry)
    if len(inference_log) > 500:
        del inference_log[: len(inference_log) - 500]

    return {
        "response": text, "text": text, "tokens": total_tokens,
        "latency_ms": elapsed_ms, "cloud_cost_saved": f"${est_cost:.4f}",
        "hardware": hardware_label, "persona": persona,
    }


# ---------------------------------------------------------------------------
# Use-case context helpers — keep prompts grounded in the JSON data.
# ---------------------------------------------------------------------------
def _syllabus_brief(course_code: str | None = None) -> str:
    courses = SYLLABI.get("courses", [])
    if course_code:
        match = [c for c in courses if c["code"].lower() == course_code.lower()]
        if match:
            courses = match
    rows = []
    for c in courses:
        rows.append(f"\n--- {c['code']}: {c['title']} ({c['term']}) ---")
        rows.append(f"Instructor: {c['instructor']}  |  Office hours: {c['office_hours']}")
        grading = ", ".join(f"{k} {v}" for k, v in c.get("grading", {}).items())
        rows.append(f"Grading: {grading}")
        rows.append("Due dates:")
        for d in c.get("due_dates", []):
            rows.append(f"  • {d['item']} — due {d['due']} ({d['submit']})")
        rows.append(f"Late policy: {c.get('late_policy', '—')}")
        rows.append("Materials: " + "; ".join(c.get("materials", [])))
        rows.append(f"Integrity: {c.get('integrity', '—')}")
        rows.append(f"Exam format: {c.get('exam_format', '—')}")
    return "SYLLABUS DATA (fictitious):" + "\n".join(rows)


def _practice_brief(topic_id: str | None = None) -> str:
    topics = PRACTICE.get("topics", [])
    if topic_id:
        match = [t for t in topics if t["id"] == topic_id]
        if match:
            topics = match
    rows = []
    for t in topics:
        rows.append(f"\n--- {t['title']} ({t['course']}) [id: {t['id']}] ---")
        rows.append("Key concepts: " + "; ".join(t.get("key_concepts", [])))
        rows.append("Sample Q&A bank:")
        for q in t.get("sample_questions", []):
            rows.append(f"  Q: {q['q']}\n  A: {q['a']}")
    return "PRACTICE TOPIC BANK (fictitious):" + "\n".join(rows)


def _assignments_brief(assignment_id: str | None = None) -> str:
    items = ASSIGNMENTS.get("assignments", [])
    if assignment_id:
        match = [a for a in items if a["id"] == assignment_id]
        if match:
            items = match
    rows = []
    for a in items:
        rows.append(f"\n--- {a['course']} · {a['title']} ---")
        rows.append(f"Due: {a['due']}  |  Weight: {a['weight']}")
        rows.append(f"Summary: {a['summary']}")
        rows.append("Deliverables: " + "; ".join(a.get("deliverables", [])))
        rows.append("Rubric focus: " + ", ".join(a.get("rubric_focus", [])))
        rows.append(f"Real-world tie: {a.get('real_world_tie', '—')}")
    return "ASSIGNMENT DATA (fictitious):" + "\n".join(rows)


def _writing_brief(draft_id: str | None = None) -> str:
    drafts = WRITING.get("drafts", [])
    if draft_id:
        match = [d for d in drafts if d["id"] == draft_id]
        if match:
            drafts = match
    rows = []
    for d in drafts:
        rows.append(f"\n--- {d['student']} · {d['assignment']} ---")
        rows.append(f"Title: {d['title']}")
        rows.append("DRAFT TEXT:")
        rows.append(d["text"])
    return "STUDENT DRAFT(S) (fictitious):" + "\n".join(rows)


def _onboarding_brief(role: str | None = None) -> str:
    tracks = ONBOARDING.get("tracks", [])
    if role:
        match = [t for t in tracks if role.lower() in t["role"].lower()]
        if match:
            tracks = match
    rows = []
    for t in tracks:
        rows.append(f"\n--- {t['role']} · {t['department']} ---")
        rows.append(f"Manager: {t['manager']}")
        rows.append("Day-1 checklist: " + "; ".join(t.get("first_day_checklist", [])))
        rows.append("30-day: " + "; ".join(t.get("30_day", [])))
        rows.append("60-day: " + "; ".join(t.get("60_day", [])))
        rows.append("90-day: " + "; ".join(t.get("90_day", [])))
        rows.append("Key systems: " + ", ".join(t.get("key_systems", [])))
        contacts = "; ".join(f"{c['who']} → {c['for']}" for c in t.get("go_to_contacts", []))
        rows.append("Contacts: " + contacts)
    return "ONBOARDING TRACKS (fictitious):" + "\n".join(rows)


def _concierge_brief() -> str:
    rows = []
    for t in CONCIERGE.get("topics", []):
        rows.append(f"\n--- {t['topic']} ({t['owner']}) ---")
        rows.append(f"System: {t['system']}  |  SLA: {t.get('sla', '—')}  |  Hours: {t.get('office_hours', '—')}")
        rows.append("Steps:")
        for i, s in enumerate(t.get("steps", []), 1):
            rows.append(f"  {i}. {s}")
        if t.get("notes"):
            rows.append(f"Notes: {t['notes']}")
    return "CONCIERGE DIRECTORY (fictitious):" + "\n".join(rows)


def _kb_brief(article_id: str | None = None) -> str:
    arts = KB.get("articles", [])
    if article_id:
        match = [a for a in arts if a["id"].lower() == article_id.lower()]
        if match:
            arts = match
    rows = [f"Department: {KB.get('department', '—')}"]
    for a in arts:
        rows.append(f"\n--- {a['id']}: {a['title']} (last reviewed {a['last_reviewed']}, owner: {a['owner']}) ---")
        rows.append(f"Summary: {a['summary']}")
        rows.append("Policy:")
        for p in a.get("policy", []):
            rows.append(f"  • {p}")
        rows.append(f"Limits: {a.get('limits', '—')}")
        rows.append("Related: " + ", ".join(a.get("related", [])))
    return "KB ARTICLES (fictitious):\n" + "\n".join(rows)


def _dev_brief(employee: str | None = None) -> str:
    sheets = DEV_GOALS.get("goal_sheets", [])
    if employee:
        match = [s for s in sheets if employee.lower() in s["employee"].lower()]
        if match:
            sheets = match
    rows = []
    for s in sheets:
        rows.append(f"\n--- {s['employee']} · {s['role']} ---")
        for g in s.get("goals", []):
            rows.append(f"  [{g['id']}] ({g['category']}) {g['statement']}")
            rows.append(f"      now: {g['current_state']}")
            rows.append(f"      success: {g['success_signal']}")
    rows.append("\nCompetency framework: " + ", ".join(DEV_GOALS.get("competency_framework", [])))
    rows.append("Reflection prompts:")
    for p in DEV_GOALS.get("reflection_prompts", []):
        rows.append(f"  • {p}")
    return "DEVELOPMENT DATA (fictitious):" + "\n".join(rows)


def _hybrid_context(scenario_id: str | None) -> tuple[str, list[dict]]:
    """Return (context_text, raw_cards) for a Hybrid AI scenario."""
    scenarios = M365.get("scenarios", [])
    scen = None
    if scenario_id:
        scen = next((s for s in scenarios if s["id"] == scenario_id), None)
    if not scen and scenarios:
        scen = scenarios[0]
    if not scen:
        return ("(no context cards available)", [])
    cards = scen.get("context", [])
    rows = [f"SCENARIO: {scen.get('label', '')}"]
    rows.append("\nCONTEXT CARDS retrieved from Microsoft 365 (mock):")
    for c in cards:
        rows.append(f"\n[{c['source']}] {c.get('title', '')}")
        if c.get("from"):
            rows.append(f"  from: {c['from']}")
        if c.get("modified_by"):
            rows.append(f"  modified by: {c['modified_by']} ({c.get('modified', '')})")
        if c.get("received"):
            rows.append(f"  received: {c['received']}")
        rows.append(f"  snippet: {c.get('snippet', '')}")
    return ("\n".join(rows), cards)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", template_folder="templates")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    runtime = "QNN" if SILICON == "qualcomm" else ("OpenVINO" if SILICON == "intel" else "Foundry Local")
    short_model = (model_id or "N/A").split(":")[0]
    infer_mode = "CLI (NPU direct)" if use_cli_inference else "HTTP API"
    voice_dev = voice_vision.whisper_device() if VOICE_VISION_OK else None
    vision_dev = voice_vision.vision_device() if VOICE_VISION_OK else None
    return jsonify({
        "ready": foundry_ok,
        "foundry_connected": foundry_ok,
        "model": short_model,
        "model_full": model_id or "N/A",
        "endpoint": foundry_service_url or ("CLI" if use_cli_inference else "N/A"),
        "mode": (f"on-device {hardware_label}" if foundry_ok else "UI preview (no AI)"),
        "hardware": (hardware_label if foundry_ok else "none"),
        "runtime": runtime,
        "inference_mode": infer_mode,
        "silicon": SILICON,
        "fallback_reason": fallback_reason,
        "voice_device": voice_dev or "not loaded",
        "vision_device": vision_dev or "not loaded",
        "voice_model": "whisper-base" if VOICE_VISION_OK else None,
        "vision_model": "phi-3.5-vision" if VOICE_VISION_OK else None,
        "message": (
            f"On-device {hardware_label} ready · {short_model}"
            if foundry_ok else "Foundry Local not connected — UI preview mode"
        ),
    })


@app.route("/api/metrics")
def api_metrics():
    log = inference_log[-50:]
    total_calls = len(inference_log)
    if log:
        avg_latency = round(sum(e["latency_ms"] for e in log) / len(log))
        total_tokens = sum(e["tokens"] for e in inference_log)
    else:
        avg_latency = 0
        total_tokens = 0

    est_total = sum(float(e["cloud_cost_saved"].strip("$")) for e in inference_log)

    tps = 0
    if len(log) >= 2:
        recent = log[-5:]
        tok = sum(e["tokens"] for e in recent)
        ms = sum(e["latency_ms"] for e in recent)
        if ms > 0:
            tps = round(tok / (ms / 1000), 1)

    return jsonify({
        "hardware": hardware_label if foundry_ok else "none",
        "runtime": "QNN" if SILICON == "qualcomm" else ("OpenVINO" if SILICON == "intel" else "Foundry Local"),
        "model": (model_id or "N/A").split(":")[0],
        "avg_latency_ms": avg_latency,
        "tokens_per_sec": tps,
        "total_calls": total_calls,
        "tokens_total": total_tokens,
        "cloud_cost_saved": f"${est_total:.4f}",
        "recent": log[-10:][::-1],
    })


@app.route("/api/data/syllabi")
def api_data_syllabi():
    courses = SYLLABI.get("courses", [])
    return jsonify({"courses": [{"code": c["code"], "title": c["title"], "term": c["term"]} for c in courses]})


@app.route("/api/data/practice")
def api_data_practice():
    topics = PRACTICE.get("topics", [])
    return jsonify({"topics": [{"id": t["id"], "title": t["title"], "course": t["course"]} for t in topics]})


@app.route("/api/data/assignments")
def api_data_assignments():
    items = ASSIGNMENTS.get("assignments", [])
    return jsonify({"assignments": [{"id": a["id"], "course": a["course"], "title": a["title"], "due": a["due"]} for a in items]})


@app.route("/api/data/writing")
def api_data_writing():
    drafts = WRITING.get("drafts", [])
    return jsonify({"drafts": [{"id": d["id"], "label": f"{d['student']} — {d['title']}"} for d in drafts]})


@app.route("/api/data/onboarding")
def api_data_onboarding():
    tracks = ONBOARDING.get("tracks", [])
    return jsonify({"roles": [t["role"] for t in tracks]})


@app.route("/api/data/kb")
def api_data_kb():
    arts = KB.get("articles", [])
    return jsonify({
        "department": KB.get("department"),
        "articles": [{"id": a["id"], "title": a["title"]} for a in arts],
    })


@app.route("/api/data/dev")
def api_data_dev():
    sheets = DEV_GOALS.get("goal_sheets", [])
    return jsonify({"employees": [s["employee"] for s in sheets]})


@app.route("/api/data/hybrid")
def api_data_hybrid():
    scenarios = M365.get("scenarios", [])
    return jsonify({
        "scenarios": [
            {
                "id": s["id"],
                "label": s["label"],
                "user_prompt_example": s.get("user_prompt_example", ""),
                "context": s.get("context", []),
            }
            for s in scenarios
        ]
    })


SAMPLE_IMAGES = [
    # ── Campus facility photos (real) ──
    {"name": "campus_aerial", "label": "ASU Tempe Campus — Aerial View", "url": "/static/samples/campus_aerial.jpg", "category": "campus",
     "description": "Aerial photograph of the Arizona State University Tempe campus. Visible are multiple academic buildings with terracotta and modern glass facades, palm-tree lined walkways connecting buildings, large open quad areas with green lawns, bicycle parking areas, and the surrounding Tempe urban landscape. A-Mountain (Hayden Butte) is visible in the background."},
    {"name": "campus_building", "label": "ASU Academic Building", "url": "/static/samples/campus_building.jpg", "category": "campus",
     "description": "Photo of a modern ASU academic building on the Tempe campus. Multi-story structure with floor-to-ceiling glass curtain walls, steel and concrete construction, covered walkways at ground level, and ASU maroon accent panels. Landscaped entrance with desert-adapted plantings, bike racks, and ADA-accessible ramps. Students visible walking nearby."},
    {"name": "lecture_hall_real", "label": "University Lecture Hall", "url": "/static/samples/lecture_hall_real.jpg", "category": "campus",
     "description": "Interior photo of a large university lecture hall with tiered stadium seating for 200+ students. Modern acoustical ceiling panels, dual projection screens at the front, integrated desk surfaces at each seat, LED overhead lighting, and AV control podium. Emergency exit signs visible on both sides. Carpet flooring in the aisles."},
    {"name": "fitness_real", "label": "Sun Devil Fitness Complex", "url": "/static/samples/fitness_real.jpg", "category": "campus",
     "description": "Interior photo of a modern university fitness center with rows of treadmills, ellipticals, and weight machines. Floor-to-ceiling windows letting in natural light. Rubber flooring, overhead fans, flat-screen TVs mounted on walls. Students working out on various equipment. Clean, well-maintained facility."},
    {"name": "library_books", "label": "Hayden Library Interior", "url": "/static/samples/library_books.jpg", "category": "campus",
     "description": "Interior photo of a university library showing rows of bookshelves with academic volumes, study carrels along windows, reading tables with task lighting, and a quiet study atmosphere. Natural light filtering through large windows. Students seated at individual study stations."},
    {"name": "science_lab", "label": "Biodesign Research Lab", "url": "/static/samples/science_lab.jpg", "category": "campus",
     "description": "Interior photo of a modern university science laboratory with lab benches, microscopes, fume hoods, chemical storage cabinets, and safety equipment. Overhead LED lighting, anti-static flooring, emergency eyewash station visible. Lab equipment includes centrifuges and analytical instruments."},
    # ── Student activity photos (real) ──
    {"name": "classroom_real", "label": "Active Learning Classroom", "url": "/static/samples/classroom_real.jpg", "category": "student",
     "description": "Photo of a university classroom during an active learning session. Students seated at tables working in small groups, some with laptops open, others writing notes. Instructor visible at the front near a whiteboard with equations. Well-lit room with modern furniture, projector screen, and collaborative seating arrangement."},
    {"name": "students_studying", "label": "Students Studying on Campus", "url": "/static/samples/students_studying.jpg", "category": "student",
     "description": "Photo of university students studying outdoors on campus. Students seated on benches and at tables under shade trees, some with laptops, others reading textbooks. Backpacks and water bottles nearby. Warm sunny day with campus buildings visible in background. Collaborative and individual study happening simultaneously."},
    {"name": "students_walking", "label": "Students on Palm Walk", "url": "/static/samples/students_walking.jpg", "category": "student",
     "description": "Photo of university students walking along a palm-tree lined campus walkway between classes. Groups of students with backpacks, some in conversation, others checking phones. Modern academic buildings on both sides. Sunny day with clear skies. Bike riders sharing the path."},
    {"name": "graduation", "label": "ASU Commencement Ceremony", "url": "/static/samples/graduation.jpg", "category": "student",
     "description": "Photo of a university graduation/commencement ceremony. Graduates in maroon caps and gowns, some with decorated mortarboards. Sun Devil Stadium or ceremony venue visible. Families in audience. Stage with university officials. ASU banners and regalia. Celebration atmosphere with students tossing caps."},
    {"name": "student_laptop", "label": "Student Working in Lab", "url": "/static/samples/student_laptop.jpg", "category": "student",
     "description": "Photo of a university student working on a laptop in a computer lab or study space. Screen shows academic work. Student wearing headphones, focused and engaged. Modern workspace with good lighting. Other workstations visible in background."},
]

@app.route("/api/samples")
def api_samples():
    # Return samples without the detailed description field (used internally for vision)
    public = [{k: v for k, v in s.items() if k != "description"} for s in SAMPLE_IMAGES]
    return jsonify({"samples": public})


# ---------------------------------------------------------------------------
# Use-case endpoints (Empower 2026: 8 ASU AI use cases + Hybrid mock)
# ---------------------------------------------------------------------------
def _user_text(req) -> str:
    data = req.get_json(force=True, silent=True) or {}
    return (data.get("message") or data.get("text") or "").strip()


def _user_payload(req) -> dict:
    return req.get_json(force=True, silent=True) or {}


# ---- Student-facing ----
@app.route("/api/syllabot", methods=["POST"])
def api_syllabot():
    data = _user_payload(request)
    user = (data.get("message") or "").strip()
    course = data.get("course") or ""
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_syllabus_brief(course)}"
    return jsonify(_run_inference(SYLLABOT, grounded, max_tokens=280, persona="syllabot"))


@app.route("/api/practice", methods=["POST"])
def api_practice():
    data = _user_payload(request)
    user = (data.get("message") or "").strip()
    topic = data.get("topic") or ""
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_practice_brief(topic)}"
    return jsonify(_run_inference(PRACTICE_AI, grounded, max_tokens=280, persona="practice"))


@app.route("/api/assignment", methods=["POST"])
def api_assignment():
    data = _user_payload(request)
    user = (data.get("message") or "").strip()
    aid = data.get("assignment") or ""
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_assignments_brief(aid)}"
    return jsonify(_run_inference(ASSIGNMENT_AI, grounded, max_tokens=360, persona="assignment"))


@app.route("/api/writing", methods=["POST"])
def api_writing():
    data = _user_payload(request)
    user = (data.get("message") or "").strip() or "Please review this draft and give structured feedback."
    draft = data.get("draft") or ""
    inline = data.get("draft_text") or ""
    if not draft and not inline:
        return jsonify({"error": "Pick a sample draft or paste text."}), 400
    if inline:
        context = "STUDENT DRAFT (pasted by user):\n" + inline
    else:
        context = _writing_brief(draft)
    grounded = f"{user}\n\n{context}"
    return jsonify(_run_inference(WRITING_GUIDE, grounded, max_tokens=400, persona="writing"))


# ---- Staff-facing ----
@app.route("/api/onboarding", methods=["POST"])
def api_onboarding():
    data = _user_payload(request)
    user = (data.get("message") or "").strip()
    role = data.get("role") or ""
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_onboarding_brief(role)}"
    return jsonify(_run_inference(ONBOARDING_ASSISTANT, grounded, max_tokens=360, persona="onboarding"))


@app.route("/api/concierge", methods=["POST"])
def api_concierge():
    user = _user_text(request)
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_concierge_brief()}"
    return jsonify(_run_inference(CONCIERGE_AI, grounded, max_tokens=320, persona="concierge"))


@app.route("/api/kb", methods=["POST"])
def api_kb():
    data = _user_payload(request)
    user = (data.get("message") or "").strip()
    article = data.get("article") or ""
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_kb_brief(article)}"
    return jsonify(_run_inference(KNOWLEDGE_BASE, grounded, max_tokens=320, persona="kb"))


@app.route("/api/dev", methods=["POST"])
def api_dev():
    data = _user_payload(request)
    user = (data.get("message") or "").strip()
    employee = data.get("employee") or ""
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_dev_brief(employee)}"
    return jsonify(_run_inference(DEVELOPMENT_COACH, grounded, max_tokens=320, persona="dev"))


# ---- Hybrid AI / M365 mock ----
@app.route("/api/hybrid", methods=["POST"])
def api_hybrid():
    data = _user_payload(request)
    user = (data.get("message") or "").strip()
    scenario_id = data.get("scenario") or ""
    if not user:
        return jsonify({"error": "Empty message"}), 400
    context_text, cards = _hybrid_context(scenario_id)
    # Fake "cloud retrieval" latency for the demo (purely visual)
    cloud_latency_ms = 240 + (hash(scenario_id or user) % 180)
    grounded = (
        f"USER REQUEST: {user}\n\n"
        f"{context_text}\n\n"
        "Use ONLY the cards above. Cite sources by name (Outlook / Teams / SharePoint)."
    )
    result = _run_inference(HYBRID_CONCIERGE, grounded, max_tokens=420, persona="hybrid")
    result["cards"] = cards
    result["cloud_latency_ms"] = cloud_latency_ms
    result["total_latency_ms"] = cloud_latency_ms + result.get("latency_ms", 0)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Health / Utility
# ---------------------------------------------------------------------------
@app.route("/api/health")
@app.route("/healthz")
def api_health():
    return jsonify({
        "status": "ok",
        "model": model_id,
        "foundry": foundry_ok,
        "silicon": SILICON,
        "hardware": hardware_label,
        "cli_inference": use_cli_inference,
        "foundry_cli_found": FOUNDRY_PATH is not None,
        "voice_vision": VOICE_VISION_OK,
        "fallback_reason": fallback_reason,
    })


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"\n  🏫 ASU NPU Showcase running at http://localhost:{SERVER_PORT}")
    app.run(host="0.0.0.0", port=SERVER_PORT, threaded=True)
