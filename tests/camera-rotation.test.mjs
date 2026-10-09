import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../app/capture/camera-rotation.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { frameGeometry, initialRotation, nextRotation } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

test('all four orientations preserve the full frame and cap the longest edge', () => {
  for (const [width, height] of [[1920, 1080], [1080, 1920], [4032, 3024]]) {
    for (const angle of [0, 90, 180, 270]) {
      const f = frameGeometry(width, height, angle);
      assert.ok(Math.max(f.width, f.height) <= 2400);
      assert.equal(f.width * f.height, f.sourceWidth * f.sourceHeight);
      const points = [-1, 1].flatMap(x => [-1, 1].map(y => {
        const sx = x * f.sourceWidth / 2, sy = y * f.sourceHeight / 2;
        return [f.width / 2 + sx * Math.cos(f.radians) - sy * Math.sin(f.radians),
          f.height / 2 + sx * Math.sin(f.radians) + sy * Math.cos(f.radians)];
      }));
      for (const [x, y] of points) {
        assert.ok(x >= -1e-8 && x <= f.width + 1e-8);
        assert.ok(y >= -1e-8 && y <= f.height + 1e-8);
      }
    }
  }
  assert.deepEqual([frameGeometry(1920, 1080, 90).width, frameGeometry(1920, 1080, 90).height], [1080, 1920]);
});

test('landscape cameras default to portrait, native portrait and saved choices are retained', () => {
  assert.equal(initialRotation(null, 1920, 1080), 90);
  assert.equal(initialRotation(null, 1080, 1920), 0);
  assert.equal(initialRotation('270', 1920, 1080), 270);
  assert.equal(initialRotation('0', 1920, 1080), 0);
  assert.equal(initialRotation('invalid', 1920, 1080), 90);
  let angle = 0;
  for (let count = 0; count < 4; count++) angle = nextRotation(angle);
  assert.equal(angle, 0);
});
