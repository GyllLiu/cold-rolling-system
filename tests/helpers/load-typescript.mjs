import { readFile } from 'node:fs/promises';
import ts from 'typescript';

// Load unchanged production TS and JSON in Node without a second implementation.
export async function loadTypescript(path, overrideSource) {
  const source = overrideSource ?? await readFile(path, 'utf8');
  let compiled = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
  }}).outputText;
  for (const match of [...compiled.matchAll(/from\s+["'](\.[^"']+)["']/g)]) {
    const dependency = new URL(match[1], path);
    const url = dependency.pathname.endsWith('.json')
      ? 'data:text/javascript;base64,' + Buffer.from('export default ' + await readFile(dependency, 'utf8')).toString('base64')
      : (await loadTypescript(new URL(dependency.href + '.ts'))).url;
    compiled = compiled.replace(match[0], `from "${url}"`);
  }
  const url = 'data:text/javascript;base64,' + Buffer.from(compiled).toString('base64');
  return { url, module: await import(url) };
}
