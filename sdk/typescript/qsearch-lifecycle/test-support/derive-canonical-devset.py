"""Derive the TS canonicaliser dev set from the real Python reference.

From the repository root:
  python -I sdk/typescript/qsearch-lifecycle/test-support/derive-canonical-devset.py
    > sdk/typescript/qsearch-lifecycle/test-support/canonical-devset.json
Accepted vectors are nano_qsearch.canonical.canonical_json(json.loads(text)).
Refusals apply the CONTRACT v2 section 2.1 text rules on top of json.loads
(duplicates, depth > 64, replacement shape); "python_json_loads" records what
plain json.loads + canonical_json does with the same text.
"""
import base64, hashlib, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))
from nano_qsearch.canonical import canonical_json

B = chr(92)
def esc(hex4): return B + "u" + hex4

def wrap(desc_text, extra=""):
    return '{"descriptor":' + desc_text + extra + '}'

ACCEPT = [
  ("basic-sort", wrap('{"b":1,"a":2}', ',"z":true,"m":null,"a":false')),
  ("whitespace", ' \t\r\n{ "descriptor" : { "x" : [ 1 , 2 , { } , [ ] ] } , "k" : "v" } \n'),
  ("codepoint-order", wrap('{"' + chr(0xff61) + '":1,"' + chr(0x1F600) + '":2,"a":3,"B":4,"":5,"' + chr(0xe9) + '":6}')),
  ("codepoint-order-escaped", wrap('{"' + esc("ff61") + '":1,"' + esc("d83d") + esc("de00") + '":2}')),
  ("string-controls", wrap('{"s":"' + "".join(esc("%04x" % c) for c in range(0x20)) + '"}')),
  ("string-short-escapes", wrap('{"s":"' + B + '"' + B + B + B + '/' + B + 'b' + B + 'f' + B + 'n' + B + 'r' + B + 't"}')),
  ("string-raw-unicode", wrap('{"s":"' + chr(0x7f) + chr(0x2028) + chr(0x2029) + chr(0xe9) + chr(0x1F600) + chr(0xfeff) + '"}')),
  ("string-escaped-unicode", wrap('{"s":"' + esc("00e9") + esc("2028") + esc("007F") + esc("D83D") + esc("DE00") + esc("0041") + '"}')),
  ("descriptor-key-escaped", '{"' + esc("0064") + 'escriptor":{"a":1}}'),
  ("integers", wrap('{"i":[0,-0,7,-7,123456789012345678901234567890,-9007199254740993,9007199254740993]}')),
  ("integer-4300-digits", wrap('{"i":' + "9" * 4300 + '}')),
  ("integer-neg-4300-digits", wrap('{"i":-' + "9" * 4300 + '}')),
  ("floats-positional", wrap('{"f":[1.0,-0.0,0e0,0.0,100.0,1E2,1e+0,0.1,0.0001,4.35,123456789.123456789,1e15,9999999999999998.0,0.30000000000000004,3600.0]}')),
  ("floats-scientific", wrap('{"f":[1e16,1.5e-5,0.00001,5e-324,1.7976931348623157e308,1e22,1e21,-1.5E+300,1e-7,2.5e-05,1e100,12345678901234567890.0]}')),
  ("floats-underflow", wrap('{"f":[1e-400,-1e-400]}')),
  ("depth-64", wrap('{"d":' + "[" * 62 + "1" + "]" * 62 + '}')),
  ("empty-descriptor", wrap('{}')),
  ("nested-sort", wrap('{"z":{"b":[{"d":1,"c":2}],"a":0}}', ',"lineage":{"y":1,"x":2}')),
  ("literals", wrap('{"t":true,"f":false,"n":null}')),
]

REFUSE = [
  ("duplicate-key", wrap('{"a":1,"a":2}')),
  ("duplicate-key-after-escape", wrap('{"a":1,"' + esc("0061") + '":2}')),
  ("duplicate-top-level", '{"descriptor":{},"descriptor":{}}'),
  ("depth-65", wrap('{"d":' + "[" * 63 + "1" + "]" * 63 + '}')),
  ("lone-high-escaped", wrap('{"s":"' + esc("d83d") + '"}')),
  ("lone-low-escaped", wrap('{"s":"' + esc("de00") + '"}')),
  ("high-then-non-low", wrap('{"s":"' + esc("d83d") + esc("0041") + '"}')),
  ("lone-surrogate-key", wrap('{"' + esc("dbff") + '":1}')),
  ("nan", wrap('{"f":NaN}')),
  ("infinity", wrap('{"f":Infinity}')),
  ("neg-infinity", wrap('{"f":-Infinity}')),
  ("float-overflow", wrap('{"f":1e400}')),
  ("float-overflow-neg", wrap('{"f":-1e400}')),
  ("integer-4301-digits", wrap('{"i":' + "9" * 4301 + '}')),
  ("trailing-comma", wrap('{"a":1,}')),
  ("comment", wrap('{"a":1/*x*/}')),
  ("single-quotes", "{'descriptor':{}}"),
  ("leading-zero", wrap('{"a":01}')),
  ("bare-dot", wrap('{"a":1.}')),
  ("leading-dot", wrap('{"a":.5}')),
  ("plus-sign", wrap('{"a":+1}')),
  ("raw-control-in-string", wrap('{"a":"' + chr(1) + '"}')),
  ("bad-escape", wrap('{"a":"' + B + 'x"}')),
  ("short-unicode-escape", wrap('{"a":"' + B + 'u12"}')),
  ("top-level-array", '[{"descriptor":{}}]'),
  ("top-level-string", '"descriptor"'),
  ("descriptor-missing", '{"descriptr":{}}'),
  ("descriptor-string", '{"descriptor":"x"}'),
  ("descriptor-array", '{"descriptor":[]}'),
  ("descriptor-null", '{"descriptor":null}'),
  ("empty", ''),
  ("whitespace-only", ' '),
  ("trailing-garbage", wrap('{}') + 'x'),
  ("two-documents", wrap('{}') + wrap('{}')),
  ("python-literal-True", wrap('{"a":True}')),
]

def depth(v, d=1):
    if isinstance(v, dict): return max([d] + [depth(x, d + 1) for x in v.values()])
    if isinstance(v, list): return max([d] + [depth(x, d + 1) for x in v])
    return d - 1  # scalars add no depth: only containers count (top-level container = 1)

def no_dups(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)): raise ValueError("duplicate")
    return dict(pairs)

def contract(text):
    v = json.loads(text, object_pairs_hook=no_dups)
    if depth(v) > 64: raise ValueError("depth")
    if not isinstance(v, dict) or not isinstance(v.get("descriptor"), dict): raise ValueError("shape")
    body = canonical_json(v).encode("utf-8")
    desc = canonical_json(v["descriptor"]).encode("utf-8")
    return body, desc

b64 = lambda b: base64.b64encode(b).decode("ascii")
out = {"vectors": [], "refusals": []}
for name, text in ACCEPT:

    raw = text.encode("utf-8")
    body, desc = contract(text)
    out["vectors"].append({"name": name, "input_b64": b64(raw), "canonical_b64": b64(body),
        "descriptor_canonical_b64": b64(desc), "descriptor_digest": "sha256:" + hashlib.sha256(desc).hexdigest()})
for name, text in REFUSE:
    raw = text.encode("utf-8")
    try:
        contract(text); verdict = "ACCEPTED"
    except Exception as e:
        verdict = type(e).__name__
    plain = None
    try:
        canonical_json(json.loads(text)).encode("utf-8"); plain = "accepts"
    except Exception as e:
        plain = "refuses:" + type(e).__name__
    out["refusals"].append({"name": name, "input_b64": b64(raw), "contract": verdict, "python_json_loads": plain})
# byte-level refusals
for name, raw in [("bom-prefix", b"\xef\xbb\xbf" + b'{"descriptor":{}}'), ("invalid-utf8", b'{"descriptor":{"a":"\xff"}}'),
                  ("utf16le", '{"descriptor":{}}'.encode("utf-16-le")), ("overlong-utf8", b'{"descriptor":{"a":"\xc0\xaf"}}'),
                  ("encoded-surrogate-utf8", b'{"descriptor":{"a":"\xed\xa0\xbd"}}')]:
    try:
        contract(raw.decode("utf-8")); verdict = "ACCEPTED"
    except Exception as e:
        verdict = type(e).__name__
    out["refusals"].append({"name": name, "input_b64": b64(raw), "contract": verdict, "python_json_loads": "n/a"})
for r in out["refusals"]:
    assert r["contract"] != "ACCEPTED", r["name"]
out["python"] = sys.version.split()[0]
print(json.dumps(out, indent=1, sort_keys=True))
