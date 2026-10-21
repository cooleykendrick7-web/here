"""
HERE — heuristic content moderation.

scan(text, author_name="") -> (flagged: bool, reasons: list[str])

Honest heuristics only: profanity word list, slur list, harassment patterns,
slander patterns, spam patterns, ALL-CAPS shouting. Flagged items are NOT
removed — the flag goes into moderation_flags and the item stays published;
only the owner decides (dismiss or remove).

Future: set MOD_LLM_HOOK to a callable(text)->reasons to use a real LLM
instead of heuristics.
"""
import re
from collections import Counter

# Set MOD_LLM_HOOK to a callable(text)->reasons to use a real LLM instead of heuristics.
MOD_LLM_HOOK = None
PROVIDER = None  # reserved: name of the moderation provider in use ("heuristics" by default)

PROFANITY = {
    "fuck", "fucking", "fucker", "fucked", "motherfucker",
    "shit", "shitty", "bullshit",
    "bitch", "bitches", "bitchass",
    "asshole", "dick", "dicks", "dickhead",
    "pussy", "cunt", "bastard", "whore", "slut",
    "douchebag", "cock", "tits", "boobs",
}

SLURS = {
    "nigger", "nigga", "niggas",
    "chink", "chinks", "gook", "gooks",
    "spic", "spics", "wetback",
    "kike", "kikes",
    "faggot", "faggots", "fag",
    "retard", "retarded",
    "coon", "coons", "darkie",
    "raghead", "towelhead",
    "tranny", "trannies",
    "dyke", "dykes",
    "cracker",  # mild, still flagged for review
}

INSULTS = {
    "ugly", "stupid", "dumb", "idiot", "loser", "worthless",
    "pathetic", "disgusting", "fat", "creepy", "weird",
    "hideous", "moron", "dumbass",
}

SLANDER_WORDS = {"liar", "thief", "scammer", "cheater", "fraud", "criminal",
                 "rapist", "pedophile", "molester"}

_WORD_RE = {w: re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE)
            for w in PROFANITY | SLURS | INSULTS}


def _find_words(text, words):
    found = []
    for w in sorted(words):
        if _WORD_RE[w].search(text):
            found.append(w)
    return found


def scan(text, author_name=""):
    """Return (flagged, reasons). flagged is True when any heuristic fires."""
    if MOD_LLM_HOOK is not None:
        try:
            reasons = list(MOD_LLM_HOOK(text) or [])
            return (len(reasons) > 0, reasons)
        except Exception:
            pass  # fall through to heuristics

    reasons = []
    t = (text or "")
    low = t.lower()
    name = (author_name or "").strip().lower()

    # --- profanity ---
    for w in _find_words(low, PROFANITY):
        reasons.append(f"profanity: {w}")

    # --- slurs ---
    for w in _find_words(low, SLURS):
        reasons.append(f"slur: {w}")

    # --- harassment: "you are a <insult>" / "you're so <insult>" ---
    insult_alt = "|".join(sorted(INSULTS))
    if re.search(r"\b(you are|you're|youre|ur)\s+(a|an|so|such|really|just)?\s*("
                 + insult_alt + r")\b", low):
        reasons.append("harassment: targeted insult")

    # --- harassment: "<name> is a <insult>" ---
    if name and re.search(r"\b" + re.escape(name) + r"\s+is\s+(a|an|so|such)?\s*("
                          + insult_alt + r")\b", low):
        reasons.append("harassment: targeted insult by name")

    # --- harassment: self-harm encouragement ---
    if re.search(r"\bkill\s+(yourself|urself)\b", low) or re.search(r"\bkys\b", low):
        reasons.append("harassment: self-harm encouragement")

    # --- harassment: "nobody likes you" ---
    if re.search(r"\bnobody\s+likes?\s+you\b", low):
        reasons.append("harassment: exclusion/bullying")

    # --- harassment: "@name" + insult in the same text ---
    if "@" in t:
        for w in _find_words(low, INSULTS):
            reasons.append(f"harassment: @{w} mention with insult")
            break

    # --- slander: "[someone] is a liar/thief/scammer/..." without evidence wording ---
    slander_alt = "|".join(sorted(SLANDER_WORDS))
    if re.search(r"\b(is|are)\s+(a|an)\s+(" + slander_alt + r")\b", low):
        reasons.append("possible slander: unproven accusation")

    # --- spam: same word 5+ times ---
    words = re.findall(r"[a-z0-9]{3,}", low)
    if words:
        common = Counter(words).most_common(1)[0]
        if common[1] >= 5:
            reasons.append(f"spam: repeated word '{common[0]}' x{common[1]}")

    # --- spam: 4+ URLs ---
    urls = re.findall(r"https?://", low)
    if len(urls) >= 4:
        reasons.append(f"spam: {len(urls)} links")

    # --- ALL-CAPS shouting / spam caps ---
    letters = [c for c in t if c.isalpha()]
    if letters and len(t) > 15:
        caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if len(t) > 20 and caps_ratio > 0.8:
            reasons.append("spam: excessive caps")
        elif caps_ratio > 0.7:
            reasons.append("shouting: all caps")

    return (len(reasons) > 0, reasons)
