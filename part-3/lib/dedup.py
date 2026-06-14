"""URL de-duplication backed by Redis.

`SET key 1 NX EX ttl` is an atomic check-and-mark: it sets the key only if it
doesn't already exist and returns whether it did. That single round trip both
tells us if the key is new AND records it — no race between checking and marking.

The TTL means old keys eventually fall out of the set, so it never grows
unbounded. A week is plenty: by then the article is stale and the market it
relates to has likely resolved.
"""
import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import redis

# Query params that identify a campaign/click, not the content. Two URLs that
# differ only by these point at the same article and must dedup together.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "igshid",
    "ref", "ref_src", "cmpid", "spm", "smid",
}


def normalize_url(url: str) -> str:
    """Canonicalize a URL so cosmetic variants collapse to one dedup key.

    Lowercases scheme/host, drops the fragment, strips tracking query params,
    and removes a trailing slash. Used only as the *fallback* identity when a
    feed entry has no stable guid.
    """
    parts = urlsplit(url.strip())
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/") or "/",
        urlencode(kept),
        "",  # drop fragment
    ))


class Dedup:
    def __init__(self, redis_url: str, ttl_seconds: int, prefix: str = "seen:"):
        self._r = redis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl_seconds
        self._prefix = prefix

    def is_new(self, key: str) -> bool:
        digest = hashlib.sha256(key.encode()).hexdigest()[:24]
        was_set = self._r.set(self._prefix + digest, "1", nx=True, ex=self._ttl)
        return bool(was_set)
