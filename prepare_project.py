#!/usr/bin/env python3
"""Materialize Defects4C recipe entries as self-contained project inputs.

The project-specific knowledge stays in ``defectsc_tpl/projects*``.  This script
turns a selected bug entry into a buggy source checkout under ``data/`` and
renders the recipe's build/test templates into a project-local validation
contract understood by Debugging-Framework.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parent
RECIPES_ROOT = ROOT / "defectsc_tpl"
DEFAULT_DATA_ROOT = ROOT / "data"
DEFAULT_CACHE_ROOT = ROOT / "out_tmp_dirs"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Materialize one recipe/bug as a direct Debugging-Framework project input."
    )
    result.add_argument(
        "recipe",
        help="Recipe name (e.g. CESNET___libyang) or its projects*/<name> directory.",
    )
    selection = result.add_mutually_exclusive_group()
    selection.add_argument(
        "--bug",
        help="Unique type.id or full/prefix commit_after.",
    )
    selection.add_argument(
        "--all",
        action="store_true",
        help="Materialize every bug version declared by this recipe.",
    )
    selection.add_argument(
        "--list",
        action="store_true",
        help="List all available versions in this recipe and exit.",
    )
    result.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    result.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    result.add_argument("--jobs", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    result.add_argument(
        "--build",
        action="store_true",
        help="Run the rendered build recipe after checkout. Dependencies/toolchain must exist.",
    )
    result.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing materialized target.",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.jobs < 1:
        raise SystemExit("--jobs must be >= 1")
    recipe_dir = find_recipe(args.recipe)
    project_meta = read_json(recipe_dir / "project.json")
    bugs_path = recipe_dir / "bugs_list_new.json"
    if not bugs_path.is_file():
        bugs_path = recipe_dir / "bugs_list.json"
    bugs = read_json(bugs_path)
    if not isinstance(bugs, list):
        raise ValueError(f"Bug list must be an array: {bugs_path}")
    if args.list:
        print_bug_versions(bugs)
        return 0
    if not args.bug and not args.all:
        raise ValueError("choose --bug, --all, or --list")
    project_name = str(project_meta.get("repo_name") or recipe_dir.name)
    selected_bugs = bugs if args.all else [select_bug(bugs, args.bug)]
    data_root = args.data_root.expanduser().resolve()
    cache_root = args.cache_root.expanduser().resolve()
    materialized = []
    for index, bug in enumerate(selected_bugs, start=1):
        print(
            f"[{index}/{len(selected_bugs)}] materialize "
            f"{(bug.get('type') or {}).get('id') or bug.get('commit_after')}",
            file=sys.stderr,
        )
        record = materialize_bug(
            bug=bug,
            recipe_dir=recipe_dir,
            project_meta=project_meta,
            project_name=project_name,
            data_root=data_root,
            cache_root=cache_root,
            jobs=args.jobs,
            build=args.build,
            force=args.force,
        )
        materialized.append(record)
        print(record["project_path"])
    write_collection_manifest(data_root, project_name, recipe_dir, materialized)
    return 0


def materialize_bug(
    *,
    bug: dict,
    recipe_dir: Path,
    project_meta: dict,
    project_name: str,
    data_root: Path,
    cache_root: Path,
    jobs: int,
    build: bool,
    force: bool,
) -> dict:
    commit_after = required_sha(bug, "commit_after")
    commit_before = required_sha(bug, "commit_before")
    bug_label = str((bug.get("type") or {}).get("id") or commit_after[:12])
    target_name = safe_name(f"{project_name}__{bug_label}__{commit_after[:12]}")
    target = data_root / target_name

    source_repo = ensure_source_repo(
        cache_root=cache_root,
        project_name=project_name,
        remote=str(project_meta.get("main_repo") or ""),
        commits=(commit_after, commit_before),
    )
    materialize_worktree(
        source_repo=source_repo,
        target=target,
        commit_after=commit_after,
        commit_before=commit_before,
        source_files=source_files(bug),
        force=force,
    )
    write_validation_contract(
        target=target,
        recipe_dir=recipe_dir,
        project_meta=project_meta,
        bug=bug,
        jobs=jobs,
    )
    write_manifest(
        target=target,
        recipe_dir=recipe_dir,
        bug_label=bug_label,
        commit_after=commit_after,
        commit_before=commit_before,
        source_files=source_files(bug),
    )

    if build:
        run_checked(
            ["bash", ".debugging-framework/recipe_build.sh"],
            cwd=target,
            timeout=60 * 60,
        )

    return {
        "project_name": project_name,
        "bug_id": bug_label,
        "commit_after": commit_after,
        "commit_before": commit_before,
        "source_files": source_files(bug),
        "project_path": str(target),
        "contract": str(target / ".debugging-framework.json"),
        "built": build,
    }


def write_collection_manifest(
    data_root: Path,
    project_name: str,
    recipe_dir: Path,
    materialized: list[dict],
) -> Path:
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / f"{safe_name(project_name)}__materialized.json"
    payload = {
        "version": 1,
        "project_name": project_name,
        "recipe": str(recipe_dir),
        "version_count": len(materialized),
        "projects": materialized,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def find_recipe(value: str) -> Path:
    direct = Path(value).expanduser()
    if direct.is_dir():
        candidate = direct.resolve()
        if (candidate / "project.json").is_file():
            return candidate
    matches = [
        path
        for group in ("projects_v1", "projects")
        for path in [RECIPES_ROOT / group / value]
        if (path / "project.json").is_file()
    ]
    if len(matches) != 1:
        raise ValueError(f"Recipe not found or ambiguous: {value}")
    return matches[0].resolve()


def select_bug(bugs: list[dict], selector: str) -> dict:
    selector = selector.strip()
    exact_sha = [bug for bug in bugs if str(bug.get("commit_after") or "") == selector]
    if len(exact_sha) == 1:
        return exact_sha[0]
    prefix_sha = [
        bug for bug in bugs
        if str(bug.get("commit_after") or "").startswith(selector) and len(selector) >= 7
    ]
    if len(prefix_sha) == 1:
        return prefix_sha[0]
    by_id = [
        bug for bug in bugs if str((bug.get("type") or {}).get("id") or "") == selector
    ]
    if len(by_id) == 1:
        return by_id[0]
    matches = exact_sha or prefix_sha or by_id
    if matches:
        shas = ", ".join(str(item.get("commit_after")) for item in matches)
        raise ValueError(f"Bug selector is ambiguous ({selector}); use commit SHA: {shas}")
    raise ValueError(f"Bug selector not found: {selector}")


def print_bug_versions(bugs: list[dict]) -> None:
    print("bug_id\tcommit_after\tcommit_before\tsource_files")
    for bug in bugs:
        bug_id = str((bug.get("type") or {}).get("id") or "")
        after = str(bug.get("commit_after") or "")
        before = str(bug.get("commit_before") or "")
        sources = ",".join(source_files(bug))
        print(f"{bug_id}\t{after}\t{before}\t{sources}")


def ensure_source_repo(
    *, cache_root: Path, project_name: str, remote: str, commits: tuple[str, str]
) -> Path:
    project_cache = cache_root / project_name
    exact = project_cache / f"git_repo_dir_{commits[0]}"
    candidates = [exact] if exact.is_dir() else []
    candidates.extend(
        path for path in sorted(project_cache.glob("git_repo_dir*")) if path not in candidates
    )
    for candidate in candidates:
        if all(git_has_commit(candidate, sha) for sha in commits):
            return candidate.resolve()

    if not remote:
        raise FileNotFoundError(
            f"No cached repo contains both commits for {project_name}, and project.json has no main_repo"
        )
    exact.parent.mkdir(parents=True, exist_ok=True)
    if not (exact / ".git").exists():
        run_checked(["git", "clone", "--no-checkout", remote, str(exact)], timeout=60 * 30)
    for sha in commits:
        if not git_has_commit(exact, sha):
            run_checked(["git", "-C", str(exact), "fetch", "origin", sha], timeout=60 * 20)
    return exact.resolve()


def materialize_worktree(
    *,
    source_repo: Path,
    target: Path,
    commit_after: str,
    commit_before: str,
    source_files: list[str],
    force: bool,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not force:
            raise FileExistsError(f"Target already exists (use --force): {target}")
        shutil.rmtree(target)
    run_checked(
        [
            "git", "clone", "--no-local", "--no-checkout",
            "--upload-pack=git -c safe.directory=* upload-pack",
            str(source_repo), str(target),
        ],
        timeout=60 * 10,
    )
    run_checked(["git", "-C", str(target), "checkout", "--detach", commit_after], timeout=300)
    if not source_files:
        raise ValueError("Bug entry has no files.src; cannot construct buggy checkout")
    for relpath in source_files:
        completed = subprocess.run(
            [
                "git", "-c", "safe.directory=*", "-C", str(source_repo),
                "show", f"{commit_before}:{relpath}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Cannot read buggy source {commit_before}:{relpath}: {detail}")
        destination = target / relpath
        if not destination.is_file():
            raise FileNotFoundError(f"Buggy overlay target does not exist: {destination}")
        mode = destination.stat().st_mode
        destination.write_bytes(completed.stdout)
        destination.chmod(mode)


def write_validation_contract(
    *,
    target: Path,
    recipe_dir: Path,
    project_meta: dict,
    bug: dict,
    jobs: int,
) -> None:
    contract_dir = target / ".debugging-framework"
    contract_dir.mkdir(parents=True, exist_ok=True)
    context = recipe_context(project_meta, bug, jobs)
    build_template = resolve_template(recipe_dir, context.get("build"), "common_build_tpl.jinja")
    test_template = resolve_template(recipe_dir, context.get("test"), "common_test_tpl.jinja")
    build_body = render(build_template, {**context, "is_rebuild": False})
    test_body = render(test_template, context)
    exports = render_exports(context.get("env") or [])

    write_executable(
        contract_dir / "recipe_build_impl.sh",
        "#!/usr/bin/env bash\nset -o pipefail\n" + exports + build_body + "\n",
    )
    write_executable(
        contract_dir / "recipe_test_impl.sh",
        "#!/usr/bin/env bash\nset -o pipefail\n" + exports + test_body + "\n",
    )
    write_executable(
        contract_dir / "recipe_build.sh",
        """#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
mkdir -p .debugging-framework/build
exec bash .debugging-framework/recipe_build_impl.sh \
  .debugging-framework/build .debugging-framework/build.log
""",
    )
    write_executable(
        contract_dir / "recipe_test.sh",
        """#!/usr/bin/env bash
set -uo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
log="$root/.debugging-framework/test.log"
status_file="$root/.debugging-framework/test.status"
msg_file="$root/.debugging-framework/test.msg"
rm -f "$log" "$status_file" "$msg_file"
bash .debugging-framework/recipe_test_impl.sh \
  "$root/.debugging-framework/build" "$log"
recipe_rc=$?
[[ -f "$msg_file" ]] && cat "$msg_file"
[[ -f "$log" ]] && cat "$log"
if [[ $recipe_rc -ne 0 ]]; then
  exit "$recipe_rc"
fi
if [[ ! -s "$status_file" ]]; then
  echo "recipe did not produce test status: $status_file" >&2
  exit 2
fi
status=$(tr '[:upper:]' '[:lower:]' < "$status_file")
if [[ "$status" == *failed* || "$status" == *error* ]]; then
  exit 1
fi
if [[ "$status" == *success* || "$status" == *pass* ]]; then
  exit 0
fi
echo "unrecognized recipe test status: $status" >&2
exit 2
""",
    )
    contract = {
        "system": "defects4c-rendered-recipe",
        "build": [["bash", ".debugging-framework/recipe_build.sh"]],
        "test": [["bash", ".debugging-framework/recipe_test.sh"]],
    }
    (target / ".debugging-framework.json").write_text(
        json.dumps(contract, indent=2) + "\n", encoding="utf-8"
    )


def recipe_context(project_meta: dict, bug: dict, jobs: int) -> dict:
    project_compile = dict(project_meta.get("c_compile") or {})
    bug_compile = dict(bug.get("c_compile") or {})

    def combined_list(name: str, outer: dict, inner: dict) -> list:
        values = []
        for value in (outer.get(name), inner.get(name)):
            if isinstance(value, list):
                values.extend(item for item in value if item is not None)
        return values

    merged = {
        **project_compile,
        **{key: value for key, value in bug_compile.items() if value is not None},
    }
    merged["build_flags"] = combined_list("build_flags", project_compile, bug_compile)
    merged["test_flags"] = combined_list("test_flags", project_compile, bug_compile)
    merged["env"] = combined_list("env", project_meta, bug_compile)
    return {
        **project_meta,
        **bug,
        **merged,
        "cpu_count": jobs,
        "test_files": list((bug.get("files") or {}).get("test") or []),
        "src_file": source_files(bug)[0] if source_files(bug) else "",
        "build_dir": ".debugging-framework/build",
        "test_log": ".debugging-framework/test.log",
        "apt_install_fn": apt_install_function(),
    }


def resolve_template(recipe_dir: Path, configured: object, common_name: str) -> Path:
    if isinstance(configured, str) and configured.endswith(".jinja"):
        candidate = recipe_dir / configured
    else:
        group_dir = recipe_dir.parent
        candidate = group_dir / common_name
    if not candidate.is_file():
        raise FileNotFoundError(f"Recipe template not found: {candidate}")
    return candidate


def render(path: Path, context: dict) -> str:
    environment = Environment(
        loader=FileSystemLoader(str(path.parent)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    return environment.get_template(path.name).render(**context)


def render_exports(values: list) -> str:
    lines = []
    for item in values:
        key, separator, value = str(item).partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid recipe environment entry: {item!r}")
        lines.append(f"export {key}={shlex.quote(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_manifest(
    *,
    target: Path,
    recipe_dir: Path,
    bug_label: str,
    commit_after: str,
    commit_before: str,
    source_files: list[str],
) -> None:
    payload = {
        "version": 1,
        "recipe": str(recipe_dir),
        "bug": bug_label,
        "commit_after": commit_after,
        "commit_before": commit_before,
        "buggy_overlay_files": source_files,
        "project_root": str(target),
    }
    (target / ".debugging-framework" / "materialization.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def source_files(bug: dict) -> list[str]:
    values = (bug.get("files") or {}).get("src") or []
    result = []
    for value in values:
        path = str(value).strip().replace("\\", "/")
        if not path or path.startswith("/") or ".." in Path(path).parts:
            raise ValueError(f"Invalid source path in recipe: {value!r}")
        result.append(path)
    return result


def required_sha(bug: dict, key: str) -> str:
    value = str(bug.get(key) or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
        raise ValueError(f"Bug entry has invalid {key}: {value!r}")
    return value


def git_has_commit(repo: Path, sha: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def run_checked(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 300,
    env: dict[str, str] | None = None,
) -> None:
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed ({completed.returncode}): {shlex.join(command)}")


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "project"


def apt_install_function() -> str:
    return """
apt_install_fn() {
    library=$1
    if dpkg -s "$library" &>/dev/null || command -v "$library" &>/dev/null; then
        return 0
    fi
    sudo apt-get update -y && sudo apt-get install -y "$library"
}
"""


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2)
