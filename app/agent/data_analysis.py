from __future__ import annotations

"""
DataAnalysisAgent — Manus specialised for data exploration, statistics, and charting.

Inherits from Manus via the correct MRO:
  DataAnalysisAgent → Manus → ToolCallAgent → ReActAgent → BaseAgent

The only change from Manus is:
  • Custom system_prompt (data-analysis focus)
  • DataVisualization tool added to the standard toolkit
"""

from app.agent.manus import Manus
from app.tool.data_viz import DataVisualization


DATA_ANALYSIS_PROMPT = """\
You are a data analysis AI agent specialised in exploring datasets, computing
statistics, and producing charts. Use Python for computation and the data_viz
tool to generate charts. Save all outputs to workspace/.

Your process:
  1. Load and inspect the data (shape, types, nulls, sample rows).
  2. Compute descriptive statistics (mean, median, std, correlations, etc.).
  3. Identify patterns, anomalies, or insights.
  4. Produce at least one chart that visualises the most important finding.
  5. Write a plain-language summary (workspace/analysis_summary.md).
  6. Call terminate when all deliverables are saved.
"""


class DataAnalysisAgent(Manus):
    """
    Manus-derived agent with data-analysis focus.

    Uses super().__init__() to traverse the full MRO:
      DataAnalysisAgent.__init__
        → Manus.__init__           (sets mode, session_id)
          → ToolCallAgent.__init__ (creates LLM, selector, retry_policy)
            → ReActAgent.__init__  (nothing extra)
              → BaseAgent.__init__ (sets state, memory, gate, db, …)

    After the full chain runs, we extend the toolkit with DataVisualization.
    """

    name          = "data_analysis"
    system_prompt = DATA_ANALYSIS_PROMPT

    def __init__(self) -> None:
        # Correctly calls the full MRO — no manual attribute assignment
        super().__init__()

        # Add DataVisualization on top of Manus's standard toolkit
        self.tools.add(DataVisualization())

        # Rebuild the tool selector so it knows about the new tool
        from app.tool.selector import ToolSelector
        self._selector = ToolSelector(tool_names=list(self.tools._tools.keys()))
