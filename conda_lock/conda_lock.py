"""
Somewhat hacky solution to create conda lock files.
"""

import atexit
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from itertools import chain
from typing import Dict, List, MutableSequence, Optional, Sequence, Set, Tuple, Union

import click
import ensureconda

from click_default_group import DefaultGroup

from conda_lock.src_parser import LockSpecification
from conda_lock.src_parser.environment_yaml import parse_environment_file
from conda_lock.src_parser.meta_yaml import parse_meta_yaml_file
from conda_lock.src_parser.pyproject_toml import parse_pyproject_toml


PathLike = Union[str, pathlib.Path]


if not (sys.version_info.major >= 3 and sys.version_info.minor >= 6):
    print("conda_lock needs to run under python >=3.6")
    sys.exit(1)


CONDA_PKGS_DIRS = None
DEFAULT_PLATFORMS = ["osx-64", "linux-64", "win-64"]


def conda_pkgs_dir():
    global CONDA_PKGS_DIRS
    if CONDA_PKGS_DIRS is None:
        temp_dir = tempfile.TemporaryDirectory()
        CONDA_PKGS_DIRS = temp_dir.name
        atexit.register(temp_dir.cleanup)
        return CONDA_PKGS_DIRS
    else:
        return CONDA_PKGS_DIRS


def conda_env_override(platform) -> Dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "CONDA_SUBDIR": platform,
            "CONDA_PKGS_DIRS": conda_pkgs_dir(),
            "CONDA_UNSATISFIABLE_HINTS_CHECK_DEPTH": "0",
            "CONDA_ADD_PIP_AS_PYTHON_DEPENDENCY": "False",
        }
    )
    return env


def solve_specs_for_arch(
    conda: PathLike, channels: Sequence[str], specs: List[str], platform: str
) -> dict:
    args: MutableSequence[PathLike] = [
        str(conda),
        "create",
        "--prefix",
        os.path.join(conda_pkgs_dir(), "prefix"),
        "--dry-run",
        "--json",
    ]
    if channels:
        args.append("--override-channels")
    for channel in channels:
        args.extend(["--channel", channel])
        if channel == "defaults" and platform in {"win-64", "win-32"}:
            # msys2 is a windows-only channel that conda automatically
            # injects if the host platform is Windows. If our host
            # platform is not Windows, we need to add it manually
            args.extend(["--channel", "msys2"])
    args.extend(specs)

    proc = subprocess.run(
        args,
        env=conda_env_override(platform),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf8",
    )

    def print_proc(proc):
        print(f"    Command: {proc.args}")
        if proc.stdout:
            print(f"    STDOUT:\n{proc.stdout}")
        if proc.stderr:
            print(f"    STDERR:\n{proc.stderr}")

    try:
        proc.check_returncode()
    except subprocess.CalledProcessError:
        try:
            err_json = json.loads(proc.stdout)
            message = err_json["message"]
        except json.JSONDecodeError as e:
            print(f"Failed to parse json, {e}")
            message = ""

        print(f"Could not lock the environment for platform {platform}")
        if message:
            print(message)
        print_proc(proc)

        sys.exit(1)

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("Could not solve for lock")
        print_proc(proc)
        sys.exit(1)


def do_conda_install(conda: PathLike, prefix: str, name: str, file: str) -> None:

    if prefix and name:
        raise ValueError("Provide either prefix, or name, but not both.")

    args: MutableSequence[PathLike] = [
        str(conda),
        "create",
        "--file",
        file,
        "--yes",
    ]

    if prefix:
        args.append("--prefix")
        args.append(prefix)
    if name:
        args.append("--name")
        args.append(name)

    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf8",
    )

    def print_proc(proc):
        print(f"    Command: {proc.args}")
        if proc.stdout:
            print(f"    STDOUT:\n{proc.stdout}")
        if proc.stderr:
            print(f"    STDERR:\n{proc.stderr}")

    try:
        proc.check_returncode()
    except subprocess.CalledProcessError:
        try:
            err_json = json.loads(proc.stdout)
            message = err_json["message"]
        except json.JSONDecodeError as e:
            print(f"Failed to parse json, {e}")
            message = ""

        print(f"Could not perform conda install using {file} lock file into {prefix}")
        if message:
            print(message)
        print_proc(proc)

        sys.exit(1)


def search_for_md5s(conda: PathLike, package_specs: List[dict], platform: str):
    """Use conda-search to determine the md5 metadata that we need.

    This is only needed if pkgs_dirs is set in condarc.
    Sadly this is going to be slow since we need to fetch each result individually
    due to the cli of conda search

    """
    found: Set[str] = set()
    packages: List[Tuple[str, str]] = [
        *[(d["name"], f"{d['name']}[url={d['url_conda']}]") for d in package_specs],
        *[(d["name"], f"{d['name']}[url={d['url']}]") for d in package_specs],
    ]

    for name, spec in packages:
        if name in found:
            continue
        out = subprocess.run(
            [str(conda), "search", "--use-index-cache", "--json", spec],
            encoding="utf8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=conda_env_override(platform),
        )
        content = json.loads(out.stdout)
        if name in content:
            assert len(content[name]) == 1
            yield content[name][0]
            found.add(name)


def fn_to_dist_name(fn: str) -> str:
    if fn.endswith(".conda"):
        fn, _, _ = fn.partition(".conda")
    elif fn.endswith(".tar.bz2"):
        fn, _, _ = fn.partition(".tar.bz2")
    else:
        raise RuntimeError(f"unexpected file type {fn}", fn)
    return fn


def make_lock_files(
    conda: PathLike,
    platforms: List[str],
    src_files: List[pathlib.Path],
    include_dev_dependencies: bool = True,
    channel_overrides: Optional[Sequence[str]] = None,
):
    """Generate the lock files for the given platforms from the src file provided

    Parameters
    ----------
    conda :
        The path to a conda or mamba executable
    platforms :
        List of platforms to generate the lock for
    src_files :
        Paths to a supported source file types
    include_dev_dependencies :
        For source types that separate out dev dependencies from regular ones,include those, default True
    channel_overrides :
        Forced list of channels to use.

    """
    for plat in platforms:
        print(f"generating lockfile for {plat}", file=sys.stderr)
        lock_specs = parse_source_files(
            src_files=src_files,
            platform=plat,
            include_dev_dependencies=include_dev_dependencies,
        )

        lock_spec = aggregate_lock_specs(lock_specs)
        if channel_overrides is not None:
            channels = channel_overrides
        else:
            channels = lock_spec.channels

        lockfile_contents = create_lockfile_from_spec(
            channels=channels, conda=conda, spec=lock_spec
        )
        with open(f"conda-{lock_spec.platform}.lock", "w") as fo:
            fo.write("\n".join(lockfile_contents) + "\n")

    print("To use the generated lock files create a new environment:", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "     conda create --name YOURENV --file conda-linux-64.lock", file=sys.stderr
    )
    print("", file=sys.stderr)


def create_lockfile_from_spec(
    *, channels: Sequence[str], conda: PathLike, spec: LockSpecification
) -> List[str]:
    dry_run_install = solve_specs_for_arch(
        conda=conda,
        platform=spec.platform,
        channels=channels,
        specs=spec.specs,
    )
    lockfile_contents = [
        f"# platform: {spec.platform}",
        f"# env_hash: {spec.env_hash()}\n",
        "@EXPLICIT\n",
    ]

    link_actions = dry_run_install["actions"]["LINK"]
    for link in link_actions:
        link["url_base"] = f"{link['base_url']}/{link['platform']}/{link['dist_name']}"
        link["url"] = f"{link['url_base']}.tar.bz2"
        link["url_conda"] = f"{link['url_base']}.conda"
    link_dists = {link["dist_name"] for link in link_actions}

    fetch_actions = dry_run_install["actions"]["FETCH"]

    fetch_by_dist_name = {fn_to_dist_name(pkg["fn"]): pkg for pkg in fetch_actions}

    non_fetch_packages = link_dists - set(fetch_by_dist_name)
    if len(non_fetch_packages) > 0:
        for search_res in search_for_md5s(
            conda,
            [x for x in link_actions if x["dist_name"] in non_fetch_packages],
            spec.platform,
        ):
            dist_name = fn_to_dist_name(search_res["fn"])
            fetch_by_dist_name[dist_name] = search_res

    for pkg in link_actions:
        url = fetch_by_dist_name[pkg["dist_name"]]["url"]
        md5 = fetch_by_dist_name[pkg["dist_name"]]["md5"]
        lockfile_contents.append(f"{url}#{md5}")

    return lockfile_contents


def main_on_docker(env_file, platforms):
    env_path = pathlib.Path(env_file)
    platform_arg = []
    for p in platforms:
        platform_arg.extend(["--platform", p])

    subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{str(env_path.parent)}:/work:rwZ",
            "--workdir",
            "/work",
            "conda-lock:latest",
            "--file",
            env_path.name,
            *platform_arg,
        ]
    )


def parse_source_files(
    src_files: List[pathlib.Path], platform: str, include_dev_dependencies: bool
) -> List[LockSpecification]:
    desired_envs = []
    for src_file in src_files:
        if src_file.name == "meta.yaml":
            desired_envs.append(
                parse_meta_yaml_file(src_file, platform, include_dev_dependencies)
            )
        elif src_file.name == "pyproject.toml":
            desired_envs.append(
                parse_pyproject_toml(src_file, platform, include_dev_dependencies)
            )
        else:
            desired_envs.append(parse_environment_file(src_file, platform))
    return desired_envs


def aggregate_lock_specs(lock_specs: List[LockSpecification]) -> LockSpecification:
    # union the dependencies
    specs = list(
        set(chain.from_iterable([lock_spec.specs for lock_spec in lock_specs]))
    )

    # pick the first non-empty channel
    channels: List[str] = next(
        (lock_spec.channels for lock_spec in lock_specs if lock_spec.channels), []
    )

    # pick the first non-empty platform
    platform = next(
        (lock_spec.platform for lock_spec in lock_specs if lock_spec.platform), ""
    )

    return LockSpecification(specs=specs, channels=channels, platform=platform)


def _determine_conda_executable(conda_executable: Optional[str], no_mamba: bool):
    if conda_executable:
        if pathlib.Path(conda_executable).exists():
            yield conda_executable
        yield shutil.which(conda_executable)
    _conda_exe = ensureconda.ensureconda(
        mamba=not no_mamba,
        # micromamba doesn't support --override-channels
        micromamba=False,
        conda=True,
        conda_exe=True,
    )
    yield _conda_exe


def determine_conda_executable(conda_executable: Optional[str], no_mamba: bool):
    for candidate in _determine_conda_executable(conda_executable, no_mamba):
        if candidate is not None:
            return candidate
    raise RuntimeError("Could not find conda (or compatible) executable")


def run_lock(
    environment_files: List[pathlib.Path],
    conda_exe: Optional[str],
    platforms: Optional[List[str]] = None,
    no_mamba: bool = False,
    include_dev_dependencies: bool = True,
    channel_overrides: Optional[Sequence[str]] = None,
) -> None:
    _conda_exe = determine_conda_executable(conda_exe, no_mamba=no_mamba)
    make_lock_files(
        conda=_conda_exe,
        src_files=environment_files,
        platforms=platforms or DEFAULT_PLATFORMS,
        include_dev_dependencies=include_dev_dependencies,
        channel_overrides=channel_overrides,
    )


@click.group(cls=DefaultGroup, default="lock", default_if_no_args=True)
def main():
    """To get help for subcommands, use the conda-lock <SUBCOMMAND> --help"""
    pass


@main.command("lock")
@click.option(
    "--conda", default=None, help="path (or name) of the conda/mamba executable to use."
)
@click.option("--no-mamba", is_flag=True, help="don't attempt to use or install mamba.")
@click.option(
    "-p",
    "--platform",
    multiple=True,
    help="generate lock files for the following platforms",
)
@click.option(
    "-c",
    "--channel",
    "channel_overrides",
    multiple=True,
    help="""Override the channels to use when solving the environment. These will replace the channels as listed in the various source files.""",
)
@click.option(
    "--dev-dependencies/--no-dev-dependencies",
    is_flag=True,
    default=True,
    help="include dev dependencies in the lockfile (where applicable)",
)
@click.option(
    "-f",
    "--file",
    "files",
    default=["environment.yml"],
    type=click.Path(),
    multiple=True,
    help="path to a conda environment specification(s)",
)
# @click.option(
#     "-m",
#     "--mode",
#     type=click.Choice(["default", "docker"], case_sensitive=True),
#     default="default",
#     help="""
#             Run this conda-lock in an isolated docker container.  This may be
#             required to account for some issues where conda-lock conflicts with
#             existing condarc configurations.""",
# )
def lock(conda, no_mamba, platform, channel_overrides, dev_dependencies, files):
    """Generate fully reproducible lock files for conda environments."""
    files = [pathlib.Path(file) for file in files]
    run_lock(
        environment_files=files,
        conda_exe=conda,
        platforms=platform,
        no_mamba=no_mamba,
        include_dev_dependencies=dev_dependencies,
        channel_overrides=channel_overrides,
    )


@main.command("install")
@click.option(
    "--conda", default=None, help="path (or name) of the conda/mamba executable to use."
)
@click.option("--no-mamba", is_flag=True, help="don't attempt to use or install mamba.")
@click.option("-p", "--prefix", help="Full path to environment location (i.e. prefix).")
@click.option("-n", "--name", help="Name of environment.")
@click.argument("lock-file")
def install(conda, no_mamba, prefix, name, lock_file):
    """Perform a conda install"""
    _conda_exe = determine_conda_executable(conda, no_mamba=no_mamba)
    do_conda_install(conda=_conda_exe, prefix=prefix, name=name, file=lock_file)


if __name__ == "__main__":
    main()
