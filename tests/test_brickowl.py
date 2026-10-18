"""
Tests of 'brickowl.py', run the way PartCAD runs it: with 'runpy', the API call
as '__name__' and the call's input in the global 'request'.

Nothing is asked of Brick Owl or Rebrickable. The one function that would
reach Brick Owl, '_send()', is not written (see its docstring), and a fake
'send' stands in for it here: a catalogue and the lots for sale, in the shapes
Brick Owl's API answers in. A fake 'http' serves a few lines of Rebrickable's
catalogue downloads.

    python -m pytest tests
"""

import gzip
import json
import runpy
import urllib.parse
from decimal import Decimal
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "brickowl.py"
API = "https://api.brickowl.com/v1/"
DOWNLOADS = "https://cdn.rebrickable.com/media/downloads/"

ELEMENTS = [("300121", "3001", "4", "3001"), ("4121715", "2780", "0", "2780"), ("6279875", "61332", "0", "61332")]
RELATIONSHIPS = [("M", "61332", "2780")]

# (id, id_type) -> BOIDs, as 'catalog/id_lookup' answers.
LOOKUP = {
    ("3001", "ldraw"): ["771344-38", "771344-81", "771344-64"],
    ("2780", "design_id"): ["101010-38"],
    ("61332", "design_id"): ["202020-38"],
}
COLOURS = {
    "38": {
        "id": "38",
        "name": "Black",
        "ldraw_ids": ["0"],
        "bl_names": ["Black"],
        "lego_colors": [{"lego_id": "26", "raw_name": "BLACK"}],
    },
    "81": {
        "id": "81",
        "name": "Red",
        "ldraw_ids": ["4"],
        "bl_names": ["Red"],
        "lego_colors": [{"lego_id": "21", "raw_name": "BR.RED"}],
    },
    "64": {
        "id": "64",
        "name": "Medium Stone Gray",
        "ldraw_ids": ["71"],
        "bl_names": ["Light Bluish Gray"],
        "lego_colors": [{"lego_id": "194", "raw_name": "MED. ST-GREY"}],
    },
    "0": {"id": "0", "name": "Not Applicable", "ldraw_ids": [None], "bl_names": ["N/A"], "lego_colors": []},
}


def lot(price, qty, store="Store", con="new", currency="USD", open=True):
    return {
        "con": con,
        "lot_id": "1",
        "price": price,
        "qty": str(qty),
        "bulk_qty": "1",
        "url": "https://%s.brickowl.com/store/lot" % store.lower(),
        "store_name": store,
        "base_currency": currency,
        "minimum_order": "5.000",
        "open": open,
    }


AVAILABILITY = {
    "771344-38": [lot("0.150", 40, "Alpha"), lot("0.080", 10, "Beta"), lot("0.010", 500, "Gamma", currency="EUR")],
    "771344-81": [
        lot("0.050", 100, "Delta", con="usedg"),
        lot("0.200", 30, "Delta"),
        lot("0.010", 99, "Shut", open=False),
    ],
    "771344-64": [lot("0.300", 5, "Epsilon")],
    "101010-38": [lot("0.040", 20, "Zeta")],
    "202020-38": [lot("0.030", 1000, "Eta")],
}


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = headers or {"content-type": "application/json"}

    @property
    def text(self):
        return self.body.decode("utf-8")


def csv_gz(header, rows):
    return gzip.compress(("\n".join([header] + [",".join(row) for row in rows]) + "\n").encode("utf-8"))


class FakeHttp:
    """Rebrickable's downloads; and nothing of Brick Owl, which only 'send' reaches."""

    def __init__(self):
        self.files = {
            DOWNLOADS + "elements.csv.gz": csv_gz("element_id,part_num,color_id,design_id", ELEMENTS),
            DOWNLOADS + "part_relationships.csv.gz": csv_gz("rel_type,child_part_num,parent_part_num", RELATIONSHIPS),
        }

    def __call__(self, method, url, headers, body):
        assert not url.startswith(API), "Brick Owl is reached through 'send' alone"
        if url in self.files:
            return FakeResponse(200, self.files[url], {"content-type": "application/octet-stream"})
        return FakeResponse(404, "Not found", {"content-type": "text/html"})


class FakeBrickOwl:
    """What '_send()' would be handed, answered the way Brick Owl answers it."""

    def __init__(self):
        self.asked = []
        self.answer = None

    def __call__(self, url, headers):
        assert url.startswith(API)
        assert headers["user-agent"].startswith("partcad-lego-brickowl/")
        parsed = urllib.parse.urlsplit(url)
        endpoint = parsed.path[len("/v1/") :]
        query = dict(urllib.parse.parse_qsl(parsed.query))
        assert query.pop("key") == "the-key"
        self.asked.append((endpoint, query))
        if self.answer:
            return self.answer
        if endpoint == "catalog/id_lookup":
            assert query["type"] == "Part"
            return FakeResponse(200, json.dumps({"boids": LOOKUP.get((query["id"], query["id_type"]), [])}))
        if endpoint == "catalog/color_list":
            return FakeResponse(200, json.dumps(COLOURS))
        if endpoint == "catalog/availability":
            assert query["country"] == "US"
            return FakeResponse(
                200, json.dumps({str(i): lot for i, lot in enumerate(AVAILABILITY.get(query["boid"], []))})
            )
        return FakeResponse(404, json.dumps({"error": "Not found"}))

    def endpoints(self):
        return [endpoint for endpoint, _ in self.asked]


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PARTCAD_LEGO_CACHE", str(tmp_path))
    monkeypatch.delenv("BRICKOWL_API_KEY", raising=False)
    return tmp_path


def run(api, request, brickowl=None, **parameters):
    request = dict(request, api=api)
    request.setdefault("partcad_version", "0.0.0")
    request["parameters"] = {"key": "the-key", "color": "", "condition": "new", "country": "US", "currency": "USD"}
    request["parameters"].update(parameters)
    globals_ = {"request": request, "http": FakeHttp(), "pause": lambda s: None}
    if brickowl is not None:
        globals_["send"] = brickowl
    return runpy.run_path(str(SCRIPT), init_globals=globals_, run_name=api)


def avail(brickowl, sku, count=1, **parameters):
    request = {"vendor": "lego", "sku": sku, "count": count, "count_per_sku": 1}
    return run("avail", request, brickowl, **parameters)["output"]


def quote(brickowl, *lines, **parameters):
    cart = {
        "qos": None,
        "parts": {
            "//pub/universe/lego/ldraw/X:%s" % sku: {"vendor": "lego", "sku": sku, "count": n} for sku, n in lines
        },
        "skus": [{"vendor": "lego", "sku": sku, "count": n, "parts": []} for sku, n in lines],
    }
    return run("quote", {"cart": cart}, brickowl, **parameters)["output"]


# The one line that is not written.


def test_as_shipped_nothing_is_sent_to_brick_owl():
    output = avail(None, "3001")
    assert output["available"] is False and "_send()" in output["reason"]
    with pytest.raises(Exception, match="the one line that does is for a person to write"):
        quote(None, ("3001", 1))


def test_the_function_that_would_send_says_what_to_write_in_it():
    send = run("__main__", {})["_send"]
    assert 'return http("GET", url, headers, None)' in send.__doc__
    with pytest.raises(Exception, match="for a person to write"):
        send(API + "catalog/color_list?key=k", {})


# What a part is bought as.


def test_lots_are_bought_whole_and_the_excess_is_paid_for():
    result = quote(FakeBrickOwl(), ("3001", 20), color="Black")
    # The lot of 40 at 0.15, whole; the lot of 10 is too small, and the cheap
    # lot is priced in euros.
    assert result["price"] == pytest.approx(6.00)
    (line,) = result["lines"]
    assert line["boid"] == "771344-38"
    assert line["lots"] == [
        {
            "quantity": 40,
            "unitPrice": 0.15,
            "price": pytest.approx(6.0),
            "store": "Alpha",
            "url": "https://alpha.brickowl.com/store/lot",
            "minimumOrder": "5.000",
        }
    ]
    assert (line["count"], line["bought"], line["excess"]) == (20, 40, 20)
    assert line["url"] == "https://www.brickowl.com/boid/771344-38"
    assert result["cartId"] is None


def test_the_cheapest_whole_lots_are_not_always_the_cheapest_pieces():
    script = run("__main__", {})
    lots = [(Decimal("0.01"), 1000), (Decimal("0.50"), 2)]
    assert script["whole_lots"](lots, 2) == (Decimal("1.00"), [(Decimal("0.50"), 2)])
    assert script["whole_lots"](lots, 3) == (Decimal("10.00"), [(Decimal("0.01"), 1000)])
    assert script["whole_lots"](lots, 1003) is None


@pytest.mark.parametrize("colour", ["Red", "81", "ldraw:4", "BR.RED"])
def test_a_colour_is_brick_owl_s_or_bricklink_s_or_lego_s_or_an_ldraw_code(colour):
    (line,) = quote(FakeBrickOwl(), ("3001", 5), color=colour)["lines"]
    assert line["boid"] == "771344-81"


def test_a_used_lot_and_a_closed_store_do_not_count_unless_asked_for():
    (line,) = quote(FakeBrickOwl(), ("3001", 20), color="Red")["lines"]
    assert line["lots"][0]["unitPrice"] == 0.2
    (line,) = quote(FakeBrickOwl(), ("3001", 20), color="Red", condition="used")["lines"]
    assert (line["lots"][0]["unitPrice"], line["condition"]) == (0.05, "used")


def test_without_a_colour_the_cheapest_of_the_first_colours_listed_is_quoted():
    brickowl = FakeBrickOwl()
    (line,) = quote(brickowl, ("3001", 5), coloursTried="2")["lines"]
    # Black (5 of 10 at 0.08, whole: 0.80) beats Red (30 at 0.20); grey is
    # third, and not asked about.
    assert line["boid"] == "771344-38"
    assert [q["boid"] for e, q in brickowl.asked if e == "catalog/availability"] == ["771344-38", "771344-81"]


def test_a_part_is_looked_up_as_an_ldraw_number_and_then_as_a_design():
    brickowl = FakeBrickOwl()
    assert avail(brickowl, "2780", 20) == {"available": True}
    lookups = [(q["id"], q["id_type"]) for e, q in brickowl.asked if e == "catalog/id_lookup"]
    assert lookups == [("2780", "ldraw"), ("2780", "design_id")]


def test_a_part_whose_own_lots_cannot_fill_the_order_is_bought_as_a_mould_of_it():
    (line,) = quote(FakeBrickOwl(), ("2780", 80))["lines"]
    assert (line["sku"], line["boid"], line["price"]) == ("2780", "202020-38", pytest.approx(30.0))


def test_more_than_is_for_sale_is_not_available():
    assert avail(FakeBrickOwl(), "3001", 6, color="Light Bluish Gray") == {
        "available": False,
        "reason": "Brick Owl has fewer than 6 of 3001 for sale in colour Light Bluish Gray, shipping to US in USD",
    }


def test_a_part_brick_owl_does_not_have_is_not_available():
    assert avail(FakeBrickOwl(), "123456") == {
        "available": False,
        "reason": "Brick Owl has no item that is LDraw part 123456",
    }


# The key, and what Brick Owl says.


def test_without_a_key_nothing_is_available_and_nothing_is_asked():
    brickowl = FakeBrickOwl()
    output = avail(brickowl, "3001", key="")
    assert output["available"] is False and "BRICKOWL_API_KEY" in output["reason"]
    assert brickowl.asked == []
    with pytest.raises(Exception, match="BRICKOWL_API_KEY"):
        quote(brickowl, ("3001", 1), key="")


def test_a_key_without_catalogue_access_is_said_with_what_to_do():
    brickowl = FakeBrickOwl()
    brickowl.answer = FakeResponse(403, json.dumps({"error": "Access Denied"}))
    with pytest.raises(Exception, match="approved for catalogue access"):
        quote(brickowl, ("3001", 1))


def test_an_unknown_key_is_said():
    brickowl = FakeBrickOwl()
    brickowl.answer = FakeResponse(403, json.dumps({"error": "Invalid key"}))
    with pytest.raises(Exception, match="does not know the key"):
        quote(brickowl, ("3001", 1))


def test_answers_are_kept_and_not_asked_for_again():
    brickowl = FakeBrickOwl()
    avail(brickowl, "3001", color="Black")
    asked = list(brickowl.asked)
    quote(brickowl, ("3001", 1), color="Black")
    assert brickowl.asked == asked


def test_a_part_of_another_vendor_is_not_sold_here():
    request = {"vendor": "lowes", "sku": "3001", "count": 1, "count_per_sku": 1}
    assert run("avail", request, FakeBrickOwl())["output"] == {"available": False}
