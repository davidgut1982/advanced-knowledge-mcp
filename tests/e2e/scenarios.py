"""Ground-truth scenario fixtures for the auto-memory extraction eval.

Each scenario is a :class:`Scenario` carrying:

* ``name`` — stable identifier (also the eval CLI key, e.g. ``A`` → ``progressive_personal``).
* ``turns`` — the conversation, a list of ``{"role": "user"|"assistant", "content": str}``.
* ``must_contain`` — expectations the extraction *should* satisfy. Each is a
  ``{"type": <MemoryType value>, "contains": <substring>}`` (or a list of
  alternative dicts, any of which satisfies the expectation — used where the
  model could reasonably phrase the same fact two ways, e.g. "Kubernetes"/"k8s").
  An expectation dict may use these optional keys in place of the basics:

  * ``contains_any`` — a list of substrings; the expectation is satisfied if
    *any* of them appear in the extracted content (case-insensitive).
  * ``type_any`` — a list of acceptable type labels; the entry type must match
    one of them (in addition to the content match).

  Type matching is *flexible*: if content matches but the type label differs,
  the expectation is still counted as satisfied (a warning is emitted). Type
  labels are therefore advisory, not gating.
* ``must_not_contain`` — content substrings whose presence in *any* extracted
  memory signals a hallucination / garbage extraction (questions, filler, raw code).
* ``max_extracted`` — optional cardinality ceiling (precision gate for noise).

These fixtures are shared by both the offline golden pytest suite
(``test_extraction_e2e_offline.py``) and the live eval harness
(``eval_extraction.py``). Keeping them in one place means the golden LLM
responses and the real-API runs are scored against identical ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A single must_contain entry is either one expectation dict or a list of
# alternative dicts (any match satisfies it). An expectation dict maps string
# keys (``type``, ``contains``, ``contains_any``, ``type_any``) to either a
# string or a list of strings.
Expectation = dict[str, "str | list[str]"]
ContainsAny = list[Expectation]


@dataclass(frozen=True)
class Scenario:
    """A conversation plus its extraction ground truth."""

    name: str
    turns: list[dict[str, str]]
    must_contain: list[Expectation | ContainsAny] = field(default_factory=list)
    must_not_contain: list[Expectation] = field(default_factory=list)
    max_extracted: int | None = None


# ---------------------------------------------------------------------------
# Scenario A: progressive_personal
# ---------------------------------------------------------------------------
PROGRESSIVE_PERSONAL = Scenario(
    name="progressive_personal",
    turns=[
        {
            "role": "user",
            "content": "I've been working on my homelab this weekend. Running Proxmox on 3 nodes.",
        },
        {"role": "assistant", "content": "Nice, what workloads are you running?"},
        {
            "role": "user",
            "content": "Mostly Docker containers. Hermes lives there, and a few data services.",
        },
        {"role": "assistant", "content": "What's your storage setup?"},
        {
            "role": "user",
            "content": (
                "TrueNAS for bulk, local NVMe for fast stuff. I prefer NVMe for databases."
            ),
        },
        {"role": "assistant", "content": "Smart. What databases?"},
        {
            "role": "user",
            "content": (
                "PostgreSQL mostly. I'm a backend engineer by trade, Python/Go day to day."
            ),
        },
        {"role": "assistant", "content": "Makes sense. Any Kubernetes?"},
        {
            "role": "user",
            "content": ("Not yet — that's the goal for Q3. Migrate the stateful services to k8s."),
        },
        {"role": "assistant", "content": "What's blocking you?"},
        {
            "role": "user",
            "content": "Time mostly. Also need to figure out persistent volumes for Postgres.",
        },
        {
            "role": "assistant",
            "content": "PVCs with local-path-provisioner are a good starting point.",
        },
    ],
    must_contain=[
        {"type": "user_fact", "contains": "backend engineer"},
        {"type": "system_fact", "contains_any": ["Proxmox", "3 nodes", "homelab"]},
        {"type": "preference", "contains": "NVMe"},
        # The model may say either "Kubernetes" or "k8s" — accept either.
        [
            {"type": "goal", "contains": "Kubernetes"},
            {"type": "goal", "contains": "k8s"},
        ],
    ],
    must_not_contain=[
        {"contains": "what's"},  # questions extracted as facts
        {"contains": "Nice"},  # assistant filler
    ],
)


# ---------------------------------------------------------------------------
# Scenario B: research_architecture
# ---------------------------------------------------------------------------
RESEARCH_ARCHITECTURE = Scenario(
    name="research_architecture",
    turns=[
        {"role": "user", "content": "Explain how lore stores memories."},
        {
            "role": "assistant",
            "content": (
                "Lore uses PostgreSQL with pgvector for semantic search. Each KB entry has a "
                "vector embedding. Retrieval uses hybrid RRF scoring combining full-text "
                "tsvector and vector ANN."
            ),
        },
        {"role": "user", "content": "How many tools does the MCP server expose?"},
        {"role": "assistant", "content": "42 MCP tools as of 0.8.6."},
        {"role": "user", "content": "What's the scoring range for rrf_score?"},
        {
            "role": "assistant",
            "content": (
                "rrf_score ranges from roughly 0.0 to 0.3. It's not cosine — don't use 0.85 "
                "as a threshold."
            ),
        },
        {"role": "user", "content": "Got it. What topic does auto-extraction write to?"},
        {
            "role": "assistant",
            "content": (
                "It writes to the 'auto-memory' topic by default. Review mode sends to "
                "'auto-memory-pending'."
            ),
        },
        {"role": "user", "content": "Hermes is the AI assistant that uses Lore?"},
        {
            "role": "assistant",
            "content": (
                "Yes — Hermes is the main consumer. The lore plugin integrates via session hooks."
            ),
        },
    ],
    must_contain=[
        {"type": "system_fact", "contains": "pgvector"},
        {"type": "system_fact", "contains_any": ["rrf_score", "RRF", "rrf"]},
        {"type": "relationship", "contains": "Hermes"},
    ],
    must_not_contain=[
        {"contains": "Got it"},
        {"contains": "Explain how"},
    ],
)


# ---------------------------------------------------------------------------
# Scenario C: debug_session
# ---------------------------------------------------------------------------
DEBUG_SESSION = Scenario(
    name="debug_session",
    turns=[
        {
            "role": "user",
            "content": "I'm getting a timeout when kb_search returns more than 20 results.",
        },
        {"role": "assistant", "content": "Is it the pgvector index scan or the application layer?"},
        {
            "role": "user",
            "content": ("Looks like the vector index — EXPLAIN ANALYZE shows a sequential scan."),
        },
        {
            "role": "assistant",
            "content": "You need to lower the probes setting. Try `SET ivfflat.probes = 3;`.",
        },
        {"role": "user", "content": "That fixed it. Search is now under 50ms consistently."},
        {"role": "assistant", "content": "Good. For production, set it in postgresql.conf."},
        {
            "role": "user",
            "content": "Done. Also bumped the index to 200 lists while I was in there.",
        },
        {"role": "assistant", "content": "Smart. That should hold you for a while."},
    ],
    must_contain=[
        # The fix event — phrased around the timeout or kb_search. The model may
        # label this as an ``event`` or a ``user_fact``; accept either type.
        {
            "type_any": ["event", "user_fact", "system_fact"],
            "contains_any": ["timeout", "sequential scan", "kb_search", "vector index"],
        },
        # The root-cause / tuning system fact.
        [
            {"type": "system_fact", "contains": "ivfflat"},
            {"type": "system_fact", "contains": "probes"},
        ],
    ],
    must_not_contain=[
        {"contains": "Is it the"},
        {"contains": "Smart"},
    ],
)


# ---------------------------------------------------------------------------
# Scenario D: noise_only  (precision test — should extract ~nothing)
# ---------------------------------------------------------------------------
NOISE_ONLY = Scenario(
    name="noise_only",
    turns=[
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "Hello! How can I help?"},
        {"role": "user", "content": "can you help me"},
        {"role": "assistant", "content": "Of course, what do you need?"},
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "Sure."},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "You're welcome!"},
    ],
    must_contain=[],
    must_not_contain=[],
    max_extracted=1,  # near-zero precision gate; ideally 0
)


# ---------------------------------------------------------------------------
# Scenario E: code_heavy  (filter test — extract facts, not code)
# ---------------------------------------------------------------------------
CODE_HEAVY = Scenario(
    name="code_heavy",
    turns=[
        {
            "role": "user",
            "content": (
                "Here's my pgvector query: `SELECT id, content, embedding <=> $1 AS distance "
                "FROM kb WHERE topic = $2 ORDER BY distance LIMIT 10;`"
            ),
        },
        {
            "role": "assistant",
            "content": "That looks right. Add `WHERE distance < 0.5` if you want a threshold.",
        },
        {
            "role": "user",
            "content": (
                "Like this? `SELECT id, content, embedding <=> $1 AS distance FROM kb WHERE "
                "topic = $2 AND distance < 0.5 ORDER BY distance LIMIT 10;`"
            ),
        },
        {
            "role": "assistant",
            "content": "Exactly. But 0.5 is cosine distance, so lower = more similar.",
        },
        {"role": "user", "content": "Got it. I use pgvector for all my semantic search needs."},
        {"role": "assistant", "content": "It's a solid choice for Postgres-native embeddings."},
    ],
    must_contain=[
        [
            {"type": "preference", "contains": "pgvector"},
            {"type": "system_fact", "contains": "pgvector"},
        ],
    ],
    must_not_contain=[
        {"contains": "SELECT"},  # raw SQL should not be extracted
        {"contains": "embedding <=>"},  # code fragments should not be extracted
    ],
)


# ---------------------------------------------------------------------------
# Scenario F: contradiction_update  (correct the corrected fact, not the error)
# ---------------------------------------------------------------------------
CONTRADICTION_UPDATE = Scenario(
    name="contradiction_update",
    turns=[
        {"role": "user", "content": "I'm running Kubernetes 1.27 on my cluster."},
        {"role": "assistant", "content": "Got it. What CNI are you using?"},
        {"role": "user", "content": "Flannel currently."},
        {"role": "assistant", "content": "Thinking of switching?"},
        {
            "role": "user",
            "content": (
                "Actually wait — I misspoke earlier. It's k3s, not full Kubernetes. Lightweight."
            ),
        },
        {"role": "assistant", "content": "Ah, k3s — much lighter for homelab use."},
        {
            "role": "user",
            "content": ("Yeah, 2 nodes. And I switched from Flannel to WireGuard mesh last month."),
        },
        {"role": "assistant", "content": "Nice upgrade — WireGuard is much faster."},
        {
            "role": "user",
            "content": "k3s is honestly great for a 2-node homelab. Wish I'd started with it.",
        },
        {
            "role": "assistant",
            "content": "Same story for a lot of people — k3s removes a lot of the ops burden.",
        },
    ],
    must_contain=[
        {
            "type_any": ["system_fact", "user_fact"],
            "contains_any": ["k3s", "lightweight", "k3", "2-node", "2 node", "homelab"],
        },
        {
            "type_any": ["event", "system_fact"],
            "contains_any": ["WireGuard", "Wireguard", "wireguard"],
        },
    ],
    must_not_contain=[
        {"contains": "Kubernetes 1.27"},  # the corrected error should NOT appear
    ],
    max_extracted=5,
)


# ---------------------------------------------------------------------------
# Scenario G: skip_rules_validation  (precision — skip filler, get the signal)
# ---------------------------------------------------------------------------
SKIP_RULES_VALIDATION = Scenario(
    name="skip_rules_validation",
    turns=[
        {"role": "user", "content": "I prefer that approach."},
        {"role": "assistant", "content": "Which approach do you mean?"},
        {"role": "user", "content": "You know, the one we discussed."},
        {"role": "assistant", "content": "I'm not sure which one — could you clarify?"},
        {"role": "user", "content": "I think maybe possibly we should try something."},
        {"role": "assistant", "content": "Sure, what specifically?"},
        {"role": "user", "content": "Never mind. I use vim for everything though."},
        {"role": "assistant", "content": "Classic. Terminal or with plugins?"},
        {"role": "user", "content": "Plugins — vim-plug. Python is my main language."},
        {"role": "assistant", "content": "Good choices."},
        {"role": "user", "content": "Yeah, that's it."},
        {"role": "assistant", "content": "Anything else?"},
        {"role": "user", "content": "I prefer that. Thanks."},
        {"role": "assistant", "content": "Of course."},
    ],
    must_contain=[
        {"type_any": ["user_fact", "preference"], "contains_any": ["vim", "Vim"]},
        {"type_any": ["user_fact", "preference"], "contains_any": ["Python", "python"]},
    ],
    must_not_contain=[
        {"contains": "prefer that"},  # unresolvable — SKIP
        {"contains": "maybe possibly"},  # hedged — SKIP
        {"contains": "something"},  # vague — SKIP
    ],
    max_extracted=4,
)


# ---------------------------------------------------------------------------
# Scenario H: relationship_org  (relationship type + people attribution)
# ---------------------------------------------------------------------------
RELATIONSHIP_ORG = Scenario(
    name="relationship_org",
    turns=[
        {"role": "user", "content": "I lead the platform team at my company."},
        {"role": "assistant", "content": "What does your team own?"},
        {
            "role": "user",
            "content": (
                "We own the internal developer platform — CI/CD, secrets management, "
                "and the Kubernetes cluster."
            ),
        },
        {"role": "assistant", "content": "How big is the team?"},
        {
            "role": "user",
            "content": (
                "Four engineers. Sarah handles the secrets vault, Miguel owns CI/CD pipelines."
            ),
        },
        {"role": "assistant", "content": "And you focus on Kubernetes?"},
        {"role": "user", "content": "Mostly, plus overall platform strategy."},
        {"role": "assistant", "content": "Who do you report to?"},
        {
            "role": "user",
            "content": "The VP of Engineering, Alex. She joined 6 months ago from Google.",
        },
    ],
    must_contain=[
        {
            "type_any": ["user_fact", "relationship"],
            "contains_any": ["platform team", "platform lead", "lead"],
        },
        {
            "type_any": ["user_fact", "relationship", "system_fact"],
            "contains_any": [
                "platform team",
                "developer platform",
                "CI/CD",
                "secrets",
                "Kubernetes cluster",
            ],
        },
    ],
    must_not_contain=[],
    max_extracted=14,
)


# ---------------------------------------------------------------------------
# Scenario I: dense_technical  (breadth — many facts across many types)
# ---------------------------------------------------------------------------
DENSE_TECHNICAL = Scenario(
    name="dense_technical",
    turns=[
        {
            "role": "user",
            "content": "Setting up a new dev environment. Running Arch Linux on a ThinkPad X1.",
        },
        {"role": "assistant", "content": "Nice hardware. What's your shell?"},
        {
            "role": "user",
            "content": "Fish shell with starship prompt. I switched from zsh a year ago.",
        },
        {"role": "assistant", "content": "What editor?"},
        {"role": "user", "content": "Neovim, configured with LazyVim. Everything in Lua now."},
        {"role": "assistant", "content": "Any language servers?"},
        {
            "role": "user",
            "content": "Pyright for Python, rust-analyzer for Rust. I do a lot of both.",
        },
        {"role": "assistant", "content": "How do you manage Python versions?"},
        {
            "role": "user",
            "content": (
                "pyenv plus uv for package management. uv is dramatically faster than pip."
            ),
        },
        {"role": "assistant", "content": "Containerization?"},
        {"role": "user", "content": "Docker for dev, Podman for production — rootless containers."},
        {"role": "assistant", "content": "How do you deploy?"},
        {
            "role": "user",
            "content": (
                "Ansible for infra provisioning, then Kamal for app deploys. No Terraform."
            ),
        },
    ],
    must_contain=[
        {
            "type_any": ["user_fact", "system_fact"],
            "contains_any": ["Arch", "ThinkPad", "Linux", "Fish", "fish"],
        },
        {
            "type_any": ["preference", "user_fact"],
            "contains_any": ["Neovim", "neovim", "LazyVim"],
        },
        {"type_any": ["preference", "user_fact", "system_fact"], "contains_any": ["uv", "pyenv"]},
        {
            "type_any": ["preference", "system_fact"],
            "contains_any": ["Ansible", "Kamal", "Podman", "Docker"],
        },
    ],
    must_not_contain=[],
    max_extracted=20,
)


# ---------------------------------------------------------------------------
# Scenario J: goal_refinement  (extract the refined goal, not the vague one)
# ---------------------------------------------------------------------------
GOAL_REFINEMENT = Scenario(
    name="goal_refinement",
    turns=[
        {"role": "user", "content": "I want to improve my infrastructure."},
        {"role": "assistant", "content": "What aspect specifically?"},
        {
            "role": "user",
            "content": "The observability stack. It's basically nonexistent right now.",
        },
        {"role": "assistant", "content": "Starting from scratch?"},
        {"role": "user", "content": "Pretty much. I want metrics, logs, and traces."},
        {"role": "assistant", "content": "Prometheus + Loki + Tempo?"},
        {
            "role": "user",
            "content": (
                "Exactly that stack. My goal is to have P95 latency dashboards for all "
                "services by end of month."
            ),
        },
        {"role": "assistant", "content": "That's a good milestone. What's the blocker?"},
        {"role": "user", "content": "Getting the Otel collectors deployed to all 8 services."},
        {
            "role": "assistant",
            "content": (
                "Start with the highest-traffic ones and expand. What's the highest traffic?"
            ),
        },
        {
            "role": "user",
            "content": "The API gateway. I'll instrument that first, then the auth service.",
        },
    ],
    must_contain=[
        {
            "type": "goal",
            "contains_any": ["P95", "latency", "dashboard", "observability", "metrics"],
        },
        {
            "type_any": ["system_fact", "goal", "event"],
            "contains_any": ["Prometheus", "Loki", "Tempo", "Otel", "OpenTelemetry"],
        },
    ],
    must_not_contain=[
        {"contains": "improve my infrastructure"},  # too vague — the refined version should win
    ],
    max_extracted=12,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
SCENARIOS: dict[str, Scenario] = {
    "A": PROGRESSIVE_PERSONAL,
    "B": RESEARCH_ARCHITECTURE,
    "C": DEBUG_SESSION,
    "D": NOISE_ONLY,
    "E": CODE_HEAVY,
    "F": CONTRADICTION_UPDATE,
    "G": SKIP_RULES_VALIDATION,
    "H": RELATIONSHIP_ORG,
    "I": DENSE_TECHNICAL,
    "J": GOAL_REFINEMENT,
}

# Convenience lookup by full name as well as letter key.
SCENARIOS_BY_NAME: dict[str, Scenario] = {s.name: s for s in SCENARIOS.values()}


def get_scenario(key: str) -> Scenario:
    """Resolve a scenario by letter key (``A``) or full name (``progressive_personal``)."""
    if key in SCENARIOS:
        return SCENARIOS[key]
    if key in SCENARIOS_BY_NAME:
        return SCENARIOS_BY_NAME[key]
    raise KeyError(f"Unknown scenario {key!r}; valid keys: {sorted(SCENARIOS)} or names")
