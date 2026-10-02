"""Closed WebAuthn/ES256 verification for admission-v2, with no native APIs.

The RP, flags, COSE layout, exact client JSON and DER constraints preserve the
fixed owner protocol. Verifying public signatures does not establish a caller,
MakeCredential user handle, exclusive lease or maintenance cut; those facts are
checked independently by the platform authority boundary.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct

RP_ID = "tradingagents-policy-owner.local"
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


class SignatureRejected(ValueError):
    pass


def require(condition: object, message: str) -> None:
    if not condition:
        raise SignatureRejected(message)


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def b64(value: object) -> bytes:
    require(type(value) is str and len(value) <= 2 * 1024 * 1024, "bounded base64 string required")
    return base64.b64decode(re.sub(r"[ \t\r\n]", "", value), validate=True)


def client_json(challenge: bytes) -> bytes:
    require(type(challenge) is bytes and len(challenge) == 32, "256-bit challenge required")
    token = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode("ascii")
    return (f'{{"type":"webauthn.get","challenge":"{token}",'
            f'"origin":"https://{RP_ID}","crossOrigin":false}}').encode("utf-8")


def authenticator_header(raw: bytes, registration: bool) -> int:
    require(len(raw) >= 37 and hmac.compare_digest(raw[:32], hashlib.sha256(RP_ID.encode()).digest()),
            "RP hash mismatch")
    require(raw[32] == (0x45 if registration else 0x05), "authenticator flags mismatch")
    return int.from_bytes(raw[33:37], "big")


def registration_key(raw: bytes, credential_id: bytes) -> bytes:
    authenticator_header(raw, True)
    require(len(raw) >= 55, "registration truncated")
    length = int.from_bytes(raw[53:55], "big")
    require(0 < length <= 4096 and len(raw) >= 55 + length and
            hmac.compare_digest(raw[55:55 + length], credential_id), "credential ID mismatch")
    offset = 55 + length
    require(offset < len(raw) and raw[offset] == 0xA5, "closed five-item COSE map required")
    offset += 1
    fields = {}
    for _ in range(5):
        require(offset < len(raw), "COSE truncated")
        key = raw[offset]
        offset += 1
        require(key not in fields, "COSE duplicate key")
        if key in (1, 3, 0x20):
            expected = {1: 2, 3: 0x26, 0x20: 1}[key]
            require(offset < len(raw) and raw[offset] == expected, "COSE algorithm/curve mismatch")
            fields[key] = raw[offset]
            offset += 1
        else:
            require(key in (0x21, 0x22) and offset + 34 <= len(raw) and
                    raw[offset:offset + 2] == b"\x58\x20", "COSE coordinate encoding")
            fields[key] = raw[offset + 2:offset + 34]
            offset += 34
    require(offset == len(raw) and set(fields) == {1, 3, 0x20, 0x21, 0x22}, "COSE missing/trailing data")
    return struct.pack("<II", 0x31534345, 32) + fields[0x21] + fields[0x22]


def der_signature(raw: bytes) -> tuple[int, int]:
    require(8 <= len(raw) <= 72 and raw[0] == 0x30 and raw[1] == len(raw) - 2, "DER sequence")
    offset, values = 2, []
    for _ in range(2):
        require(offset + 2 <= len(raw) and raw[offset] == 2, "DER integer tag")
        length = raw[offset + 1]
        offset += 2
        require(0 < length <= 33 and offset + length <= len(raw) and not raw[offset] & 0x80,
                "DER positive integer")
        value = raw[offset:offset + length]
        offset += length
        if len(value) > 1 and value[0] == 0:
            require(bool(value[1] & 0x80), "DER nonminimal integer")
            value = value[1:]
        require(len(value) <= 32, "DER oversized integer")
        values.append(int.from_bytes(value, "big"))
    require(offset == len(raw), "DER trailing data")
    return values[0], values[1]


def _add(left: tuple[int, int] | None, right: tuple[int, int] | None) -> tuple[int, int] | None:
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        slope = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (slope * slope - x1 - x2) % P
    return x3, (slope * (x1 - x3) - y1) % P


def _multiply(scalar: int, point: tuple[int, int] | None) -> tuple[int, int] | None:
    result = None
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def verify_es256(public_blob: bytes, message: bytes, signature: bytes) -> None:
    require(len(public_blob) == 72 and public_blob[:8] == struct.pack("<II", 0x31534345, 32),
            "P-256 public blob required")
    point = (int.from_bytes(public_blob[8:40], "big"), int.from_bytes(public_blob[40:72], "big"))
    x, y = point
    require(0 <= x < P and 0 <= y < P and (y * y - x * x * x - A * x - B) % P == 0,
            "public point not on P-256")
    require(_multiply(N, point) is None, "public point order")
    r, s = der_signature(signature)
    require(0 < r < N and 0 < s < N, "signature scalar range")
    inverse = pow(s, -1, N)
    digest = int.from_bytes(hashlib.sha256(message).digest(), "big")
    check = _add(_multiply(digest * inverse % N, G), _multiply(r * inverse % N, point))
    require(check is not None and check[0] % N == r, "ES256 signature invalid")


def verify_proof(challenge: bytes, registration: dict, proof: dict) -> int:
    require(type(registration) is dict and set(registration) == {"id", "authenticator_data", "public_key"},
            "registration closed schema")
    require(type(proof) is dict and set(proof) == {"credential_id", "authenticator_data", "signature", "client_data"},
            "proof closed schema")
    credential_id = b64(registration["id"])
    require(0 < len(credential_id) <= 4096, "credential length")
    public_key = b64(registration["public_key"])
    require(hmac.compare_digest(public_key, registration_key(b64(registration["authenticator_data"]), credential_id)),
            "registration public key mismatch")
    require(hmac.compare_digest(b64(proof["credential_id"]), credential_id), "assertion credential mismatch")
    client = b64(proof["client_data"])
    require(hmac.compare_digest(client, client_json(challenge)), "exact client JSON mismatch")
    auth = b64(proof["authenticator_data"])
    counter = authenticator_header(auth, False)
    require(len(auth) == 37, "assertion extensions/trailing data forbidden")
    verify_es256(public_key, auth + hashlib.sha256(client).digest(), b64(proof["signature"]))
    return counter
