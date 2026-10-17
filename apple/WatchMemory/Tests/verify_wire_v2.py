#!/usr/bin/env python3
"""Public synthetic CryptoKit ↔ Python fixture. Never loads production pairing files."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import struct
import sys
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def key(secret, info):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b'watch-memory-v2', info=info.encode()).derive(secret.encode())


def rejects(function):
    try:
        function()
    except InvalidTag:
        return
    raise AssertionError('Tampered AEAD unexpectedly authenticated')


def main():
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    fixture = json.loads(source.read_text())
    assert fixture['warning'].startswith('PUBLIC TEST SECRET/PLAINTEXT ONLY')
    config = fixture['configuration']
    assert config['endpoint'] == 'http://127.0.0.1:18437'
    secret = config['pairing_secret']
    attempt = fixture['attempt']
    sealed = base64.b64decode(fixture['sealed_base64'], validate=True)
    assert len(sealed) == attempt['cipherByteCount']
    assert hashlib.sha256(sealed).hexdigest() == attempt['cipherSHA256']
    aad = f"watch-memory-upload-v2\nPUT\n{attempt['path']}\n{attempt['challenge']}".encode()
    encryption_key = key(secret, 'upload-encryption')
    plain = AESGCM(encryption_key).decrypt(sealed[:12], sealed[12:], aad)
    meta_len, = struct.unpack('>I', plain[:4])
    assert 0 < meta_len <= 65536
    metadata = json.loads(plain[4:4 + meta_len])
    audio = plain[4 + meta_len:]
    assert audio == base64.b64decode(fixture['expected_audio_base64'])
    assert hashlib.sha256(audio).hexdigest() == metadata['sha256']
    assert metadata == attempt['expectedMetadata']
    assert sealed == sealed[:12] + AESGCM(encryption_key).encrypt(sealed[:12], plain, aad)
    print('PASS Swift CryptoKit ciphertext decrypted and reproduced by Python AESGCM/HKDF')
    damaged = sealed[:-1] + bytes([sealed[-1] ^ 1])
    rejects(lambda: AESGCM(encryption_key).decrypt(damaged[:12], damaged[12:], aad))
    rejects(lambda: AESGCM(key('QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI', 'upload-encryption')).decrypt(sealed[:12], sealed[12:], aad))
    rejects(lambda: AESGCM(encryption_key).decrypt(sealed[:12], sealed[12:], aad.replace(b'/v2/chunks/', b'/v2/other/')))
    rejects(lambda: AESGCM(encryption_key).decrypt(sealed[:12], sealed[12:], aad + b'x'))
    print('PASS tampered tag / wrong key / altered path / altered challenge rejected')
    body = base64.b64decode(fixture['receipt_body_base64'])
    prefix = f"watch-memory-receipt-v2\n{attempt['challenge']}\n{attempt['cipherSHA256']}\n201\n".encode()
    code = hmac.new(key(secret, 'receipt-authentication'), prefix + body, hashlib.sha256).digest()
    assert hmac.compare_digest(code, base64.b64decode(fixture['mac_base64']))
    receipt = json.loads(body)
    receipt['status'] = 'already_accepted'
    # Different ordering/whitespace/newline proves verification uses raw response bytes.
    response = (json.dumps(receipt, ensure_ascii=False, indent=1) + '\n').encode()
    prefix = f"watch-memory-receipt-v2\n{attempt['challenge']}\n{attempt['cipherSHA256']}\n200\n".encode()
    fixture['receipt_body_base64'] = base64.b64encode(response).decode()
    fixture['status_code'] = 200
    fixture['mac_base64'] = base64.b64encode(hmac.new(key(secret, 'receipt-authentication'), prefix + response, hashlib.sha256).digest()).decode()
    output.write_text(json.dumps(fixture, indent=2))
    print('PASS Swift receipt MAC reproduced; Python raw-byte receipt fixture written')


if __name__ == '__main__':
    main()
