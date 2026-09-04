"""Mints short-lived Cloudflare TURN credentials for the SmallWebRTC voice
transport.

Without ANY ice_servers configured, SmallWebRTCRequestHandler defaults to an
empty list -- not even a STUN server -- which only ever connects when the
client and server share a network (e.g. local dev, where both sides trivially
match on loopback/host candidates). Render's Web Service networking doesn't
forward inbound UDP from the public internet to the container at all, so once
client and server are on different networks, STUN alone wouldn't be enough
either -- a TURN relay (reached outbound by both sides, which Render does
allow) is required for the deployed backend to connect at all.
"""
from __future__ import annotations

import os

import httpx
from loguru import logger
from pipecat.transports.smallwebrtc.connection import IceServer

# 24h -- comfortably longer than any single call, and Render's free tier
# restarts the process on every cold start anyway (which re-runs the startup
# fetch below), so there's no need for anything shorter or a refresh loop.
_TURN_TTL_SECONDS = 86400


async def fetch_ice_servers() -> list[IceServer] | None:
    """Returns None if CF_TURN_TOKEN_ID/CF_TURN_API_TOKEN aren't set, or if
    the Cloudflare call fails -- the caller falls back to no ICE servers,
    the same (locally-fine) behavior as before this existed."""
    key_id = os.getenv("CF_TURN_TOKEN_ID")
    api_token = os.getenv("CF_TURN_API_TOKEN")
    if not key_id or not api_token:
        logger.warning(
            "TURN: CF_TURN_TOKEN_ID/CF_TURN_API_TOKEN not set -- WebRTC voice will only "
            "connect when the client and server are on the same network (e.g. local dev)."
        )
        return None

    url = f"https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {api_token}"}, json={"ttl": _TURN_TTL_SECONDS}
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001 -- a TURN outage must degrade, never crash startup
        logger.warning(f"TURN: failed to fetch Cloudflare ICE servers: {type(e).__name__}: {e}")
        return None

    servers = [IceServer(**entry) for entry in data.get("iceServers", [])]
    logger.info(f"TURN: fetched {len(servers)} ICE server(s) from Cloudflare (ttl={_TURN_TTL_SECONDS}s)")
    return servers
