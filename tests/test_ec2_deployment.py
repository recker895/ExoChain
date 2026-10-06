"""Deployment safety tests: temporary files/mocks only, never a real server."""

import io
import json
import os
from pathlib import Path
import tarfile
import tempfile

import pytest
import yaml

from deploy.ec2 import check_dependencies, deploy_release, package_release

COMMIT = "a" * 40
IDENTITY = COMMIT + "-123-1"
MARKER = {"commit": COMMIT, "run_id": "123"}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def archive(path, entries):
    with tarfile.open(path, "w:gz") as bundle:
        for name, value in entries.items():
            member = tarfile.TarInfo(name)
            data = value.encode()
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))


@pytest.mark.parametrize("name", ["../outside", "/etc/fstab", "data/runs.db", ".env",
                                  "config/.env.production", "foo\\..\\outside"])
def test_archive_rejects_unsafe_paths(tmp_path, name):
    bundle = tmp_path / "release.tar.gz"
    archive(bundle, {name: "must not be written"})
    with pytest.raises(ValueError):
        deploy_release.extract_release(bundle, tmp_path / "destination")
    assert not (tmp_path / "outside").exists()


def test_archive_rejects_escaping_symlink(tmp_path):
    bundle = tmp_path / "release.tar.gz"
    with tarfile.open(bundle, "w:gz") as output:
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        output.addfile(member)
    with pytest.raises(tarfile.FilterError):
        deploy_release.extract_release(bundle, tmp_path / "destination")


@pytest.mark.parametrize("version", ["16.3.5", "^16.3.6", "1636", "16.3.6-beta.1"])
def test_dependency_baseline_rejects_old_or_unpinned(tmp_path, version):
    package = {"dependencies": {"next": version}, "devDependencies": {"eslint-config-next": version}}
    write(tmp_path / "dashboard/frontend/package.json", json.dumps(package))
    write(tmp_path / "dashboard/frontend/package-lock.json", json.dumps({"packages": {}}))
    with pytest.raises(ValueError):
        check_dependencies.check(tmp_path)


def test_dependency_baseline_requires_matching_lock(tmp_path):
    package = {"dependencies": {"next": "16.3.6"}, "devDependencies": {"eslint-config-next": "16.3.6"}}
    lock = {"packages": {"": package, "node_modules/next": {"version": "16.3.6"},
                         "node_modules/eslint-config-next": {"version": "16.3.6"}}}
    write(tmp_path / "dashboard/frontend/package.json", json.dumps(package))
    write(tmp_path / "dashboard/frontend/package-lock.json", json.dumps(lock))
    check_dependencies.check(tmp_path)
    lock["packages"]["node_modules/next"]["version"] = "16.3.5"
    write(tmp_path / "dashboard/frontend/package-lock.json", json.dumps(lock))
    with pytest.raises(ValueError, match="do not match"):
        check_dependencies.check(tmp_path)


def setup_switch(tmp_path, monkeypatch):
    base = tmp_path / "deploy"
    old = base / "current"
    new = base / "releases" / IDENTITY
    old.mkdir(parents=True)
    write(new / "release.json", json.dumps(MARKER))
    events = []
    monkeypatch.setattr(deploy_release, "command", lambda args, **kw: events.append(tuple(args)))
    monkeypatch.setattr(deploy_release, "replace_link", lambda link, target: events.append((link, target)))
    # This unit test mocks the symlink boundary, including Windows sandbox realpath.
    real_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *args, **kw:
                        old if self == old else real_resolve(self, *args, **kw))
    return base, old, new, events


def test_successful_switch_health_checks_before_recording_previous(tmp_path, monkeypatch):
    base, old, new, events = setup_switch(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy_release, "health", lambda settings, marker: events.append(("health", marker)))
    deploy_release.activate(base, {}, new)
    assert events == [
        ("sudo", "systemctl", "stop", *deploy_release.SERVICES),
        (base / "current", new),
        ("sudo", "systemctl", "start", *deploy_release.SERVICES),
        ("health", MARKER), (base / "previous", old.resolve()),
    ]


def test_failed_health_restores_previous_and_checks_it(tmp_path, monkeypatch):
    base, old, new, events = setup_switch(tmp_path, monkeypatch)
    def health(settings, marker):
        events.append(("health", marker))
        if marker is not None:
            raise RuntimeError("new code is unhealthy")
    monkeypatch.setattr(deploy_release, "health", health)
    with pytest.raises(RuntimeError, match="previous release restored"):
        deploy_release.activate(base, {}, new)
    assert (base / "current", old.resolve()) in events
    assert events[-1] == ("health", None)
    assert not any(event[0] == base / "previous" for event in events)


def test_failed_rollback_is_reported_not_hidden(tmp_path, monkeypatch):
    base, _old, new, _events = setup_switch(tmp_path, monkeypatch)
    def health(*args):
        raise RuntimeError("unhealthy")
    monkeypatch.setattr(deploy_release, "health", health)
    with pytest.raises(RuntimeError, match="AND rollback"):
        deploy_release.activate(base, {}, new)


@pytest.mark.skipif(os.name != "posix", reason="Actual server symlinks are verified on Linux CI")
def test_atomic_link_replacement_on_linux(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    current = tmp_path / "current"
    current.symlink_to(old, target_is_directory=True)
    deploy_release.replace_link(current, new)
    assert current.resolve(strict=True) == new.resolve()
    assert old.is_dir()  # Original release remains recoverable.


def test_preparation_preserves_shared_data_and_does_not_restart(tmp_path, monkeypatch):
    base = tmp_path / "deploy"
    original = tmp_path / "original"
    write(original / ".env", "TEST_SECRET=unchanged")
    write(original / "data/runs.db", "original run history")
    (base / "incoming").mkdir(parents=True)
    (base / "releases").mkdir()
    entries = {"release.json": json.dumps(MARKER), "requirements.lock": "hashed requirements",
               "dashboard/backend/main.py": "# test",
               "node_modules/@arcnautical/maritime-routing/package.json": "{}",
               "dashboard/frontend/.next/standalone/dashboard/frontend/server.js": "// test",
               "dashboard/frontend/.next/standalone/dashboard/frontend/public/deployment.json": json.dumps(MARKER)}
    archive(base / "incoming" / (IDENTITY + ".tar.gz"), entries)
    monkeypatch.setattr(deploy_release.shutil, "disk_usage", lambda path: type("Disk", (), {"free": 3 * 1024**3})())
    calls, links = [], []
    monkeypatch.setattr(deploy_release, "command", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(Path, "symlink_to", lambda self, target, **kw: links.append((self, target)))
    release = deploy_release.prepare(base, {"original_root": str(original), "node": "/node/bin/node", "uv": "/uv"}, IDENTITY)
    assert links == [(release / "data", original / "data"), (release / ".env", original / ".env")]
    assert all("systemctl" not in command for command in calls)
    assert "--require-hashes" in calls[-1]
    assert (original / ".env").read_text() == "TEST_SECRET=unchanged"
    assert (original / "data/runs.db").read_text() == "original run history"


@pytest.mark.parametrize("identity", ["../outside", "abc", COMMIT + "-123-1; touch bad"])
def test_release_id_cannot_escape_or_inject_commands(tmp_path, identity):
    with pytest.raises(ValueError, match="Invalid release ID"):
        deploy_release.prepare(tmp_path, {}, identity)


def test_public_release_identity_is_checked(monkeypatch):
    responses = {
        "http://127.0.0.1:8003/livez": b'{"status":"ALIVE"}',
        "http://127.0.0.1:8003/api/v1/runs": b"[]",
        "http://127.0.0.1:3000/": b"ExoChain",
        "https://test.invalid/": b"ExoChain",
        "https://test.invalid/api/v1/runs": b"[]",
        "https://test.invalid/deployment.json?commit=" + COMMIT: json.dumps(MARKER).encode(),
    }
    monkeypatch.setattr(deploy_release, "get", responses.__getitem__)
    deploy_release.health({"public_url": "https://test.invalid"}, MARKER)


@pytest.mark.parametrize("name", [".env", "services/.env", "services/key.pem", "data/runs.db",
                                  "config/secrets/token", "dashboard/backend/__pycache__/main.pyc"])
def test_source_selection_excludes_secrets_and_state(name):
    assert not package_release.selected(name)


def test_source_selection_includes_required_runtime_files():
    for name in ("requirements.lock", "config/settings.py", "dashboard/backend/main.py",
                 "services/providers/arcnautical_route.mjs", "agents/route_agent.py"):
        assert package_release.selected(name)


def package_fixture(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    write(root / "dashboard/frontend/.next/standalone/dashboard/frontend/server.js", "// built server")
    write(root / "dashboard/frontend/.next/static/app.js", "// static asset")
    write(root / "dashboard/frontend/public/map-worker.js", "// map worker")
    write(root / "node_modules/@arcnautical/maritime-routing/package.json", "{}")
    def export(args, **kwargs):
        destination = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--output=")))
        archive(destination, {"requirements.lock": "hashes", "dashboard/backend/main.py": "# backend",
                              "services/providers/arcnautical_route.mjs": "// routing bridge",
                              ".env": "SECRET=must not leak", "data/runs.db": "must not leak"})
    monkeypatch.setattr(package_release.subprocess, "run", export)
    original_temp = tempfile.TemporaryDirectory
    monkeypatch.setattr(package_release.tempfile, "TemporaryDirectory",
                        lambda **kw: original_temp(dir=tmp_path, **kw))
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    return root


def test_package_contains_runtime_assets_and_excludes_state(tmp_path, monkeypatch):
    root = package_fixture(tmp_path, monkeypatch)
    output = tmp_path / "release.tar.gz"
    package_release.package(root, output, COMMIT)
    with tarfile.open(output) as bundle:
        names = bundle.getnames()
        prefix = "dashboard/frontend/.next/standalone/dashboard/frontend/"
        for required in (prefix + "server.js", prefix + "public/map-worker.js",
                         prefix + ".next/static/app.js", prefix + "public/deployment.json",
                         "node_modules/@arcnautical/maritime-routing/package.json"):
            assert required in names
        assert ".env" not in names and "data/runs.db" not in names
        assert json.load(bundle.extractfile("release.json")) == MARKER


def test_package_rejects_environment_files_in_build_trace(tmp_path, monkeypatch):
    root = package_fixture(tmp_path, monkeypatch)
    write(root / "dashboard/frontend/.next/standalone/.env.production", "SECRET=do not bundle")
    with pytest.raises(ValueError, match="possible secret"):
        package_release.package(root, tmp_path / "release.tar.gz", COMMIT)


def test_public_marker_mismatch_fails_health(monkeypatch):
    def get(url):
        if "/livez" in url:
            return b'{"status":"ALIVE"}'
        if "/runs" in url:
            return b"[]"
        if "/deployment.json" in url:
            return b'{"commit":"wrong"}'
        return b"ExoChain"
    clock = iter([0, 0, 200])
    monkeypatch.setattr(deploy_release, "get", get)
    monkeypatch.setattr(deploy_release.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(deploy_release.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="different release"):
        deploy_release.health({"public_url": "https://test.invalid"}, MARKER)


def test_workflow_has_main_and_environment_gates_and_cleanup():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/ci-cd.yml").read_text())
    # PyYAML's YAML 1.1 parser treats the key `on` as boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request_target" not in triggers
    deploy = workflow["jobs"]["deploy"]
    assert "github.event_name != 'pull_request'" in deploy["if"]
    assert "refs/heads/main" in deploy["if"] and "DEPLOY_ENABLED" in deploy["if"]
    assert deploy["needs"] == "verify" and deploy["environment"]["name"] == "production"
    text = json.dumps(deploy)
    assert "StrictHostKeyChecking=yes" in text
    assert "0.0.0.0/0" not in text
    assert "revoke-security-group-ingress" in text and "systemd-run" in text
    assert "aws-access-key-id" not in text
    assert workflow["concurrency"]["cancel-in-progress"] is False
