"""Explicit operator-called testnet enrollment; no wallet-key file access or funding."""
from __future__ import annotations

import json
import time
from eth_account import Account
from eth_account.messages import encode_typed_data, encode_defunct
from eth_utils import keccak
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from nerya.financial.contracts import FinancialError
from .trading import HTTP, b64

CHAIN_ID = 10143
API = 'https://testnet.perpl.xyz/api'
ORIGIN = 'https://testnet.perpl.xyz'
RPC = 'https://testnet-rpc.monad.xyz'
EXCHANGE = '0x1964c32f0be608e7d29302aff5e61268e72080cc'
COLLATERAL = '0xa9012a055bd4e0edff8ce09f960291c09d5322dc'


def validate_enrollment(typed_data, *, address, public_key, scope_mask, expires_at, label):
    """Exact schema observed from official testnet payload on 2026-10-13."""
    domain, message = typed_data.get('domain', {}), typed_data.get('message', {})
    expected_types = {
        'EIP712Domain': [('name', 'string'), ('version', 'string'), ('chainId', 'uint256'),
                        ('verifyingContract', 'address'), ('salt', 'bytes32')],
        'PerplRegisterApiKey': [('signer', 'address'), ('statement', 'string'), ('publicKey', 'string'),
            ('scope', 'string'), ('label', 'string'), ('expiresAt', 'string'), ('ipCidrs', 'string'),
            ('origin', 'string'), ('builderId', 'string'), ('maxBuilderFeePer100K', 'string'), ('time', 'uint64')]}
    expected_types = {name: [{'name': field, 'type': kind} for field, kind in fields]
                      for name, fields in expected_types.items()}
    if typed_data.get('primaryType') != 'PerplRegisterApiKey' or typed_data.get('types') != expected_types:
        raise FinancialError('perpl_enrollment_schema_unrecognized', 422)
    def number(value):
        return int(value, 16) if isinstance(value, str) and value.startswith('0x') else int(value)
    if (set(domain) != {'name', 'version', 'chainId', 'verifyingContract', 'salt'} or
        number(domain.get('chainId', 0)) != CHAIN_ID or domain.get('name') != 'perpl.xyz' or
        domain.get('version') != '1' or domain.get('verifyingContract', '').lower() != '0x'+'0'*40):
        raise FinancialError('perpl_enrollment_domain_mismatch', 403)
    try:
        if len(bytes.fromhex(domain['salt'].removeprefix('0x'))) != 32:
            raise ValueError()
    except (KeyError, ValueError):
        raise FinancialError('perpl_enrollment_domain_mismatch', 403) from None
    expected_message = {
        'signer': address, 'statement': 'I authorize the creation of Perpl API key with the specified scope and parameters',
        'publicKey': b64(bytes.fromhex(public_key.removeprefix('0x'))), 'scope': str(scope_mask),
        'label': label, 'expiresAt': str(expires_at), 'ipCidrs': '', 'origin': ORIGIN,
        'builderId': '0', 'maxBuilderFeePer100K': '0'}
    if set(message) != set(expected_message) | {'time'}:
        raise FinancialError('perpl_enrollment_schema_unrecognized', 422)
    for key, value in expected_message.items():
        actual = message[key]
        if key == 'signer':
            actual, value = actual.lower(), value.lower()
        if actual != value:
            raise FinancialError('perpl_enrollment_' + key + '_mismatch', 403)
    if not -5000 <= int(time.time()*1000) - number(message['time']) <= 30000:
        raise FinancialError('perpl_enrollment_challenge_stale', 403)
    return encode_typed_data(full_message=typed_data)


def enroll_testnet(*, vault, seed_ref, token_name, address, wallet_sign, label='Nerya Monad testnet',
                   scope_mask=3, expires_at=None, operator_authorized=False, http=None):
    """Parent supplies wallet_sign(SignableMessage); this module never reads wallet files.

    Explicit call only. The API enrollment POST is a remote mutation. No retry is
    performed on unknown outcome; use the operator UI to inspect before new enrollment.
    """
    if not operator_authorized:
        raise FinancialError('perpl_enrollment_operator_authorization_required', 403)
    if scope_mask not in (1, 3) or not seed_ref.startswith('vault://') or not callable(wallet_sign):
        raise FinancialError('perpl_invalid_enrollment_configuration', 400)
    if not label or len(label) > 100:
        raise FinancialError('perpl_invalid_enrollment_label', 400)
    expires_at = expires_at if expires_at is not None else int(time.time()*1000)+7*24*3600000
    if type(expires_at) is not int or expires_at <= int(time.time()*1000):
        raise FinancialError('perpl_invalid_enrollment_expiry', 400)
    from nerya.financial.adapters import validate_evm_address
    validate_evm_address(address)
    scope = 'perpl:trade' if scope_mask == 3 else 'perpl:read'
    seed = bytes.fromhex(vault.resolve(seed_ref[8:], required_scope=scope).removeprefix('0x'))
    key = Ed25519PrivateKey.from_private_bytes(seed)
    public = '0x'+key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    http = http or HTTP()
    headers = {'Content-Type': 'application/json', 'Origin': ORIGIN}
    def send(target, data):
        code, result = http.request('POST', API+target, headers=headers, body=json.dumps(data,separators=(',', ':')).encode())
        if code == 404 and target == '/v1/api-key/enroll':
            raise FinancialError('perpl_target_profile_not_found_connect_wallet_first', 404)
        if code != 200 or not isinstance(result, dict):
            raise FinancialError('perpl_enrollment_http_'+str(code), code)
        return result
    challenge = send('/v1/api-key/payload', {'chain_id': CHAIN_ID, 'address': address,
        'public_key': public, 'scope_mask': scope_mask, 'label': label, 'expires_at': expires_at})
    typed_data, mac = challenge.get('typed_data'), challenge.get('mac')
    if not isinstance(typed_data, dict) or not isinstance(mac, str) or not mac:
        raise FinancialError('perpl_enrollment_invalid_challenge', 422)
    message = validate_enrollment(typed_data, address=address, public_key=public, scope_mask=scope_mask,
                                  expires_at=expires_at, label=label)
    signature = wallet_sign(message)
    if not isinstance(signature, str) or Account.recover_message(message, signature=signature).lower() != address.lower():
        raise FinancialError('perpl_enrollment_wallet_signature_mismatch', 403)
    digest = keccak(b'\x19'+message.version+message.header+message.body)
    result = send('/v1/api-key/enroll', {'chain_id': CHAIN_ID, 'address': address, 'typed_data': typed_data,
                  'mac': mac, 'signature': signature, 'pop_signature': '0x'+key.sign(digest).hex()})
    info = result.get('api_key')
    if not isinstance(info, dict) or info.get('address', '').lower() != address.lower() or info.get('scope_mask') != scope_mask or info.get('expires_at') != expires_at or not info.get('api_key'):
        raise FinancialError('perpl_enrollment_response_identity_mismatch', 403)
    # Store opaque token before returning; never return it as a receipt.
    scopes = ['perpl:read'] + (['perpl:trade'] if scope_mask == 3 else [])
    vault.put(name=token_name, value=info['api_key'], kind='api_key', scope=scopes)
    return {'chain_id': CHAIN_ID, 'address': address, 'public_key_hex': public[2:],
            'scope_mask': scope_mask, 'expires_at': expires_at, 'api_key_ref': 'vault://'+token_name,
            'private_key_ref': seed_ref, 'status': 'enrolled', 'account_funding_performed': False}


def connect_testnet_profile(*, address, wallet_sign, terms_acknowledged=False,
                            operator_authorized=False, http=None):
    """Explicit profile bootstrap discovered in live frontend; no API-key enrollment.

    Caller must already acknowledge the exact protocol terms statement. This is
    separate from on-chain createAccount and must not be done implicitly on a 404.
    No login nonce/session credential is returned or logged.
    """
    if not operator_authorized or not terms_acknowledged:
        raise FinancialError('perpl_profile_requires_operator_and_terms_acknowledgment', 403)
    from nerya.financial.adapters import validate_evm_address
    validate_evm_address(address)
    if not callable(wallet_sign):
        raise FinancialError('perpl_wallet_signer_required', 400)
    http = http or HTTP()
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Origin': ORIGIN}
    def send(target, payload):
        code, doc = http.request('POST', API+target, headers=headers, body=json.dumps(payload,separators=(',',':')).encode())
        if code != 200 or not isinstance(doc, dict):
            raise FinancialError('perpl_profile_http_'+str(code), code)
        return doc
    challenge = send('/v1/auth/payload', {'chain_id': CHAIN_ID, 'address': address})
    if set(challenge) != {'message', 'nonce', 'issued_at', 'mac'}:
        raise FinancialError('perpl_profile_challenge_schema_changed', 422)
    message = challenge['message']
    nonce, issued = challenge['nonce'], challenge['issued_at']
    if not isinstance(message, str) or not isinstance(nonce, str) or len(nonce)<16 or type(issued) is not int or not -5000 <= int(time.time()*1000)-issued <= 30000:
        raise FinancialError('perpl_profile_challenge_invalid_or_stale', 403)
    from datetime import datetime, timezone
    import re
    issued_iso=datetime.fromtimestamp(issued/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    # Current testnet server returns :// as URI/resources without forwarded protocol.
    # Accept only that exact observed placeholder or the exact official testnet URI.
    pattern = (r'testnet\.perpl\.xyz wants you to sign in with your Ethereum account:\n' + re.escape(address) +
        r'\n\nBy signing this message I acknowledge the risks of using this protocol and accept the terms of use\n\nURI: (https://testnet\.perpl\.xyz|://)\nVersion: 1\nChain ID: 10143\nNonce: ' + re.escape(nonce) +
        r'\nIssued At: ' + re.escape(issued_iso) +
        r'\nResources:\n- (https://testnet\.perpl\.xyz|://)\n- https://perpl\.xyz/terms-of-use\n- https://perpl\.xyz/privacy-policy')
    if re.fullmatch(pattern,message) is None or not isinstance(challenge['mac'],str) or not challenge['mac']:
        raise FinancialError('perpl_profile_message_identity_or_terms_mismatch', 403)
    signable = encode_defunct(text=message)
    signature = wallet_sign(signable)
    if not isinstance(signature,str) or Account.recover_message(signable,signature=signature).lower()!=address.lower():
        raise FinancialError('perpl_profile_wallet_signature_mismatch',403)
    result = send('/v1/auth/connect', {**challenge,'address':address,'chain_id':CHAIN_ID,'signature':signature})
    if not isinstance(result.get('nonce'),str) or not result['nonce'] or type(result.get('expires_at')) is not int or result['expires_at']<=int(time.time()*1000):
        raise FinancialError('perpl_profile_response_invalid',422)
    return {'status':'profile_connected','chain_id':CHAIN_ID,'address':address,
            'expires_at':result['expires_at'],'account_created':False,'api_key_enrolled':False}


def read_testnet_account(address, *, http=None):
    """Unauthenticated on-chain account identity read using live frontend ABI."""
    from eth_abi import encode, decode
    from nerya.financial.adapters import validate_evm_address
    validate_evm_address(address)
    http=http or HTTP()
    def rpc(method, params):
        status, result=http.request('POST',RPC,headers={'Content-Type':'application/json'},body=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode())
        if status == 200 and method == 'eth_call' and isinstance(result.get('error'), dict):
            error_data = result['error'].get('data')
            expected = '0x03a0e277' + encode(['address'], [address]).hex()
            if isinstance(error_data, str) and error_data.lower() == expected.lower():
                raise FinancialError('perpl_account_does_not_exist', 404)
        if status!=200 or 'error' in result or 'result' not in result:
            raise FinancialError('perpl_setup_rpc_unavailable',503)
        return result['result']
    if rpc('eth_chainId',[]) != hex(CHAIN_ID):
        raise FinancialError('perpl_setup_chain_mismatch',403)
    call='0x'+(keccak(text='getAccountByAddr(address)')[:4]+encode(['address'],[address])).hex()
    try:
        raw=rpc('eth_call',[{'to':EXCHANGE,'data':call},'latest'])
    except FinancialError as exc:
        if exc.code != 'perpl_account_does_not_exist':
            raise
        return {'chain_id':CHAIN_ID,'instance_id':12,'exchange':EXCHANGE,'account_address':address,
                'account_id':None,'exists':False,'reason':'AccountDoesNotExist(address)'}
    values=decode(['(uint256,uint256,uint256,uint8,address,(uint256,uint256,uint256,uint256))'],bytes.fromhex(raw[2:]))[0]
    if values[0] and values[4].lower()!=address.lower():
        raise FinancialError('perpl_setup_account_identity_mismatch',403)
    return {'chain_id':CHAIN_ID,'instance_id':12,'exchange':EXCHANGE,'account_id':values[0],
            'balance_base':str(values[1]),'locked_balance_base':str(values[2]),'frozen':values[3],
            'account_address':values[4],'position_banks':[str(v) for v in values[5]],'exists':bool(values[0])}


def forwarding_calldata(allow=True):
    """Unsigned transaction data only; parent must authorize/sign/broadcast separately."""
    from eth_abi import encode
    if type(allow) is not bool:
        raise FinancialError('perpl_forwarding_bool_required',400)
    return {'chain_id':CHAIN_ID,'to':EXCHANGE,'value':'0x0',
            'data':'0x'+(keccak(text='allowOrderForwarding(bool)')[:4]+encode(['bool'],[allow])).hex()}
