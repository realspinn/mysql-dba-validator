// The update notice's wording when the page has no file to offer (src/app/updates.ts).
import assert from 'node:assert/strict';
import test from 'node:test';
import { noFileDetail } from '../src/app/updates.ts';

test('an installed copy is told to download and run the Windows setup file', () => {
	const text = noFileDetail(true);
	assert.match(text, /setup file/);
	assert.match(text, /release notes page/);
	assert.match(text, /close the app/);
	assert.doesNotMatch(text, /zip/i);
});

test('a copy that was not installed with Setup keeps the plain wording', () => {
	assert.equal(noFileDetail(false), 'Download it from the release notes page.');
});
