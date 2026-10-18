import re
from typing import List, Dict, Optional, Set
from rapidfuzz import fuzz

# Industry sectors and corporate suffixes to ignore when detecting the distinct entity
INDUSTRY_NOISE: Set[str] = {
    "pharma", "pharmaceuticals", "pharmaceutical", "jewels", "jewellers", 
    "jewellery", "tech", "technologies", "technology", "solutions", 
    "infra", "infrastructure", "finance", "financial", "enterprises", 
    "logistics", "industries", "energy", "power", "capital", "holdings",
    "limited", "ltd", "pvt", "private", "corp", "corporation", "ipo",
    "india", "global", "international", "services", "group"
}

# Common conglomerate parent prefixes that may or may not appear across feeds
PARENT_PREFIXES: Set[str] = {
    "reliance", "tata", "adani", "bajaj", "birla", "mahindra", "godrej", "l&t"
}

def clean_tokens(name: str) -> Set[str]:
    """Extracts alphanumeric words from name in lowercase."""
    return set(re.findall(r"\b[a-zA-Z0-9]+\b", name.lower()))

def extract_core_entity(name: str) -> Set[str]:
    """
    Strips noise words and parent conglomerate prefixes to leave only
    the unique core entity name (e.g. 'Reliance Jio Pharma' -> {'jio'}).
    """
    tokens = clean_tokens(name) - INDUSTRY_NOISE
    specific_tokens = tokens - PARENT_PREFIXES
    return specific_tokens if specific_tokens else tokens

def check_alias_match(name1: str, name2: str, aliases: Dict[str, str]) -> bool:
    """Checks if either name maps to the other in manual_overrides.json."""
    n1 = name1.strip().lower()
    n2 = name2.strip().lower()
    if aliases.get(n1) == n2 or aliases.get(n2) == n1:
        return True
    for k, v in aliases.items():
        if (k in n1 and v in n2) or (v in n1 and k in n2):
            return True
    return False

def find_best_match(
    target_ipo: Dict, 
    candidates: List[Dict], 
    aliases: Dict[str, str],
    min_score: int = 86
) -> Optional[Dict]:
    """
    Multi-stage composite matcher:
    1. Direct Symbol match (if available)
    2. Explicit manual aliases
    3. Core brand anchor match + close date gating (+/- 1 day)
    4. Guarded Token Set Fuzzy ratio
    """
    target_name = target_ipo.get("name", "")
    target_symbol = target_ipo.get("symbol", "").strip().upper()
    target_close = target_ipo.get("closeDate", "").strip()

    best_candidate = None
    highest_score = 0

    # 1. Symbol Match (100% deterministic)
    if target_symbol:
        for cand in candidates:
            cand_symbol = cand.get("symbol", "").strip().upper()
            if cand_symbol and cand_symbol == target_symbol:
                return cand

    target_core = extract_core_entity(target_name)

    for cand in candidates:
        cand_name = cand.get("name", "")
        cand_close = cand.get("closeDate", "").strip()

        # 2. Manual Alias Match
        if check_alias_match(target_name, cand_name, aliases):
            return cand

        # 3. Core Anchor Overlap Gate
        cand_core = extract_core_entity(cand_name)
        common_anchors = target_core.intersection(cand_core)
        
        # If both entities have distinct names and share NO anchor tokens, reject immediately.
        # (e.g., 'Zenith Pharma' vs 'Apex Pharma' both have {} after removing pharma, 
        # so they will fail here and never cross-match).
        if target_core and cand_core and not common_anchors:
            continue

        # 4. Date Gate (if close dates are known on both sides)
        if target_close and cand_close and target_close != cand_close:
            # Skip if close dates differ
            continue

        # 5. Fuzzy Match on Cleaned Strings
        score = fuzz.token_set_ratio(target_name.lower(), cand_name.lower())
        if score > highest_score and score >= min_score:
            highest_score = score
            best_candidate = cand

    return best_candidate
