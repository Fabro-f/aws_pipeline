"""Educational Tooltips and Glossary for BionovaQ."""

from typing import Dict, Optional


class Glossary:
    TERMS = {
        "BI": "Biological Indicator - A test device used to verify sterilization effectiveness",
        "load": "A batch of materials or packages processed together in sterilization",
        "charge": "Same as load - a sterilization batch with a cycle number",
        "cycle": "One complete sterilization or washing process with unique number",
        "package": "A grouped set of materials prepared for sterilization",
        "release": "Approval of a sterilization cycle after validation",
        "status": "Current state of material/package (21 possible material statuses)",
        "autoclave": "Steam sterilization equipment",
        "ATP": "Adenosine Triphosphate - Cleanliness monitoring test",
        "protein": "Protein residue test - Validates washing effectiveness",
        "area": "Clinical location where materials are used (e.g., Operating Room 1)",
        "incubation": "Monitoring period for biological indicator results",
        "validation": "Verification that process meets quality standards"
    }

    def get_definition(self, term: str) -> Optional[str]:
        term_lower = term.lower().strip()
        return self.TERMS.get(term_lower)

    def add_tooltips(self, content: str) -> str:
        result = content
        for term, definition in self.TERMS.items():
            if term in result.lower():
                result = result.replace(term, f"{term} [?]")
        return result


_glossary = None


def get_glossary() -> Glossary:
    global _glossary
    if _glossary is None:
        _glossary = Glossary()
    return _glossary
