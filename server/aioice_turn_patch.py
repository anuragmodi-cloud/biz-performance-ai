"""Works around a TURN-server compatibility gap between aioice and
Cloudflare's TURN service.

Confirmed via production logging (2026-09-04): ALLOCATE's first attempt
gets a proper RFC 5389 challenge (401 with REALM+NONCE) and its retry with
real credentials succeeds -- normal long-term-credential flow. But
CHANNEL_BIND, sent using the *cached* integrity_key/nonce/realm from that
successful ALLOCATE (aioice's TurnClientMixin.request() always attaches
cached credentials once integrity_key is set -- see aioice/turn.py:215-216),
gets back a bare 401 with NO NONCE and NO REALM at all. That's not a
compliant challenge -- there's nothing in it a client could use to
authenticate correctly -- so aioice's own single retry (and this file's
earlier bounded-retry-loop attempt) had nothing to retry WITH and simply
failed every time, leaving "Connect Karein" hanging on Connecting...
forever once Cloudflare TURN was wired in.

Cloudflare's TURN server appears to require its own fresh, request-specific
challenge for each new STUN method within an allocation, rather than
accepting a nonce reused across method types (coturn allows this reuse;
Cloudflare apparently does not, and unlike coturn it doesn't even bother
re-challenging on the mismatch, it just rejects outright). The fix: when
this exact bare 401 (no nonce) happens while cached credentials were
attached, drop them and force one genuinely UNAUTHENTICATED resend -- that
provokes a real, answerable challenge, which the normal retry logic below
can then satisfy.
"""
from __future__ import annotations

from loguru import logger

from aioice import stun
from aioice.turn import TurnClientMixin, make_integrity_key
from aioice.utils import random_transaction_id

_MAX_AUTH_ATTEMPTS = 5


async def _request_with_retry(self, request):
    for attempt in range(_MAX_AUTH_ATTEMPTS):
        try:
            return await self.request(request)
        except stun.TransactionFailed as e:
            error_code, reason = e.response.attributes.get("ERROR-CODE", (None, None))
            has_nonce = "NONCE" in e.response.attributes
            has_realm = "REALM" in e.response.attributes
            last_attempt = attempt == _MAX_AUTH_ATTEMPTS - 1

            bare_reject_with_cached_creds = error_code == 401 and not has_nonce and self.integrity_key is not None
            can_retry = (
                has_nonce
                and self.username is not None
                and self.password is not None
                and ((error_code == 401 and has_realm) or (error_code == 438 and self.realm is not None))
            )
            logger.warning(
                f"TURN auth: {request.message_method.name} attempt {attempt + 1}/{_MAX_AUTH_ATTEMPTS} "
                f"failed: error_code={error_code} reason={reason!r} has_nonce={has_nonce} "
                f"has_realm={has_realm} realm={e.response.attributes.get('REALM')!r} "
                f"username={self.username!r} "
                f"will_retry={(bare_reject_with_cached_creds or can_retry) and not last_attempt}"
            )
            if last_attempt:
                raise

            if bare_reject_with_cached_creds:
                # Force a genuinely unauthenticated resend so the server has
                # to issue a real, answerable challenge instead of silently
                # rejecting a reused nonce with nothing to retry with.
                self.integrity_key = None
                request.transaction_id = random_transaction_id()
                continue

            if not can_retry:
                raise

            # update long-term credentials and retry with a fresh transaction id
            self.nonce = e.response.attributes["NONCE"]
            if error_code == 401:
                self.realm = e.response.attributes["REALM"]
            self.integrity_key = make_integrity_key(self.username, self.realm, self.password)
            request.transaction_id = random_transaction_id()


def apply() -> None:
    TurnClientMixin.request_with_retry = _request_with_retry
