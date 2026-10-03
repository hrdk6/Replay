"""PII and secret redaction applied at ingest, before anything is stored.

Built-in detectors are regex-based (with a Luhn check for card numbers to cut
false positives). They will miss things and occasionally over-redact; they
are a safety net, not a guarantee. Projects can add custom patterns.

Custom patterns run on the ``regex`` engine with a per-call timeout so a
catastrophic pattern cannot stall ingest; on timeout the whole string is
replaced (fail closed).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import regex

TIMEOUT_SECONDS = 0.05
MAX_CUSTOM_RULES = 20
MAX_PATTERN_LENGTH = 500
MAX_DEPTH = 64


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass(frozen=True)
class Detector:
    name: str
    pattern: Any  # compiled regex
    replacement: str
    validate: Callable[[str], bool] | None = None


def _card_validate(match: str) -> bool:
    digits = "".join(c for c in match if c.isdigit())
    return 13 <= len(digits) <= 19 and _luhn_ok(digits)


# Order matters: secrets first (they can contain digit runs), then cards before phones.
BUILTIN: dict[str, Detector] = {
    "api_key": Detector(
        "api_key",
        regex.compile(
            r"(?:sk-ant-[A-Za-z0-9_\-]{16,}|sk-(?:proj-)?[A-Za-z0-9_\-]{20,}|rk_[0-9a-f]{8}_[A-Za-z0-9_\-]{20,}"
            r"|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}"
            r"|xox[abposr]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{35}"
            r"|(?i:bearer)\s+[A-Za-z0-9._~+/\-]{20,}=*)"
        ),
        "[REDACTED:SECRET]",
    ),
    "email": Detector(
        "email",
        regex.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED:EMAIL]",
    ),
    "credit_card": Detector(
        "credit_card",
        regex.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)"),
        "[REDACTED:CARD]",
        _card_validate,
    ),
    "ssn": Detector("ssn", regex.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:SSN]"),
    "phone": Detector(
        "phone",
        # North American (optionally +1) or explicitly international (+CC ...) numbers only;
        # bare digit groups (order ids, dates, non-Luhn card-like numbers) are left alone.
        regex.compile(
            r"(?<![\w+])(?:(?:\+?1[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}"
            r"|\+\d{1,3}(?:[\s.\-]?\d{1,4}){2,5})(?![\w])"
        ),
        "[REDACTED:PHONE]",
    ),
    "ipv4": Detector(
        "ipv4",
        regex.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
        "[REDACTED:IP]",
    ),
}
BUILTIN_ORDER = ["api_key", "email", "credit_card", "ssn", "phone", "ipv4"]


class RedactionConfigError(ValueError):
    pass


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalise and validate a project's redaction config; raises RedactionConfigError."""
    enabled = bool(config.get("enabled", True))
    builtin = config.get("builtin", [])
    if not isinstance(builtin, list) or any(b not in BUILTIN for b in builtin):
        raise RedactionConfigError(f"builtin must be a subset of {sorted(BUILTIN)}")
    custom = config.get("custom", [])
    if not isinstance(custom, list) or len(custom) > MAX_CUSTOM_RULES:
        raise RedactionConfigError(f"at most {MAX_CUSTOM_RULES} custom rules")
    out_custom = []
    for rule in custom:
        if not isinstance(rule, dict):
            raise RedactionConfigError("custom rules must be objects")
        name = str(rule.get("name", "")).strip()[:40]
        pattern = str(rule.get("pattern", ""))
        if not name or not pattern:
            raise RedactionConfigError("custom rules need a name and a pattern")
        if len(pattern) > MAX_PATTERN_LENGTH:
            raise RedactionConfigError(f"pattern longer than {MAX_PATTERN_LENGTH} characters")
        try:
            regex.compile(pattern)
        except regex.error as exc:
            raise RedactionConfigError(f"invalid pattern for {name!r}: {exc}") from exc
        out_custom.append({"name": name, "pattern": pattern})
    return {"enabled": enabled, "builtin": [b for b in BUILTIN_ORDER if b in builtin], "custom": out_custom}


@dataclass
class Redactor:
    detectors: list[Detector]
    counts: Counter[str] = field(default_factory=Counter)

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> Redactor:
        if not config or not config.get("enabled", True):
            return cls([])
        dets = [BUILTIN[b] for b in BUILTIN_ORDER if b in config.get("builtin", [])]
        for rule in config.get("custom", []):
            name = regex.sub(r"[^A-Za-z0-9_]", "_", rule["name"]).upper()[:30]
            dets.append(Detector(rule["name"], regex.compile(rule["pattern"]), f"[REDACTED:{name}]"))
        return cls(dets)

    @property
    def active(self) -> bool:
        return bool(self.detectors)

    def redact_text(self, text: str) -> str:
        if not self.detectors or not text:
            return text
        for det in self.detectors:
            try:
                if det.validate is None:
                    text, n = det.pattern.subn(det.replacement, text, timeout=TIMEOUT_SECONDS)
                else:
                    hits = 0

                    def _sub(m: Any, det: Detector = det) -> str:
                        nonlocal hits
                        assert det.validate is not None
                        if det.validate(m.group(0)):
                            hits += 1
                            return det.replacement
                        return str(m.group(0))

                    text = det.pattern.sub(_sub, text, timeout=TIMEOUT_SECONDS)
                    n = hits
            except TimeoutError:
                self.counts["timeout"] += 1
                return "[REDACTED:TIMEOUT]"
            if n:
                self.counts[det.name] += n
        return text

    def redact(self, value: Any, depth: int = 0) -> Any:
        """Recursively redact strings inside JSON-like data (keys are left intact)."""
        if not self.detectors:
            return value
        if depth > MAX_DEPTH:
            return "[TRUNCATED:DEPTH]"
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {k: self.redact(v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v, depth + 1) for v in value]
        return value
