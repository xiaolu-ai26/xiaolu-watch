"""Canonical sealed upload v2. Maintained cryptography primitives, no custom crypto."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAX_AUDIO=16*1024*1024
MAX_METADATA=65536
MAX_WIRE=MAX_AUDIO+MAX_METADATA+4+12+16
PROTOCOL='watch-memory-chunks-v2'

class WireError(ValueError):
    pass

def canonical_uuid(value):
    try:return isinstance(value,str) and str(uuid.UUID(value))==value
    except ValueError:return False

def derive(secret):
    if not isinstance(secret,str) or not re.fullmatch('[A-Za-z0-9_-]{43}',secret):raise WireError('invalid_pairing_secret')
    decoded=base64.urlsafe_b64decode(secret+'=')
    if len(decoded)!=32 or base64.urlsafe_b64encode(decoded).decode().rstrip('=')!=secret:raise WireError('invalid_pairing_secret')
    def key(info):return HKDF(algorithm=hashes.SHA256(),length=32,salt=b'watch-memory-v2',info=info).derive(secret.encode('utf-8'))
    return key(b'upload-encryption'),key(b'receipt-authentication')

def aad(path,challenge):
    if not canonical_uuid(challenge):raise WireError('invalid_challenge')
    return ('watch-memory-upload-v2\nPUT\n'+path+'\n'+challenge).encode('utf-8')

def seal(secret,meta,audio,path,challenge,nonce=None):
    encoded=meta if isinstance(meta,bytes) else json.dumps(meta,separators=(',',':'),ensure_ascii=False).encode('utf-8')
    if len(encoded)>MAX_METADATA or not 0<len(audio)<=MAX_AUDIO:raise WireError('payload_size_limit')
    nonce=secrets.token_bytes(12) if nonce is None else nonce
    if len(nonce)!=12:raise WireError('invalid_nonce')
    key,_=derive(secret)
    return nonce+AESGCM(key).encrypt(nonce,struct.pack('>I',len(encoded))+encoded+audio,aad(path,challenge))

def unseal(secret,body,path,challenge):
    if not 33<=len(body)<=MAX_WIRE:raise WireError('wire_size_limit')
    key,_=derive(secret)
    try:plain=AESGCM(key).decrypt(body[:12],body[12:],aad(path,challenge))
    except InvalidTag:raise WireError('invalid_seal')
    if len(plain)<5:raise WireError('invalid_frame')
    length=struct.unpack('>I',plain[:4])[0]
    if not 0<length<=MAX_METADATA or length+4>=len(plain):raise WireError('invalid_metadata_length')
    audio=plain[4+length:]
    if not 0<len(audio)<=MAX_AUDIO:raise WireError('audio_size_limit')
    try:meta=json.loads(plain[4:4+length].decode('utf-8'))
    except (ValueError,UnicodeError,RecursionError):raise WireError('invalid_metadata_json')
    if not isinstance(meta,dict):raise WireError('invalid_metadata_json')
    return meta,audio

def receipt_mac(secret,challenge,cipher_sha,status,raw_json):
    if not canonical_uuid(challenge) or len(cipher_sha)!=64:raise WireError('invalid_receipt_context')
    _,key=derive(secret)
    prefix=f'watch-memory-receipt-v2\n{challenge}\n{cipher_sha}\n{status}\n'.encode('utf-8')
    return base64.b64encode(hmac.new(key,prefix+raw_json,hashlib.sha256).digest()).decode('ascii')

def verify_receipt(secret,challenge,cipher_sha,status,raw_json,received_mac):
    try:
        expected=receipt_mac(secret,challenge,cipher_sha,status,raw_json)
        return hmac.compare_digest(expected.encode('ascii'),received_mac.encode('ascii'))
    except (ValueError,UnicodeError,AttributeError):return False

def retirement_mac(secret,challenge,cipher_sha,raw_json):
    """Owner deletion retirement, intentionally incompatible with a stored ACK."""
    derive(secret)  # Same canonical secret validation, distinct HKDF purpose.
    if not canonical_uuid(challenge) or not re.fullmatch('[0-9a-f]{64}',cipher_sha):raise WireError('invalid_retirement_context')
    key=HKDF(algorithm=hashes.SHA256(),length=32,salt=b'watch-memory-v2',info=b'retirement-authentication').derive(secret.encode('utf-8'))
    prefix=f'watch-memory-retired-v2\n{challenge}\n{cipher_sha}\n410\n'.encode('utf-8')
    return base64.b64encode(hmac.new(key,prefix+raw_json,hashlib.sha256).digest()).decode('ascii')

def verify_retirement(secret,challenge,cipher_sha,raw_json,received_mac):
    try:return hmac.compare_digest(retirement_mac(secret,challenge,cipher_sha,raw_json).encode('ascii'),received_mac.encode('ascii'))
    except (ValueError,UnicodeError,AttributeError,TypeError):return False
