"""
Brick Owl, the LEGO marketplace, as a PartCAD provider of the type 'store'.

PartCAD runs this file with 'runpy', once per call: the call is '__name__'
('avail', 'quote', ...), its input is the global 'request', and the answer is
whatever is left in the global 'output'. See "Providers" in PartCAD's
configuration documentation.

What is quoted is what Brick Owl's sellers have for sale now
('catalog/availability'): for each part, the lots to buy, whole, that between
them hold at least as many as are wanted and cost the least - in the colour
asked for or, when none is, in the cheapest of the first colours Brick Owl
lists the part in. A lot is paid for whole, at its unit price, however few of
it are wanted, so what is quoted is what would be paid, the pieces over what is
wanted included, before shipping and before any store's minimum order. The
quote says which lots, from which stores, with a link to each, what each costs
and how many pieces are over. Nothing is put in a cart at Brick Owl, so a quote
has no cart ID, and ordering is not implemented.

A part arrives here as its LDraw number (see //pub/universe/lego), and is
looked up in Brick Owl's catalogue by it ('catalog/id_lookup'), as an LDraw
number and as a design ID, and then by the numbers Rebrickable's public
catalogue says the part goes by.

Most of the catalogue API needs a key that Brick Owl's staff have approved for
catalogue access ("Access Brick Owl Catalog"); without it Brick Owl answers
"Access Denied", and this says so.
"""

import csv
import gzip
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

NOW = datetime.now(timezone.utc)

# What every LEGO part says it is sold as (see //pub/universe/lego).
VENDOR = "lego"
STORE = "brickowl"

if "request" not in globals():
    request = {"api": "caps"}

# Unique to this tool, as Brick Owl's terms ask.
USER_AGENT = "partcad-lego-%s/%s (+https://github.com/partcad/svc-commerce-brickowl)" % (
    STORE,
    request.get("partcad_version", "unknown"),
)


def _parameter(name, env=None):
    """A provider parameter as a string, or the environment variable, or None."""
    value = (request.get("parameters") or {}).get(name)
    if value is not None:
        value = str(value).strip()
    if not value and env:
        value = os.environ.get(env, "").strip()
    return value or None


KEY = _parameter("key", "BRICKOWL_API_KEY")
COLOUR = _parameter("color")
# 'new' or 'used': which lots count. Brick Owl tells conditions apart more
# finely ('newc', 'usedg', ...), and every one of them is one or the other.
CONDITION = (_parameter("condition") or "new").lower()
# Where the parts are to be shipped: only the stores that ship there are asked.
COUNTRY = (_parameter("country") or "US").upper()
# Brick Owl prices each lot in its store's own currency, and converts nothing:
# only the lots priced in this one are counted.
CURRENCY = (_parameter("currency") or "USD").upper()
# How many of the colours a part is listed in are asked about when no colour is.
COLOURS_TRIED = max(int(_parameter("coloursTried") or 3), 1)


# --- HTTP, with answers kept on disk ------------------------------------------
#
# A quote asks about every part in the cart, and 'avail' about each one of them
# beforehand, each in a separate run of this script. Without a cache every part
# is asked for twice per command, and every command asks again - and Brick Owl
# gives each kind of request a daily quota.


# What the stores share - the catalogue downloads every one of them reads - is
# kept once, beside each store's own answers.
SHARED = "shared"


def _cache_dir(scope=None):
    root = os.environ.get("PARTCAD_LEGO_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "partcad-lego"
    )
    return os.path.join(root, scope or STORE)


def _cache_path(key, scope=None, suffix=".json"):
    return os.path.join(_cache_dir(scope), hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + suffix)


def _cache_read(key, ttl, scope=None):
    try:
        path = _cache_path(key, scope)
        if time.time() - os.path.getmtime(path) > ttl.total_seconds():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _cache_store(path, data):
    """Write 'data' to 'path' whole or not at all: other runs may be reading it."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        partial = "%s.%d.tmp" % (path, os.getpid())
        with open(partial, "wb") as f:
            f.write(data)
        os.replace(partial, path)
    except OSError:
        pass  # a cache that cannot be written is a slower run, not a failed one


def _cache_write(key, value, scope=None):
    _cache_store(_cache_path(key, scope), json.dumps(value).encode("utf-8"))


class Response:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")


# How often a request that died on the way (a reset, a timeout) is sent again.
RETRIES = 3


def _urlopen(method, url, headers, body):
    data = body.encode("utf-8") if isinstance(body, str) else body
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return Response(response.status, response.read(), dict(response.headers))
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read(), dict(e.headers or {}))
        except (urllib.error.URLError, OSError) as e:
            if attempt == RETRIES - 1:
                raise Exception("%s could not be reached: %s" % (urllib.parse.urlsplit(url).netloc, e))
            time.sleep(2**attempt)


# Replaced by the tests, which answer every request themselves.
if "http" not in globals():
    http = _urlopen
if "pause" not in globals():
    pause = time.sleep


# --- what is sold as an LDraw part -------------------------------------------
#
# The SKU of a LEGO part is its LDraw number (see //pub/universe/lego), and no
# store sells by that number alone. What LEGO actually sells as one is what
# Rebrickable's public catalogue downloads say, and they need no account: which
# LEGO elements (a design in a colour) each part has been made as, and which
# parts are moulds of one another - the cone LDraw draws as 3942b is no longer
# made, and the one LEGO sells in its place is a later mould of it.
#
# A part LDraw draws together with another one (a wheel with its tyre on) is not
# a part here at all: //pub/universe/lego serves it as an assembly of the two,
# which PartCAD buys as its parts when no store sells it whole.
#
# This block is the same in each of the LEGO stores under //pub/svc/commerce
# that uses it; change them together.

REBRICKABLE_DOWNLOADS_URL = "https://cdn.rebrickable.com/media/downloads/"

# How long what the catalogue says is kept. It changes when Rebrickable
# publishes a refresh, which is weeks apart.
CATALOGUE_TTL = timedelta(days=7)

_FORMED_RE = re.compile(r"^(.+)-f\d+$")


def _download(url, ttl):
    """The body of 'url' as bytes, from the shared cache while it is younger than 'ttl'."""
    path = _cache_path("download:" + url, SHARED, ".bin")
    try:
        if time.time() - os.path.getmtime(path) <= ttl.total_seconds():
            with open(path, "rb") as f:
                return f.read()
    except OSError:
        pass
    response = http("GET", url, {"user-agent": USER_AGENT}, None)
    if response.status != 200:
        return None
    _cache_store(path, response.body)
    return response.body


_catalogue = None


def rebrickable_catalogue():
    """{part number: [element, ...]} and {part number: [its moulds]}.

    An element is (element ID, Rebrickable colour ID, LEGO design ID or "").
    Read from Rebrickable's public downloads and kept, digested, for a week.
    """
    global _catalogue
    if _catalogue is not None:
        return _catalogue
    digest_key = "rebrickable-catalogue-v2"
    cached = _cache_read(digest_key, CATALOGUE_TTL, SHARED)
    if cached is None:
        elements_gz = _download(REBRICKABLE_DOWNLOADS_URL + "elements.csv.gz", CATALOGUE_TTL)
        relations_gz = _download(REBRICKABLE_DOWNLOADS_URL + "part_relationships.csv.gz", CATALOGUE_TTL)
        if not elements_gz:
            raise Exception(
                "Rebrickable's catalogue (%selements.csv.gz) could not be downloaded" % REBRICKABLE_DOWNLOADS_URL
            )
        elements = {}
        for row in csv.DictReader(io.StringIO(gzip.decompress(elements_gz).decode("utf-8"))):
            elements.setdefault(row["part_num"], []).append([row["element_id"], row["color_id"], row["design_id"]])
        related = {}
        if relations_gz:
            for row in csv.DictReader(io.StringIO(gzip.decompress(relations_gz).decode("utf-8"))):
                # A mould ('M') is the same part made again, and fits where it
                # fits. An alternate ('A') is only alike - the Technic pin
                # without friction is an alternate of the one with it, and does
                # not hold an angle - and a print, a pair or a sub-part is not
                # the part at all.
                if row["rel_type"] == "M":
                    child, parent = row["child_part_num"], row["parent_part_num"]
                    related.setdefault(child, []).append(parent)
                    related.setdefault(parent, []).append(child)
        cached = {"elements": elements, "related": related}
        _cache_write(digest_key, cached, SHARED)
    _catalogue = cached
    return _catalogue


def rebrickable_parts(sku):
    """The Rebrickable part numbers that are LDraw part 'sku', best first.

    The part itself where Rebrickable numbers it the same, which for a part
    anybody stocks it nearly always does. A part LDraw draws in a pose of its
    own (76320-f2, a shock absorber compressed) is the part it is a pose of.
    Where none of those has ever been made in any colour, the numbers the two
    catalogues may tell the same mould apart by - a letter after it, which they
    do not always put on the same moulds - and then its moulds (see
    'rebrickable_moulds').
    """
    elements = rebrickable_catalogue()["elements"]
    found = []

    def add(part):
        if part not in found:
            found.append(part)

    add(sku)
    formed = _FORMED_RE.match(sku)
    if formed:
        base = formed.group(1)
        add(base)
        for suffix in range(1, 10):
            add("%sc%02d" % (base, suffix))
    if not any(elements.get(part) for part in found):
        stem = re.sub(r"[a-z]$", "", sku)
        add(stem)
        for letter in "abcdefgh":
            add(stem + letter)
    if not any(elements.get(part) for part in found):
        for part in rebrickable_moulds(sku):
            add(part)
    return [part for part in found if elements.get(part) or part == sku]


def rebrickable_moulds(sku):
    """The parts made in the place of LDraw part 'sku': its moulds.

    What a store sells when the part itself is no longer made: LEGO sells the
    Technic pin LDraw draws as 2780 as design 61332, a later mould of it.
    """
    related = rebrickable_catalogue()["related"]
    own = [sku] + ([_FORMED_RE.match(sku).group(1)] if _FORMED_RE.match(sku) else [])
    found = []
    for part in own:
        for other in sorted(related.get(part, [])):
            if other not in own and other not in found:
                found.append(other)
    return found


def lego_elements(sku, moulds=False):
    """[(element ID, Rebrickable colour ID, design ID, part number)] LDraw part 'sku' is made as.

    With 'moulds', those its moulds are made as instead (see 'rebrickable_moulds').
    """
    elements = rebrickable_catalogue()["elements"]
    parts = rebrickable_moulds(sku) if moulds else rebrickable_parts(sku)
    return [(e, c, d, part) for part in parts for e, c, d in elements.get(part, [])]


# --- asking Brick Owl ------------------------------------------------------------

API_URL = "https://api.brickowl.com/v1/"

# What is kept, and for how long. The catalogue changes seldom; what is for sale
# changes all day, and a quote is an estimate.
CATALOGUE_ANSWER_TTL = timedelta(days=30)
AVAILABILITY_TTL = timedelta(days=1)
# The most lots one availability answer holds.
LOTS_LIMIT = 300

_NO_KEY = (
    "Brick Owl needs an API key, approved by Brick Owl for catalogue access: set 'key' for this provider under "
    "'parameters:' in ~/.partcad/config.yaml, or BRICKOWL_API_KEY in the environment"
)
NOT_CONNECTED = (
    "The Brick Owl store does not send requests yet: the one line that does is for a person to write, in "
    "'_send()' in brickowl.py (see the README of //pub/svc/commerce/brickowl)"
)


class NotConnected(Exception):
    pass


def _send(url, headers):
    """Send one GET request to Brick Owl's API, and return the 'Response'."""
    return http("GET", url, headers, None)


# Replaced by the tests, which answer every request themselves.
if "send" not in globals():
    send = _send


def call(endpoint, params, ttl):
    """The JSON of one Brick Owl API answer, kept for 'ttl'."""
    query = dict({k: v for k, v in params.items() if v not in (None, "")}, key=KEY)
    url = API_URL + endpoint + "?" + urllib.parse.urlencode(sorted(query.items()))
    # Kept by what was asked, not by whom: the key is not part of it.
    cache_key = "call:%s?%s" % (
        endpoint,
        urllib.parse.urlencode(sorted((k, v) for k, v in query.items() if k != "key")),
    )
    cached = _cache_read(cache_key, ttl)
    if cached is not None:
        return cached
    response = send(url, {"user-agent": USER_AGENT, "accept": "application/json"})
    try:
        document = json.loads(response.text)
    except ValueError:
        raise Exception("Brick Owl answered %s with something that is not JSON (HTTP %d)" % (endpoint, response.status))
    error = document.get("error") if isinstance(document, dict) else None
    if error == "Invalid key":
        raise Exception("Brick Owl does not know the key given for this provider")
    if error == "Access Denied":
        raise Exception(
            "Brick Owl denied '%s' to this key: the catalogue needs a key Brick Owl's staff have approved for "
            "catalogue access ('Access Brick Owl Catalog'), which is asked for through its contact page" % endpoint
        )
    if response.status == 429:
        raise Exception("Brick Owl says this key has made too many requests; what was read before is kept")
    if error or not 200 <= response.status < 300:
        raise Exception(
            "Brick Owl answered %s with HTTP %d: %s" % (endpoint, response.status, error or response.text[:200])
        )
    _cache_write(cache_key, document)
    return document


def boids(part):
    """Every coloured BOID of LDraw part 'part' (as 'base-colour'), Brick Owl's order kept."""
    for number in dict.fromkeys([part] + rebrickable_parts(part)):
        for id_type in ("ldraw", "design_id"):
            found = (
                call("catalog/id_lookup", {"id": number, "type": "Part", "id_type": id_type}, CATALOGUE_ANSWER_TTL)
                or {}
            ).get("boids") or []
            coloured = [str(boid) for boid in found if "-" in str(boid)]
            if coloured:
                return coloured
    return []


def _name(text):
    return re.sub(r"[\s_-]+", "", text or "").casefold()


def colour_ids():
    """The Brick Owl colour IDs the 'color' parameter names, or None for any colour.

    A colour is Brick Owl's number or name, BrickLink's name for it, LEGO's, or
    an LDraw colour as 'ldraw:<code>'.
    """
    if not COLOUR:
        return None
    if COLOUR.isdigit():
        return {COLOUR}
    colours = call("catalog/color_list", {}, CATALOGUE_ANSWER_TTL) or {}
    found = set()
    for colour in colours.values() if isinstance(colours, dict) else colours:
        if COLOUR.lower().startswith("ldraw:"):
            if COLOUR.split(":", 1)[1] in [str(i) for i in colour.get("ldraw_ids") or [] if i is not None]:
                found.add(str(colour["id"]))
            continue
        names = [colour.get("name")] + list(colour.get("bl_names") or []) + list(colour.get("peeron_names") or [])
        names += [lego.get("raw_name") for lego in colour.get("lego_colors") or []]
        if _name(COLOUR) in {_name(name) for name in names if name}:
            found.add(str(colour["id"]))
    if not found:
        raise Exception("Brick Owl has no colour called '%s'" % COLOUR)
    return found


def lots(boid):
    """Every lot of coloured 'boid' for sale to COUNTRY, in CONDITION and CURRENCY, as dicts."""
    answer = (
        call("catalog/availability", {"boid": boid, "country": COUNTRY, "limit": LOTS_LIMIT}, AVAILABILITY_TTL) or {}
    )
    found = []
    # An object of lots, keyed by something no source agrees on: only the
    # values are read.
    for lot in answer.values() if isinstance(answer, dict) else answer:
        if not isinstance(lot, dict) or lot.get("open") is False:
            continue
        if not str(lot.get("con") or "").lower().startswith(CONDITION):
            continue
        if str(lot.get("base_currency") or "").upper() != CURRENCY:
            continue
        price = _decimal(lot.get("price"))
        quantity = _int(lot.get("qty"))
        if price is None or price <= 0 or quantity <= 0:
            continue
        found.append(
            {
                "price": price,
                "quantity": quantity,
                "store": lot.get("store_name"),
                "url": lot.get("url") or lot.get("store_url"),
                "bulk": _int(lot.get("bulk_qty")) or 1,
                "minimumOrder": lot.get("minimum_order"),
            }
        )
    return found


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _int(value):
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return 0


def whole_lots(offers, count):
    """The lots to buy at least 'count' items in, whole, for the least: (cost, lots), or None.

    'offers' is (unit price, quantity) per lot. A lot is bought as it is
    offered, so what it costs is its whole quantity at its unit price, whatever
    part of it is wanted; the pieces over 'count' are paid for all the same.
    Which lots those are is the cheapest way to cover 'count' with whole lots -
    not always the cheapest unit prices: two pieces from a lot of two at 0.50
    cost less than from a lot of a thousand at 0.01. None when all of 'offers'
    together hold fewer than 'count'.
    """
    offers = sorted(offers, key=lambda offer: (offer[0], offer[1]))
    if sum(quantity for _, quantity in offers) < count:
        return None
    # best[j] is the least cost of whole lots holding at least j items, and
    # took[i] the j at which lot i is part of that cheapest choice.
    best = [Decimal(0)] + [None] * count
    took = []
    for price, quantity in offers:
        cost = price * quantity
        taken = set()
        for j in range(count, 0, -1):
            before = best[max(0, j - quantity)]
            if before is not None and (best[j] is None or before + cost < best[j]):
                best[j] = before + cost
                taken.add(j)
        took.append(taken)
    chosen = []
    j = count
    for i in range(len(offers) - 1, -1, -1):
        if j > 0 and j in took[i]:
            chosen.append(offers[i])
            j = max(0, j - offers[i][1])
    return best[count], sorted(chosen)


def best(part, count):
    """(cost, line) of 'count' of LDraw part 'part' at Brick Owl, or (None, why not).

    The part's own BOIDs first, and only when none of them can fill the order,
    those of the moulds made in its place (see 'rebrickable_moulds').
    """
    found = boids(part)
    if not found:
        return None, "Brick Owl has no item that is LDraw part %s" % part
    cost, line = _best_of(part, found, count)
    if cost is not None:
        return cost, line
    for mould in rebrickable_moulds(part):
        others = [boid for boid in boids(mould) if boid not in found]
        if others:
            cost, other = _best_of(part, others, count)
            if cost is not None:
                return cost, other
    return None, line


def _best_of(part, coloured, count):
    """(cost, line) of 'count' from the lots of the BOIDs 'coloured', or (None, why not)."""
    wanted = colour_ids()
    if wanted is not None:
        coloured = [boid for boid in coloured if boid.rsplit("-", 1)[1] in wanted]
    else:
        coloured = coloured[:COLOURS_TRIED]
    options = []
    for boid in coloured:
        offered = lots(boid)
        chosen = whole_lots([(lot["price"], lot["quantity"]) for lot in offered], count)
        if chosen is not None:
            options.append((chosen[0], boid, offered, chosen[1]))
    if not options:
        return None, "Brick Owl has fewer than %d of %s for sale%s, shipping to %s in %s" % (
            count,
            part,
            " in colour %s" % COLOUR if COLOUR else "",
            COUNTRY,
            CURRENCY,
        )
    cost, boid, offered, bought = min(options, key=lambda option: (option[0], option[1]))
    lines = []
    remaining = list(offered)
    for price, quantity in bought:
        lot = next(lot for lot in remaining if (lot["price"], lot["quantity"]) == (price, quantity))
        remaining.remove(lot)
        lines.append(
            {
                "quantity": quantity,
                "unitPrice": float(price),
                "price": float(price * quantity),
                "store": lot["store"],
                "url": lot["url"],
                "minimumOrder": lot["minimumOrder"],
            }
        )
    pieces = sum(quantity for _, quantity in bought)
    return cost, {
        "boid": boid,
        "condition": CONDITION,
        "lots": lines,
        "bought": pieces,
        "excess": pieces - count,
        "lotsOnSale": len(offered),
        "onSale": sum(lot["quantity"] for lot in offered),
        "url": "https://www.brickowl.com/boid/%s" % boid,
    }


# --- the API -------------------------------------------------------------------


def order_lines(cart):
    """(vendor, SKU, how many of it to order), one per SKU, as PartCAD worked it out."""
    for name, part in (cart.get("parts") or {}).items():
        if not part.get("vendor") or not part.get("sku"):
            raise Exception("'%s' is not something a store sells: it has no vendor and SKU" % name)
    if "skus" in cart:
        return [(line["vendor"], str(line["sku"]), int(line["count"])) for line in cart["skus"]]
    needed = {}
    for part in (cart.get("parts") or {}).values():
        key = (part["vendor"], str(part["sku"]))
        per_sku = max(int(part.get("count_per_sku") or 1), 1)
        needed[key] = needed.get(key, 0) + -(-int(part["count"]) // per_sku)
    return [(vendor, sku, count) for (vendor, sku), count in sorted(needed.items())]


def avail(vendor, sku, count):
    if vendor != VENDOR or not sku:
        return {"available": False}
    if not KEY:
        # Said quietly: every LEGO part names this store, and most people asking
        # for a quote have not set it up. A quote asked of it says why.
        return {"available": False, "reason": _NO_KEY}
    try:
        cost, why = best(sku, count)
    except NotConnected as e:
        return {"available": False, "reason": str(e)}
    if cost is None:
        return {"available": False, "reason": why}
    return {"available": True}


def quote(cart):
    if not KEY:
        raise Exception(_NO_KEY)
    lines = []
    total = Decimal(0)
    for vendor, sku, count in order_lines(cart):
        if vendor != VENDOR:
            raise Exception("SKU '%s' is sold by '%s', not by LEGO" % (sku, vendor))
        cost, line = best(sku, count)
        if cost is None:
            raise Exception(line)
        total += cost
        lines.append(dict(line, sku=sku, count=count, price=float(cost)))
    return {
        "qos": cart.get("qos"),
        "price": float(total),
        "currency": CURRENCY,
        "expire": (NOW + AVAILABILITY_TTL).timestamp(),
        # Nothing was put in a cart at Brick Owl.
        "cartId": None,
        "lines": lines,
    }


if __name__ == "caps":
    raise Exception("Not supported by stores")

elif __name__ == "avail":
    output = avail(request.get("vendor"), str(request.get("sku") or ""), int(request.get("count") or 1))

elif __name__ == "quote":
    output = quote(request["cart"])

elif __name__ == "order":
    raise Exception("Not implemented")

elif __name__ != "__main__":
    raise Exception("Unknown API: {}".format(__name__))
