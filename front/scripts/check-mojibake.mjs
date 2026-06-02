import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';

const ROOT = path.resolve('src');
const EXTENSIONS = new Set(['.ts', '.tsx', '.js', '.jsx', '.css', '.html']);

const MOJIBAKE_RE =
  /(?:\u0420[\u0090-\u009f\u0452\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u2122\u0459\u203a\u045a\u045c\u045b\u045f\u00a0\u040e\u045e\u0408\u00a4\u0490\u00a6\u00a7\u0401\u00a9\u0404\u00ab\u00ac\u00ad\u00ae\u0407\u00b0-\u00bf]|\u0421[\u0080-\u008f\u0402\u0403\u201a\u0453\u201e\u2026\u2020\u2021\u20ac\u2030\u0409\u2039\u040a\u040c\u040b\u040f]|\u0432[\u0080-\u009f\u0402\u0403\u201a\u0453\u201e\u2026\u2020\u2021\u20ac\u2030\u0409\u2039\u040a\u040c\u040b\u040f\u0452\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u2122\u0459\u203a\u045a\u045c\u045b\u045f])/u;
const REPLACEMENT_CHAR_RE = /\ufffd/u;
const UTF8_BOM_RE = /^\ufeff/u;

async function walk(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];

  for (const entry of entries) {
    if (entry.name === 'node_modules' || entry.name === 'dist') continue;
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...await walk(fullPath));
    } else if (EXTENSIONS.has(path.extname(entry.name))) {
      files.push(fullPath);
    }
  }

  return files;
}

const findings = [];

for (const file of await walk(ROOT)) {
  const content = await readFile(file, 'utf8');
  if (UTF8_BOM_RE.test(content)) {
    findings.push({
      file,
      line: 1,
      text: 'UTF-8 BOM detected',
    });
  }
  const lines = content.split(/\r?\n/);

  lines.forEach((line, index) => {
    if (MOJIBAKE_RE.test(line) || REPLACEMENT_CHAR_RE.test(line)) {
      findings.push({
        file,
        line: index + 1,
        text: line.trim().slice(0, 180),
      });
    }
  });
}

if (findings.length) {
  console.error('Detected possible mojibake/corrupted text in frontend source:');
  for (const finding of findings) {
    console.error(`${path.relative(process.cwd(), finding.file)}:${finding.line}: ${finding.text}`);
  }
  process.exit(1);
}

console.log('No mojibake patterns detected in frontend source.');
