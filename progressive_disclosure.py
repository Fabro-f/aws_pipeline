"""Progressive Disclosure for BionovaQ MCP Server."""

from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from enum import Enum


class DisclosureLevel(Enum):
    SUMMARY = "summary"
    DETAIL = "detail"
    DEEP_DIVE = "deep_dive"


@dataclass
class TieredResponse:
    summary: str
    details: str
    deep_dive: str
    current_level: DisclosureLevel
    navigation_options: List[str]


class ProgressiveDisclosureFormatter:
    def format_tiered_response(self, content: str, level: DisclosureLevel = DisclosureLevel.SUMMARY) -> TieredResponse:
        summary = self._extract_summary(content)
        details = self._extract_details(content)
        deep_dive = content
        
        navigation = []
        if level == DisclosureLevel.SUMMARY:
            navigation = ["[1] Show step details", "[2] Show full workflow", "[3] Technical deep dive"]
        elif level == DisclosureLevel.DETAIL:
            navigation = ["[Back] Summary", "[3] Technical deep dive"]
        else:
            navigation = ["[Back] Summary"]
        
        return TieredResponse(
            summary=summary,
            details=details,
            deep_dive=deep_dive,
            current_level=level,
            navigation_options=navigation
        )

    def _extract_summary(self, content: str) -> str:
        lines = content.split('\n')
        return lines[0] if lines else content[:200]

    def _extract_details(self, content: str) -> str:
        return content[:1000] if len(content) > 1000 else content


_disclosure_formatter = None


def get_disclosure_formatter() -> ProgressiveDisclosureFormatter:
    global _disclosure_formatter
    if _disclosure_formatter is None:
        _disclosure_formatter = ProgressiveDisclosureFormatter()
    return _disclosure_formatter
