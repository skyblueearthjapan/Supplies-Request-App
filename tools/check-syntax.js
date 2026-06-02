const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const gasFiles = fs
  .readdirSync(root)
  .filter((file) => file.endsWith('.gs'))
  .sort();

const source = gasFiles.map((file) => fs.readFileSync(path.join(root, file), 'utf8')).join('\n');
new vm.Script(source, { filename: 'combined-gas-source.js' });

for (const file of ['Index.html', 'Styles.html', 'Client.html', 'PdfTemplate.html']) {
  const fullPath = path.join(root, file);
  if (!fs.existsSync(fullPath)) {
    throw new Error(`${file} is missing`);
  }
  const content = fs.readFileSync(fullPath, 'utf8');
  if (!content.trim()) {
    throw new Error(`${file} is empty`);
  }
}

const clientHtml = fs.readFileSync(path.join(root, 'Client.html'), 'utf8');
const scriptMatch = clientHtml.match(/<script>([\s\S]*)<\/script>/);
if (!scriptMatch) {
  throw new Error('Client.html does not contain a script block');
}
new vm.Script(scriptMatch[1], { filename: 'Client.html script' });

console.log(`Checked ${gasFiles.length} Apps Script files and HTML assets.`);
