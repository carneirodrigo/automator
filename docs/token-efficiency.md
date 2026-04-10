# Token Efficiency Strategy

Read this when touching prompt assembly, serialization, context management, or adding new prompt sections. For architecture overview see engine/ORCHESTRATION.md.

## Token Efficiency Strategy

Prompt reduction in this system is deliberate and mostly **lossless or structurally bounded**. The goal is to reduce token cost without hiding important facts from the engine or agents.

Current techniques:

1. **TOON serialization for structured data:** `serialize_for_prompt()` renders JSON-like data in TOON format, which is typically 30-45% smaller than pretty JSON while preserving full structure.
2. **Prompt minification:** `minify_text()` strips markdown decoration, comments, code fences, and redundant whitespace from injected specs and docs.
3. **Section stripping:** `build_prompt()` removes spec sections that would otherwise be duplicated by separately injected schemas or capability references.
4. **Prefix-cache-friendly prompt order:** prompts are built with static content first (specs, schemas, capability reference) and dynamic context last so provider-side prompt prefix caching is reused across repeated calls.
5. **Condensed research handoff:** worker prompts suppress full research payloads and inject a compact handoff of the key findings instead.
6. **Skills selection:** the engine injects only research-selected downstream skill bodies, never the full skill cache.
7. **Input summarization and sampling:** directories are summarized, CSV/TSV inputs are sampled, and large text inputs are truncated with explicit markers instead of injected verbatim.
8. **Model-aware context budgeting:** `_effective_context_tokens()` reads the configured model from `config/backends.json` and returns a model-specific effective window (e.g. 120K for Claude 200K models, 600K for Gemini 1M models, 76K for GPT-4o). Compaction and recall-anchor thresholds scale with this value so the system stays within safe capacity across backends.
9. **Proactive compaction:** `_compact_prompt_sections()` drops low-priority agent sections when the prompt exceeds 70% of the effective window. This fires before hitting the hard 512KB ceiling so structure is preserved rather than truncated. Sections are dropped in ascending criticality order: repo fingerprint first, then project file listing, then condensed research summary. Higher-priority sections (agent spec, schemas, capability reference, task context) are never dropped.
10. **Recall anchors:** for prompts above 40% of the effective window, `build_prompt()` echoes the critical output-format constraint at the very end of the prompt. The U-shaped attention curve gives end positions 85-95% recall accuracy vs 10-40% less for middle content.
11. **Source priority clash mitigation:** when two or more of `{research handoff, agent skills}` are present in the same agent prompt, a priority header is injected before those sections establishing resolution order (research handoff > skills). This prevents contradictory retrieved documents from poisoning context silently.
12. **Per-stage prompt token visibility:** `stage_start_message()` includes a token estimate (`~N,NNN tokens`) for prompts above 1,000 tokens. Multi-agent pipelines cost approximately 15x a single-agent chat; per-stage visibility makes that cost observable without post-hoc analysis.
13. **Capability "when to use" contracts:** `_CAPABILITY_QUICK_REFERENCE` entries include explicit "use for X, NOT for Y" guidance for ambiguous pairs (`write_file` vs `persist_artifact`, `read_file` vs `load_artifact`). This prevents agents from selecting the wrong capability and wasting a round on an incorrect operation.
14. **Prompt-aware command output truncation:** `run_command` caps inline stdout/stderr at `CMD_OUTPUT_INLINE_LIMIT` (8 KB ≈ 2K tokens) instead of the raw `MAX_STAGE_OUTPUT_BYTES` ceiling. Output beyond the limit is replaced with a truncation note directing the agent to use `grep`/`tail`/`head` for targeted retrieval. Prevents large test suite or build logs from blowing the context window on the next capability round. This is non-lossy deferral, not destruction: the full output remains accessible via targeted follow-up capability requests.

15. **Artifact serialization:** `serialize_artifact_for_prompt()` is the single entry point for re-injecting artifact data into downstream prompts. All artifact data is serialized using lossless TOON encoding regardless of source role.

16. **Session resume via conversation ID:** when `session.persistent=True` and `session.conversation_id` is set after a prior run, the CLI backend receives the session/conversation ID so the provider can resume the thread. The full prompt is still sent — there is no abbreviated followup path — but the provider may use its own conversation history to reduce repeated context. API mode always receives the full prompt regardless of session state.

17. **Structured host capabilities replacing raw subprocess output:** the 9 structured capabilities (`query_git_status`, `query_git_diff`, `query_git_log`, `search_code`, `run_tests`, `list_dir`, `find_files`, `stat_file`, `read_file_lines`) return typed structured data instead of raw subprocess text. Where an agent would previously call `run_command(["git", "status"])` and receive verbose human-readable output, it now receives a parsed `{branch, staged[], unstaged[], untracked[]}` object serialized in TOON. Structured results are significantly more compact than raw CLI output and eliminate the model's token cost of parsing unstructured text.

18. **Agent rationalization guards:** each core agent spec includes a `## Common Rationalization Traps` section that names role-specific shortcuts that produce false passes. Preventing a single false pass avoids a full rework cycle. The guard text is injected into every agent prompt as part of the agent spec.

Non-goals:

- Do not use lossy free-text compression for requirements, logs, diffs, research facts, security findings, or validator evidence.
- Do not inject full caches (knowledge entries, skills, artifacts) when compact metadata or selected substructures are sufficient.
- Do not apply `minify_text` to code artifacts or file content read through capabilities — only to spec and schema documents.
- Do not add lossy compression to `serialize_artifact_for_prompt()` — all artifact re-injection must use lossless TOON.
