import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("mobile CSS does not force a wider-than-390 layout", async () => {
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(css, /html, body, \.parameter-app \{[\s\S]*overflow-x: clip;/);
  assert.match(css, /@media \(max-width: 1120px\) \{[\s\S]*\.mill-workbench \{ grid-template-columns: 1fr; \}/);
  assert.match(css, /@media \(max-width: 560px\) \{[\s\S]*\.spec-grid \{ grid-template-columns: 1fr;/);
  assert.match(css, /@media \(max-width: 560px\) \{[\s\S]*\.force-bay \{ grid-template-columns: 1fr; \}/);
  const mobileBlock = css.slice(css.indexOf("@media (max-width: 560px)"));
  assert.doesNotMatch(mobileBlock, /minmax\(640px/);
  assert.doesNotMatch(mobileBlock, /min-width: [4-9][0-9]{2}px/);
});
