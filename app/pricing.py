"""Own pricing of Claude calls (standard library only). The rules and the evidence for them are in
research/pricing/FINDINGS.md; they reproduce ccusage 20.0.48 on the made-up transcripts of research/pricing/probe.py.

Rules proven against ccusage 20.0.48 (black box, research/pricing/probe.py; evidence in FINDINGS.md):
  fast mode   speed "fast" multiplies the whole cost by the list's provider_specific_entry.fast of the model,
              else by ccusage's own table (opus-4-6 and 4-7: 6, opus-4-8: 2), else 1
  inference_geo  ccusage ignores it. The app follows Anthropic instead: "us" exactly costs 1.1x on every
                 token type for Claude Opus 4.6, Sonnet 4.6 and every later model, none for earlier ones; it stacks with
                 fast mode. So the app and ccusage differ on those calls (FINDINGS.md, case 2)
  tier at 200k   each token type is split at 200,000 tokens of its own: the part above uses the `_above_200k_tokens` price
  other tiers    (100k, 272k, ...) the whole message switches when input + cache writes + cache read is above N thousand
  threshold      always '>' (a message exactly on the threshold is priced at the base price)
  costUSD        a call that carries a numeric costUSD costs exactly that (also when the model has no price)
  duplicates     of one message.id + requestId the line with the most tokens (input + output + both cache writes
                 + cache read) counts; on a tie the one with the higher cost

Price list: LiteLLM's model_prices_and_context_window.json, Claude entries only. The app ships a copy
(prices.json); a refresh fetches the current list, keeps the last good copy in the cache folder and falls
back to it, then to the shipped one. The fetched file is untrusted data: only the price keys below are read
and only finite, non-negative numbers are accepted.

  load(cache_dir)                       -> (prices, offline)   fetch (unless USAGE_CLAUDE_OFFLINE=1), store, fall back
  sanitize(raw)                         -> {model id: entry}   what load() uses; entries that cannot be priced are dropped
  find(prices, model)                   -> entry or None
  message_cost(p, i, out, e5, e1, cr, fast=False, us=False)   -> USD of one message
  cache_saved(p, i, e5, e1, cr, fast=False, us=False)         -> USD saved by the cache reads of one message
"""
import json
import math
import os
import re
import ssl
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
TIMEOUT_S = 10
MAX_BYTES = 20 * 1024 * 1024
SYSTEM_CA = "/etc/ssl/cert.pem"
MAX_PRICE = 1.0  # USD per token; anything at or above is not a price (a real one is about 1e-5)
BASE_KEYS = ("input_cost_per_token", "output_cost_per_token", "cache_creation_input_token_cost",
             "cache_creation_input_token_cost_above_1hr", "cache_read_input_token_cost")
REQUIRED = ("input_cost_per_token", "output_cost_per_token", "cache_creation_input_token_cost",
            "cache_read_input_token_cost")  # the 1-hour write price falls back to the 5-minute one
MAX_FAST = 100.0
SPLIT_K = 200  # a tier at exactly 200k is split per token type (see _cost); every other tier switches the whole message
# ccusage's own fast-mode multipliers for ids whose list entry has none (matched as a prefix); the numbers
# were measured, they are not in the price list (FINDINGS.md, case 1)
FAST_PREFIX = (("claude-opus-4-6", 6.0), ("claude-opus-4-7", 6.0), ("claude-opus-4-8", 2.0))
US_MULT = 1.1  # data residency: a call with usage.inference_geo "us" (see _us_of for the models)
# Opus 4.6, Sonnet 4.6 and later: major.minor from 4.6 on, every 5.x, fable and mythos. A trailing date is not a minor.
US_ID = re.compile(r"claude-(?:(?:opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?!\d)|(?:fable|mythos)-)")
TIER = re.compile(r"^(" + "|".join(BASE_KEYS) + r")_above_(\d{1,6})k_tokens$")
DATE = re.compile(r"[-@]\d{8}$")
VERSION = re.compile(r"-v\d+(:\d+)?$")


def _price(v):
    """A usable price, else None (bool, string, NaN, infinity, negative and huge values are refused)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) and 0 <= v < MAX_PRICE else None


def _mult(v):
    """A usable multiplier, else None (bool, string, NaN, infinity, zero, negative and huge values are refused)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 < v <= MAX_FAST:
        return None
    return float(v)


def _fast_of(model, e):
    """The fast-mode multiplier of a model: a stored `_fast` (a copy kept by load()), else the list's
    provider_specific_entry.fast, else ccusage's own table by id prefix, else None."""
    v = _mult(e.get("_fast"))
    if v is None:
        pse = e.get("provider_specific_entry")
        v = _mult(pse.get("fast")) if isinstance(pse, dict) else None
    if v is None:
        for prefix, mult in FAST_PREFIX:
            if model.startswith(prefix):
                v = mult
    return v


def _us_of(model):
    """Does the data residency surcharge apply to this model id? (4.6 and later; 3.x, 4, 4.1, 4.5 and older: no)"""
    m = US_ID.search(model)
    return bool(m) and (m.group(1) is None or (int(m.group(1)), int(m.group(2) or 0)) >= (4, 6))


def sanitize(raw):
    """{model id: entry} from a LiteLLM-format dict: only `claude-*` and `anthropic/claude-*` keys, only the
    price keys above (base and `_above_<N>k_tokens`). An entry without the required prices is dropped.
    An entry is the flat dict of valid prices plus "_tiers", the sorted thresholds N (thousands of tokens), and
    "_fast", the fast-mode multiplier when the model has one, and "_us", the data residency multiplier when it applies
    to the model id (decided by the id, the list is not asked)."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for name, e in raw.items():
        if not isinstance(name, str) or not isinstance(e, dict):
            continue
        model = name[len("anthropic/"):] if name.startswith("anthropic/") else name
        if not model.startswith("claude-") or (model != name and model in out):
            continue  # a bare key wins over its `anthropic/` twin
        p, tiers = {}, set()
        for k in BASE_KEYS:
            v = _price(e.get(k))
            if v is not None:
                p[k] = v
        for k, v in e.items():
            m = TIER.match(k) if isinstance(k, str) else None
            v = _price(v) if m else None
            if v is not None:
                p[k] = v
                tiers.add(int(m.group(2)))
        if all(k in p for k in REQUIRED):
            p["_tiers"] = sorted(tiers)
            fast = _fast_of(model, e)
            if fast is not None:
                p["_fast"] = fast
            if _us_of(model):
                p["_us"] = US_MULT
            out[model] = p
    return out


def find(prices, model):
    """The entry for a transcript's model id: the exact id first, then the same id without a provider prefix
    (`anthropic/`, `us.anthropic.`), then without a trailing date or `-v1:0`. No fuzzy matching: an id we
    cannot map stays unpriced."""
    if not isinstance(model, str):
        return None
    ids, m = [model], model
    for sep in ("anthropic/", "anthropic."):
        if sep in m:
            m = m.rsplit(sep, 1)[1]
    for step in (lambda s: s, lambda s: VERSION.sub("", s), lambda s: DATE.sub("", s)):
        m = step(m)
        if m not in ids:
            ids.append(m)
    for i in ids:
        if i in prices:
            return prices[i]
    return None


def _tier_suffix(p, total_in):
    """Suffix of the highest tier the message exceeds (the whole message switches), else "". Tiers at exactly
    200k are not handled here (see _cost)."""
    sfx = ""
    for n in p["_tiers"]:
        if n != SPLIT_K and total_in > n * 1000:  # '>' : a message exactly on the threshold keeps the base price
            sfx = "_above_%dk_tokens" % n
    return sfx


def _get(p, base, sfx):
    return p.get(base + sfx, p.get(base)) if sfx else p.get(base)


def _rates(p, sfx):
    """(input, output, 5-minute write, 1-hour write, read) prices at one tier suffix; a missing tier key falls back
    to the base price of that field, a missing 1-hour price to the 5-minute one."""
    c5 = _get(p, "cache_creation_input_token_cost", sfx)
    c1 = _get(p, "cache_creation_input_token_cost_above_1hr", sfx)
    return (_get(p, "input_cost_per_token", sfx), _get(p, "output_cost_per_token", sfx), c5,
            c5 if c1 is None else c1, _get(p, "cache_read_input_token_cost", sfx))


def _cost(p, toks):
    """USD of one message at standard speed; toks = (input, output, 5m write, 1h write, read)."""
    if SPLIT_K in p["_tiers"]:
        lo, hi, t = _rates(p, ""), _rates(p, "_above_%dk_tokens" % SPLIT_K), SPLIT_K * 1000
        return sum(a * min(n, t) + b * max(0, n - t) for n, a, b in zip(toks, lo, hi))
    r = _rates(p, _tier_suffix(p, toks[0] + toks[2] + toks[3] + toks[4]))
    return sum(n * x for n, x in zip(toks, r))


def _mult_of(p, fast, us):
    return (p.get("_fast", 1.0) if fast else 1.0) * (p.get("_us", 1.0) if us else 1.0)


def message_cost(p, i, out, e5, e1, cr, fast=False, us=False):
    """USD of one message: input, output, 5-minute write, 1-hour write and cache read, each at the model's own
    price. fast: the call ran in fast mode (usage.speed "fast"), which multiplies the whole cost. us: the call has
    usage.inference_geo "us", which multiplies it again (on models where that applies)."""
    return _cost(p, (i, out, e5, e1, cr)) * _mult_of(p, fast, us)


def cache_saved(p, i, e5, e1, cr, fast=False, us=False):
    """USD the cache reads saved against paying the input price for them, at the same tier rule."""
    mult = _mult_of(p, fast, us)
    if SPLIT_K in p["_tiers"]:
        lo, hi, t = _rates(p, ""), _rates(p, "_above_%dk_tokens" % SPLIT_K), SPLIT_K * 1000
        return mult * (min(cr, t) * (lo[0] - lo[4]) + max(0, cr - t) * (hi[0] - hi[4]))
    r = _rates(p, _tier_suffix(p, i + e5 + e1 + cr))
    return mult * cr * (r[0] - r[4])


# --- the price list: shipped, stored, fetched -------------------------------------------

class _SameHost(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        a, b = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if b.scheme != "https" or b.hostname != a.hostname:
            return None  # surfaces as an HTTP error: the fetch fails and the stored list is used
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url=URL):
    """The parsed JSON of the price list; raises on any failure."""
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and os.path.exists(SYSTEM_CA):
        ctx.load_verify_locations(SYSTEM_CA)  # a python.org build has no trust store of its own; verification stays on
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _SameHost())
    req = urllib.request.Request(url, headers={"User-Agent": "usage-claude-pricing"})
    with opener.open(req, timeout=TIMEOUT_S) as r:
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("price list too large")
    return json.loads(body)


def _read_list(path):
    """Sanitized prices of a stored or shipped file ({"models": {...}}), or {} when unreadable."""
    try:
        with open(path) as fh:
            return sanitize(json.load(fh).get("models"))
    except (OSError, ValueError, AttributeError, RecursionError):
        return {}


def load(cache_dir, fetcher=fetch):
    """(prices, offline). Fetches the current list and stores its Claude entries in cache_dir; on any
    failure (or with USAGE_CLAUDE_OFFLINE=1, which skips the request) uses the stored copy, else the
    shipped one, and offline is True."""
    stored = os.path.join(cache_dir, "prices.json")
    if os.environ.get("USAGE_CLAUDE_OFFLINE") != "1":
        try:
            fresh = sanitize(fetcher())
            if fresh:
                tmp = "%s.%d.tmp" % (stored, os.getpid())
                os.makedirs(cache_dir, mode=0o700, exist_ok=True)
                with open(tmp, "w") as fh:
                    json.dump({"models": fresh}, fh, separators=(",", ":"), sort_keys=True)
                os.replace(tmp, stored)
                return fresh, False
        except Exception:  # network, certificate, timeout, size, bad JSON: whatever it is, fall back
            pass
    return _read_list(stored) or _read_list(os.path.join(HERE, "prices.json")), True
