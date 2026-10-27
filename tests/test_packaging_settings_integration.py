"""Packaging and the settings screen must agree on the runtime module API."""
import shutil
import subprocess

import pytest

from skyn3t.agents.packaging_agent import _config_js


def test_packaged_config_supports_settings_screen_round_trip(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to exercise the generated runtime module')
    (tmp_path / 'config.mjs').write_text(_config_js(['VITE_TITLE']))
    result = subprocess.run(
        [node, '--input-type=module', '-e', '''
import { CONFIG_KEYS, getConfig, setConfig, useConfig, Settings } from './config.mjs';
import assert from 'node:assert/strict';
const storage = new Map();
globalThis.localStorage = {
  getItem: key => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, value),
};
assert.equal(CONFIG_KEYS[0].name, 'VITE_TITLE');
assert.equal(typeof Settings, 'object');
assert.equal(setConfig({ VITE_TITLE: 'Operator title' }), true);
assert.equal(getConfig('VITE_TITLE'), 'Operator title');
assert.equal(useConfig().VITE_TITLE, 'Operator title');
'''], cwd=tmp_path, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
