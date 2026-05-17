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

### Step 2: Capture their context

Ask the user a small number of focused questions (use the `ask_user_input_v0` tool if it's available; otherwise ask inline). The minimum context you need:

- Sex, age, height, current weight, approximate body fat (DEXA-measured if known)
- Primary goal (body recomp, fat loss, muscle gain, longevity, athletic performance, health maintenance)
- Their actual data stack (which wearables, scale, bloodwork provider, CGM, nutrition app, DEXA provider)
- Any current medical workup or condition that should constrain advice
- Their target LLM platform (Claude Projects, ChatGPT Custom GPT, or other)

Don't ask for everything at once. Three to five focused questions, then iterate.

### Step 3: Help them adapt the source library — do not impose the defaults

The single most important instruction in this skill: **the template's source library is examples, not a curriculum.** When you help a user adapt the template, your default behavior should be to ask them which sources they actually trust and follow, not to pre-fill the example sources.

Specifically:
- If the user is a woman, point them to the women's physiology sub-section and ask which of those (or other female-physiology specialists) they want to keep, plus any they want to add.
- If the user has never heard of any of the Tier A researchers, that's fine — ask who they do follow for evidence-based fitness, nutrition, sleep, or longevity content. Add those instead.
- Honor the "Example Source Library — Replace With Your Own" framing. Treat the template's named sources as illustrations of the format, not as a recommended list.

### Step 4: Honor the verification standard

The template was built under a zero-hallucination standard. If you add a source not already in the template, you must:
- Verify the person exists and the URL resolves (use web_search if available)
- Note the source type (academic researcher, clinician, practitioner, commercial educator, podcaster)
- Add any major scientific criticism inline (don't hide controversies)
- Use the same format as existing entries (name, credential, what they're best for, primary verification link)

If the user names a source and you can't verify them quickly, tell the user directly: "I'd want to verify their credentials before adding them with a citation — can you point me to their institutional page or main publication record?" Do not invent credentials, affiliations, or URLs.

### Step 5: Honor the medical boundary

The template includes explicit clinical-boundary language. When helping the user fill in supplement, hormone, or medication-adjacent placeholders, do not provide medical advice. Frame these as "what to discuss with your clinician" rather than recommendations. If the user appears to be using the prompt for self-treatment of a diagnosed condition, flag that the template is not a substitute for clinical care.

### Step 6: Deliver the customized template

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
