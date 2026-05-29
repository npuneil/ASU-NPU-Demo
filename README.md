# ASU — Empower 2026 · On-Device AI Showcase

A Copilot+ PC demo aligned to **Arizona State University's** published top
AI use cases for [Empower 2026](https://tech.asu.edu/events/empower_2026),
running entirely on-device via **Microsoft Foundry Local** and the NPU
(Qualcomm Snapdragon X / QNN or Intel Core Ultra / OpenVINO).

## The 8 ASU use cases

| Audience | Use case | What it does |
|----------|----------|--------------|
| 👩‍🎓 Students | 📑 **Syllabot** | Turn syllabi into interactive Q&A on due dates, grading, materials |
| 👩‍🎓 Students | 🧠 **Practice AI** | Quiz yourself on key concepts beyond the classroom |
| 👩‍🎓 Students | 🗂️ **Assignment Coach** | Break major projects into milestones and tie to real-world skills |
| 👩‍🎓 Students | ✍️ **Writing Guide** | Structured pre-submission feedback on clarity, organization, grammar, tone |
| 🧑‍💼 Staff   | 🎒 **Onboarding Assistant** | Help new hires ramp up — checklists, training, contacts |
| 🧑‍💼 Staff   | 💬 **Concierge AI** | One conversational interface for HR, IT, parking, library, registrar |
| 🧑‍💼 Staff   | 📚 **Knowledge Base AI** | Find departmental procedures, policies, exceptions instantly |
| 🧑‍💼 Staff   | 🌱 **Development Coach** | On-demand goal-setting, communication, and reflection support |

Plus a 9th tab — **⚡ Hybrid AI · M365 (mock)** — that simulates retrieval
from Outlook / Teams / SharePoint and composes the answer on-device. No
real M365 connection; mock data only.

## Quick Start

```bash
# 1. Install Foundry Local (Snapdragon X / Intel Core Ultra)
winget install Microsoft.FoundryLocal

# 2. Install Python deps
#    Snapdragon X (ARM64):
pip install -r requirements.txt
#    Intel Core Ultra (adds OpenVINO for the voice+vision path):
pip install -r requirements-intel.txt

# 3. Run
python app.py
# → http://localhost:5007
```

On Qualcomm Snapdragon X, the app uses CLI-based NPU inference
(`foundry model run --device NPU`). On Intel Core Ultra it uses the HTTP API.
OpenVINO is **optional** and only required for the Intel voice+vision
companion (`voice_vision.py`); the core 9-tab demo runs without it.

## Architecture

```
app.py                 — Flask app, Foundry Local discovery, all API routes
prompts/
  system_brand.py      — ASU brand voice + wrap()
  personas.py          — 8 use-case prompts + HYBRID_CONCIERGE
data/
  syllabi.json         — 3 sample syllabi (CSE 110, ENG 101, BIO 181)
  practice_topics.json — Quiz banks for 3 topics
  assignments.json     — 3 large projects with milestones
  writing_samples.json — 2 student drafts to critique
  onboarding_tracks.json — 30/60/90 plans for 2 staff roles
  concierge_kb.json    — Cross-functional "where do I…" directory
  kb_articles.json     — 5 sample policy/procedure articles
  dev_goals.json       — Sample employee goal sheet + reflection prompts
  m365_mock.json       — Fake Outlook / Teams / SharePoint cards
  _archive/            — Previous demo data (advising/research/etc.)
templates/index.html   — Single-page UI, 9 tabs grouped Students / Staff / Hybrid
static/
  css/asu.css          — ASU Maroon (#8C1D40) + Gold (#FFC627) branding
  js/app.js            — Frontend (tabs, voice, dropdowns, hybrid mock cards)
voice_vision.py        — Whisper STT + Phi-3.5-vision (kept for future)
```

## API surface

`/api/syllabot`, `/api/practice`, `/api/assignment`, `/api/writing`,
`/api/onboarding`, `/api/concierge`, `/api/kb`, `/api/dev`, `/api/hybrid`
— all POST `{message, …}` and return `{response, latency_ms, tokens, hardware, cloud_cost_saved}`.

`/api/data/*` returns dropdown metadata (courses, topics, assignments,
drafts, roles, KB articles, employees, hybrid scenarios).

## Disclaimers

- **All data is fictitious** — courses, syllabi, drafts, KB articles,
  policies, M365 cards are sample data for demonstration only.
- **Not affiliated with or endorsed by Arizona State University.**
- The Hybrid tab is a **mock** — no Microsoft Graph or M365 connection.
- AI outputs may be incorrect — verify all recommendations.
- Built on Microsoft Foundry Local + Qualcomm QNN / Intel OpenVINO.
