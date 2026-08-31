from __future__ import annotations

from typing import Any, Mapping

from .models import Artifact


DECISIONS = {"supported", "rejected", "uncertain"}


def build_evidence_adjudication(
    claim_map: Mapping[str, Any] | Artifact,
    decisions: list[Mapping[str, Any]],
    *,
    adjudicator: str,
) -> Artifact:
    """Create an immutable human decision record over a ClaimEvidenceMap.

    Every supported/rejected passage must be one of the mapper's candidates for
    that claim. The source map and extracted passage are never modified.
    """
    claim_map_data: Mapping[str, Any] = claim_map.to_dict() if isinstance(claim_map, Artifact) else claim_map
    if claim_map_data.get("kind") != "ClaimEvidenceMap":
        raise ValueError("claim_map must be a ClaimEvidenceMap")
    if not isinstance(adjudicator, str) or not adjudicator.strip():
        raise ValueError("adjudicator must be a non-empty string")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("decisions must be a non-empty array")

    claims = claim_map_data.get("payload", {}).get("claims", [])
    by_claim = {claim.get("claim_id"): claim for claim in claims if isinstance(claim, Mapping)}
    normalized: list[dict[str, Any]] = []
    seen_claims: set[str] = set()
    for item in decisions:
        if not isinstance(item, Mapping):
            raise ValueError("each decision must be an object")
        claim_id = item.get("claim_id")
        decision = item.get("decision")
        passage_id = item.get("passage_id")
        note = item.get("note", "")
        if not isinstance(claim_id, str) or not claim_id:
            raise ValueError("each decision requires claim_id")
        if claim_id not in by_claim:
            raise ValueError(f"unknown claim_id: {claim_id}")
        if claim_id in seen_claims:
            raise ValueError(f"duplicate decision for claim_id: {claim_id}")
        if decision not in DECISIONS:
            raise ValueError("decision must be supported, rejected or uncertain")
        if passage_id is not None and (not isinstance(passage_id, str) or not passage_id):
            raise ValueError("passage_id must be a non-empty string when supplied")
        if decision in {"supported", "rejected"} and not passage_id:
            raise ValueError(f"{decision} decisions require passage_id")
        candidate_ids = {
            candidate.get("passage_id")
            for candidate in by_claim[claim_id].get("candidates", [])
            if isinstance(candidate, Mapping)
        }
        if passage_id and passage_id not in candidate_ids:
            raise ValueError(f"passage_id {passage_id} is not a candidate for claim_id {claim_id}")
        if not isinstance(note, str):
            raise ValueError("note must be a string")
        normalized.append({
            "claim_id": claim_id,
            "passage_id": passage_id,
            "decision": decision,
            "note": note,
            "adjudicator": adjudicator.strip(),
            "verification_status": "human_adjudicated",
        })
        seen_claims.add(claim_id)

    counts = {decision: sum(item["decision"] == decision for item in normalized) for decision in DECISIONS}
    return Artifact(
        kind="EvidenceAdjudication",
        producer="human-review",
        inputs=[claim_map_data["artifact_id"]],
        payload={
            "claim_map_artifact_id": claim_map_data["artifact_id"],
            "decisions": normalized,
            "summary": {
                "decision_count": len(normalized),
                "supported_count": counts["supported"],
                "rejected_count": counts["rejected"],
                "uncertain_count": counts["uncertain"],
                "adjudicated_claim_count": len(seen_claims),
                "remaining_unadjudicated_claim_count": max(0, len(by_claim) - len(seen_claims)),
            },
            "limitations": [
                "This records a human review decision; it does not establish truth, causality, or publication-level evidence.",
                "The reviewer remains responsible for checking the cited passage in the original full text.",
            ],
        },
    )
