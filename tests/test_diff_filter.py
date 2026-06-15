"""Tests for diff filtering."""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from diff_filter import should_include, filter_files  # noqa: E402


class _File:
    def __init__(self, filename):
        self.filename = filename


class TestShouldInclude:
    def test_keeps_normal_source(self):
        assert should_include("src/app.py")

    def test_strips_lockfiles_and_minified(self):
        assert not should_include("yarn.lock")
        assert not should_include("dist/bundle.min.js")
        assert not should_include("a/b/styles.min.css")
        assert not should_include("out.js.map")

    def test_always_keeps_migrations_even_if_excluded(self):
        # Migration wins over an explicit exclude pattern.
        assert should_include("app/migrations/0003_add.py", exclude_patterns=["*.py"])
        assert should_include("db/schema.sql", ignore_files=["*.sql"])

    def test_applies_user_exclude_and_repo_ignore(self):
        assert not should_include("a.test.ts", ignore_files=["*.test.ts"])
        assert not should_include("vendor/x.go", exclude_patterns=["vendor/*"])

    def test_matches_on_basename(self):
        assert not should_include("deep/nested/package-lock.json")


class TestFilterFiles:
    def test_filters_and_caps(self):
        files = [
            _File("a.py"),
            _File("yarn.lock"),
            _File("b/migrations/1.py"),
            _File("c.min.js"),
            _File("d.py"),
        ]
        kept = filter_files(files, exclude_patterns=[], ignore_files=[], max_files=10)
        names = [f.filename for f in kept]
        assert names == ["a.py", "b/migrations/1.py", "d.py"]

    def test_max_files_cap(self):
        files = [_File(f"f{i}.py") for i in range(10)]
        kept = filter_files(files, max_files=3)
        assert len(kept) == 3
