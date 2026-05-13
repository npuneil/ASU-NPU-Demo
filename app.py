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

import json
import os
import re
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
    ACADEMIC_ADVISOR,
    CAMPUS_OPERATIONS,
    CAREER_SERVICES,
    FINANCIAL_AID,
    RESEARCH_ASSISTANT,
    STUDENT_SUCCESS,
    VISION_CAMPUS,
    VISION_STUDENT,
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


STUDENTS = _load_json("students.json")
COURSES = _load_json("courses.json")
RESEARCH = _load_json("research.json")
CAMPUS = _load_json("campus.json")
FINANCIAL = _load_json("financial_aid.json")
CAREERS = _load_json("careers.json")


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
    openvino = "openvino" in m
    base = 0 if phi4 else (2 if phi3 else (4 if phi else 6))
    return base + (0 if openvino else 1)


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
                    print(f"[STARTUP] ✓ NPU model ready via CLI: {model_id}")
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
            print(f"[STARTUP] ✓ Model ready: {model_id} on {tier_name}")
            return
        print(f"[STARTUP] Skipping {mid}; trying next.")

    print("[STARTUP] No model verified. UI-preview mode.")
    foundry_service_url = None


init_foundry()

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
# Persona context helpers — keep prompts grounded in the JSON data.
# ---------------------------------------------------------------------------
def _students_brief() -> str:
    rows = []
    for s in STUDENTS.get("students", []):
        aid = s.get("financial_aid", {})
        rows.append(
            f"- {s['name']} ({s['major']}, {s['year']}, {s['campus']}): "
            f"GPA {s['gpa']}, {s['credits_completed']}/{s['credits_required']} credits, "
            f"engagement {s['engagement_score']}, attendance {s['attendance_rate']}, "
            f"risk: {s['risk_level']}"
        )
    return "STUDENTS (fictitious):\n" + "\n".join(rows)


def _courses_brief() -> str:
    rows = []
    for c in COURSES.get("courses", []):
        prereqs = ", ".join(c.get("prerequisites", [])) or "none"
        rows.append(
            f"- {c['code']}: {c['title']} ({c['credits']}cr, {c['level']}), "
            f"prereqs: {prereqs}, seats: {c['seats_available']}/{c['seats_total']}, "
            f"{c['schedule']}, {c['modality']}"
        )
    return "COURSES (fictitious):\n" + "\n".join(rows)


def _research_brief(area_id: str | None = None) -> str:
    areas = RESEARCH.get("research_areas", [])
    if area_id:
        match = [a for a in areas if a["id"] == area_id]
        if match:
            areas = match
    rows = []
    for r in areas[:3]:
        methods = ", ".join(r["methods"][:3])
        rows.append(
            f"- {r['title']} ({r['school']}): PI {r['pi']}, "
            f"funding: {r['funding_status']}, {r['grad_students']} grad students, "
            f"methods: {methods}"
        )
    return "RESEARCH (fictitious):\n" + "\n".join(rows)


def _campus_brief() -> str:
    rows = []
    for f in CAMPUS.get("facilities", []):
        features = ", ".join(f.get("features", [])[:3])
        rows.append(
            f"- {f['name']} ({f['campus']}, {f['type']}): cap {f['capacity']}, "
            f"util {f['utilization_avg']:.0%}, {f['maintenance_status']}"
        )
    return "FACILITIES (fictitious):\n" + "\n".join(rows)


def _financial_brief() -> str:
    rows = []
    for p in FINANCIAL.get("aid_programs", []):
        rows.append(
            f"- {p['name']} ({p['type']}): {p['amount_range']}, "
            f"deadline: {p['deadline']}"
        )
    tuition = FINANCIAL.get("tuition_snapshot", {})
    rows.append(f"\nTuition: in-state {tuition.get('in_state_full_time', 'N/A')}, "
                f"out-of-state {tuition.get('out_of_state_full_time', 'N/A')}, "
                f"online {tuition.get('asu_online', 'N/A')}")
    return "FINANCIAL AID (fictitious):\n" + "\n".join(rows)


def _careers_brief() -> str:
    rows = []
    for i in CAREERS.get("industry_insights", []):
        employers = ", ".join(i.get("top_employers_at_asu", [])[:3])
        rows.append(
            f"- {i['field']}: demand {i['demand']}, avg start ${i['avg_starting_salary']}, "
            f"top employers: {employers}"
        )
    return "CAREER DATA (fictitious):\n" + "\n".join(rows)


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


@app.route("/api/data/research")
def api_data_research():
    return jsonify({"areas": RESEARCH.get("research_areas", [])})


@app.route("/api/samples")
def api_samples():
    return jsonify({"samples": []})


# ---------------------------------------------------------------------------
# Persona endpoints
# ---------------------------------------------------------------------------
def _user_text(req) -> str:
    data = req.get_json(force=True, silent=True) or {}
    return (data.get("message") or data.get("text") or "").strip()


@app.route("/api/advisor", methods=["POST"])
def api_advisor():
    user = _user_text(request)
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_students_brief()}\n\n{_courses_brief()}"
    return jsonify(_run_inference(ACADEMIC_ADVISOR, grounded, max_tokens=320, persona="advisor"))


@app.route("/api/research", methods=["POST"])
def api_research():
    data = request.get_json(force=True, silent=True) or {}
    user = (data.get("message") or data.get("text") or "").strip()
    area = data.get("area", "")
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_research_brief(area)}"
    return jsonify(_run_inference(RESEARCH_ASSISTANT, grounded, max_tokens=340, persona="research"))


@app.route("/api/success", methods=["POST"])
def api_success():
    user = _user_text(request)
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_students_brief()}"
    return jsonify(_run_inference(STUDENT_SUCCESS, grounded, max_tokens=320, persona="success"))


@app.route("/api/financial", methods=["POST"])
def api_financial():
    user = _user_text(request)
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_financial_brief()}\n\n{_students_brief()}"
    return jsonify(_run_inference(FINANCIAL_AID, grounded, max_tokens=340, persona="financial"))


@app.route("/api/career", methods=["POST"])
def api_career():
    user = _user_text(request)
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_careers_brief()}\n\n{_students_brief()}"
    return jsonify(_run_inference(CAREER_SERVICES, grounded, max_tokens=320, persona="career"))


@app.route("/api/campus", methods=["POST"])
def api_campus():
    user = _user_text(request)
    if not user:
        return jsonify({"error": "Empty message"}), 400
    grounded = f"{user}\n\n{_campus_brief()}"
    return jsonify(_run_inference(CAMPUS_OPERATIONS, grounded, max_tokens=320, persona="campus"))


# ---------------------------------------------------------------------------
# Voice / Vision endpoints
# ---------------------------------------------------------------------------
@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    if not VOICE_VISION_OK:
        return jsonify({"error": "Voice module not available (OpenVINO required)"}), 503
    audio = request.files.get("audio")
    if not audio:
        return jsonify({"error": "No audio file uploaded"}), 400
    try:
        wav_bytes = audio.read()
        result = voice_vision.transcribe_wav(wav_bytes)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/vision/campus", methods=["POST"])
def api_vision_campus():
    return _handle_vision(VISION_CAMPUS, "campus")


@app.route("/api/vision/student", methods=["POST"])
def api_vision_student():
    return _handle_vision(VISION_STUDENT, "student")


def _handle_vision(prompt: str, category: str):
    if not VOICE_VISION_OK:
        return jsonify({"error": "Vision module not available (OpenVINO required)"}), 503
    image_file = request.files.get("image")
    if image_file:
        image_bytes = image_file.read()
    else:
        data = request.get_json(force=True, silent=True) or {}
        sample_name = data.get("sample")
        if not sample_name:
            return jsonify({"error": "No image provided"}), 400
        return jsonify({"error": "Sample images not configured"}), 400
    extra = ""
    msg = request.form.get("message") or ""
    if msg:
        extra = f"\n\nAdditional notes from user: {msg}"
    try:
        result = voice_vision.analyze_image(image_bytes, prompt + extra, max_new_tokens=320)
        tokens = _estimate_tokens(prompt + extra) + _estimate_tokens(result.get("text", ""))
        result["tokens"] = tokens
        result["response"] = result.get("text", "")
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Health / Utility
# ---------------------------------------------------------------------------
@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "model": model_id, "foundry": foundry_ok})


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"\n  🏫 ASU NPU Showcase running at http://localhost:{SERVER_PORT}")
    app.run(host="0.0.0.0", port=SERVER_PORT, threaded=True)
