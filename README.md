# ASU — On-Device AI NPU Showcase

A Copilot+ PC demo for **Arizona State University** higher education operations,
running entirely on-device via **Microsoft Foundry Local** and the NPU
(Qualcomm Snapdragon X / QNN or Intel Core Ultra / OpenVINO).

## Personas

| # | Persona | Description |
|---|---------|-------------|
| 1 | 📚 Academic Advisor | Course planning, degree progress, prerequisites, graduation |
| 2 | 🔬 Research Assistant | Grant proposals, literature review, IRB prep, methodology |
| 3 | 🎯 Student Success Coach | At-risk identification, retention, engagement, interventions |
| 4 | 💰 Financial Aid Advisor | FAFSA, scholarships, grants, loans, tuition planning |
| 5 | 💼 Career Services | Resume, internships, job market, professional development |
| 6 | 🏛️ Campus Operations | Facilities, scheduling, maintenance, sustainability |

Plus **Vision** tabs (campus facility + student engagement analysis) and
**Voice** input via Whisper on NPU.

## Quick Start

```bash
# 1. Install Foundry Local
winget install Microsoft.FoundryLocal

# 2. Install Python deps
pip install flask

# 3. Run
python app.py
# → http://localhost:5007
```

On Qualcomm Snapdragon X, the app automatically uses CLI-based NPU inference
(`foundry model run --device NPU`). On Intel Core Ultra, it uses the HTTP API.

## Architecture

```
app.py              — Flask app, Foundry Local discovery, all API routes
prompts/
  system_brand.py   — ASU brand voice + wrap() function
  personas.py       — 6 text personas + 2 vision prompts
data/
  students.json     — Fictitious student roster (6 students)
  courses.json      — Fictitious course catalog (10 courses)
  research.json     — Fictitious research areas (5 projects)
  campus.json       — Fictitious campus facilities (8 buildings)
  financial_aid.json — Aid programs + tuition snapshot
  careers.json      — Career resources + industry insights
templates/
  index.html        — Single-page UI with 10 tabs
static/
  css/asu.css       — ASU Maroon (#8C1D40) + Gold (#FFC627) branding
  js/app.js         — Frontend logic (tabs, forms, voice, vision, dashboard)
voice_vision.py     — Whisper STT + Phi-3.5-vision (OpenVINO)
```

## Disclaimers

- **All data is fictitious** — student names, courses, research, financial
  figures are sample data for demonstration only.
- **Not affiliated with or endorsed by Arizona State University.**
- AI outputs may be incorrect — verify all recommendations.
- Built on Microsoft Foundry Local + Qualcomm QNN / Intel OpenVINO.
