"use strict";
// Tests for the bin shims: they must forward args, stdin, and exit codes to
// the real binary found on PATH, and fail helpfully when it is absent.
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SHIMS = {
  topdeck: path.join(__dirname, "..", "bin", "topdeck.js"),
  "topdeck-mcp": path.join(__dirname, "..", "bin", "topdeck-mcp.js"),
};

function makeTempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "topdeck-shim-"));
}

function writeExecutable(dir, name, body) {
  const p = path.join(dir, name);
  fs.writeFileSync(p, body);
  fs.chmodSync(p, 0o755);
  return p;
}

function runShim(shimPath, binDir, args, input) {
  return spawnSync(process.execPath, [shimPath, ...args], {
    input,
    encoding: "utf8",
    // Prepend: the fake binary wins, but system tools (cat, sh) still resolve.
    env: { ...process.env, PATH: binDir + path.delimiter + (process.env.PATH || "") },
  });
}

for (const [name, shimPath] of Object.entries(SHIMS)) {
  describe(`shim ${name}`, () => {
    it("forwards args and stdin and keeps a zero exit code", () => {
      const dir = makeTempDir();
      writeExecutable(
        dir,
        name,
        '#!/bin/sh\necho "ARGS:$*"\ncat\nexit 0\n'
      );
      const r = runShim(shimPath, dir, ["price", "mtg", "bolt"], "hello\n");
      assert.equal(r.status, 0);
      assert.match(r.stdout, /ARGS:price mtg bolt/);
      assert.match(r.stdout, /hello/);
    });

    it("propagates a non-zero exit code", () => {
      const dir = makeTempDir();
      writeExecutable(dir, name, "#!/bin/sh\nexit 42\n");
      const r = runShim(shimPath, dir, []);
      assert.equal(r.status, 42);
    });

    it("exits 1 with install guidance when the binary is absent", () => {
      const dir = makeTempDir(); // empty: no binary on PATH
      const r = runShim(shimPath, dir, []);
      assert.equal(r.status, 1);
      assert.match(r.stderr, new RegExp(`${name} is not installed`));
      assert.match(r.stderr, /python3 -m pip install --user topdeck/);
    });

    it("skips itself when npm links the shim onto PATH", () => {
      // Real-world layout: npm puts a `topdeck` symlink to this very shim on
      // PATH. The shim must not find itself and recurse; it must find the
      // real binary in the later directory.
      const selfDir = makeTempDir();
      fs.symlinkSync(shimPath, path.join(selfDir, name));
      const realDir = makeTempDir();
      writeExecutable(
        realDir,
        name,
        '#!/bin/sh\necho "ARGS:$*"\nexit 0\n'
      );
      const r = spawnSync(process.execPath, [shimPath, "price", "mtg"], {
        encoding: "utf8",
        timeout: 10000,
        env: {
          ...process.env,
          PATH:
            selfDir +
            path.delimiter +
            realDir +
            path.delimiter +
            (process.env.PATH || ""),
        },
      });
      assert.equal(r.status, 0);
      assert.match(r.stdout, /ARGS:price mtg/);
    });

    it("does not recurse when only itself is on PATH", () => {
      const selfDir = makeTempDir();
      fs.symlinkSync(shimPath, path.join(selfDir, name));
      const r = spawnSync(process.execPath, [shimPath], {
        encoding: "utf8",
        timeout: 10000,
        env: {
          ...process.env,
          PATH: selfDir + path.delimiter + "/nonexistent-topdeck-test",
        },
      });
      assert.equal(r.status, 1);
      assert.match(r.stderr, new RegExp(`${name} is not installed`));
    });
  });
}
