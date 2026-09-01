"""Cross-writer persistence races: persist_* merges under an advisory file lock.

persist_overrides / persist_prompt_override were load->merge->write with no
lock; atomic_write_text only prevents torn files, not lost updates, so two
writers that both read the same baseline dropped one writer's keys (last atomic
replace wins). The lock serializes the critical section; these tests force the
pre-fix interleaving by slowing the in-lock load and assert both writers' keys
survive.
"""

from __future__ import annotations

import threading
import time

from skyn3t.cortex import prompt_store, tuning_store


def _run_all(threads):
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_concurrent_persist_overrides_keeps_both_writers(tmp_path, monkeypatch):
    real_load = tuning_store.load_overrides

    def slow_load(data_dir):
        current = real_load(data_dir)
        time.sleep(0.2)  # hold the read open so both writers overlap
        return current

    monkeypatch.setattr(tuning_store, "load_overrides", slow_load)
    _run_all(
        [
            threading.Thread(
                target=tuning_store.persist_overrides,
                args=(tmp_path, {"critic_enabled": True}),
            ),
            threading.Thread(
                target=tuning_store.persist_overrides,
                args=(tmp_path, {"reflective_retry": True}),
            ),
        ]
    )
    assert real_load(tmp_path) == {"critic_enabled": True, "reflective_retry": True}


def test_concurrent_prompt_override_writers_both_persist(tmp_path, monkeypatch):
    real_load = prompt_store.load_prompt_overrides

    def slow_load(data_dir):
        current = real_load(data_dir)
        time.sleep(0.2)
        return current

    monkeypatch.setattr(prompt_store, "load_prompt_overrides", slow_load)
    _run_all(
        [
            threading.Thread(
                target=prompt_store.persist_prompt_override,
                args=(tmp_path, "code", "BE FAST"),
            ),
            threading.Thread(
                target=prompt_store.persist_prompt_override,
                args=(tmp_path, "review", "CHECK TYPES"),
            ),
        ]
    )
    assert real_load(tmp_path) == {"code": "BE FAST", "review": "CHECK TYPES"}


def test_empty_persist_creates_no_lock_sidecar(tmp_path):
    # The lock sidecar must be acquired lazily: an empty persist stays a
    # no-file no-op (not even the cortex dir may appear).
    assert tuning_store.persist_overrides(tmp_path, {}) == {}
    assert prompt_store.persist_prompt_override(tmp_path, "", "") == {}
    assert not (tmp_path / "cortex").exists()


def test_lock_failure_degrades_to_unlocked_write(tmp_path):
    # A cortex path that cannot host the sidecar must not block the write
    # path's own error handling (never-raises contract).
    (tmp_path / "cortex").write_text("not a dir")
    assert tuning_store.persist_overrides(tmp_path, {"critic_enabled": False}) == {}
    assert prompt_store.persist_prompt_override(tmp_path, "code", "X") == {}
