"""Nerya evolution subsystem.

Produces proposals (learning updates, prompt patches, scripts, skills, triggers,
strategy configs) that operators review and approve. Never auto-applies changes
to protected scopes."""

from .patch_proposal import Proposal, create_proposal, list_proposals, set_state
from .learning_writer import append_learning
from .promotion import apply_proposal
from .ranking import (
    EvidenceBundle, RankedProposal,
    build_evidence, rank_proposals, rank_proposal,
    write_ranking_snapshot,
)
from .rollback import rollback_proposal
from .runner import evolve, rank_proposal_seeds

__all__ = [
    "Proposal", "create_proposal", "list_proposals", "set_state",
    "append_learning", "apply_proposal", "rollback_proposal",
    "EvidenceBundle", "RankedProposal",
    "build_evidence", "rank_proposals", "rank_proposal",
    "write_ranking_snapshot",
    "evolve", "rank_proposal_seeds",
]
