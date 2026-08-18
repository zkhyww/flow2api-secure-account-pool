import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services.account_profile_store import AccountProfileStore


class AccountProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name) / "account_profiles"
        self.store = AccountProfileStore(self.root)

    def tearDown(self):
        self._temp_dir.cleanup()

    def test_create_key_is_opaque_unique_lowercase_uuid_hex(self):
        first = self.store.create_key()
        second = self.store.create_key()

        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertNotIn("fixture", first)
        self.assertNotIn("@", first)

    def test_resolve_is_stable_across_store_restarts_and_separates_accounts(self):
        first_key = self.store.create_key()
        second_key = self.store.create_key()
        first_path = self.store.resolve(first_key, create=True)
        second_path = self.store.resolve(second_key, create=True)

        restarted = AccountProfileStore(self.root)
        self.assertEqual(first_path, restarted.resolve(first_key))
        self.assertEqual(second_path, restarted.resolve(second_key))
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(first_path.is_dir())
        self.assertTrue(second_path.is_dir())

    def test_invalid_or_escaping_keys_are_rejected(self):
        invalid = [
            "",
            ".",
            "..",
            "../outside",
            "abc/def",
            "abc\\def",
            str((self.root / "absolute").resolve()),
            "not-a-uuid",
            "A" * 32,
        ]

        for key in invalid:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.store.resolve(key, create=True)

    def test_existing_symlink_profile_cannot_escape_root(self):
        key = self.store.create_key()
        self.root.mkdir(parents=True, exist_ok=True)
        outside = Path(self._temp_dir.name) / "outside"
        outside.mkdir()
        link = self.root / key
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

        with self.assertRaises(ValueError):
            self.store.resolve(key)
        with self.assertRaises(ValueError):
            self.store.resolve(key, create=True)

    def test_exists_is_false_for_missing_and_rejects_invalid_keys(self):
        key = self.store.create_key()
        self.assertFalse(self.store.exists(key))
        self.store.resolve(key, create=True)
        self.assertTrue(self.store.exists(key))
        with self.assertRaises(ValueError):
            self.store.exists("../outside")

    def test_clone_to_new_key_copies_existing_state_without_mutating_source(self):
        clone_to_new_key = getattr(self.store, "clone_to_new_key", None)
        self.assertTrue(callable(clone_to_new_key))
        source_key = self.store.create_key()
        source = self.store.resolve(source_key, create=True)
        (source / "synthetic-state.txt").write_text("preserved", encoding="utf-8")

        candidate_key = clone_to_new_key(source_key)

        self.assertNotEqual(source_key, candidate_key)
        self.assertEqual(
            "preserved",
            (self.store.resolve(candidate_key) / "synthetic-state.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "preserved",
            (self.store.resolve(source_key) / "synthetic-state.txt").read_text(encoding="utf-8"),
        )

    def test_clone_rejects_nested_symlink_and_leaves_no_candidate(self):
        clone_to_new_key = getattr(self.store, "clone_to_new_key", None)
        self.assertTrue(callable(clone_to_new_key))
        source_key = self.store.create_key()
        source = self.store.resolve(source_key, create=True)
        outside = Path(self._temp_dir.name) / "outside-file.txt"
        outside.write_text("outside", encoding="utf-8")
        link = source / "nested-link"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"file symlink unavailable: {exc}")
        before = {path.name for path in self.root.iterdir()}

        with self.assertRaises(ValueError):
            clone_to_new_key(source_key)

        after = {path.name for path in self.root.iterdir()}
        self.assertEqual(before, after)

    def test_remove_deletes_only_the_selected_profile(self):
        remove = getattr(self.store, "remove", None)
        self.assertTrue(callable(remove))
        first_key = self.store.create_key()
        second_key = self.store.create_key()
        self.store.resolve(first_key, create=True)
        self.store.resolve(second_key, create=True)

        remove(first_key)

        self.assertFalse(self.store.exists(first_key))
        self.assertTrue(self.store.exists(second_key))

    def test_remove_failure_is_marked_and_retried_on_store_restart(self):
        key = self.store.create_key()
        self.store.resolve(key, create=True)

        with patch(
            "src.services.account_profile_store.shutil.rmtree",
            side_effect=OSError("synthetic-lock"),
        ):
            self.store.remove(key)

        markers = [
            path
            for path in self.root.iterdir()
            if path.name.startswith(".cleanup-key-")
        ]
        self.assertEqual(1, len(markers))
        self.assertTrue(self.store.exists(key))

        restarted = AccountProfileStore(self.root)
        self.assertTrue(restarted.exists(key))
        restarted.create_key()

        self.assertFalse(restarted.exists(key))
        self.assertEqual(
            [],
            [
                path
                for path in self.root.iterdir()
                if path.name.startswith(".cleanup-key-")
            ],
        )


if __name__ == "__main__":
    unittest.main()
