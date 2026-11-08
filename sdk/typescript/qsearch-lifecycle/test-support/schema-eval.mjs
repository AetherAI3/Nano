// Tiny, test-only JSON Schema 2020-12 evaluator for exactly the keywords the
// frozen job-state schema uses. Any other keyword throws, so a schema change
// can never be silently ignored. Used as an independent oracle for the
// hand-written validator (differential testing), not shipped.

const ANNOTATIONS = new Set(["$schema", "$id", "title", "description", "$defs"]);
const ASSERTIONS = new Set([
  "type",
  "const",
  "enum",
  "pattern",
  "minimum",
  "maximum",
  "required",
  "properties",
  "additionalProperties",
  "$ref",
  "oneOf",
  "allOf",
  "if",
  "then",
  "else",
]);

const isObject = (v) => typeof v === "object" && v !== null && !Array.isArray(v);

function jsonEqual(a, b) {
  if (typeof a === "number" && typeof b === "number") return a === b; // 0 == -0 == 0.0
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((x, i) => jsonEqual(x, b[i]));
  }
  if (isObject(a) || isObject(b)) {
    if (!isObject(a) || !isObject(b)) return false;
    const ka = Object.keys(a).sort();
    const kb = Object.keys(b).sort();
    return jsonEqual(ka, kb) && ka.every((k) => jsonEqual(a[k], b[k]));
  }
  return a === b;
}

function typeMatches(type, v) {
  switch (type) {
    case "object":
      return isObject(v);
    case "array":
      return Array.isArray(v);
    case "string":
      return typeof v === "string";
    case "number":
      return typeof v === "number" && Number.isFinite(v);
    case "integer":
      return Number.isInteger(v);
    case "boolean":
      return typeof v === "boolean";
    case "null":
      return v === null;
    default:
      throw new Error(`unsupported type ${type}`);
  }
}

export function compileSchema(root) {
  const resolve = (ref) => {
    const match = /^#\/\$defs\/([A-Za-z]+)$/.exec(ref);
    if (!match || !root.$defs || !(match[1] in root.$defs)) throw new Error(`unsupported $ref ${ref}`);
    return root.$defs[match[1]];
  };

  function valid(schema, v) {
    for (const key of Object.keys(schema)) {
      if (!ANNOTATIONS.has(key) && !ASSERTIONS.has(key)) throw new Error(`unsupported keyword ${key}`);
    }
    if ("$ref" in schema && !valid(resolve(schema.$ref), v)) return false;
    if ("type" in schema && ![].concat(schema.type).some((t) => typeMatches(t, v))) return false;
    if ("const" in schema && !jsonEqual(schema.const, v)) return false;
    if ("enum" in schema && !schema.enum.some((e) => jsonEqual(e, v))) return false;
    if ("pattern" in schema && typeof v === "string" && !new RegExp(schema.pattern, "u").test(v)) return false;
    if ("minimum" in schema && typeof v === "number" && !(v >= schema.minimum)) return false;
    if ("maximum" in schema && typeof v === "number" && !(v <= schema.maximum)) return false;
    if (isObject(v)) {
      if ("required" in schema && !schema.required.every((k) => Object.prototype.hasOwnProperty.call(v, k))) return false;
      const props = schema.properties ?? {};
      for (const [k, sub] of Object.entries(props)) {
        if (Object.prototype.hasOwnProperty.call(v, k) && !valid(sub, v[k])) return false;
      }
      if ("additionalProperties" in schema && schema.additionalProperties !== false) {
        throw new Error("unsupported additionalProperties form");
      }
      if (schema.additionalProperties === false && !Object.keys(v).every((k) => Object.prototype.hasOwnProperty.call(props, k))) return false;
    }
    if ("allOf" in schema && !schema.allOf.every((s) => valid(s, v))) return false;
    if ("oneOf" in schema && schema.oneOf.filter((s) => valid(s, v)).length !== 1) return false;
    if ("if" in schema) {
      const branch = valid(schema.if, v) ? schema.then : schema.else;
      if (branch !== undefined && !valid(branch, v)) return false;
    }
    return true;
  }

  return (value) => valid(root, value);
}
