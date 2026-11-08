/**
 * Canonical replacement bytes (CONTRACT v2 section 2.1).
 *
 * Reproduces, byte for byte, Python
 * `canonical_json(json.loads(text)).encode("utf-8")` where `canonical_json` is
 * `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
 * allow_nan=False)` (`nano_qsearch/canonical.py`).
 *
 * The input is parsed by a lexeme-preserving strict parser (separate from the
 * response parser in `json.ts`, whose behaviour is unchanged), so a number
 * keeps the kind its lexeme has in Python's `json` module:
 * - no `.`/`e`/`E`: an integer, written as its exact decimal value (`-0` is `0`);
 *   more than 4300 digits is refused, as Python's `json.loads` refuses it;
 * - otherwise a float: the nearest IEEE-754 double, written as Python
 *   `repr(float)`; a value that overflows to infinity is refused.
 * Also refused: duplicate member names (after escape decoding), nesting deeper
 * than 64 (the top-level value is depth 1), lone surrogates (raw or escaped),
 * a BOM, invalid UTF-8, and anything RFC 8259 does not allow.
 *
 * Every refusal is `INVALID_REQUEST` (field `replacement`); nothing about the
 * input is echoed. This module holds no backslash literals on purpose: escape
 * characters are built from code points.
 */
import { copyUint8Array } from "./bytes.js";
import { LifecycleClientError, invalidRequest } from "./errors.js";
import { MAX_CANONICAL_DEPTH, MAX_INTEGER_DIGITS } from "./wire.js";

type Node =
  | { readonly kind: "raw"; readonly text: string } // literal or already-canonical number
  | { readonly kind: "string"; readonly value: string }
  | { readonly kind: "array"; readonly items: readonly Node[] }
  | { readonly kind: "object"; readonly members: ReadonlyArray<readonly [string, Node]> };

/** Internal signal; always mapped to INVALID_REQUEST before leaving the module. */
class Refused extends Error {
  constructor() {
    super("not canonicalisable");
  }
}

function refuse(): never {
  throw new Refused();
}

const BACKSLASH = String.fromCharCode(0x5c);
const QUOTE = String.fromCharCode(0x22);

/** JSON short escapes accepted on input: escape letter -> decoded character. */
const INPUT_ESCAPES: ReadonlyMap<string, string> = new Map([
  [QUOTE, QUOTE],
  [BACKSLASH, BACKSLASH],
  ["/", "/"],
  ["b", String.fromCharCode(0x08)],
  ["f", String.fromCharCode(0x0c)],
  ["n", String.fromCharCode(0x0a)],
  ["r", String.fromCharCode(0x0d)],
  ["t", String.fromCharCode(0x09)],
]);

/** Python `json.dumps` output escapes (ESCAPE_DCT): code unit -> escaped text. */
const OUTPUT_ESCAPES: ReadonlyMap<number, string> = new Map([
  [0x22, BACKSLASH + QUOTE],
  [0x5c, BACKSLASH + BACKSLASH],
  [0x08, BACKSLASH + "b"],
  [0x0c, BACKSLASH + "f"],
  [0x0a, BACKSLASH + "n"],
  [0x0d, BACKSLASH + "r"],
  [0x09, BACKSLASH + "t"],
]);

const WHITESPACE: ReadonlySet<number> = new Set([0x20, 0x09, 0x0a, 0x0d]);

function isDigit(code: number): boolean {
  return code >= 0x30 && code <= 0x39;
}

function isHexDigit(code: number): boolean {
  return isDigit(code) || (code >= 0x41 && code <= 0x46) || (code >= 0x61 && code <= 0x66);
}

/** True when `text` contains no unpaired UTF-16 surrogate. */
function isWellFormed(text: string): boolean {
  for (let i = 0; i < text.length; i += 1) {
    const unit = text.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = text.charCodeAt(i + 1); // NaN past the end
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
      i += 1;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      return false;
    }
  }
  return true;
}

class CanonicalParser {
  private pos = 0;

  constructor(private readonly text: string) {}

  parseDocument(): Node {
    this.skipWhitespace();
    const node = this.parseValue(0);
    this.skipWhitespace();
    if (this.pos !== this.text.length) refuse();
    return node;
  }

  private code(offset = 0): number {
    return this.text.charCodeAt(this.pos + offset);
  }

  private skipWhitespace(): void {
    while (this.pos < this.text.length && WHITESPACE.has(this.code())) this.pos += 1;
  }

  private parseValue(depth: number): Node {
    switch (this.text[this.pos]) {
      case "{":
        return this.parseObject(depth + 1);
      case "[":
        return this.parseArray(depth + 1);
      case QUOTE:
        return { kind: "string", value: this.parseString() };
      case "t":
        return this.parseLiteral("true");
      case "f":
        return this.parseLiteral("false");
      case "n":
        return this.parseLiteral("null");
      default:
        return { kind: "raw", text: this.parseNumber() };
    }
  }

  private parseLiteral(word: "true" | "false" | "null"): Node {
    if (!this.text.startsWith(word, this.pos)) refuse();
    this.pos += word.length;
    return { kind: "raw", text: word };
  }

  private digits(): void {
    const start = this.pos;
    while (isDigit(this.code())) this.pos += 1;
    if (this.pos === start) refuse();
  }

  /** RFC 8259 number grammar; returns the canonical text for its Python kind. */
  private parseNumber(): string {
    const start = this.pos;
    if (this.text[this.pos] === "-") this.pos += 1;
    if (this.text[this.pos] === "0") this.pos += 1;
    else if (this.code() >= 0x31 && this.code() <= 0x39) this.digits();
    else refuse();
    let isFloat = false;
    if (this.text[this.pos] === ".") {
      this.pos += 1;
      this.digits();
      isFloat = true;
    }
    if (this.text[this.pos] === "e" || this.text[this.pos] === "E") {
      this.pos += 1;
      if (this.text[this.pos] === "+" || this.text[this.pos] === "-") this.pos += 1;
      this.digits();
      isFloat = true;
    }
    const lexeme = this.text.slice(start, this.pos);
    return isFloat ? canonicalFloat(lexeme) : canonicalInteger(lexeme);
  }

  /** Decoded string; a lone surrogate (escaped or raw) is refused. */
  private parseString(): string {
    const { text } = this;
    this.pos += 1; // opening quote
    let out = "";
    let runStart = this.pos;
    while (this.pos < text.length) {
      const c = this.code();
      if (c === 0x22) {
        out += text.slice(runStart, this.pos);
        this.pos += 1;
        if (!isWellFormed(out)) refuse();
        return out;
      }
      if (c < 0x20) refuse();
      if (c === 0x5c) {
        out += text.slice(runStart, this.pos);
        out += this.parseEscape();
        runStart = this.pos;
        continue;
      }
      this.pos += 1;
    }
    return refuse(); // unterminated
  }

  private parseEscape(): string {
    const kind = this.text[this.pos + 1];
    if (kind === "u") {
      for (let i = 2; i < 6; i += 1) if (!isHexDigit(this.code(i))) refuse();
      const unit = Number.parseInt(this.text.slice(this.pos + 2, this.pos + 6), 16);
      this.pos += 6;
      return String.fromCharCode(unit);
    }
    const decoded = kind === undefined ? undefined : INPUT_ESCAPES.get(kind);
    if (decoded === undefined) refuse();
    this.pos += 2;
    return decoded;
  }

  private parseArray(depth: number): Node {
    if (depth > MAX_CANONICAL_DEPTH) refuse();
    this.pos += 1; // [
    const items: Node[] = [];
    this.skipWhitespace();
    if (this.text[this.pos] === "]") {
      this.pos += 1;
      return { kind: "array", items };
    }
    for (;;) {
      this.skipWhitespace();
      items.push(this.parseValue(depth));
      this.skipWhitespace();
      const ch = this.text[this.pos];
      this.pos += 1;
      if (ch === "]") return { kind: "array", items };
      if (ch !== ",") refuse();
    }
  }

  private parseObject(depth: number): Node {
    if (depth > MAX_CANONICAL_DEPTH) refuse();
    this.pos += 1; // {
    const members: Array<readonly [string, Node]> = [];
    const seen = new Set<string>();
    this.skipWhitespace();
    if (this.text[this.pos] === "}") {
      this.pos += 1;
      return { kind: "object", members };
    }
    for (;;) {
      this.skipWhitespace();
      if (this.text[this.pos] !== QUOTE) refuse();
      const key = this.parseString();
      if (seen.has(key)) refuse(); // duplicate member name after escape decoding
      seen.add(key);
      this.skipWhitespace();
      if (this.text[this.pos] !== ":") refuse();
      this.pos += 1;
      this.skipWhitespace();
      members.push([key, this.parseValue(depth)]);
      this.skipWhitespace();
      const ch = this.text[this.pos];
      this.pos += 1;
      if (ch === "}") return { kind: "object", members };
      if (ch !== ",") refuse();
    }
  }
}

/** Python `int(lexeme)` then `int.__repr__`: exact value, `-0` -> `0`. */
function canonicalInteger(lexeme: string): string {
  const digitCount = lexeme.startsWith("-") ? lexeme.length - 1 : lexeme.length;
  if (digitCount > MAX_INTEGER_DIGITS) refuse();
  return BigInt(lexeme).toString();
}

/**
 * Python `repr(float)`: shortest round-trip digits; positional when the
 * decimal exponent e satisfies -4 <= e < 16 (always with a fraction),
 * otherwise `d[.ddd]e+XX` / `d[.ddd]e-XX` with at least two exponent digits.
 * Non-finite values are refused.
 */
export function pythonFloatRepr(value: number): string {
  if (!Number.isFinite(value)) refuse();
  if (value === 0) return Object.is(value, -0) ? "-0.0" : "0.0";
  const sign = value < 0 ? "-" : "";
  // toExponential() without an argument gives the shortest digits that round-trip.
  const [mantissa = "", exponentText = ""] = Math.abs(value).toExponential().split("e");
  const digits = mantissa.replace(".", "");
  const exponent = Number(exponentText);
  if (exponent >= -4 && exponent < 16) {
    if (exponent < 0) return `${sign}0.${"0".repeat(-exponent - 1)}${digits}`;
    const whole = exponent + 1;
    if (digits.length <= whole) return `${sign}${digits}${"0".repeat(whole - digits.length)}.0`;
    return `${sign}${digits.slice(0, whole)}.${digits.slice(whole)}`;
  }
  const fraction = digits.length > 1 ? `.${digits.slice(1)}` : "";
  const magnitude = String(Math.abs(exponent)).padStart(2, "0");
  return `${sign}${digits.slice(0, 1)}${fraction}e${exponent < 0 ? "-" : "+"}${magnitude}`;
}

function canonicalFloat(lexeme: string): string {
  return pythonFloatRepr(Number(lexeme));
}

/** Code-point order (Python `str` comparison), not UTF-16 code-unit order. */
export function compareCodePoints(a: string, b: string): number {
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    const x = a.codePointAt(i) ?? 0;
    const y = b.codePointAt(j) ?? 0;
    if (x !== y) return x < y ? -1 : 1;
    i += x > 0xffff ? 2 : 1;
    j += y > 0xffff ? 2 : 1;
  }
  if (i < a.length) return 1;
  if (j < b.length) return -1;
  return 0;
}

/** Python `json.dumps(..., ensure_ascii=False)` string encoding. */
function quote(value: string): string {
  let out = QUOTE;
  let runStart = 0;
  for (let i = 0; i < value.length; i += 1) {
    const unit = value.charCodeAt(i);
    if (unit >= 0x20 && unit !== 0x22 && unit !== 0x5c) continue;
    out += value.slice(runStart, i);
    out += OUTPUT_ESCAPES.get(unit) ?? `${BACKSLASH}u00${unit.toString(16).padStart(2, "0")}`;
    runStart = i + 1;
  }
  return out + value.slice(runStart) + QUOTE;
}

function emit(node: Node): string {
  switch (node.kind) {
    case "raw":
      return node.text;
    case "string":
      return quote(node.value);
    case "array":
      return `[${node.items.map(emit).join(",")}]`;
    case "object": {
      const sorted = [...node.members].sort((x, y) => compareCodePoints(x[0], y[0]));
      return `{${sorted.map(([key, value]) => `${quote(key)}:${emit(value)}`).join(",")}}`;
    }
  }
}

/** JSON text (or UTF-8 bytes, fatal decode, BOM refused) -> parsed canonical tree. */
function parseInput(input: unknown): Node {
  let text: string;
  if (typeof input === "string") {
    text = input;
  } else {
    const bytes = copyUint8Array(input);
    if (bytes === null) refuse();
    // ignoreBOM keeps a leading U+FEFF, which the parser then refuses (as json.loads does).
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
  }
  if (!isWellFormed(text)) refuse(); // a raw lone surrogate can never round-trip through UTF-8
  return new CanonicalParser(text).parseDocument();
}

function guarded<T>(work: () => T): T {
  try {
    return work();
  } catch (error: unknown) {
    if (error instanceof LifecycleClientError) throw error;
    throw invalidRequest("replacement");
  }
}

const encoder = new TextEncoder();

/**
 * Canonical UTF-8 bytes of any JSON document given as text or UTF-8 bytes.
 * Throws `LifecycleClientError("INVALID_REQUEST")`, field `replacement`.
 */
export function canonicalJson(input: string | Uint8Array): Uint8Array {
  return guarded(() => encoder.encode(emit(parseInput(input))));
}

export interface CanonicalReplacement {
  /** The request body: canonical bytes of the whole replacement. */
  readonly body: Uint8Array;
  /** Canonical bytes of `replacement.descriptor` (the new request's digest input). */
  readonly descriptor: Uint8Array;
}

/**
 * The replacement must be a JSON object whose `descriptor` member is an
 * object. Throws `INVALID_REQUEST` (field `replacement`) otherwise.
 */
export function canonicalizeReplacement(input: string | Uint8Array): CanonicalReplacement {
  return guarded(() => {
    const root = parseInput(input);
    if (root.kind !== "object") refuse();
    const descriptor = root.members.find(([key]) => key === "descriptor")?.[1];
    if (descriptor === undefined || descriptor.kind !== "object") refuse();
    return Object.freeze({ body: encoder.encode(emit(root)), descriptor: encoder.encode(emit(descriptor)) });
  });
}
