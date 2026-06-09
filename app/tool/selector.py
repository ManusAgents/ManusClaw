from __future__ import annotations

"""
ToolSelector — confidence-based tool scoring for intelligent dispatch.

Given a natural-language goal, the selector computes a confidence score
(0.0–1.0) for every tool in the collection, explains its reasoning, and
returns an ordered recommendation list.

Two modes:
  1. Heuristic (zero-cost): keyword + recency + failure-rate signals
  2. LLM-powered (one extra call): the LLM scores tools against the goal

The selected tool name + reasoning is injected into the agent prompt so the
LLM knows WHY a particular tool was recommended, making its choice more
deliberate.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from app.logger import logger


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ToolScore:
    tool_name: str
    confidence: float          # 0.0 – 1.0
    reasoning: str
    matched_signals: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        bar_len = int(self.confidence * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        pct = f"{self.confidence * 100:.0f}%"
        signals = ", ".join(self.matched_signals[:4]) or "general capability"
        return (
            f"  [{bar}] {pct:>4s}  {self.tool_name:<22s}"
            f"  ← {self.reasoning[:80]}  (signals: {signals})"
        )


@dataclass
class SelectionResult:
    goal: str
    scores: list[ToolScore]    # Descending by confidence
    recommended: str           # Top tool name
    rationale: str             # One-sentence explanation for prompt injection

    def to_prompt_hint(self) -> str:
        """
        Returns a compact, formatted hint for injection into the LLM prompt.
        """
        top3 = self.scores[:3]
        lines = [
            "┌─ TOOL INTELLIGENCE LAYER ─────────────────────────────────────┐",
            f"│ Goal analysis: {self.goal[:60]:<60s} │",
            "│                                                               │",
            "│ Confidence scores (top tools for this step):                 │",
        ]
        for s in top3:
            bar_len = int(s.confidence * 20)
            bar = "█" * bar_len + "░" * (20 - bar_len)
            pct = f"{s.confidence * 100:.0f}%"
            name = f"{s.tool_name:<20s}"
            lines.append(f"│  [{bar}] {pct:>4s}  {name}                │")
        lines.append("│                                                               │")
        lines.append(f"│ ▶ Recommended: {self.recommended:<20s}                        │")
        lines.append(f"│   Reason: {self.rationale[:57]:<57s} │")
        lines.append("└───────────────────────────────────────────────────────────────┘")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Heuristic signal definitions
# ---------------------------------------------------------------------------

# Each entry: (tool_name, [(keyword_pattern, weight)])
# Weight > 0 boosts confidence; weight < 0 suppresses it.
_HEURISTIC_SIGNALS: dict[str, list[tuple[str, float]]] = {
    "python_execute": [
        (r"\b(python|code|script|compute|calculate|math|sort|parse|json|csv|dataframe|pandas|numpy|algorithm|function|class|import|pip)\b", 0.45),
        (r"\b(run|execute|test|benchmark|simulate|generate|process)\b", 0.15),
        (r"\b(browse|search|web|url|click|navigate)\b", -0.25),
    ],
    "bash": [
        (r"\b(shell|bash|terminal|command|ls|mkdir|cp|mv|rm|tar|zip|git|curl|wget|apt|pip|npm|chmod|env|path|file system|directory)\b", 0.50),
        (r"\b(install|build|compile|deploy|run|start|stop|kill|process|port)\b", 0.20),
        (r"\b(draw|chart|graph|plot|visuali)\b", -0.20),
    ],
    "str_replace_editor": [
        (r"\b(file|read|write|edit|create|view|open|save|content|text|code|config|yaml|toml|json|markdown|modify|append|replace|patch)\b", 0.40),
        (r"\b(look at|show me|display|inspect|check)\b", 0.15),
        (r"\b(browse|search|web|url)\b", -0.20),
    ],
    "browser_use": [
        (r"\b(browse|browser|navigate|click|website|webpage|url|http|scrape|screenshot|form|login|button|javascript|dom|playwright)\b", 0.55),
        (r"\b(search|find|look up)\b", 0.10),
        (r"\b(python|bash|file|code|script)\b", -0.15),
    ],
    "web_search": [
        (r"\b(search|find|look up|research|what is|who is|news|latest|current|price|weather|definition|wiki|information about|facts about)\b", 0.50),
        (r"\b(google|bing|ddg|duckduckgo|internet|online)\b", 0.20),
        (r"\b(file|edit|code|run|execute|install)\b", -0.20),
    ],
    "crawl": [
        (r"\b(crawl|extract|scrape|content|article|page|text from|read url|fetch url|html)\b", 0.50),
        (r"\b(url|http|website|webpage|link)\b", 0.20),
        (r"\b(click|interact|form|login)\b", -0.20),
    ],
    "data_viz": [
        (r"\b(chart|graph|plot|visuali|diagram|bar|line|pie|scatter|histogram|matplotlib|seaborn|bokeh|figure)\b", 0.60),
        (r"\b(data|numbers|csv|dataframe|trend|distribution)\b", 0.20),
        (r"\b(web|browse|search|file|bash)\b", -0.15),
    ],
    "ask_human": [
        (r"\b(unclear|ambiguous|missing information|need to know|what do you want|clarif|confirm|ask|human|user|permission|approval)\b", 0.55),
        (r"\b(unsure|uncertain|not sure|don't know|cannot determine)\b", 0.30),
    ],
    "planning": [
        (r"\b(plan|decompose|break down|steps|roadmap|organise|structure|sequence|milestone|todo|checklist)\b", 0.55),
        (r"\b(complex|multi-step|long|large|big|comprehensive)\b", 0.20),
    ],
    "terminate": [
        (r"\b(done|finished|complete|all steps|final|accomplished|task complete|nothing more|no more steps)\b", 0.70),
        (r"\b(stop|exit|quit|end|halt)\b", 0.30),
    ],
    "image_gen": [
        (r"\b(image|picture|photo|draw|generate image|create image|artwork|illustration|icon|logo|graphic|render)\b", 0.55),
        (r"\b(dalle|midjourney|stable diffusion|visual|design)\b", 0.25),
    ],
    "delegate": [
        (r"\b(delegate|subtask|sub-agent|sub agent|hand off|assign task|spawn agent|another agent)\b", 0.55),
        (r"\b(parallel|concurrent|multiple agents|team of agents)\b", 0.20),
    ],
    "memory": [
        (r"\b(remember|memory|note|store|recall|memo|save for later|persist|forget|update preference)\b", 0.55),
        (r"\b(USER\.md|MEMORY\.md|preferences|facts about|knowledge base)\b", 0.25),
    ],
    "skill_manager": [
        (r"\b(skill|capability|plugin|extension|install skill|load skill|list skills|available skills)\b", 0.55),
    ],
    "cross_session_search": [
        (r"\b(search session|find previous|past conversation|history search|previous task|old session|cross-session)\b", 0.55),
    ],
    "node_execute": [
        (r"\b(node|javascript|js|npm|typescript|ts|react|vue|express|deno)\b", 0.50),
        (r"\b(frontend|web app|bundle|webpack|vite|package\.json)\b", 0.20),
    ],
    "platform_control": [
        (r"\b(github|vercel|netlify|wordpress|deploy|ci/cd|release|huggingface|discord bot|telegram bot)\b", 0.55),
        (r"\b(api endpoint|rest api|platform|cloud service|webhook)\b", 0.20),
    ],
}


# ---------------------------------------------------------------------------
# ToolSelector
# ---------------------------------------------------------------------------

class ToolSelector:
    """
    Scores all available tools against a goal string using heuristic signals
    and optionally an LLM call. Returns an ordered SelectionResult.
    """

    def __init__(self, tool_names: list[str]) -> None:
        self._tool_names = tool_names
        # Track per-tool failure counts for adaptive penalty
        self._failure_counts: dict[str, int] = {}
        # Track recently used tools (recency penalty to diversify selection)
        self._recent_uses: list[str] = []   # Most-recent last
        self._recency_window = 5

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(self, goal: str, recently_failed: Optional[list[str]] = None) -> SelectionResult:
        """
        Compute confidence scores for all tools and return a SelectionResult.

        Args:
            goal: The current sub-goal text (from the agent's reasoning).
            recently_failed: Tool names that failed in the last few steps.
        """
        goal_lower = goal.lower()
        recently_failed_set = set(recently_failed or [])
        scores: list[ToolScore] = []

        for name in self._tool_names:
            conf, signals = self._heuristic_score(name, goal_lower)

            # Failure penalty — suppresses tools that just failed
            if name in recently_failed_set:
                penalty = 0.30 * (1 + self._failure_counts.get(name, 0) * 0.15)
                conf = max(0.0, conf - penalty)
                signals.append(f"failure-penalty(-{penalty:.2f})")

            # Recency penalty — gently penalise the most-recently-used tool
            # to encourage trying alternatives when stuck
            if self._recent_uses and self._recent_uses[-1] == name:
                conf = max(0.0, conf - 0.10)
                signals.append("recency-penalty(-0.10)")

            conf = round(min(1.0, max(0.0, conf)), 3)
            reasoning = self._build_reasoning(name, conf, signals)
            scores.append(ToolScore(
                tool_name=name,
                confidence=conf,
                reasoning=reasoning,
                matched_signals=signals,
            ))

        scores.sort(key=lambda s: s.confidence, reverse=True)
        top = scores[0]

        result = SelectionResult(
            goal=goal,
            scores=scores,
            recommended=top.tool_name,
            rationale=top.reasoning,
        )

        logger.debug(
            f"[ToolSelector] Goal: {goal[:60]}\n"
            + "\n".join(str(s) for s in scores[:5])
        )
        return result

    def record_use(self, tool_name: str) -> None:
        self._recent_uses.append(tool_name)
        if len(self._recent_uses) > self._recency_window:
            self._recent_uses.pop(0)

    def record_failure(self, tool_name: str) -> None:
        self._failure_counts[tool_name] = self._failure_counts.get(tool_name, 0) + 1

    def record_success(self, tool_name: str) -> None:
        # Success resets the failure count for that tool
        self._failure_counts.pop(tool_name, None)

    # ------------------------------------------------------------------
    # Heuristic scoring
    # ------------------------------------------------------------------

    def _heuristic_score(self, tool_name: str, goal_lower: str) -> tuple[float, list[str]]:
        signals_str: list[str] = []
        base = 0.10  # Every tool gets a small baseline

        patterns = _HEURISTIC_SIGNALS.get(tool_name, [])
        for pattern, weight in patterns:
            if re.search(pattern, goal_lower, re.IGNORECASE):
                base += weight
                # Extract what matched for the signal label
                m = re.search(pattern, goal_lower, re.IGNORECASE)
                if m:
                    signals_str.append(f"{m.group(0).strip()[:20]}({weight:+.2f})")

        return base, signals_str

    def _build_reasoning(self, tool_name: str, conf: float, signals: list[str]) -> str:
        if conf >= 0.7:
            strength = "strongly recommended"
        elif conf >= 0.45:
            strength = "good fit"
        elif conf >= 0.25:
            strength = "possible option"
        else:
            strength = "low relevance"

        sig_str = ", ".join(signals[:3]) if signals else "no strong signals"
        return f"{tool_name} is a {strength} for this step (conf={conf:.2f}, signals: {sig_str})"


# ---------------------------------------------------------------------------
# LLM-powered scoring (optional, one extra LLM call)
# ---------------------------------------------------------------------------

async def llm_score_tools(
    goal: str,
    tool_names: list[str],
    tool_descriptions: dict[str, str],
    llm,  # app.llm.llm.LLM instance
) -> SelectionResult:
    """
    Ask the LLM to score tools 0-100 against a goal. Falls back to
    heuristic scoring if the LLM call fails or returns malformed JSON.
    """
    from app.schema import Message
    import json

    tool_list = "\n".join(
        f"  - {name}: {tool_descriptions.get(name, 'No description')[:100]}"
        for name in tool_names
    )

    prompt = f"""\
Goal: {goal}

Available tools:
{tool_list}

Score each tool from 0-100 for how well it fits this goal.
Respond ONLY in this JSON format (no extra text):
{{
  "scores": [
    {{"tool": "tool_name", "score": 85, "reason": "one sentence"}},
    ...
  ]
}}
"""
    try:
        from app.schema import Message
        response = await llm.ask([
            Message.system(
                "You are a tool selection expert. Given a goal, score each tool by "
                "how appropriate it is. Be precise and justify your scores."
            ),
            Message.user(prompt),
        ])
        raw = (response.content or "{}").strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:-1])
        data = json.loads(raw)

        scores: list[ToolScore] = []
        for entry in data.get("scores", []):
            conf = round(entry["score"] / 100.0, 3)
            scores.append(ToolScore(
                tool_name=entry["tool"],
                confidence=conf,
                reasoning=entry.get("reason", ""),
                matched_signals=["llm-scored"],
            ))

        scores.sort(key=lambda s: s.confidence, reverse=True)
        top = scores[0] if scores else ToolScore(tool_name=tool_names[0], confidence=0.5, reasoning="fallback")

        return SelectionResult(
            goal=goal,
            scores=scores,
            recommended=top.tool_name,
            rationale=top.reasoning,
        )

    except Exception as e:
        logger.warning(f"[ToolSelector] LLM scoring failed ({e}), falling back to heuristic.")
        selector = ToolSelector(tool_names)
        return selector.score(goal)
