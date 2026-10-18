#!/usr/bin/env python3
"""Run the latest source on container start (opt-in: CONFIGFLOW_AUTO_UPDATE=1).

Fetches the newest commit of CONFIGFLOW_UPDATE_BRANCH (default main) from
CONFIGFLOW_UPDATE_REPO (default thsrite/configflow), rebuilds only what changed
(frontend with npm, Agent binaries with Go) and installs it over the image's
copy. Builds and caches live under /data/.auto-update, so recreating the
container does not rebuild. Any failure keeps the last good build, or the
image's own code when there is none: startup must never be blocked.
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

REPO = os.environ.get('CONFIGFLOW_UPDATE_REPO', 'thsrite/configflow')
BRANCH = os.environ.get('CONFIGFLOW_UPDATE_BRANCH', 'main')
STATE = os.path.join(os.environ.get('DATA_DIR', '/data'), '.auto-update')
BACKEND, HTML, AGENTS = '/app/backend', '/usr/share/nginx/html', '/opt/configflow/static/agents'
NODE_PROBE = '/opt/configflow/node-probe'
IMAGE_COPY = '/app/.image'  # the image's own code, restored when no build is usable
IMAGE_TREES = '/app/.image-trees.json'  # source fingerprints of the bundled frontend/Agent
ARCHES = (('amd64', {}), ('arm64', {}), ('armv7', {'GOARCH': 'arm', 'GOARM': '7'}))


def log(message):
    print(f'[auto-update] {message}', flush=True)


def fetch(url, timeout=30):
    request = urllib.request.Request(url, headers={'User-Agent': 'configflow-auto-update',
                                                   'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _frontend_input(name):
    # Matches what reaches the image build: .dockerignore drops *.md and dotfiles.
    return not name.endswith('.md') and not name.startswith('.')


def _agent_input(name):
    return name.endswith('.go') or name in ('go.mod', 'go.sum')


def tree_hash(path, include=_frontend_input):
    """Hash of the files that affect a build, to rebuild only what changed.

    Only build inputs count, so the fingerprint taken from the Docker build
    context equals the one taken from the GitHub source archive.
    """
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in ('node_modules', 'dist') and not d.startswith('.'))
        for name in sorted(n for n in files if include(n)):
            full = os.path.join(root, name)
            digest.update(os.path.relpath(full, path).encode())
            with open(full, 'rb') as handle:
                digest.update(hashlib.sha256(handle.read()).digest())
    return digest.hexdigest()


def run(args, cwd, env=None, timeout=900):
    merged = dict(os.environ, **(env or {}))
    result = subprocess.run(args, cwd=cwd, env=merged, timeout=timeout,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'{" ".join(args)} failed:\n{result.stdout[-2000:]}')


def load_manifest():
    try:
        with open(os.path.join(STATE, 'manifest.json')) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_manifest(manifest):
    path = os.path.join(STATE, 'manifest.json')
    with open(path + '.tmp', 'w') as handle:
        json.dump(manifest, handle, indent=2)
    os.replace(path + '.tmp', path)


def download(sha):
    source = os.path.join(STATE, f'src-{sha}')
    if os.path.isdir(source):
        return source
    archive = os.path.join(STATE, f'{sha}.tar.gz')
    with open(archive, 'wb') as handle:
        handle.write(fetch(f'https://codeload.github.com/{REPO}/tar.gz/{sha}', timeout=120))
    staging = source + '.tmp'
    shutil.rmtree(staging, ignore_errors=True)
    with tarfile.open(archive) as bundle:
        try:
            bundle.extractall(staging, filter='data')
        except TypeError:  # Python without extraction filters
            bundle.extractall(staging)
    os.remove(archive)
    (top,) = os.listdir(staging)  # codeload wraps everything in <repo>-<sha>/
    os.replace(os.path.join(staging, top), source)
    shutil.rmtree(staging, ignore_errors=True)
    return source


def image_trees():
    try:
        with open(IMAGE_TREES) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


class MissingToolchain(Exception):
    """A changed component cannot be built here; updating would mix versions."""


def build_frontend(source, manifest):
    frontend = os.path.join(source, 'frontend')
    key = tree_hash(frontend)
    if not shutil.which('npm'):
        if key != image_trees().get('frontend'):
            raise MissingToolchain('前端有改动但镜像里没有 npm')
        manifest['frontend'] = None  # unchanged: the bundled frontend matches
        return None
    out = os.path.join(STATE, f'frontend-{key[:16]}')
    lock_key = hashlib.sha256(open(os.path.join(frontend, 'package-lock.json'), 'rb').read()).hexdigest()[:16]
    # Node resolves packages by walking up to directories literally named
    # node_modules (on the symlink's real path), so keep that name.
    modules = os.path.join(STATE, f'deps-{lock_key}', 'node_modules')
    manifest['deps'] = os.path.dirname(modules)  # kept by prune even when the build is reused
    if not os.path.isdir(out):
        if not os.path.isdir(modules):
            log('installing frontend dependencies (package-lock changed)')
            run(['npm', 'ci', '--no-audit', '--no-fund', '--cache', os.path.join(STATE, 'npm-cache')], frontend)
            os.makedirs(os.path.dirname(modules), exist_ok=True)
            shutil.move(os.path.join(frontend, 'node_modules'), modules)
        os.symlink(modules, os.path.join(frontend, 'node_modules'))
        log('building frontend')
        run(['npm', 'run', 'build'], frontend)
        shutil.copytree(os.path.join(frontend, 'dist'), out + '.tmp')
        os.replace(out + '.tmp', out)
    manifest['frontend'] = out
    return out


def build_agents(source, manifest):
    agent = os.path.join(source, 'backend', 'agents', 'go-agent')
    key = tree_hash(agent, _agent_input)
    if not os.path.exists('/usr/local/go/bin/go'):
        if key != image_trees().get('agents'):
            raise MissingToolchain('Agent 有改动但镜像里没有 Go')
        manifest['agents'] = None  # unchanged: the bundled binaries match
        return None
    out = os.path.join(STATE, f'agents-{key[:16]}')
    if not os.path.isdir(out):
        log('building Agent binaries')
        staging = out + '.tmp'
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        env = {'CGO_ENABLED': '0', 'GOOS': 'linux', 'GOPROXY': os.environ.get('GOPROXY', 'https://goproxy.cn,direct'),
               'GOCACHE': os.path.join(STATE, 'go-cache'), 'GOMODCACHE': os.path.join(STATE, 'go-mod'),
               'PATH': '/usr/local/go/bin:' + os.environ.get('PATH', '')}
        for name, extra in ARCHES:
            arch_env = dict(env, GOARCH=name)
            arch_env.update(extra)  # armv7 is GOARCH=arm with GOARM=7
            run(['go', 'build', '-ldflags=-s -w', '-o', os.path.join(staging, f'configflow-agent-linux-{name}'), '.'],
                agent, env=arch_env)
        os.replace(staging, out)
    manifest['agents'] = out
    return out


def build_node_probe(source, manifest):
    probe = os.path.join(source, 'backend', 'node-probe')
    if not os.path.isfile(os.path.join(probe, 'go.mod')):
        manifest['node_probe'] = None
        return None
    key = tree_hash(probe, _agent_input)
    if not os.path.exists('/usr/local/go/bin/go'):
        if key != image_trees().get('node_probe'):
            raise MissingToolchain('独立测速组件有改动但镜像里没有 Go')
        manifest['node_probe'] = None
        return None
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64',
            'armv7l': 'arm', 'armv6l': 'arm'}.get(platform.machine())
    if not arch:
        raise MissingToolchain('此 CPU 架构尚未支持独立测速组件')
    out = os.path.join(STATE, f'node-probe-{key[:16]}-{platform.machine()}')
    if not os.path.isfile(os.path.join(out, 'configflow-node-probe')):
        log('building independent node latency worker')
        staging = out + '.tmp'
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        env = {'CGO_ENABLED': '0', 'GOOS': 'linux', 'GOARCH': arch,
               'GOARM': '6' if platform.machine() == 'armv6l' else '7',
               'GOCACHE': os.path.join(STATE, 'go-cache'),
               'GOMODCACHE': os.path.join(STATE, 'go-mod'),
               'PATH': '/usr/local/go/bin:' + os.environ.get('PATH', '')}
        run(['go', 'build', '-trimpath', '-ldflags=-s -w', '-o',
             os.path.join(staging, 'configflow-node-probe'), '.'], probe, env=env)
        for notice in ('LICENSE', 'NOTICE'):
            if os.path.isfile(os.path.join(probe, notice)):
                shutil.copy2(os.path.join(probe, notice), staging)
        os.replace(staging, out)
    manifest['node_probe'] = out
    return out


def install_requirements(source, manifest):
    requirements = os.path.join(source, 'backend', 'requirements.txt')
    key = hashlib.sha256(open(requirements, 'rb').read()).hexdigest()
    image_key = manifest.setdefault('image_requirements', _image_requirements_hash())
    if key not in (image_key, manifest.get('requirements')):
        log('installing backend requirements')
        run([sys.executable, '-m', 'pip', 'install', '-q', '-r', requirements], source)
    manifest['requirements'] = key


def _image_requirements_hash():
    marker = '/app/.image-requirements.sha256'
    try:
        return open(marker).read().strip()
    except OSError:
        return ''


def replace_dir(src, dst):
    # Directories from image layers cannot be renamed on overlayfs (EXDEV), so
    # delete and copy. Services are not running yet; a failure here is undone
    # by restoring the preserved image copy.
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)


def preserve_image():
    """Keep the image's code once per container, so a failed update can return to it."""
    if os.path.isdir(IMAGE_COPY):
        return
    staging = IMAGE_COPY + '.tmp'
    shutil.rmtree(staging, ignore_errors=True)
    for name, path in (('backend', BACKEND), ('html', HTML), ('agents', AGENTS), ('node_probe', NODE_PROBE)):
        if os.path.isdir(path):
            shutil.copytree(path, os.path.join(staging, name), symlinks=True)
    os.replace(staging, IMAGE_COPY)


def restore_image():
    for name, path in (('backend', BACKEND), ('html', HTML), ('agents', AGENTS), ('node_probe', NODE_PROBE)):
        saved = os.path.join(IMAGE_COPY, name)
        if os.path.isdir(saved):
            replace_dir(saved, path)


def install(manifest):
    """Copy the recorded build over the image (also after a container recreate)."""
    source = manifest.get('source')
    if not source or not os.path.isdir(source):
        restore_image()
        return False
    replace_dir(os.path.join(source, 'backend'), BACKEND)
    version = os.path.join(BACKEND, 'version.py')
    with open(version) as handle:
        text = handle.read()
    with open(version, 'w') as handle:
        handle.write(text.replace('__version__ = "dev"', f'__version__ = "{BRANCH}-{manifest["sha"][:7]}"'))
    # A component without its own build uses the image's copy, never a stale
    # build left over from an earlier commit.
    for key, path, saved in (('frontend', HTML, 'html'), ('agents', AGENTS, 'agents'), ('node_probe', NODE_PROBE, 'node_probe')):
        if manifest.get(key) and os.path.isdir(manifest[key]):
            replace_dir(manifest[key], path)
        elif os.path.isdir(os.path.join(IMAGE_COPY, saved)):
            replace_dir(os.path.join(IMAGE_COPY, saved), path)
    return True


def prune(manifest):
    """Drop sources and builds the manifest no longer points at (caches stay)."""
    keep = {os.path.basename(manifest.get(k) or '') for k in ('source', 'frontend', 'agents', 'node_probe', 'deps')}
    for name in os.listdir(STATE):
        path = os.path.join(STATE, name)
        built = name.startswith(('src-', 'frontend-', 'agents-', 'node-probe-', 'deps-'))
        leftover = name.endswith(('.tmp', '.tar.gz'))
        if (built and name not in keep) or leftover:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)


def record_image(root):
    """Build time: fingerprint the sources the image's frontend/Agent were built from."""
    trees = {'frontend': tree_hash(os.path.join(root, 'frontend')),
             'agents': tree_hash(os.path.join(root, 'go-agent'), _agent_input),
             'node_probe': tree_hash(os.path.join(root, 'node-probe'), _agent_input)}
    with open(IMAGE_TREES, 'w') as handle:
        json.dump(trees, handle)
    print(f'recorded image source fingerprints: {trees}')


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--record-image':
        record_image(sys.argv[2])
        return
    if os.environ.get('CONFIGFLOW_AUTO_UPDATE') != '1':
        return
    os.makedirs(STATE, exist_ok=True)
    preserve_image()
    manifest = load_manifest()
    started = time.time()
    try:
        sha = json.loads(fetch(f'https://api.github.com/repos/{REPO}/commits/{BRANCH}'))['sha']
    except Exception as error:  # noqa: BLE001 - offline must not block startup
        log(f'cannot reach GitHub ({error.__class__.__name__}); using {manifest.get("sha", "image")[:7]}')
        install(manifest)
        return
    if sha == manifest.get('sha'):
        log(f'{BRANCH} is at {sha[:7]}, already built')
        install(manifest)
        return
    log(f'updating to {REPO}@{BRANCH} {sha[:7]}')
    candidate = dict(manifest)
    try:
        candidate['source'] = download(sha)
        build_frontend(candidate['source'], candidate)
        build_agents(candidate['source'], candidate)
        build_node_probe(candidate['source'], candidate)
        install_requirements(candidate['source'], candidate)
        candidate['sha'] = sha
        install(candidate)
        save_manifest(candidate)
        prune(candidate)
        log(f'running {sha[:7]} (took {time.time() - started:.0f}s)')
    except MissingToolchain as error:
        log(f'skipped {sha[:7]}: {error}; use the autoupdate image to build it. '
            f'Keeping {manifest.get("sha", "image")[:7]} so backend and frontend stay in step')
        install(manifest)
        prune(manifest)
    except Exception as error:  # noqa: BLE001 - fall back, never block startup
        log(f'update failed, keeping {manifest.get("sha", "image")[:7]}: {error}')
        try:
            install(manifest)
        except Exception as restore_error:  # noqa: BLE001
            log(f'restoring the previous build failed ({restore_error}); using the image code')
            restore_image()


if __name__ == '__main__':
    main()
