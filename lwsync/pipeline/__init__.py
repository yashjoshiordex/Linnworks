"""3PL pipeline (work in progress): checks AJC order files from Linnworks before they go to
the 3PL, and swaps / holds lines according to the substitution rules.

Flow (per region): Linnworks exports a CSV to new_orders/ -> the pipeline reads it, decides per
order -> writes the file the 3PL picks up to orders/ -> archives the input.

What is final and what is draft:
  csv_io.py, files.py, file_settings.py  file handling, built carefully; reused by the final pipeline
  config.py, rules.py                    EARLY DRAFT, superseded by the agreed design
                                         (3PL-Pipeline-Prompt.md); to be rewritten
"""

from lwsync.pipeline.config import PipelineConfig, SkuPairRule
from lwsync.pipeline.rules import RuleEngine, RuleDecision, PipelineAction

__all__ = [
    "PipelineConfig",
    "SkuPairRule",
    "RuleEngine",
    "RuleDecision",
    "PipelineAction",
]

