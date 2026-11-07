import assert from "node:assert/strict";
import { test } from "node:test";

import { LifecycleClientError, parseStrictJson } from "../dist/index.js";

function rejects(text, label = JSON.stringify(text)) {
  let error;
  try {
    parseStrictJson(text);
  } catch (e) {
    error = e;
  }
  assert.ok(error instanceof LifecycleClientError, `must reject ${label}`);
  assert.equal(error.code, "INVALID_RESPONSE");
  assert.equal(error.message, "q-search lifecycle failed (INVALID_RESPONSE)");
  assert.equal(error.field, null);
  assert.equal(error.nextAction, "MANUAL_RECONCILIATION");
}

/** Null-prototype objects -> plain objects, structurally (keeps -0, unlike a JSON round trip). */
function plain(value) {
  if (Array.isArray(value)) return value.map(plain);
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, plain(v)]));
  }
  return value;
}

test("accepts RFC 8259 documents and agrees with JSON.parse on them", () => {
  const docs = [
    "{}",
    "[]",
    ' \t\r\n{ "a" : [ 1 , 2.5 , -0.25e+2 , 1E2 , true , false , null ] } \n',
    '"plain"',
    "0",
    "-0",
    "123",
    '{"esc":"\\" \\\\ \\/ \\b \\f \\n \\r \\t \\u0041\\u00e9\\u20AC"}',
    '{"pair":"\\ud83d\\ude00","raw":"é\u{1F600}"}',
    '{"nested":{"a":{"b":{"c":[{}]}}}}',
    '{"max":9007199254740991,"min":-9007199254740991,"tiny":1e-400,"frac":3600.0}',
    '{"a":1,"A":2,"a ":3}',
  ];
  for (const doc of docs) assert.deepStrictEqual(plain(parseStrictJson(doc)), JSON.parse(doc), doc);
});

test("escaped lone surrogates are accepted as RFC 8259 permits (Python parity)", () => {
  assert.equal(parseStrictJson('"\\ud800"'), "\ud800");
});

test("rejects duplicate member names at any depth, including escape-equivalent names", () => {
  rejects('{"a":1,"a":2}');
  rejects('{"a":1,"b":{"c":1,"c":1}}');
  rejects('[{"x":null,"x":null}]');
  rejects('{"a":1,"\\u0061":2}');
  rejects('{"__proto__":1,"__proto__":2}');
});

test("rejects non-finite and unsafe numbers and non-RFC number syntax", () => {
  for (const text of [
    "NaN",
    "Infinity",
    "-Infinity",
    '{"x":NaN}',
    "9007199254740992",
    "-9007199254740992",
    "9007199254740993",
    "1e16",
    "1e400",
    "-1e400",
    "01",
    "-01",
    "+1",
    ".5",
    "1.",
    "1.e5",
    "1e",
    "1e+",
    "0x10",
    "-",
    "--1",
    "1_000",
  ]) {
    rejects(text);
  }
});

test("rejects structural, literal, string and whitespace violations", () => {
  for (const text of [
    "",
    "   ",
    "{",
    "}",
    "[1,]",
    '{"a":1,}',
    "[,1]",
    '{,"a":1}',
    '{"a" 1}',
    '{"a":}',
    "{a:1}",
    "{'a':1}",
    "['a']",
    "[1 2]",
    '{"a":1 "b":2}',
    "{} {}",
    "{}x",
    "{}\u0000",
    "tru",
    "nul",
    "True",
    "undefined",
    "/* c */ {}",
    "{} // c",
    '"unterminated',
    '"raw\nnewline"',
    '"raw\ttab"',
    '"nul\u0000"',
    '"\u001f"',
    '"\\x41"',
    '"\\a"',
    '"\\u12"',
    '"\\u12G4"',
    '"\\U0041"',
    '"\\',
    "﻿{}",
    " {}",
    "{}\u000b",
    "\f{}",
    "{} ",
  ]) {
    rejects(text);
  }
});

test("depth: 64 nested containers accepted, 65 rejected (top-level value is depth 1)", () => {
  const arrays = (n) => "[".repeat(n) + "]".repeat(n);
  const objects = (n) => '{"k":'.repeat(n - 1) + "{}" + "}".repeat(n - 1);
  assert.ok(Array.isArray(parseStrictJson(arrays(64))));
  rejects(arrays(65), "65 arrays");
  assert.equal(typeof parseStrictJson(objects(64)), "object");
  rejects(objects(65), "65 objects");
  rejects("[".repeat(100_000), "deep unterminated");
  rejects("[".repeat(100_000) + "]".repeat(100_000), "very deep");
});

test("objects have a null prototype and __proto__ is an ordinary member", () => {
  const parsed = parseStrictJson('{"__proto__":{"polluted":true},"constructor":1,"hasOwnProperty":2,"nested":{"__proto__":[]}}');
  assert.equal(Object.getPrototypeOf(parsed), null);
  assert.deepStrictEqual(Object.keys(parsed), ["__proto__", "constructor", "hasOwnProperty", "nested"]);
  assert.equal(Object.getPrototypeOf(parsed.__proto__), null);
  assert.equal(parsed.__proto__.polluted, true);
  assert.ok(Array.isArray(parsed.nested.__proto__));
  assert.equal({}.polluted, undefined);
  assert.equal(Object.prototype.polluted, undefined);
});

test("errors never echo the input", () => {
  const secret = "s3cr3t-token-value";
  let error;
  try {
    parseStrictJson(`{"${secret}":1,"${secret}":2}`);
  } catch (e) {
    error = e;
  }
  assert.ok(!String(error).includes(secret));
  assert.ok(!JSON.stringify(error).includes(secret));
  assert.ok(!String(error.stack).includes(secret));
});

test("a 1 MiB document parses in bounded time", () => {
  const item = '{"k":"' + "x".repeat(1000) + '","n":1.5}';
  const doc = "[" + Array(1000).fill(item).join(",") + "]";
  assert.ok(doc.length > 1_000_000);
  const started = Date.now();
  assert.equal(parseStrictJson(doc).length, 1000);
  assert.ok(Date.now() - started < 2000);
});
