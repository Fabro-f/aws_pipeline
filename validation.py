"""
Validation and error handling system for BionovaQ MCP Server.
Provides graceful handling of nonexistent data with helpful suggestions.

Features:
- Existence validation for cycles, materials, packages
- Fuzzy matching for typos and similar names
- Recent items suggestions
- Helpful error messages with actionable search tips
- Caching for performance optimization
"""

import time
import logging
from typing import Dict, Any, List, Tuple, Optional
from difflib import SequenceMatcher
from datetime import datetime

logger = logging.getLogger("bionovaq-validation")


class DataValidator:
    """Validates data existence and provides helpful error messages."""

    def __init__(self):
        """Initialize validator with cache."""
        self._cache = {}
        self._cache_ttl = 300  # 5 minutes in seconds

    def _get_cached(self, key: str) -> Optional[Any]:
        """Get cached value if not expired."""
        if key in self._cache:
            data, timestamp = self._cache[key]
            if time.time() - timestamp < self._cache_ttl:
                return data
            else:
                del self._cache[key]
        return None

    def _set_cached(self, key: str, value: Any) -> None:
        """Store value in cache with timestamp."""
        self._cache[key] = (value, time.time())

    def validate_cycle_exists(self, cycle_number: int, charges: List[Dict]) -> Dict[str, Any]:
        """
        Validate if cycle exists and provide helpful alternatives if not.

        Args:
            cycle_number: Cycle number to find
            charges: List of charge dictionaries from API

        Returns:
            {
                "exists": bool,
                "data": Dict if exists,
                "error_message": str if not exists,
                "suggestions": Dict with alternatives
            }
        """
        if not charges:
            return {
                "exists": False,
                "error_message": f"No charges found in system",
                "suggestions": {
                    "recent_cycles": [],
                    "search_tips": [
                        "Check if you have permission to view charges",
                        "Verify your session is active",
                        "Try searching by date range"
                    ]
                }
            }

        # Search for exact match
        for charge in charges:
            if charge.get("cycleNumber") == cycle_number or charge.get("number") == cycle_number:
                return {
                    "exists": True,
                    "data": charge
                }

        # Not found - generate helpful suggestions
        recent_cycles = self._get_recent_cycles(charges, limit=5)
        similar_cycles = self._find_similar_cycle_numbers(cycle_number, charges)

        error_msg = self._format_cycle_not_found_message(
            cycle_number,
            recent_cycles,
            similar_cycles
        )

        return {
            "exists": False,
            "error_message": error_msg,
            "suggestions": {
                "recent_cycles": recent_cycles,
                "similar_cycles": similar_cycles,
                "search_tips": [
                    "Check spelling of cycle number",
                    "Try searching by date: use date_from and date_to parameters",
                    "Check if it's a washing cycle instead: use get_washing_charge_list()",
                    "Verify cycle status with status parameter"
                ]
            }
        }

    def validate_material_exists(self, material_name: str, materials: List[Dict]) -> Dict[str, Any]:
        """
        Validate material existence with fuzzy matching suggestions.

        Args:
            material_name: Material name to find
            materials: List of material dictionaries from API

        Returns:
            {
                "exists": bool,
                "data": Dict if exists,
                "error_message": str if not exists,
                "suggestions": Dict with alternatives
            }
        """
        if not materials:
            return {
                "exists": False,
                "error_message": f"No materials found in system",
                "suggestions": {
                    "recent_materials": [],
                    "search_tips": [
                        "Check if materials exist in the system",
                        "Verify your session is active",
                        "Try listing all materials without filters"
                    ]
                }
            }

        # Exact match (case-insensitive)
        material_name_lower = material_name.lower().strip()
        for material in materials:
            name = material.get("name", "")
            if name.lower().strip() == material_name_lower:
                return {
                    "exists": True,
                    "data": material
                }

        # Not found - fuzzy match and suggestions
        material_names = [m.get("name", "") for m in materials if m.get("name")]
        fuzzy_matches = self.fuzzy_match(material_name, material_names, threshold=0.6)
        recent_materials = self.get_recent_items("material", materials, limit=5)

        error_msg = self._format_material_not_found_message(
            material_name,
            fuzzy_matches,
            recent_materials
        )

        return {
            "exists": False,
            "error_message": error_msg,
            "suggestions": {
                "fuzzy_matches": fuzzy_matches,
                "recent_materials": recent_materials,
                "search_tips": [
                    "Check spelling and accents",
                    "Try partial name search: search parameter supports wildcards",
                    "List all materials: call get_material_list() without filters",
                    "Search by material type or sterilization method"
                ]
            }
        }

    def validate_package_exists(self, package_id: str, packages: List[Dict]) -> Dict[str, Any]:
        """
        Validate package existence with suggestions.

        Args:
            package_id: Package ID to find
            packages: List of package dictionaries from API

        Returns:
            {
                "exists": bool,
                "data": Dict if exists,
                "error_message": str if not exists,
                "suggestions": Dict with alternatives
            }
        """
        if not packages:
            return {
                "exists": False,
                "error_message": f"No packages found in system",
                "suggestions": {
                    "recent_packages": [],
                    "search_tips": [
                        "Check if packages exist in the system",
                        "Verify your session is active",
                        "Try listing all packages without filters"
                    ]
                }
            }

        # Search by ID or number
        package_id_str = str(package_id).strip()
        for package in packages:
            pkg_id = str(package.get("id", ""))
            pkg_number = str(package.get("number", ""))

            if pkg_id == package_id_str or pkg_number == package_id_str:
                return {
                    "exists": True,
                    "data": package
                }

        # Not found - generate suggestions
        recent_packages = self.get_recent_items("package", packages, limit=5)

        error_msg = self._format_package_not_found_message(
            package_id,
            recent_packages
        )

        return {
            "exists": False,
            "error_message": error_msg,
            "suggestions": {
                "recent_packages": recent_packages,
                "search_tips": [
                    "Check package ID format",
                    "Search by status: use status parameter (stored, dispatched, etc.)",
                    "Search by cycle number: use cycle_number parameter",
                    "Try date range: use date_from and date_to parameters"
                ]
            }
        }

    def validate_status_value(self, status: str, valid_statuses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Validate status value and suggest valid options.

        Args:
            status: Status value to validate
            valid_statuses: List of valid status dictionaries with id and name

        Returns:
            {
                "is_valid": bool,
                "error_message": str if invalid,
                "valid_values": List of valid status options
            }
        """
        if not valid_statuses:
            return {
                "is_valid": False,
                "error_message": "No status values available",
                "valid_values": []
            }

        # Check if status matches any valid status (by ID or name)
        status_str = str(status).lower().strip()
        for valid_status in valid_statuses:
            status_id = str(valid_status.get("id", "")).lower()
            status_name = valid_status.get("name", "").lower()

            if status_str in [status_id, status_name]:
                return {
                    "is_valid": True,
                    "matched_status": valid_status
                }

        # Invalid - provide list of valid values
        error_msg = self._format_invalid_status_message(status, valid_statuses)

        return {
            "is_valid": False,
            "error_message": error_msg,
            "valid_values": valid_statuses
        }

    def get_recent_items(self, item_type: str, data: List[Dict], limit: int = 5) -> List[Dict]:
        """
        Get recent items for suggestions.

        Args:
            item_type: Type of item (material, package, charge, etc.)
            data: List of item dictionaries
            limit: Maximum number of items to return

        Returns:
            List of recent items with relevant details
        """
        if not data:
            return []

        # Check cache first
        cache_key = f"recent_{item_type}_{len(data)}_{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        # Sort by date (most recent first)
        sorted_data = sorted(
            data,
            key=lambda x: self._extract_date(x),
            reverse=True
        )

        # Get top items
        recent_items = []
        for item in sorted_data[:limit]:
            formatted = self._format_item_summary(item_type, item)
            if formatted:
                recent_items.append(formatted)

        # Cache result
        self._set_cached(cache_key, recent_items)

        return recent_items

    def fuzzy_match(self, query: str, candidates: List[str], threshold: float = 0.6) -> List[Tuple[str, float]]:
        """
        Find similar strings using fuzzy matching.

        Args:
            query: Search string
            candidates: List of candidate strings
            threshold: Minimum similarity score (0.0 to 1.0)

        Returns:
            List of (candidate, similarity_score) tuples sorted by score (max 100 candidates)
        """
        if not query or not candidates:
            return []

        # Limit candidates for performance
        candidates = candidates[:100] if len(candidates) > 100 else candidates

        matches = []
        query_lower = query.lower().strip()

        for candidate in candidates:
            if not candidate:
                continue

            candidate_lower = candidate.lower().strip()

            # Calculate similarity
            similarity = SequenceMatcher(None, query_lower, candidate_lower).ratio()

            # Bonus for substring matches
            if query_lower in candidate_lower or candidate_lower in query_lower:
                similarity = min(1.0, similarity + 0.2)

            if similarity >= threshold:
                matches.append((candidate, similarity))

        # Sort by similarity descending
        matches.sort(key=lambda x: x[1], reverse=True)

        # Return top 3 matches
        return matches[:3]

    # Private helper methods

    def _extract_date(self, item: Dict) -> datetime:
        """Extract date from item for sorting."""
        date_fields = ["createdDate", "date", "dateCreated", "timestamp", "modifiedDate"]

        for field in date_fields:
            if field in item and item[field]:
                try:
                    # Try parsing ISO format
                    return datetime.fromisoformat(str(item[field]).replace('Z', '+00:00'))
                except:
                    pass

        # Return epoch if no date found
        return datetime.fromtimestamp(0)

    def _format_item_summary(self, item_type: str, item: Dict) -> Optional[Dict]:
        """Format item for display in suggestions."""
        if item_type == "material":
            return {
                "name": item.get("name", "Unknown"),
                "type": item.get("materialType", {}).get("name", ""),
                "method": item.get("method", {}).get("name", ""),
                "serialized": item.get("isSerialized", False),
                "serial": item.get("serialNumber", "")
            }

        elif item_type == "package":
            return {
                "id": item.get("id", ""),
                "number": item.get("number", ""),
                "description": item.get("description", ""),
                "status": item.get("status", {}).get("name", ""),
                "method": item.get("method", {}).get("name", ""),
                "materials_count": len(item.get("materials", []))
            }

        elif item_type == "charge":
            return {
                "id": item.get("id", ""),
                "cycle_number": item.get("cycleNumber", ""),
                "status": item.get("status", {}).get("name", ""),
                "sterilizer": item.get("sterilizer", {}).get("name", ""),
                "program": item.get("program", {}).get("name", ""),
                "packages_count": len(item.get("packages", []))
            }

        return None

    def _get_recent_cycles(self, charges: List[Dict], limit: int = 5) -> List[Dict]:
        """Get recent cycles grouped by type (sterilization vs washing)."""
        sterilization = []
        washing = []

        for charge in sorted(charges, key=lambda x: self._extract_date(x), reverse=True):
            method_name = charge.get("method", {}).get("name", "").lower()
            cycle_num = charge.get("cycleNumber", "")

            summary = {
                "cycle_number": cycle_num,
                "status": charge.get("status", {}).get("name", "Unknown"),
                "date": self._extract_date(charge).strftime("%Y-%m-%d %H:%M"),
                "packages": len(charge.get("packages", [])),
                "method": charge.get("method", {}).get("name", "")
            }

            if "wash" in method_name:
                if len(washing) < limit:
                    washing.append(summary)
            else:
                if len(sterilization) < limit:
                    sterilization.append(summary)

            if len(sterilization) >= limit and len(washing) >= limit:
                break

        return {
            "sterilization": sterilization,
            "washing": washing
        }

    def _find_similar_cycle_numbers(self, cycle_number: int, charges: List[Dict]) -> List[int]:
        """Find cycle numbers close to the requested one."""
        cycle_numbers = []
        for charge in charges:
            num = charge.get("cycleNumber")
            if num is not None:
                try:
                    cycle_numbers.append(int(num))
                except:
                    pass

        if not cycle_numbers:
            return []

        cycle_numbers = sorted(set(cycle_numbers))

        # Find closest numbers
        similar = []
        for num in cycle_numbers:
            diff = abs(num - cycle_number)
            if diff <= 10 and diff > 0:
                similar.append(num)

        return sorted(similar)[:3]

    def _format_cycle_not_found_message(self, cycle_number: int, recent_cycles: Dict, similar_cycles: List[int]) -> str:
        """Format detailed error message for cycle not found."""
        msg = f"NOT FOUND: Cycle {cycle_number}\n\n"

        # Recent sterilization cycles
        if recent_cycles.get("sterilization"):
            msg += "Recent sterilization cycles:\n"
            for cycle in recent_cycles["sterilization"]:
                msg += f"  - Cycle {cycle['cycle_number']}: {cycle['status']} on {cycle['date']} ({cycle['packages']} packages)\n"
            msg += "\n"

        # Recent washing cycles
        if recent_cycles.get("washing"):
            msg += "Recent washing cycles:\n"
            for cycle in recent_cycles["washing"]:
                msg += f"  - Cycle {cycle['cycle_number']}: {cycle['status']} on {cycle['date']}\n"
            msg += "\n"

        # Similar cycle numbers
        if similar_cycles:
            msg += "Did you mean:\n"
            for num in similar_cycles:
                msg += f"  - Cycle {num}?\n"
            msg += "\n"

        msg += "Search tips:\n"
        msg += "  - Check spelling of cycle number\n"
        msg += "  - Search by date: get_charge_list(date_from='2025-01-01')\n"
        msg += "  - Check washing cycles: get_washing_charge_list()\n"

        return msg.strip()

    def _format_material_not_found_message(self, material_name: str, fuzzy_matches: List[Tuple[str, float]],
                                          recent_materials: List[Dict]) -> str:
        """Format detailed error message for material not found."""
        msg = f"NOT FOUND: Material '{material_name}'\n\n"

        # Fuzzy matches
        if fuzzy_matches:
            msg += "Did you mean:\n"
            for match, score in fuzzy_matches:
                percentage = int(score * 100)
                msg += f"  - {match} (similarity: {percentage}%)\n"
            msg += "\n"

        # Recent materials
        if recent_materials:
            msg += "Recent materials:\n"
            for material in recent_materials:
                serial = f", Serial: {material['serial']}" if material.get('serial') else ""
                msg += f"  - {material['name']} ({material.get('type', 'Unknown type')}{serial})\n"
            msg += "\n"

        msg += "Search tips:\n"
        msg += "  - Check spelling and accents\n"
        msg += "  - Try partial name: get_material_list(search='partial')\n"
        msg += "  - List all: get_material_list() without filters\n"

        return msg.strip()

    def _format_package_not_found_message(self, package_id: str, recent_packages: List[Dict]) -> str:
        """Format detailed error message for package not found."""
        msg = f"NOT FOUND: Package '{package_id}'\n\n"

        # Recent packages
        if recent_packages:
            msg += "Recent packages:\n"
            for pkg in recent_packages:
                msg += f"  - {pkg.get('number', pkg.get('id'))}: {pkg.get('description', 'No description')}\n"
                msg += f"    Status: {pkg.get('status', 'Unknown')}, Materials: {pkg.get('materials_count', 0)}\n"
            msg += "\n"

        msg += "Search tips:\n"
        msg += "  - Check package ID format\n"
        msg += "  - Search by status: get_package_list(status='stored')\n"
        msg += "  - Search by location: get_stored_packages()\n"

        return msg.strip()

    def _format_invalid_status_message(self, status: str, valid_statuses: List[Dict]) -> str:
        """Format error message for invalid status value."""
        msg = f"INVALID STATUS: '{status}'\n\n"

        msg += "Valid status values:\n"
        for valid in valid_statuses:
            status_id = valid.get("id", "")
            status_name = valid.get("name", "Unknown")
            msg += f"  - {status_id}: {status_name}\n"

        return msg.strip()


# Singleton instance
_validator_instance = None


def get_validator() -> DataValidator:
    """Get or create singleton validator instance."""
    global _validator_instance
    if _validator_instance is None:
        _validator_instance = DataValidator()
    return _validator_instance
