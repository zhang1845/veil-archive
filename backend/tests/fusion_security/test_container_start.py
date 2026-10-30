"""Container setup with disposable directories; no application, DB or model I/O."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SPEC = importlib.util.spec_from_file_location("container_start_fixture", Path(__file__).resolve().parents[2] / "scripts/container_start.py")
assert SPEC and SPEC.loader
start = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(start)


@pytest.fixture
def folders(tmp_path):
    base = tmp_path.resolve()
    backend = base / "app/backend"
    backend.mkdir(parents=True)
    (backend / ".data").mkdir()
    (backend / "static").mkdir()
    root = base / "persistent"
    return backend, root


def test_storage_is_opt_in_and_does_not_touch_host_directories(folders):
    backend, root = folders
    (backend / ".data/user-file").write_text("preserve")
    start.prepare_storage({}, backend)
    assert not root.exists()
    assert (backend / ".data/user-file").read_text() == "preserve"


def test_single_volume_mapping_is_private_idempotent_and_preserves_seed(folders):
    backend, root = folders
    (backend / "static/placeholder.txt").write_text("This is a placeholder file for static assets.")
    root.mkdir()
    (root / "uploads").mkdir()
    (root / "uploads/existing.bin").write_bytes(b"persisted")
    env = {"CONTAINER_STORAGE_ROOT": str(root)}
    start.prepare_storage(env, backend)
    start.prepare_storage(env, backend)
    assert (backend / ".data").resolve() == root / "uploads"
    assert (backend / "static").resolve() == root / "static"
    assert (backend / "static.container-image-seed/placeholder.txt").is_file()
    assert (root / "uploads/existing.bin").read_bytes() == b"persisted"
    for directory in [root, *root.iterdir()]:
        if directory.is_dir():
            assert directory.stat().st_mode & 0o777 == 0o700
    assert env["FUSION_SOURCE_BUNDLE_DIR"] == str(root / "source-bundles")


@pytest.mark.parametrize("name", [".data", "static"])
def test_unknown_image_data_is_never_deleted_or_moved(folders, name):
    backend, root = folders
    path = backend / name / "unknown.bin"
    path.write_bytes(b"do-not-delete")
    with pytest.raises(start.ContainerStartError, match="NOT_EMPTY"):
        start.prepare_storage({"CONTAINER_STORAGE_ROOT": str(root)}, backend)
    assert path.read_bytes() == b"do-not-delete"
    assert not (backend / name).is_symlink()


def test_changed_placeholder_is_unknown_data(folders):
    backend, root = folders
    path = backend / "static/placeholder.txt"
    path.write_text("user material")
    with pytest.raises(start.ContainerStartError, match="NOT_EMPTY"):
        start.prepare_storage({"CONTAINER_STORAGE_ROOT": str(root)}, backend)
    assert path.read_text() == "user material"


def test_storage_inside_repository_is_rejected(folders):
    backend, _ = folders
    with pytest.raises(start.ContainerStartError, match="INSIDE_REPOSITORY"):
        start.prepare_storage({"CONTAINER_STORAGE_ROOT": str(backend.parent / "data")}, backend)


@pytest.mark.parametrize("location", ["root", "source", "local"])
def test_unknown_symlinks_are_rejected(folders, location):
    backend, root = folders
    other = backend.parent.parent / "external"
    other.mkdir()
    if location == "root":
        root.symlink_to(other, target_is_directory=True)
    elif location == "source":
        root.mkdir()
        (root / "source-bundles").symlink_to(other, target_is_directory=True)
    else:
        (backend / ".data").rmdir()
        (backend / ".data").symlink_to(other, target_is_directory=True)
    with pytest.raises(start.ContainerStartError, match="SYMLINK|MISMATCH"):
        start.prepare_storage({"CONTAINER_STORAGE_ROOT": str(root)}, backend)
    assert other.is_dir() and not list(other.iterdir())


def test_custom_private_path_outside_volume_is_rejected(folders):
    backend, root = folders
    with pytest.raises(start.ContainerStartError, match="OUTSIDE_VOLUME"):
        start.prepare_storage({"CONTAINER_STORAGE_ROOT": str(root), "FUSION_SOURCE_BUNDLE_DIR": str(root.parent / "ephemeral")}, backend)


@pytest.mark.parametrize("name", ["ENABLE_CREATIVE_WORKER", "RUN_DATABASE_MIGRATIONS"])
def test_invalid_boolean_does_not_enable_runtime_actions(name):
    with pytest.raises(start.ContainerStartError, match="BOOLEAN_INVALID"):
        start.enabled({name: "yes"}, name)


def test_worker_requires_separate_paid_switch_before_storage_or_spawn(monkeypatch):
    monkeypatch.setenv("ENABLE_CREATIVE_WORKER", "true")
    monkeypatch.setenv("ENABLE_PAID_MODEL_CALLS", "false")
    prepare = Mock()
    monkeypatch.setattr(start, "prepare_storage", prepare)
    with pytest.raises(start.ContainerStartError, match="REQUIRES_PAID_OPT_IN"):
        start.main([])
    prepare.assert_not_called()


def test_explicit_worker_command_also_requires_paid_switch(monkeypatch):
    monkeypatch.setenv("ENABLE_CREATIVE_WORKER", "false")
    monkeypatch.setenv("ENABLE_PAID_MODEL_CALLS", "false")
    with pytest.raises(start.ContainerStartError, match="REQUIRES_PAID_OPT_IN"):
        start.main(["python", "scripts/creative_worker.py", "--allow-paid", "--watch"])


def test_worker_flag_and_worker_command_cannot_spawn_duplicate_workers(monkeypatch):
    monkeypatch.setenv("ENABLE_CREATIVE_WORKER", "true")
    monkeypatch.setenv("ENABLE_PAID_MODEL_CALLS", "true")
    with pytest.raises(start.ContainerStartError, match="DUPLICATE_WORKER"):
        start.main(["python", "scripts/creative_worker.py", "--allow-paid", "--watch"])


def test_api_default_executes_no_worker_and_no_migration(monkeypatch):
    for key in ("ENABLE_CREATIVE_WORKER", "RUN_DATABASE_MIGRATIONS", "CONTAINER_STORAGE_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(start.os, "chdir", lambda _: None)
    execute = Mock()
    monkeypatch.setattr(start.os, "execvp", execute)
    child = Mock()
    monkeypatch.setattr(start, "supervise", child)
    run = Mock()
    monkeypatch.setattr(start.subprocess, "run", run)
    start.main(["python", "main.py"])
    execute.assert_called_once_with("python", ["python", "main.py"])
    child.assert_not_called()
    run.assert_not_called()


def test_storage_failure_restores_callers_umask(monkeypatch):
    monkeypatch.setenv("ENABLE_CREATIVE_WORKER", "false")
    monkeypatch.setattr(start, "prepare_storage", Mock(side_effect=start.ContainerStartError("EXPECTED")))
    before = os.umask(0o022)
    os.umask(before)
    try:
        with pytest.raises(start.ContainerStartError, match="EXPECTED"):
            start.main([])
        observed = os.umask(before)
        assert observed == before
    finally:
        os.umask(before)


def test_mock_exec_restores_umask_but_receives_private_mask(monkeypatch):
    monkeypatch.setenv("ENABLE_CREATIVE_WORKER", "false")
    monkeypatch.setenv("RUN_DATABASE_MIGRATIONS", "false")
    monkeypatch.delenv("CONTAINER_STORAGE_ROOT", raising=False)
    monkeypatch.setattr(start.os, "chdir", lambda _: None)
    observed = []
    def execute(*_args):
        value = os.umask(0o077)
        observed.append(value)
    monkeypatch.setattr(start.os, "execvp", execute)
    before = os.umask(0o022)
    os.umask(before)
    try:
        start.main(["python", "main.py"])
        restored = os.umask(before)
        assert observed == [0o077] and restored == before
    finally:
        os.umask(before)


def test_worker_is_supervised_only_after_both_explicit_switches(monkeypatch):
    monkeypatch.setenv("ENABLE_CREATIVE_WORKER", "true")
    monkeypatch.setenv("ENABLE_PAID_MODEL_CALLS", "true")
    monkeypatch.setenv("RUN_DATABASE_MIGRATIONS", "false")
    monkeypatch.delenv("CONTAINER_STORAGE_ROOT", raising=False)
    child = Mock(return_value=0)
    monkeypatch.setattr(start, "supervise", child)
    assert start.main(["python", "main.py"]) == 0
    commands, _ = child.call_args.args
    assert commands[0] == ["python", "main.py"]
    assert commands[1][-2:] == ["--allow-paid", "--watch"]


def test_exit_of_one_child_stops_other_without_restarting(monkeypatch, tmp_path):
    alive = SimpleNamespace(pid=411, poll=lambda: None, wait=Mock(return_value=0))
    stopped = SimpleNamespace(pid=412, poll=lambda: 0, wait=Mock(return_value=0), returncode=0)
    spawn = Mock(side_effect=[alive, stopped])
    terminate = Mock()
    monkeypatch.setattr(start.subprocess, "Popen", spawn)
    monkeypatch.setattr(start.os, "killpg", terminate)
    assert start.supervise([["api"], ["worker"]], tmp_path) == 1
    assert spawn.call_count == 2
    terminate.assert_called_once_with(alive.pid, start.signal.SIGTERM)
