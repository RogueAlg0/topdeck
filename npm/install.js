"use strict";
// Postinstall for topdeck-prices: make sure the real `topdeck` Python CLI is
// available, since the bin shims forward to it. Never fails the npm install:
// every error path prints guidance and exits 0.
const { spawnSync } = require("node:child_process");
const pkg = require("./package.json");
const VERSION = pkg.version;

function manualInstallHint() {
  return (
    "You can install it by hand with:\n" +
    `  python3 -m pip install --user "topdeck==${VERSION}"`
  );
}

function main() {
  try {
    const existing = spawnSync("topdeck", ["--version"], { encoding: "utf8" });
    if (existing.status === 0) {
      console.log("topdeck is already installed, skipping the pip install.");
      return;
    }

    console.log(`Installing topdeck ${VERSION} with pip...`);
    const result = spawnSync(
      "python3",
      ["-m", "pip", "install", "--user", `topdeck==${VERSION}`],
      { stdio: "inherit" }
    );

    if (result.error && result.error.code === "ENOENT") {
      console.error(
        "python3 was not found on PATH, so the pip install was skipped.\n" +
          manualInstallHint()
      );
      return;
    }

    if (result.status !== 0) {
      console.error(
        "The pip install did not finish cleanly, so the topdeck command may not work yet.\n" +
          manualInstallHint()
      );
      return;
    }

    console.log("topdeck installed. Run `topdeck --help` to get started.");
  } catch (err) {
    console.error(`Postinstall hit a snag and skipped the pip install: ${err.message}`);
    console.error(manualInstallHint());
  }
  process.exit(0);
}

main();
