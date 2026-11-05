"""Read-only, durable source binding for tool approvals and question answers."""
from __future__ import annotations

import hashlib
import json

from .command_store import encode
from ..approval_service import ApprovalService
from ..memory.scope import memory_actor


def trusted_continuation_source(config, command, *, connection):
    """Return one exact source command, None for ordinary input, or raise ValueError.

    The caller supplies a durable agent_commands row and its open connection.
    No writes, migrations, session-wide fallback, or delivery/CAS changes occur.
    Walk returned rows to resolve a chain; each hop is independently attested.
    """
    con = connection
    request = json.loads(command['request_json'])
    iid = request.get('interaction_id')
    approval = None
    if iid:
        interaction = con.execute('SELECT * FROM agent_interactions WHERE interaction_id=?', (iid,)).fetchone()
        if (interaction is None or interaction['kind'] != 'question'
                or interaction['state'] not in {'answered', 'resolved'}
                or interaction['session_id'] != command['session_id']
                or interaction['actor_id'] != command['actor_id']
                or command['command_id'] != 'response_' + iid or command['kind'] != 'resume'
                or request.get('resume_turn_id') != interaction['turn_id']
                or command['turn_id'] != interaction['turn_id']):
            raise ValueError('continuation_binding_unverified')
        payload, response = json.loads(interaction['payload_json']), json.loads(interaction['response_json'])
        feedback = 'User response to ' + payload['title'] + ': ' + encode(response)
        if 'questions' in payload:
            feedback += ' Questions: ' + encode(payload['questions']) + ' Omitted answers are unknown. Continue using best judgment within existing permissions. This is not approval for trading or other gated actions.'
        if response.get('action') != 'answer' or request.get('continuation_feedback') != feedback:
            raise ValueError('continuation_binding_unverified')
        turn = interaction['turn_id']
    elif request.get('source') == 'approval_continue':
        aid = (request.get('payload') or {}).get('approval_id')
        approval = ApprovalService(config).resolved(aid)
        if (not approval or approval.get('state') != 'approved'
                or not ApprovalService.is_native_tool(approval)
                or command['kind'] != 'send' or request.get('kind') != 'approval.continue'
                or command['command_id'] != 'approval_' + hashlib.sha256(aid.encode()).hexdigest()[:40]
                or (approval.get('requester_session_id') or approval.get('session_id')) != command['session_id']
                or ApprovalService.owner_actor_id(approval) not in {command['actor_id'], memory_actor(command['actor_id'])}):
            raise ValueError('continuation_binding_unverified')
        turn = approval.get('turn_id')
    else:
        return None
    source = con.execute(
        "SELECT * FROM agent_commands WHERE session_id=? AND turn_id=? AND kind!='guide' "
        'AND command_id!=? AND (created_at<? OR (created_at=? AND position<?)) '
        'ORDER BY created_at DESC,position DESC LIMIT 1',
        (command['session_id'], turn, command['command_id'], command['created_at'],
         command['created_at'], command['position'])).fetchone()
    if (source is None or source['actor_id'] != command['actor_id']
            or request.get('_auth_actor_id') != command['actor_id']
            or json.loads(source['request_json']).get('_auth_actor_id') != command['actor_id']
            or source['state'] not in ({'succeeded'} if approval else {'awaiting_input', 'succeeded', 'interrupted'})
            or (approval and (approval.get('requester_strategy_id') or approval.get('strategy_id') or '')
                != (json.loads(source['request_json']).get('strategy_id') or ''))):
        raise ValueError('continuation_binding_unverified')
    return source
