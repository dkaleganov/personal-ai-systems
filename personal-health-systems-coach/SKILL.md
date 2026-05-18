---
name: personal-health-systems-coach
description: A scaffold for building a personalized AI health and performance coach using Claude Projects, ChatGPT Custom GPTs, or any LLM platform that supports persistent context. Use this skill whenever a user wants to set up an AI coach that synthesizes their wearable data, lab work, DEXA scans, CGM readings, and nutrition logs into systematic body composition, hormonal, metabolic, or longevity coaching. Trigger whenever the user mentions building a personal health coach, AI fitness coach, multi-source health data synthesis, evidence-based personalized health AI, or wants to combine data from wearables, DEXA, bloodwork, CGMs, and nutrition apps into a coherent coaching context. Also trigger when users mention Claude Projects, Custom GPTs, or any persistent-context LLM setup for personal health, fitness, body recomposition, or longevity use cases — even if they don't explicitly say "system prompt."
---

# Personal Health Systems Coach

A scaffold for building a personalized AI health and performance coach. The skill helps a user adapt the included template (`assets/template.md`) into their own working system prompt for Claude Projects, ChatGPT Custom GPTs, or any LLM platform that supports persistent context.

The template is opinionated about structure (data tiers, source discipline, failure modes, coaching hierarchy) but explicitly **not opinionated about who the user should follow** — the source library is examples, not endorsements. Your job when this skill triggers is to help the user adapt the template to their physiology, goals, data stack, and trusted sources — not to push the defaults.

## What this skill does

When triggered, the skill helps the user:
1. Understand the template's architecture (operating identity, data hierarchy, eight interconnected systems, source tiers, active protocol, failure modes)
2. Fill in personal data placeholders (demographics, current state, goals, macros, training, supplements)
3. **Build their own source library** rather than inheriting the example list
4. Configure data-source-specific calibration (e.g., wearable TDEE overestimate, DEXA-estimated RMR)
5. Define escalation rules for clinical territory
6. Set up the template in their chosen platform (Claude Projects, ChatGPT, etc.)

## How to use it with a user

### Step 1: Read the template first

Before doing anything else, read `assets/template.md` end-to-end. The template is the source of truth for structure, language, and the verification philosophy. Don't paraphrase or summarize sections from memory.

### Step 2: Check for an existing profile, then offer the intake — opt-in only

Before asking any context questions, do two things in order:

**2a. Check for an existing profile.** Look for a saved profile at the platform-appropriate location:
- **Claude Code:** `~/.claude/health-coach-profile.md` (or the project's working memory directory if more appropriate)
- **Claude Projects / ChatGPT GPTs:** any earlier intake content in the conversation history or attached knowledge files

If a profile is found, summarize what's in it in 3-5 lines and ask: "I found a profile from earlier. Want to (1) use it as-is, (2) edit specific fields, or (3) start a fresh intake?" Do not proceed without an explicit choice.

**2b. If no profile exists, present three choices.** Do NOT start asking intake questions automatically. Show the user this opt-in:

> I work better with context about you. Pick one:
>
> **(a) Run a 5-minute intake now** — ~8 focused questions covering demographics, goals, data stack, current state, medical context, training, influences, and platform. I'll save your answers to a profile so I don't have to ask again.
>
> **(b) Ask me as we go** — no upfront questions. I'll pull context inline only when I actually need it for a specific answer.
>
> **(c) Skip context entirely** — I'll produce a lightly personalized version of the template based only on what you tell me, no questions asked.

Wait for an explicit choice. If they pick **(b)** or **(c)**, skip Step 3 entirely and go straight to Step 4. If they pick **(a)**, proceed to Step 3.

### Step 3: Run the intake (only if the user chose option (a))

Ask one question at a time. After each answer, restate what you captured in one short line (e.g., "Got it — 38M, 180cm, ~178lb, ~18% BF per DEXA"), then move on. Do not dump all 8 at once. Do not invent answers or fill in plausible-sounding defaults; if the user doesn't know an answer, write "unknown" and move on.

The question list, in order:

1. **Basics** — sex, age, height, current weight (range is fine), body fat % if known (DEXA-measured if available)
2. **Primary goal** — body recomp / fat loss / muscle gain / longevity / athletic performance / health maintenance — with a measurable target if any
3. **Data stack** — which of these do you actually use today: wearable(s), smart scale (brand), CGM (which), nutrition app, bloodwork provider, DEXA provider + last scan date
4. **Current state** — anything affecting baseline right now (recent illness, injury, stress period, travel, hormonal workup, life-stage transition, medication changes)
5. **Medical context** — any diagnoses, medications, or active workups the coach should know about (you will NOT diagnose — context only; flag clinical boundaries as you go)
6. **Training** — current week shape (strength sessions, cardio modality and frequency, NEAT/steps target, non-negotiables)
7. **Influences** — whose programming or content do you currently follow (coaches, researchers, podcasters — "I don't know" is a valid answer)
8. **Platform & one personal failure mode** — where will this prompt live (Claude Project / ChatGPT GPT / API), and one pattern you want the coach to call out (e.g., rationalizing travel deficits, adding supplements when sleep is broken)

When all 8 are answered, write the captured profile as a markdown file to the platform-appropriate location (see 2a). Then summarize the profile in 4-6 lines and ask the user to confirm or edit before you proceed to Step 4. If they say "redo intake" later, restart from question 1 and overwrite the saved profile.

### Step 4: Help them adapt the source library — do not impose the defaults

The single most important instruction in this skill: **the template's source library is examples, not a curriculum.** When you help a user adapt the template, your default behavior should be to ask them which sources they actually trust and follow, not to pre-fill the example sources.

Specifically:
- If the user is a woman, point them to the women's physiology sub-section and ask which of those (or other female-physiology specialists) they want to keep, plus any they want to add.
- If the user has never heard of any of the Tier A researchers, that's fine — ask who they do follow for evidence-based fitness, nutrition, sleep, or longevity content. Add those instead.
- Honor the "Example Source Library — Replace With Your Own" framing. Treat the template's named sources as illustrations of the format, not as a recommended list.

### Step 5: Honor the verification standard

The template was built under a zero-hallucination standard. If you add a source not already in the template, you must:
- Verify the person exists and the URL resolves (use web_search if available)
- Note the source type (academic researcher, clinician, practitioner, commercial educator, podcaster)
- Add any major scientific criticism inline (don't hide controversies)
- Use the same format as existing entries (name, credential, what they're best for, primary verification link)

If the user names a source and you can't verify them quickly, tell the user directly: "I'd want to verify their credentials before adding them with a citation — can you point me to their institutional page or main publication record?" Do not invent credentials, affiliations, or URLs.

### Step 6: Honor the medical boundary

The template includes explicit clinical-boundary language. When helping the user fill in supplement, hormone, or medication-adjacent placeholders, do not provide medical advice. Frame these as "what to discuss with your clinician" rather than recommendations. If the user appears to be using the prompt for self-treatment of a diagnosed condition, flag that the template is not a substitute for clinical care.

### Step 7: Deliver the customized template

Once the user has filled in their personalization, produce the final, ready-to-paste system prompt. They can then drop it into Claude Projects (claude.ai), ChatGPT Custom GPT instructions, or any other LLM platform with persistent context.

## What this skill is NOT for

- Diagnosing health conditions or interpreting lab results
- Recommending specific supplements, medications, or hormone therapy
- Endorsing any particular researcher, brand, or product (the included examples are illustrative)
- Generating workout programs or meal plans directly (the resulting coach prompt can do that downstream)
- Generating content that requires verified citations Claude can't actually verify

## Output format

The final deliverable is a customized version of `assets/template.md` with the user's placeholders filled in. Preserve all structural sections — the architecture is what makes the prompt work. Replace bracketed `[PLACEHOLDERS]` with the user's actual data, and replace the example source library with the user's own trusted sources.

If the user wants both a personal version and a shareable/public version, produce two outputs: one with their actual data, and one that retains the template structure with their source library substitutions but keeps demographic and protocol placeholders generic.

## Reference

The template at `assets/template.md` is the canonical artifact this skill produces output from. Read it before producing anything. Do not summarize, paraphrase, or shortcut sections — copy structure faithfully and only modify the placeholders the user has filled in.
