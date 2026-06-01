# Context & Retrieval: why the migration agent has no vector DB (in v1)

This doc exists because "everyone uses RAG" is not a reason. Here is exactly
what knowledge this agent needs, where it lives, how we fetch it, and the
specific future conditions under which embeddings + pgvector become justified.

## The one job RAG actually does

Retrieval-Augmented Generation solves a single problem:

> "I have **more relevant knowledge than fits in the context window**, and I
> **don't know in advance which pieces I'll need**, so I search by *meaning*
> (semantic similarity) and inject the top matches into the prompt."

Embeddings + a vector DB are the right tool **only when both halves are true**:
the corpus is too big to include wholesale, *and* relevance is fuzzy/semantic
rather than something you can compute exactly.

The DJ agent hits both: thousands of tracks, and "does this fit the vibe?" is
inherently fuzzy. The migration agent mostly does not. Here's the breakdown.

## The migration agent's four knowledge needs

### 1. Migration rules — "how do I convert X → Y?"
The breaking changes for Java→Kotlin, JUnit4→5, Spring 2→3 are a **bounded,
well-documented set** (tens to low-hundreds of rules). They map to **syntactic
patterns** the AST already identifies.

- **Where it lives:** `profiles/<name>/rules.md`, parsed into a structured index
  keyed by AST pattern (import path, annotation, type, call signature).
- **How we fetch:** deterministic lookup. tree-sitter says "this file imports
  `javax.persistence`" → we load the `javax→jakarta` rule. Exact, every time.
- **Why NOT embeddings:** for a *safety-critical, bounded* ruleset, fuzzy search
  that occasionally **misses** the rule for a breaking change is a defect. You
  want precision and auditability ("rule #37 fired on line 12"), not vibes.

### 2. Repo context — "what else does this file touch?"
To migrate a file correctly you need its neighbors: what it imports, what calls
it, what types it shares.

- **Where it lives:** the **tree-sitter dependency graph** (`depgraph.py`).
- **How we fetch:** graph traversal — exact import/call/type edges.
- **Why NOT embeddings:** the graph gives the *precise* answer. Semantic search
  would be a lossy approximation of a relationship you can compute exactly.
  (Also: dependency order = the topological sort of this same graph.)

### 3. Sibling code in a HUGE repo (> context window)
For a 200K-LOC monorepo, even the precise neighbor set may not fit, and some
relevant usages aren't direct graph edges (similar patterns elsewhere).

- **Status: the FIRST place semantic code search could supplement** — and only
  here. Even then, dep-graph neighbors come first; embeddings fill the gap.
- **Trigger to add it:** you actually hit context-window limits on a real repo.
  Don't build it speculatively.

### 4. Learning from past migrations — "we've done one like this before"
Store every **accepted** (before → after) patch. When migrating a new file,
retrieve the most *semantically similar* past success as a few-shot example.

- **Status: the strongest genuine vector-DB use case** — fuzzy similarity over a
  growing, open-ended corpus of exemplars. Makes the agent improve over time.
- **Trigger to add it:** Phase 5+, once you have a corpus of accepted patches
  worth learning from. This is a differentiator, not a foundation.

## Decision

| Layer | Phases 1–5 (current) | Phase 6+ (only if justified) |
|---|---|---|
| Rules | structured AST-keyed lookup (per-profile `keywords.toml`) | unchanged |
| Repo context | tree-sitter dependency graph | + semantic code search *if* repos exceed context |
| Cross-repo learning | — | pgvector library of accepted patch pairs (few-shot) |
| Infra needed | tree-sitter only | pgvector + Supabase + an embedding model |

**Phase 5 ships with zero embeddings/pgvector/Supabase.** This is not a shortcut —
for bounded, structured, safety-critical transformation knowledge, deterministic
retrieval is the *correct and more defensible* design.

Phase 5 added new profiles (Spring Boot 2→3, Maven variants) and a `scaffold-profile`
command that uses the LLM to generate `rules.md` and `keywords.toml` from migration
docs — but still no vector DB. Rules remain deterministic AST-keyed lookups.

## The reusable principle (say this on a panel)

> "Reach for a vector DB when relevance is fuzzy *and* the corpus won't fit in
> context. Migration rules are bounded and map to exact syntax, so I use precise
> AST-keyed lookup and a dependency graph — more auditable than retrieval. I add
> embeddings only for the two places they genuinely fit: oversized repos and a
> learn-from-past-migrations exemplar library."
