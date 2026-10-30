"""Container-only storage setup and explicit API/worker lifecycle.

No dotenv loading, database connection or model call occurs on import. Storage
mapping is opt-in so host development and existing Compose mounts stay intact.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import MutableMapping, Sequence


class ContainerStartError(RuntimeError):
    """Content-free deployment diagnostics."""


def enabled(env: MutableMapping[str, str], name: str) -> bool:
    value = env.get(name, "false").strip().lower()
    if value not in {"true", "false"}:
        raise ContainerStartError("CONTAINER_BOOLEAN_INVALID")
    return value == "true"


def reject_symlink_chain(path: Path) -> None:
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise ContainerStartError("CONTAINER_STORAGE_PATH_INVALID")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ContainerStartError("CONTAINER_STORAGE_SYMLINK_REJECTED")


def private_directory(path: Path) -> None:
    reject_symlink_chain(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ContainerStartError("CONTAINER_STORAGE_NOT_DIRECTORY")
    path.chmod(0o700)


def map_image_directory(local: Path, target: Path, *, placeholder: bool = False) -> None:
    """Map an empty image directory, preserving any known seed directory.

    Never migrate or delete unknown files or replace a different symbolic link.
    Existing persisted target contents are retained without inspection.
    """
    if local.is_symlink():
        if local.resolve() != target.resolve() or not target.is_dir():
            raise ContainerStartError("CONTAINER_EXISTING_MAPPING_MISMATCH")
        return
    reject_symlink_chain(local)
    if local.exists():
        if not local.is_dir():
            raise ContainerStartError("CONTAINER_IMAGE_PATH_NOT_DIRECTORY")
        children = list(local.iterdir())
        known_placeholder = (placeholder and len(children) == 1
                             and children[0].name == "placeholder.txt"
                             and not children[0].is_symlink()
                             and children[0].is_file()
                             and children[0].stat().st_size <= 128
                             and children[0].read_bytes().strip()
                             == b"This is a placeholder file for static assets.")
        if children and not known_placeholder:
            raise ContainerStartError("CONTAINER_IMAGE_DIRECTORY_NOT_EMPTY")
        seed = local.with_name(local.name + ".container-image-seed")
        if seed.exists() or seed.is_symlink():
            raise ContainerStartError("CONTAINER_IMAGE_SEED_ALREADY_EXISTS")
        local.rename(seed)
    local.symlink_to(target, target_is_directory=True)


def prepare_storage(env: MutableMapping[str, str], backend: Path) -> None:
    configured = env.get("CONTAINER_STORAGE_ROOT", "").strip()
    if not configured:
        return
    root = Path(configured)
    reject_symlink_chain(root)
    repository = backend.resolve().parent
    if root.resolve().is_relative_to(repository):
        raise ContainerStartError("CONTAINER_PRIVATE_STORAGE_INSIDE_REPOSITORY")
    private_directory(root)
    defaults = {
        "FUSION_SOURCE_BUNDLE_DIR": "source-bundles",
        "FUSION_CREATIVE_JOB_DIR": "creative-jobs",
        "SPEECH_INPUT_STATE_DIR": "speech-input",
        "DOUBAO_ASR_STATE_DIR": "speech-input",
    }
    for name, subdirectory in defaults.items():
        path = Path(env.setdefault(name, str(root / subdirectory)))
        reject_symlink_chain(path)
        if not path.resolve().is_relative_to(root.resolve()) or path.resolve() == root.resolve():
            raise ContainerStartError("CONTAINER_PRIVATE_STORAGE_OUTSIDE_VOLUME")
        private_directory(path)
    for name in ("uploads", "static"):
        private_directory(root / name)
    map_image_directory(backend / ".data", root / "uploads")
    map_image_directory(backend / "static", root / "static", placeholder=True)


def supervise(commands: Sequence[Sequence[str]], backend: Path) -> int:
    """Stop the whole service when either process exits; never retry a call."""
    children: list[subprocess.Popen] = []
    received: list[int] = []

    def terminate_children() -> None:
        for child in children:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def stop(signum: int, _frame: object) -> None:
        received.append(signum)
        terminate_children()

    old_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    code = 1
    try:
        for command in commands:
            children.append(subprocess.Popen(list(command), cwd=backend, start_new_session=True))
        while not received:
            finished = next((child for child in children if child.poll() is not None), None)
            if finished is not None:
                code = finished.returncode or 1
                break
            time.sleep(0.2)
    finally:
        terminate_children()
        deadline = time.monotonic() + 10
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    return 128 + received[0] if received else code


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    backend = Path(__file__).resolve().parents[1]
    worker = enabled(os.environ, "ENABLE_CREATIVE_WORKER")
    explicit_worker = any(Path(arg).name == "creative_worker.py" for arg in args)
    if (worker or explicit_worker) and not enabled(os.environ, "ENABLE_PAID_MODEL_CALLS"):
        raise ContainerStartError("CONTAINER_WORKER_REQUIRES_PAID_OPT_IN")
    if worker and explicit_worker:
        raise ContainerStartError("CONTAINER_DUPLICATE_WORKER_REJECTED")
    previous_umask = os.umask(0o077)
    try:
        prepare_storage(os.environ, backend)
        if enabled(os.environ, "RUN_DATABASE_MIGRATIONS"):
            # This import reads only environment variables. Reject unsafe production
            # settings before migration can connect to the configured database.
            sys.path.insert(0, str(backend))
            from src.core.security_preflight import enforce_security_configuration
            enforce_security_configuration()
            subprocess.run([sys.executable, "-m", "alembic", "-c", "src/db/migrations/alembic.ini", "upgrade", "head"],
                           cwd=backend, check=True)
        command = args or [sys.executable, "main.py"]
        if worker:
            return supervise([command, [sys.executable, "scripts/creative_worker.py", "--allow-paid", "--watch"]], backend)
        os.chdir(backend)
        os.execvp(command[0], command)
        return 1
    finally:
        # A successful exec inherits 077 and never returns here. Exceptions,
        # mocked exec and a stopped supervisor must not mutate their caller.
        os.umask(previous_umask)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContainerStartError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
