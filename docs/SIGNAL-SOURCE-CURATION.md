# Signal Intelligence Source Curation

Source baseline: a private AI-source watchlist supplied out-of-band.

## Curation rules

- `core` — official accounts, known researchers/builders, or high-signal communities that can seed a digest item.
- `candidate` — useful weak-signal sources, but digest language must stay scoped and require corroboration.
- `quarantine` — noisy, obsolete, speculative, meme/jailbreak, or unrelated sources. They stay in the registry for audit visibility but are excluded from normal digest collection.
- Duplicate/near-duplicate communities are collapsed to the stronger source to avoid double-counting evidence.

## Reddit decisions

Promoted/added as core: AI model/product communities, coding-agent communities, and implementation frameworks:
`LocalLLaMA`, `machinelearning`, `AI_Agents`, `ClaudeCode`, `cursor`, `Cline`, `DeepSeek`, `ChatGPTPro`, `ClaudeAI`, `Anthropic`, `GeminiAI`, `GithubCopilot`, `LangChain`, `llamaIndex`, `comfyui`.

Kept as candidates: secondary agent/productivity/content-generation communities and broad product-idea sources such as `aiagents`, `AIAutomations`, `aiprojects`, `datascience`, `AIAssisted`, `aicuriosity`, `AISearchLab`, `HowToAIAgent`, `learnAIAgents`, `aivideo`, `elevenlabs`, `grok`, `kimi`, `labsdotgoogle`, `AIcodingProfessional`, `AugmentCodeAI`, `ChatGPTCoding`, `Codeium`, `codex`, `LLMDevs`, `ContextEngine`, `GPTStore`.

Quarantined: speculative/noisy/obsolete communities including `agi`, `AIDangers`, `ArtificialSentience`, `aiArt`, `aislop`, `dalle2`, `Bard`, `GPT3`, `gpt5`, `AIDankmemes`, `ChatGPTPromptGenius`, `GPT_Jailbreaks`.

Collapsed duplicates: `LocalLLM` → `LocalLLaMA`, `CursorAI` → `cursor`, `GoogleGeminiAI` → `GeminiAI`, `aivideos` → `aivideo`.

## X/Twitter decisions

Core allowlist: official AI/developer accounts, research leaders, and high-signal product/business sources: OpenAI/Google/DeepMind/Claude/Mistral/Qwen/Kimi, Google AI Studio/Labs/Flow/Stitch, Claude Code, Vercel, NVIDIA, Menlo Research, Alibaba Wan, Omar Sar, François Chollet, Demis Hassabis, Shane Legg, Clémentine Fourrier, Lance Martin, Boris Cherny, YC, Lenny, IdeaBrowser, Dan Shipper, Every.

Candidates: product builders, developer-tool accounts, AI-tool startups, indie/business accounts, and secondary news sources. These can surface ideas, but should not produce market-wide claims without corroboration.

Quarantined examples: bot/fandom/culture-war/unrelated handles such as `bellcurvebot`, `cb_doge`, `massgravel`, `reddit_lies`.

## Operating note

The legacy Telegram watchlist remains in the private workspace at
`workspace/self/skills/group-digest/references/GROUPS.md`. At runtime the
Signal Intelligence loader merges those Telegram sources into the registry so
the new pipeline does not drop previously monitored Telethon channels/chats.
Legacy `Job Market` Telegram categories are skipped by the old `group-digest`
cron and routed through the dedicated `Digest: Jobs` signal pipeline instead.

The biweekly source-quality audit should promote/demote/quarantine sources based on observed accepted-rate, duplicate-rate, contribution to clusters/digests, confidence, and fetch errors.
