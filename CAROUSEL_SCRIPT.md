# Carousel Script — AI Profile Platform
## LinkedIn / Social media · 15 slides · Portrait (1080 × 1350) or Square (1080 × 1080)

---

### How to use this script
- Paste the **Headline** as the large bold text on each slide
- Paste the **Body** as the smaller supporting text
- Follow the **Screenshot / Visual** note to capture the right screen from the live app
- **Design tip**: Use a consistent dark background (e.g. #0f172a navy) with white/light text for slides 1–3 (hook), then switch to a lighter card style for slides 4–14, and back to dark for slide 15 (CTA)

---

## Slide 1 — HOOK (Why should you care?)
**Headline:**
> Your resume is static.
> Your story is not.

**Body:**
> What if anyone could have a real conversation with your career — at any time, without you being there?

**Screenshot / Visual:**
> None needed — pure text. Use a bold dark background with a single animated blinking cursor effect if possible. Or a split-screen: left = a plain PDF resume, right = a chat bubble answering a question.

---

## Slide 2 — THE PROBLEM
**Headline:**
> LinkedIn has your title.
> Not your thinking.

**Body:**
> Recruiters spend 30 seconds on a PDF. They miss everything that made you exceptional.

**Screenshot / Visual:**
> Mockup: a greyed-out resume with a red clock icon showing "30 sec". No app screenshot needed — pure concept.

---

## Slide 3 — THE SOLUTION (Platform intro)
**Headline:**
> Introducing AI Profile Platform

**Body:**
> A multi-tenant SaaS where professionals host a conversational AI twin — powered by their own documents, accessible to the world.

**Screenshot / Visual:**
> Screenshot of `/explore` page — the profile directory. Crop to show 2–3 profile cards. Caption overlay: "One platform. Many professionals."

---

## Slide 4 — WHO IS IT FOR? (Three roles)
**Headline:**
> Built for three types of people

**Body:**
> 👤 Professionals — publish your AI twin
> 🔍 Visitors — chat with any profile, no login
> 🛠 Admins — manage the whole platform

**Screenshot / Visual:**
> No screenshot — use three icon cards side by side with the labels above. Keep it clean.

---

## Slide 5 — VISITOR EXPERIENCE
**Headline:**
> Visitors just… ask.

**Body:**
> No login. No friction. Just: "What platforms did she build?" — and get a grounded answer from her actual documents.

**Screenshot / Visual:**
> Screenshot of `/chat/{slug}` showing a question being answered. Crop to show: the question bubble, the AI answer paragraph, and the 3 follow-up chips below. Blur or anonymise any personal details if needed.

---

## Slide 6 — HOW IT WORKS (RAG pipeline)
**Headline:**
> Every answer comes from real documents.

**Body:**
> Upload documents → AI splits them by topic → visitor asks → system retrieves the right sections → LLM generates a grounded answer.
> No hallucination. No guessing.

**Screenshot / Visual:**
> Screenshot of the admin/owner Documents tab showing uploaded files (resume.pdf, recommendations.txt, etc.) with their status badges. Caption: "Source of truth — always".

---

## Slide 7 — OWNER: SELF-SERVICE IN 2 MINUTES
**Headline:**
> Go live in under 2 minutes.

**Body:**
> 1. Sign in with Google
> 2. Upload your documents
> 3. Click Index
> 4. Enable your profile
> Done — your AI twin is live.

**Screenshot / Visual:**
> Screenshot of the Owner Dashboard. Show the sidebar nav and the main panel. Highlight the "Enable Profile" toggle if visible. Caption overlay: "Fully self-service."

---

## Slide 8 — OWNER: APPEARANCE & CAROUSEL
**Headline:**
> Make it yours.

**Body:**
> Customise your carousel slides, chat colours, header, and profile photo — all from a live preview panel.

**Screenshot / Visual:**
> Screenshot of owner Appearance page — show the Carousel Preview panel with a dark-themed slide visible, and the AI Theme Generator panel below it. This is a great visual slide.

---

## Slide 9 — AI THEME GENERATOR (New feature highlight)
**Headline:**
> ✦ AI picks your colours.

**Body:**
> Type a mood — "Professional, dark navy, executive" — hit Generate. AI creates a colour theme with guaranteed readable contrast. No design skills needed.

**Screenshot / Visual:**
> Close-up screenshot of the AI Theme Generator panel: mood input with a chip selected, the colour swatches showing bg/title/body/nav, and "Theme applied!" message. Make sure the carousel preview above it shows the applied theme.

---

## Slide 10 — OWNER: PERSONA & PROMPTS
**Headline:**
> Your AI twin speaks in your voice.

**Body:**
> Write a persona prompt that defines your AI twin's tone, scope, and style. The platform keeps it grounded — you control the personality.

**Screenshot / Visual:**
> Screenshot of the owner Prompts or AI tab — show the persona textarea with example text. Blur any real personal prompt text if preferred, or use a placeholder like "I am a senior engineer with 15 years experience…"

---

## Slide 11 — LEAD CAPTURE
**Headline:**
> Every conversation is a lead.

**Body:**
> When a visitor shares their email in chat, you're notified instantly via Pushover. Their details are logged — no CRM setup needed.

**Screenshot / Visual:**
> Two-panel split:
> Left: screenshot of a chat bubble where the AI says "Feel free to share your email and I'll make sure [name] follows up personally."
> Right: a phone mockup showing a Pushover notification: "New lead: visitor@email.com asked about your leadership experience."
> (You can mock the phone notification in Canva — no need for a real screenshot.)

---

## Slide 12 — ADMIN POWER
**Headline:**
> Full platform control for admins.

**Body:**
> Create profiles, trigger indexing, manage billing, tail live logs, monitor LLM token usage — all from a browser dashboard.

**Screenshot / Visual:**
> Screenshot of the Admin dashboard — show the sidebar with tabs (Registry, Manage, Logs, Billing, Tokens). If possible show the Logs tab with some live log lines visible. Caption: "Operational visibility — zero SSH."

---

## Slide 13 — TECH UNDER THE HOOD
**Headline:**
> Production-grade, from day one.

**Body:**
> FastAPI · ChromaDB · OpenRouter · Google OAuth
> HTMX · Tailwind CSS · HuggingFace Spaces
> Per-profile vector isolation. WCAG contrast enforcement. Atomic writes. CSRF protection.

**Screenshot / Visual:**
> No screenshot — use a dark code-style background with the tech stack listed as badge icons (reuse the shield badges from the README). Or a simple grid of logos.

---

## Slide 14 — DEPLOYMENT
**Headline:**
> Runs free on HuggingFace Spaces.

**Body:**
> Docker container. Persistent storage via HF Dataset sync. ChromaDB rebuilt on restart from synced documents. No cloud bill. No ops team.

**Screenshot / Visual:**
> Screenshot of the HuggingFace Spaces page for the live demo (https://arcshukla-ai-profile-platform.hf.space/) — show the Space card with the "Running" green badge. Caption: "Always on. Zero infrastructure cost."

---

## Slide 15 — CTA
**Headline:**
> Your career story deserves a conversation.

**Body:**
> Try the live demo ↓
> arcshukla-ai-profile-platform.hf.space
>
> Built with FastAPI · ChromaDB · OpenRouter · HTMX
> ⭐ Star on GitHub if this resonates

**Screenshot / Visual:**
> None — dark background, large URL, minimal. Add a QR code (generate from qr.io) pointing to the live demo URL. Optional: your profile photo bottom-right corner.

---

## Screenshots checklist (what to capture before starting)

| # | What to capture | URL / Location |
|---|---|---|
| A | Profile directory | `/explore` |
| B | Chat page — question + answer + followups | `/chat/{your-slug}` |
| C | Owner dashboard overview | `/owner/dashboard` |
| D | Documents tab with uploaded files | `/owner/documents` |
| E | Appearance page with carousel preview + AI Theme panel | `/owner/appearance` |
| F | AI Theme Generator close-up with swatches showing | `/owner/appearance` (scroll to theme section) |
| G | Owner Prompts/AI tab | `/owner/prompts` or `/owner/ai` |
| H | Admin dashboard with sidebar | `/admin` |
| I | Admin Logs tab with live log lines | `/admin/logs` |
| J | HuggingFace Spaces live page | External URL |

---

## Suggested tools to build the carousel

| Tool | Best for |
|---|---|
| **Canva** | Easiest — use a LinkedIn Carousel template, paste text, add screenshots |
| **ChatGPT (GPT-4o)** | Paste this script, ask it to write slide-by-slide Canva instructions or generate image prompts for DALL·E |
| **Figma** | Most control — if you want pixel-perfect design |
| **Beautiful.ai** | Auto-layouts slides from bullet points |
