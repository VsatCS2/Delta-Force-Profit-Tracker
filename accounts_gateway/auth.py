"""Verify Supabase access tokens locally.

Supabase signs each user's access token (a JWT). Newer projects sign with an
asymmetric key (ES256/RS256) whose public half is published at
<project>/auth/v1/.well-known/jwks.json, so this server can check a token
without calling Supabase on every request. Older projects that still use the
shared "legacy JWT secret" (HS256) are supported when SUPABASE_JWT_SECRET is set.

Only the token's identity is trusted. Tier, role and ban status always come
from our own database - never from claims the user could influence.
"""
from dataclasses import dataclass, field

import jwt
from jwt import PyJWKClient


class AuthError(Exception):
    def __init__(self, code, status=401, message=""):
        super().__init__(code)
        self.code, self.status, self.message = code, status, message


@dataclass
class Claims:
    sub: str
    email: str = ""
    name: str = ""
    avatar: str = ""
    provider: str = ""
    exp: int = 0
    raw: dict = field(default_factory=dict, repr=False)


class Verifier:
    def __init__(self, supabase_url="", jwt_secret="", audience="authenticated",
                 jwks_client=None):
        base = (supabase_url or "").rstrip("/")
        self.issuer = f"{base}/auth/v1" if base else None
        self.secret = jwt_secret or None
        self.audience = audience
        self.jwks = jwks_client
        if self.jwks is None and self.issuer:
            try:
                self.jwks = PyJWKClient(f"{self.issuer}/.well-known/jwks.json",
                                        cache_keys=True, lifespan=3600, timeout=5)
            except TypeError:                     # older PyJWT without these options
                self.jwks = PyJWKClient(f"{self.issuer}/.well-known/jwks.json")

    @property
    def mode(self):
        parts = []
        if self.jwks:
            parts.append("jwks")
        if self.secret:
            parts.append("hs256")
        return "+".join(parts) or "none"

    def verify(self, token) -> Claims:
        if not token:
            raise AuthError("sign_in_required")
        try:
            alg = jwt.get_unverified_header(token).get("alg")
        except jwt.PyJWTError:
            raise AuthError("invalid_token")

        if alg in ("ES256", "RS256"):
            if not self.jwks:
                raise AuthError("server_misconfigured", 500)
            try:
                key = self.jwks.get_signing_key_from_jwt(token).key
            except Exception:
                raise AuthError("invalid_token")
        elif alg == "HS256":
            if not self.secret:
                raise AuthError("invalid_token")
            key = self.secret
        else:                                      # includes "none"
            raise AuthError("invalid_token")

        try:
            payload = jwt.decode(
                token, key, algorithms=[alg], audience=self.audience,
                issuer=self.issuer, leeway=10,
                options={"require": ["exp", "sub"]})
        except jwt.ExpiredSignatureError:
            raise AuthError("token_expired")
        except jwt.PyJWTError:
            raise AuthError("invalid_token")
        if payload.get("role") != "authenticated":
            raise AuthError("invalid_token")

        meta = payload.get("user_metadata") or {}
        app_meta = payload.get("app_metadata") or {}
        return Claims(
            sub=str(payload["sub"]),
            email=str(payload.get("email") or ""),
            name=str(meta.get("full_name") or meta.get("name")
                     or meta.get("user_name") or meta.get("preferred_username") or ""),
            avatar=str(meta.get("avatar_url") or meta.get("picture") or ""),
            provider=str(app_meta.get("provider") or ""),
            exp=int(payload["exp"]), raw=payload)
