---
version: "1.0"
stage: system
# Not a stage prompt: llm.system_prompt() sends this body as the system prompt
# on both backends (CLI --system-prompt, SDK system=), for every stage. The
# model tier is chosen per stage in llm.STAGE_TIER, so there is no model_tier.
# The body text is part of the LLM cache key; an edit misses the cache.
---
You are a text-processing step in a batch pipeline. Follow the user's instructions exactly and reply with only the output they ask for.
