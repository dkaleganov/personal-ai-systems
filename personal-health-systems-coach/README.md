# Personal Health Systems Coach

A plug-and-play system prompt template for building your own AI health and performance coach using **Claude Projects**, **ChatGPT Custom GPTs**, or any LLM platform that supports persistent context.

It also ships as an [Anthropic Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview) so Claude can help you adapt it interactively.

---

## What this is

Most people building AI health coaches paste a one-paragraph prompt and hope for the best. This is the opposite approach: an opinionated **architecture** that defines:

- **Operating identity** — what the coach is and is not (specifically: not a clinician)
- **Data hierarchy** — which sources to trust when they conflict (DEXA > scale; logged nutrition > wearable TDEE)
- **Eight interconnected systems** — energy balance, hormones, glucose, cardiovascular, sleep, recovery, body composition, stress
- **Source discipline** — explicit anti-hallucination rules for citations and credentials
- **Tiered example source library** — academic researchers, clinicians/communicators, practitioners — all clearly labeled
- **Personalization rules** — the source list is examples, not a curriculum
- **Failure modes** — common AI coaching failures pre-flagged
- **Clinical boundaries** — when to escalate to a real provider

The architecture is the point. The named sources are just examples.

## What this is NOT

- Medical advice or a substitute for clinical care
- A curriculum you must follow — every researcher and brand mentioned is an example, not an endorsement
- A universal solution — the research base it draws on is heavily male-skewed; women, older adults, adolescents, and people with chronic conditions should personalize the source library to match their physiology and life stage
- A guarantee that anything the resulting AI coach says is accurate — verify every claim against the linked primary sources

## Quick start

### Option A: Use it as a Claude Skill

If you're on a platform that supports Anthropic Skills (Claude Code, Claude Projects with skills enabled, Cowork, etc.):

1. Clone or download this repo
2. Install as a skill in your environment
3. In a conversation, ask Claude something like "help me build a personal health coach" — the skill will trigger and walk you through adapting the template

### Option B: Use the template directly (no skill needed)

1. Open [`assets/template.md`](assets/template.md)
2. Replace every `[BRACKETED_PLACEHOLDER]` with your own data
3. **Replace the example source library** with researchers, clinicians, and practitioners you actually trust for your goals
4. **Replace the example trainers section** with whoever's programming you actually run
5. Paste the result into:
   - **Claude Projects:** Project Instructions field at [claude.ai](https://claude.ai)
   - **ChatGPT Custom GPT:** Instructions field when configuring a custom GPT
   - **Any other LLM platform** with a system-prompt or persistent-context feature
6. Attach knowledge files: recent DEXA PDFs, bloodwork reports, supplement labels, training history

## Repo structure

```
personal-health-systems-coach/
├── README.md                  ← you are here
├── LICENSE                    ← MIT
├── SKILL.md                   ← Anthropic Skill definition
└── assets/
    └── template.md            ← the actual system prompt template
```

## Verification standard

Every named researcher, clinician, practitioner, brand, and product in the template was spot-checked against primary sources (institutional pages, peer-reviewed publications, official product pages) before inclusion. URLs and credentials can change — **verify before relying on any specific claim**. Commercial biographies, podcasts, social media, and Wikipedia are treated as weaker evidence than institutional or peer-reviewed sources. Where researchers have credible scientific criticism (Walker, Huberman, Lustig, Fung), the controversy is noted inline rather than hidden.

If you find an error, please open an issue or submit a PR.

## Limitations you should know about

- **Male-default bias:** Most of the source library's research base was conducted predominantly on men, often men in their 20s–40s. Women, older adults, and people outside that demographic should personalize the source library to match their physiology and life stage. The template includes a women's-physiology sub-section as a starting point.
- **Western/white population skew** in much of the underlying research
- **No guarantee of accuracy** — every claim should be verified before clinical use
- **Not a clinician replacement** — the resulting AI coach is for personal optimization, not medical decisions

## Acknowledgments

The architecture emerged from iterating on a personal AI health coach across multiple data streams (DEXA, bloodwork, CGM, wearables, nutrition logging) and identifying which patterns made the AI useful versus which made it actively misleading. The "tiered example source library" structure was refined through adversarial review by both Claude and ChatGPT to surface and correct hallucinations, source-type confusion, and scientific inaccuracies.

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, adapt it, share improvements.

## Contributing

Issues and PRs welcome. Two ground rules:

1. **No unverified claims.** Every named source must have a primary verification link. Commercial bios are weaker evidence than institutional pages.
2. **Honor the "examples not endorsements" framing.** Don't propose researchers as authoritative — propose them as illustrative examples readers can keep or replace.
