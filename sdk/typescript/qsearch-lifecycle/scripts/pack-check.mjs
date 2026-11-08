// Out-of-checkout proof: `npm pack`, install the tarball into a fresh temp
// project OUTSIDE the repository, run a copy of the shipped example there
// (imports `@nano/qsearch-lifecycle` and `@nano/qsearch-lifecycle/webcrypto`),
// then delete the temp directory. Needs no Python and no network beyond npm's
// local tarball install. Run via `npm run pack:check` (builds first).
import { execSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const packageDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(packageDir, "../../..");
const work = mkdtempSync(join(tmpdir(), "nano-qsearch-pack-"));
if (!relative(repoRoot, work).startsWith("..")) throw new Error("temp dir must be outside the repository");

const run = (command, cwd) => execSync(command, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });

try {
  const packed = JSON.parse(run(`npm pack --json --pack-destination "${work}"`, packageDir));
  const { filename, files } = packed[0];
  const tarball = join(work, filename);
  const sha256 = createHash("sha256").update(readFileSync(tarball)).digest("hex");
  const shipped = files.map((f) => f.path).sort();
  for (const forbidden of [/^src\//, /^test\//, /^scripts\//, /node_modules/, /\.tgz$/]) {
    const hit = shipped.find((p) => forbidden.test(p));
    if (hit) throw new Error(`tarball must not contain ${hit}`);
  }
  for (const required of ["package.json", "README.md", "dist/index.js", "dist/index.d.ts", "dist/webcrypto.js", "dist/fetch.js", "examples/offline-status.mjs"]) {
    if (!shipped.includes(required)) throw new Error(`tarball is missing ${required}`);
  }

  const app = join(work, "app");
  mkdirSync(app);
  writeFileSync(join(app, "package.json"), JSON.stringify({ name: "pack-check-app", private: true, type: "module" }));
  run(`npm install --no-audit --no-fund "${tarball}"`, app);
  const installed = readdirSync(join(app, "node_modules")).filter((n) => !n.startsWith("."));
  copyFileSync(join(app, "node_modules/@nano/qsearch-lifecycle/examples/offline-status.mjs"), join(app, "example.mjs"));
  const output = run("node example.mjs", app);
  if (!output.includes("offline-status: ok")) throw new Error(`example failed:\n${output}`);

  console.log(`tarball: ${filename}`);
  console.log(`tarball sha256: ${sha256}`);
  console.log(`files: ${shipped.length} (${shipped.join(", ")})`);
  console.log(`installed outside the checkout; node_modules: ${installed.join(", ")}`);
  console.log(output.trimEnd());
  console.log("pack:check: ok");
} finally {
  rmSync(work, { recursive: true, force: true });
}
