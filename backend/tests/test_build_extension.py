"""The store-package build (scripts/build_extension.py).

Covers what a store would otherwise be the one to tell us: the store
manifest, the checks that fail a build, and that the same source always zips
to the same bytes.
"""

import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_extension as build  # noqa: E402

DEV_MANIFEST: dict[str, Any] = json.loads(
    (REPO_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8")
)


def make_extension(root: Path, manifest: dict[str, Any] | None = None) -> Path:
    """A tiny but complete extension tree using the real manifest."""
    manifest = manifest or DEV_MANIFEST
    files = build._referenced_files(manifest) + [
        build.SPEECH_RUNTIME_MARKER,
        "app/app.mjs",
        "tests/unit.test.mjs",
        "spike/throwaway.mjs",
        "icons/make_icons.py",
    ]
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            (
                '<!doctype html><script type="module" src="./app.mjs"></script>'
                if name.endswith(".html")
                else "export const ok = true;\n"
            ),
            encoding="utf-8",
        )
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


class TestStoreManifest:
    def test_stamps_the_version_and_keeps_the_service_worker(self) -> None:
        manifest = build.store_manifest(DEV_MANIFEST, "1.2.3")
        assert manifest["version"] == "1.2.3"
        assert "browser_specific_settings" not in manifest
        assert manifest["background"] == {
            "service_worker": "background/service_worker.js",
            "type": "module",
        }

    def test_installing_asks_for_no_host_at_all(self) -> None:
        manifest = build.store_manifest(DEV_MANIFEST, "1.2.3")
        assert "host_permissions" not in manifest
        # The backend's addresses and Gemini are asked for when first needed.
        optional = manifest["optional_host_permissions"]
        assert "http://127.0.0.1/*" in optional
        assert "https://generativelanguage.googleapis.com/*" in optional
        # WebSockets need no host permission, so none is requested for them.
        assert not [host for host in optional if host.startswith("ws")]

    def test_the_development_manifest_is_left_untouched(self) -> None:
        before = json.dumps(DEV_MANIFEST, sort_keys=True)
        build.store_manifest(DEV_MANIFEST, "9.9.9")
        assert json.dumps(DEV_MANIFEST, sort_keys=True) == before


class TestFiles:
    def test_tests_spikes_and_tooling_are_left_out(self, tmp_path: Path) -> None:
        files = build.collect_files(make_extension(tmp_path))
        assert "app/app.mjs" in files
        assert build.SPEECH_RUNTIME_MARKER in files
        assert not [name for name in files if name.startswith(("tests/", "spike/"))]
        assert "icons/make_icons.py" not in files
        assert "manifest.json" not in files  # written separately


class TestLint:
    def lint(self, tmp_path: Path, **changes: Any) -> list[str]:
        root = make_extension(tmp_path)
        manifest = build.store_manifest({**DEV_MANIFEST, **changes}, "1.0.0")
        return build.lint_manifest(manifest, build.collect_files(root))

    def test_the_real_manifest_is_clean(self, tmp_path: Path) -> None:
        assert self.lint(tmp_path) == []

    def test_firefox_keys_fail(self, tmp_path: Path) -> None:
        problems = self.lint(
            tmp_path,
            browser_specific_settings={"gecko": {"id": "x@example.org"}},
            background={
                **DEV_MANIFEST["background"],
                "scripts": ["background/service_worker.js"],
            },
        )
        assert any("browser_specific_settings" in problem for problem in problems)
        assert any("background.scripts" in problem for problem in problems)

    def test_a_description_over_the_store_limit_fails(self, tmp_path: Path) -> None:
        problems = self.lint(tmp_path, description="x" * 133)
        assert any(
            "description" in problem and "133" in problem for problem in problems
        )

    def test_an_unexpected_permission_fails(self, tmp_path: Path) -> None:
        problems = self.lint(
            tmp_path, permissions=[*DEV_MANIFEST["permissions"], "history"]
        )
        assert any(
            "history" in problem and "justified" in problem for problem in problems
        )

    def test_a_missing_file_named_by_the_manifest_fails(self, tmp_path: Path) -> None:
        root = make_extension(tmp_path)
        (root / "popup" / "popup.html").unlink()
        manifest = build.store_manifest(DEV_MANIFEST, "1.0.0")
        problems = build.lint_manifest(manifest, build.collect_files(root))
        assert any("popup/popup.html" in problem for problem in problems)

    def test_chrome_needs_the_speech_runtime_vendored(self, tmp_path: Path) -> None:
        root = make_extension(tmp_path)
        (root / build.SPEECH_RUNTIME_MARKER).unlink()
        manifest = build.store_manifest(DEV_MANIFEST, "1.0.0")
        problems = build.lint_manifest(manifest, build.collect_files(root))
        assert any("vendor_stt_runtime.py" in problem for problem in problems)

    def test_a_policy_allowing_eval_fails(self, tmp_path: Path) -> None:
        problems = self.lint(
            tmp_path,
            content_security_policy={
                "extension_pages": "script-src 'self' 'unsafe-eval'"
            },
        )
        assert any("eval or remote hosts" in problem for problem in problems)

    def test_a_bad_version_and_a_pinned_key_fail(self, tmp_path: Path) -> None:
        root = make_extension(tmp_path)
        manifest = build.store_manifest({**DEV_MANIFEST, "key": "abc"}, "1.0-beta")
        problems = build.lint_manifest(manifest, build.collect_files(root))
        assert any("version" in problem for problem in problems)
        assert any("'key'" in problem for problem in problems)


class TestCodeScan:
    def scan(self, tmp_path: Path, name: str, text: str) -> list[str]:
        root = make_extension(tmp_path)
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return build.scan_code(root, build.collect_files(root))

    def test_the_fixture_tree_is_clean(self, tmp_path: Path) -> None:
        root = make_extension(tmp_path)
        assert build.scan_code(root, build.collect_files(root)) == []

    @pytest.mark.parametrize(
        ("text", "what"),
        [
            (
                'import x from "https://cdn.example.com/x.js";\n',
                "imports a module from a URL",
            ),
            (
                'const m = await import("https://cdn.example.com/x.js");\n',
                "dynamically imports",
            ),
            ('importScripts("https://cdn.example.com/x.js");\n', "importScripts"),
            ("const two = eval('1 + 1');\n", "eval()"),
            ('const f = new Function("return 1");\n', "new Function()"),
        ],
    )
    def test_remote_and_dynamic_code_is_found(
        self, tmp_path: Path, text: str, what: str
    ) -> None:
        problems = self.scan(tmp_path, "app/bad.mjs", text)
        assert (
            len(problems) == 1
            and what in problems[0]
            and "app/bad.mjs:1" in problems[0]
        )

    def test_html_may_not_load_or_inline_script(self, tmp_path: Path) -> None:
        remote = self.scan(
            tmp_path,
            "app/remote.html",
            '<script src="https://cdn.example.com/x.js"></script>',
        )
        assert any(
            "loads https://cdn.example.com/x.js" in problem for problem in remote
        )
        inline = self.scan(tmp_path, "app/inline.html", "<script>alert(1)</script>")
        assert any("inline <script>" in problem for problem in inline)

    def test_comments_urls_in_strings_and_vendor_code_are_not_flagged(
        self, tmp_path: Path
    ) -> None:
        assert (
            self.scan(
                tmp_path,
                "app/fine.mjs",
                '// never eval(input) or import from "https://x"\n'
                'const docs = "https://example.com/help";\n'
                "const retrieval = 3;\n",
            )
            == []
        )
        # Vendored third-party code is checked at vendoring time, not here.
        assert self.scan(tmp_path, "vendor/lib/lib.js", "eval('1')\n") == []


class TestVersion:
    def test_explicit_beats_manifest(self, tmp_path: Path) -> None:
        assert build.resolve_version("2.0.1", DEV_MANIFEST, tmp_path) == "2.0.1"

    def test_falls_back_to_the_manifest_outside_a_tagged_repo(
        self, tmp_path: Path
    ) -> None:
        assert build.resolve_version(None, {"version": "0.4"}, tmp_path) == "0.4"

    def test_rejects_what_chrome_would_reject(self, tmp_path: Path) -> None:
        with pytest.raises(build.BuildError, match="dot-separated integers"):
            build.resolve_version("1.0.0-rc1", DEV_MANIFEST, tmp_path)


class TestBuild:
    def test_builds_a_package_and_a_reproducible_zip(self, tmp_path: Path) -> None:
        source = make_extension(tmp_path / "extension")
        first = build.build_package(
            extension_dir=source, out_dir=tmp_path / "one", version="1.0.0"
        )
        second = build.build_package(
            extension_dir=source, out_dir=tmp_path / "two", version="1.0.0"
        )
        assert first["sha256"] == second["sha256"]
        with zipfile.ZipFile(first["zip"]) as archive:
            names = archive.namelist()
            packed = json.loads(archive.read("manifest.json"))
        assert names == sorted(names)
        assert "manifest.json" in names and "app/app.mjs" in names
        assert not [name for name in names if name.startswith(("tests/", "spike/"))]
        assert packed["version"] == "1.0.0"
        assert "host_permissions" not in packed

    def test_check_only_writes_nothing(self, tmp_path: Path) -> None:
        source = make_extension(tmp_path / "extension")
        out_dir = tmp_path / "out"
        result = build.build_package(
            extension_dir=source,
            out_dir=out_dir,
            version="1.0.0",
            check_only=True,
        )
        assert result["files"] > 0 and not out_dir.exists()

    def test_a_problem_stops_the_build_and_lists_every_one(
        self, tmp_path: Path
    ) -> None:
        source = make_extension(
            tmp_path / "extension", {**DEV_MANIFEST, "description": "y" * 200}
        )
        (source / "app" / "bad.mjs").write_text("eval('1')\n", encoding="utf-8")
        with pytest.raises(build.BuildError) as raised:
            build.build_package(
                extension_dir=source,
                out_dir=tmp_path / "out",
                version="1.0.0",
            )
        message = str(raised.value)
        assert (
            "2 problem(s)" in message
            and "description" in message
            and "eval()" in message
        )
        assert not (tmp_path / "out").exists()
