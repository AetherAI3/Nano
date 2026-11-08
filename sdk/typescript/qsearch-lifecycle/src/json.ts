/**
 * Strict RFC 8259 parser for response bodies (CONTRACT.md section 2.1).
 *
 * Differences from `JSON.parse`, all fail-closed:
 * - a duplicate member name at any depth is refused (JSON.parse keeps the last);
 * - every number must be finite with magnitude <= 2^53-1 (JSON.parse rounds);
 * - nesting deeper than 64 containers is refused (the top-level value is depth 1);
 * - objects are built with a null prototype and `defineProperty`, so a member
 *   named `__proto__` is an ordinary key and cannot pollute any prototype.
 *
 * Grammar is otherwise exact RFC 8259: one value surrounded by optional
 * whitespace (space, tab, LF, CR), no comments, no trailing commas, no
 * unescaped control characters in strings, only the eight short escapes plus
 * `\uXXXX`. Escaped lone surrogates are accepted, as RFC 8259 permits.
 */
import { invalidResponse } from "./errors.js";
import { MAX_JSON_DEPTH, MAX_SAFE_INTEGER } from "./wire.js";

export type JsonValue = null | boolean | number | string | JsonArray | JsonObject;
export type JsonArray = readonly JsonValue[];
export interface JsonObject {
  readonly [key: string]: JsonValue;
}

/** Internal signal; never escapes the package (mapped to INVALID_RESPONSE). */
export class StrictJsonError extends Error {
  constructor() {
    super("strict JSON violation");
  }
}

const NUMBER = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y;
const HEX4 = /^[0-9A-Fa-f]{4}$/;

const SPACE = 0x20;
const TAB = 0x09;
const LF = 0x0a;
const CR = 0x0d;
const QUOTE = 0x22;
const BACKSLASH = 0x5c;

const SHORT_ESCAPES: ReadonlyMap<string, string> = new Map([
  ['"', '"'],
  ["\\", "\\"],
  ["/", "/"],
  ["b", "\b"],
  ["f", "\f"],
  ["n", "\n"],
  ["r", "\r"],
  ["t", "\t"],
]);

function fail(): never {
  throw new StrictJsonError();
}

class Parser {
  private pos = 0;

  constructor(private readonly text: string) {}

  parseDocument(): JsonValue {
    this.skipWhitespace();
    const value = this.parseValue(0);
    this.skipWhitespace();
    if (this.pos !== this.text.length) fail();
    return value;
  }

  private skipWhitespace(): void {
    const { text } = this;
    while (this.pos < text.length) {
      const c = text.charCodeAt(this.pos);
      if (c !== SPACE && c !== TAB && c !== LF && c !== CR) return;
      this.pos += 1;
    }
  }

  private parseValue(depth: number): JsonValue {
    switch (this.text[this.pos]) {
      case "{":
        return this.parseObject(depth + 1);
      case "[":
        return this.parseArray(depth + 1);
      case '"':
        return this.parseString();
      case "t":
        return this.parseLiteral("true", true);
      case "f":
        return this.parseLiteral("false", false);
      case "n":
        return this.parseLiteral("null", null);
      default:
        return this.parseNumber();
    }
  }

  private parseLiteral<T extends JsonValue>(word: string, value: T): T {
    if (!this.text.startsWith(word, this.pos)) fail();
    this.pos += word.length;
    return value;
  }

  private parseNumber(): number {
    NUMBER.lastIndex = this.pos;
    const match = NUMBER.exec(this.text);
    if (match === null) fail();
    const literal = match[0];
    const value = Number(literal);
    if (!Number.isFinite(value) || Math.abs(value) > MAX_SAFE_INTEGER) fail();
    this.pos += literal.length;
    return value;
  }

  private parseString(): string {
    const { text } = this;
    this.pos += 1; // opening quote
    let out = "";
    let runStart = this.pos;
    while (this.pos < text.length) {
      const c = text.charCodeAt(this.pos);
      if (c === QUOTE) {
        out += text.slice(runStart, this.pos);
        this.pos += 1;
        return out;
      }
      if (c < 0x20) fail();
      if (c === BACKSLASH) {
        out += text.slice(runStart, this.pos);
        out += this.parseEscape();
        runStart = this.pos;
        continue;
      }
      this.pos += 1;
    }
    return fail(); // unterminated
  }

  private parseEscape(): string {
    const { text } = this;
    const kind = text[this.pos + 1];
    if (kind === "u") {
      const hex = text.slice(this.pos + 2, this.pos + 6);
      if (!HEX4.test(hex)) fail();
      this.pos += 6;
      return String.fromCharCode(Number.parseInt(hex, 16));
    }
    const replacement = kind === undefined ? undefined : SHORT_ESCAPES.get(kind);
    if (replacement === undefined) fail();
    this.pos += 2;
    return replacement;
  }

  private parseArray(depth: number): JsonArray {
    if (depth > MAX_JSON_DEPTH) fail();
    this.pos += 1; // [
    const items: JsonValue[] = [];
    this.skipWhitespace();
    if (this.text[this.pos] === "]") {
      this.pos += 1;
      return items;
    }
    for (;;) {
      this.skipWhitespace();
      items.push(this.parseValue(depth));
      this.skipWhitespace();
      const ch = this.text[this.pos];
      this.pos += 1;
      if (ch === "]") return items;
      if (ch !== ",") fail();
    }
  }

  private parseObject(depth: number): JsonObject {
    if (depth > MAX_JSON_DEPTH) fail();
    this.pos += 1; // {
    const object = Object.create(null) as Record<string, JsonValue>;
    this.skipWhitespace();
    if (this.text[this.pos] === "}") {
      this.pos += 1;
      return object;
    }
    for (;;) {
      this.skipWhitespace();
      if (this.text[this.pos] !== '"') fail();
      const key = this.parseString();
      if (Object.prototype.hasOwnProperty.call(object, key)) fail(); // duplicate member name
      this.skipWhitespace();
      if (this.text[this.pos] !== ":") fail();
      this.pos += 1;
      this.skipWhitespace();
      const value = this.parseValue(depth);
      Object.defineProperty(object, key, { value, enumerable: true, writable: true, configurable: true });
      this.skipWhitespace();
      const ch = this.text[this.pos];
      this.pos += 1;
      if (ch === "}") return object;
      if (ch !== ",") fail();
    }
  }
}

/** Internal entry point: throws `StrictJsonError` on any violation. */
export function parseStrict(text: string): JsonValue {
  if (typeof text !== "string") fail();
  return new Parser(text).parseDocument();
}

/**
 * Parse `text` as strict JSON. Objects in the result have a null prototype.
 * Any violation throws `LifecycleClientError("INVALID_RESPONSE")` without
 * echoing the input.
 */
export function parseStrictJson(text: string): JsonValue {
  try {
    return parseStrict(text);
  } catch {
    throw invalidResponse(null);
  }
}
