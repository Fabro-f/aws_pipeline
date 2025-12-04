"""Metrics and Feedback Loop for BionovaQ MCP Server."""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List
from collections import defaultdict


class MetricsLogger:
    def __init__(self, log_dir: str = "metrics"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.log_file = self.log_dir / "usage_log.json"
        self.stats = defaultdict(int)

    def log_tool_call(self, tool_name: str, session_uuid: str, success: bool, response_time_ms: float, error: str = None):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "tool_name": tool_name,
            "session_uuid": session_uuid,
            "success": success,
            "response_time_ms": response_time_ms,
            "error": error
        }
        
        with open(self.log_file, 'a') as f:
            f.write(json.dumps(entry) + '\n')
        
        self.stats[f"{tool_name}_calls"] += 1
        if success:
            self.stats[f"{tool_name}_success"] += 1
        else:
            self.stats[f"{tool_name}_errors"] += 1

    def get_statistics(self) -> Dict[str, Any]:
        return dict(self.stats)

    def generate_weekly_report(self) -> str:
        total_calls = sum(v for k, v in self.stats.items() if k.endswith('_calls'))
        total_errors = sum(v for k, v in self.stats.items() if k.endswith('_errors'))
        
        report = f"""Weekly Metrics Report
Generated: {datetime.now().isoformat()}

Total Tool Calls: {total_calls}
Total Errors: {total_errors}
Error Rate: {(total_errors/total_calls*100 if total_calls > 0 else 0):.2f}%

Top Tools:
{self._format_top_tools()}
"""
        return report

    def _format_top_tools(self) -> str:
        tool_calls = {k.replace('_calls', ''): v for k, v in self.stats.items() if k.endswith('_calls')}
        sorted_tools = sorted(tool_calls.items(), key=lambda x: x[1], reverse=True)[:10]
        return '\n'.join(f"  - {tool}: {count} calls" for tool, count in sorted_tools)


_metrics_logger = None


def get_metrics_logger() -> MetricsLogger:
    global _metrics_logger
    if _metrics_logger is None:
        _metrics_logger = MetricsLogger()
    return _metrics_logger
