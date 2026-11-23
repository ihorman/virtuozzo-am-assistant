# DESIGN.md — widget design system & UX bar

The widget must look **premium** and feel **effortless**. Target: a developer
who has never deployed anything can deploy an app without reading docs.
Aesthetics: dark, cinematic, glass — Virtuozzo brand DNA, modern AI-product feel
(Linear / Vercel / Raycast class), never "enterprise admin panel".

## Color tokens (dark-first)

| Token | Value | Use |
|---|---|---|
| `--bg` | `#0B1220` | panel background (deep navy, near-black) |
| `--surface` | `rgba(255,255,255,0.05)` | glass cards, inputs |
| `--surface-2` | `rgba(255,255,255,0.09)` | hover, raised |
| `--border` | `rgba(255,255,255,0.10)` | 1px hairlines |
| `--accent` | `#3B82F6` | primary actions (Virtuozzo blue #234F95, lifted for dark bg) |
| `--accent-grad` | `linear-gradient(135deg,#234F95,#3B82F6)` | hero, send button, FAB |
| `--vz-red` | `#D71920` | destructive only (Virtuozzo red) |
| `--ok` | `#16A34A` | success |
| `--warn` | `#F97316` | warnings, in-progress accents |
| `--text` | `#E6EDF7` | primary text |
| `--muted` | `#8B9BB4` | secondary text, captions |

Light theme = same layout, `--bg #F7F9FC`, dark text; follow the dashboard's
theme or `prefers-color-scheme`.

## Type & shape

- UI font: **Inter** (fallback system-ui). Code/env names/values: **JetBrains
  Mono**.
- Radii: 14px panels/cards, 10px buttons/inputs, 999px pills/chips.
- Spacing scale: 4/8/12/16/24/32. Side panel width ~400px.
- Elevation: glass blur `backdrop-filter: blur(16px)`, hairline borders, one
  soft shadow layer. No heavy drop shadows, no bevels.

## Motion

- 150-250ms ease-out on all transitions; message entrance = fade + 6px slide.
- Typing indicator: three-dot wave. Long ops: indeterminate shimmer bar with
  elapsed seconds. Nothing bounces or spins forever without a label.

## Component inventory

1. **Context bar** (top): env pill (mono name + status dot: running=ok,
   sleeping=warn, stopped=muted), platform host, session indicator, settings.
2. **Empty state / hero**: gradient headline ("What should we deploy?"),
   3-4 suggestion chips ("Deploy my first app", "Add a database", "Make my app
   faster", "Explain my bill") — one click starts a guided flow.
3. **Messages**: user = right-aligned accent bubble; assistant = left glass
   card with markdown. Code/values get a **copy button**.
4. **Wizard card** (guided flow): step dots (1 Source, 2 Topology, 3 Size,
   4 Domain, 5 Review), current step highlighted, Back allowed, big primary
   CTA at review.
5. **ask_user chips**: option pills under the question; free-text input always
   active. One question per card. Never raw JSON.
6. **Confirm card**: plain-language recap ("Creates 1 environment with Node.js
   20 + PostgreSQL, ~$12/mo est."), [Confirm] primary + [Cancel] ghost;
   destructive variant = vz-red border + typed confirmation.
7. **Progress card**: operation label, shimmer bar, elapsed timer, cancel
   link. Survives page reloads (state on the agent).
8. **Success card**: big green check, what was created (mono values with copy
   buttons), action buttons: **Open app ↗**, **Open dashboard ↗**, **View
   logs**. This card is the "wow" moment — it must feel like a finished
   product, not a log dump.
9. **Error card**: never a dead end. The agent **diagnoses first** (reads
   logs, stats, env/node state around the failure), explains the cause in
   plain language, then offers **Fix it automatically** as the primary button
   (plus Retry / Open logs / Open docs as secondary). Auto-fix runs as a
   guarded repair flow: fix plan recap -> Confirm -> apply -> verify -> success
   card (or an honest "couldn't fix it, here is what I tried and what to do
   next"). No raw error codes without translation.
10. **Security card** (`kind: security`): shield header, findings list with
    severity pills (high=warn, medium=warn, low=muted), each finding with a
    one-click fix button. Appears as the last step of the deploy wizard and on
    demand ("check my environment security"). Secrets are always masked.
11. **Audit card** ("What I changed"): timeline of every mutation (time,
    action, target, result pill) — the compliance face of the assistant.

## UX rules (non-negotiable)

- Every mutating action: recap -> Confirm -> progress -> verified success card.
- The assistant asks ONE clarifying question at a time, with options.
- All values the user needs (domains, connection strings, passwords once)
  render in mono with copy buttons.
- No configuration needed to start: hosted default endpoint, session captured
  automatically; settings page is an escape hatch, not a requirement.
- Keyboard: Enter sends, Shift+Enter newline, Esc closes panels, Cmd/Ctrl+K
  quick-action palette (stretch).
- Empty/loading/error states designed for every view — no blank screens.
- Everything works at 360px panel width without horizontal scroll.
- Icons: consistent SVG line icons (Lucide/Phosphor class) in the shipped
  extension — emojis are acceptable in friendly chips but never mixed with
  vector icons in the same cluster.

## Verification bar before "done"

- Screenshot review of: empty state, mid-wizard, confirm card, progress card,
  success card, error card (light + dark).
- First-run test: a person with no context completes "deploy my first app"
  end-to-end using only on-screen affordances.
