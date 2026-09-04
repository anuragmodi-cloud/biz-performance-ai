"""Works around a TURN-server compatibility gap between aioice and
Cloudflare's TURN service.

aioice's TurnClientMixin.request_with_retry() (aioice/turn.py) retries a
401/438 long-term-credential challenge exactly ONE time -- correct for a
server like coturn, whose nonce is valid for many requests across an entire
allocation's lifetime (RFC 5766's usual model: challenge once, then reuse
the nonce for ALLOCATE/CHANNEL_BIND/REFRESH alike). Cloudflare's TURN
server appears to issue a fresh, effectively single-use nonce challenge on
CHANNEL_BIND requests too, not just the initial unauthenticated one -- so
aioice's one retry (using the nonce from ALLOCATE's challenge) itself gets
re-challenged, and that second 401 propagates as an uncaught
TransactionFailed instead of being retried, which is what left "Connect
Karein" hanging on Connecting... forever once Cloudflare TURN was wired in.

This replaces the single retry with a bounded loop of the exact same
challenge-response logic aioice already implements -- no other behavior
changes, and a genuinely bad credential still fails (just after a few
attempts instead of one).
"""
from __future__ import annotations

from aioice import stun
from aioice.turn import TurnClientMixin, make_integrity_key
from aioice.utils import random_transaction_id

_MAX_AUTH_ATTEMPTS = 5


async def _request_with_retry(self, request):
    for attempt in range(_MAX_AUTH_ATTEMPTS):
        try:
            return await self.request(request)
        except stun.TransactionFailed as e:
            error_code = e.response.attributes["ERROR-CODE"][0]
            can_retry = (
                "NONCE" in e.response.attributes
                and self.username is not None
                and self.password is not None
                and (
                    (error_code == 401 and "REALM" in e.response.attributes)
                    or (error_code == 438 and self.realm is not None)
                )
            )
            if not can_retry or attempt == _MAX_AUTH_ATTEMPTS - 1:
                raise

            # update long-term credentials and retry with a fresh transaction id
            self.nonce = e.response.attributes["NONCE"]
            if error_code == 401:
                self.realm = e.response.attributes["REALM"]
            self.integrity_key = make_integrity_key(self.username, self.realm, self.password)
            request.transaction_id = random_transaction_id()


def apply() -> None:
    TurnClientMixin.request_with_retry = _request_with_retry
