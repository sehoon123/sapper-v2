
ENGAGEMENT_FIELDS = (
    "objective", "scope", "exclusions", "environment", "window", "identities",
    "access", "methods", "constraints",
)
OUTCOME_FIELDS = {
    "vulnerability": ("comparison", "impact", "remediation", "root_cause"),
    "validated_defense": ("comparison",),
    "not_observed": (),
    "inconclusive": ("reason", "next_step"),
    "blocked": ("reason", "next_step"),
    "not_tested": ("reason", "next_step"),
    "not_applicable": ("reason", "next_step"),
}
TESTED_OUTCOMES = ("vulnerability", "validated_defense", "not_observed", "inconclusive")
EVIDENCE_FIELDS = ("method", "expected", "observed", "evidence", "basis")


def validate_record(kind: str | None, data: dict | None, doc: str | None = None) -> None:
    if kind not in ("engagement", "assessment"):
        return
    if not isinstance(data, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or not value.strip()
        for key, value in data.items()
    ):
        raise ValueError(f"{kind} data must contain nonempty string values")
    if kind == "engagement":
        required = allowed = set(ENGAGEMENT_FIELDS)
    else:
        outcome = data.get("outcome")
        if outcome not in OUTCOME_FIELDS:
            raise ValueError("assessment outcome must be one of: " + ", ".join(OUTCOME_FIELDS))
        required = {"outcome", "asset", "conditions", "limitations", *OUTCOME_FIELDS[outcome]}
        allowed = required | set(EVIDENCE_FIELDS) | {
            "comparison", "impact", "remediation", "root_cause", "reason", "next_step",
            "severity", "confidence",
        }
        if outcome in TESTED_OUTCOMES:
            required.update(EVIDENCE_FIELDS)
            if not isinstance(doc, str) or not doc.strip():
                raise ValueError("tested assessment requires a redacted evidence snapshot in doc")
        if "basis" in data and data["basis"] not in ("static", "dynamic"):
            raise ValueError("assessment basis must be static or dynamic")
        if outcome == "validated_defense" and data.get("basis") != "dynamic":
            raise ValueError("validated_defense requires dynamic evidence")
        if "severity" in data and data["severity"] not in ("info", "low", "medium", "high", "critical"):
            raise ValueError("invalid assessment severity")
        if "confidence" in data and data["confidence"] not in ("low", "medium", "high"):
            raise ValueError("invalid assessment confidence")
    missing, unknown = required - data.keys(), data.keys() - allowed
    if missing:
        raise ValueError(f"{kind} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{kind} unknown fields: {', '.join(sorted(unknown))}")
