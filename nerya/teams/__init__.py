"""Agent Team — durable multi-agent coordination layer.

See ``docs/agent-team-clawteam-design.md`` for the full design. The
package brings ClawTeam-style team coordination into Nerya without
spawning external processes; instead the existing
``SubAgentDispatcher`` runs every member, while this package owns the
durable team state (run/tasks/messages/blackboard) and the orchestration
loop that wires shared evidence into each child's prompt.
"""

from .models import (
    BlackboardEntry,
    TeamMember,
    TeamMemberSpec,
    TeamMessage,
    TeamRun,
    TeamRunResult,
    TeamTask,
    TeamTaskSpec,
    TeamTemplate,
)
from .store import TeamStore
from .mailbox import Mailbox
from .blackboard import Blackboard
from .templates import (
    BUILTIN_TEMPLATES,
    get_template,
    list_templates,
)
from .orchestrator import TeamOrchestrator
from .aggregator import TeamAggregator

__all__ = [
    "BlackboardEntry",
    "TeamMember",
    "TeamMemberSpec",
    "TeamMessage",
    "TeamRun",
    "TeamRunResult",
    "TeamTask",
    "TeamTaskSpec",
    "TeamTemplate",
    "TeamStore",
    "Mailbox",
    "Blackboard",
    "BUILTIN_TEMPLATES",
    "get_template",
    "list_templates",
    "TeamOrchestrator",
    "TeamAggregator",
]
