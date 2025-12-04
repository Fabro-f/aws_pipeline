"""Quick Reference Mode for BionovaQ MCP Server."""

from typing import Dict, Any, Optional, List
from dataclasses import dataclass
import re


@dataclass
class QuickResponse:
    summary: str
    key_steps: List[str]
    show_more_available: bool
    full_response: str


class QuickFormatter:
    def format_quick(self, full_response: str, response_type: str = "explanation") -> QuickResponse:
        summary = self._extract_summary(full_response)
        key_steps = self._extract_key_steps(full_response)
        return QuickResponse(
            summary=summary,
            key_steps=key_steps,
            show_more_available=True,
            full_response=full_response
        )

    def _extract_summary(self, content: str) -> str:
        clean = re.sub(r'\*\*|__|\#', '', content)
        sentences = clean.split('. ')
        if sentences:
            return sentences[0] + '.'
        return content[:200] + '...'

    def _extract_key_steps(self, content: str) -> List[str]:
        steps = []
        for line in content.split('\n'):
            if re.match(r'^\d+\.', line.strip()) or line.strip().startswith(('-', '*')):
                steps.append(line.strip())
                if len(steps) >= 5:
                    break
        return steps if steps else ["See full details for step-by-step guide"]


class QuickModeManager:
    def __init__(self):
        self.formatter = QuickFormatter()
        self.user_preferences: Dict[str, bool] = {}

    def should_use_quick_mode(self, session_uuid: str, explicit_mode: Optional[str] = None) -> bool:
        if explicit_mode:
            return explicit_mode.lower() == "quick"
        return self.user_preferences.get(session_uuid, False)

    def format_response(self, full_response: str, session_uuid: str = "", explicit_mode: Optional[str] = None, response_type: str = "explanation") -> str:
        if self.should_use_quick_mode(session_uuid, explicit_mode):
            quick = self.formatter.format_quick(full_response, response_type)
            return f"**Quick Reference:** {quick.summary}\n\n**Key Steps:**\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(quick.key_steps, 1)) + "\n\n[Use mode='full' for details]"
        return full_response


_quick_mode_manager = None


def get_quick_mode_manager() -> QuickModeManager:
    global _quick_mode_manager
    if _quick_mode_manager is None:
        _quick_mode_manager = QuickModeManager()
    return _quick_mode_manager
