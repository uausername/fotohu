"""The folder-layout matrix — the feature the family actually configures."""

from __future__ import annotations

from datetime import datetime

import pytest

from fotohu.core.models import FolderMode, Group, Person
from fotohu.core.naming import (
    dedupe_filename,
    owner_segment,
    render_template,
    sanitize_filename,
    sanitize_path,
    slugify,
)
from fotohu.core.pipeline import resolve_destination
from fotohu.services.settings import Settings


def person(name="Дмитрий", **kwargs) -> Person:
    return Person(id=kwargs.pop("id", 1), name=name, **kwargs)


class TestSlugify:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Дмитрий", "dmitrii"),
            ("Anna Smith", "anna-smith"),
            ("Мама и Папа", "mama-i-papa"),
            ("  spaces  ", "spaces"),
            ("!!!", "user"),
            ("Ёлка", "elka"),
        ],
    )
    def test_transliterates_and_normalises(self, value, expected):
        assert slugify(value) == expected


class TestSanitize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("photo.jpg", "photo.jpg"),
            ("../../etc/passwd", "passwd"),
            ("a/b/c.jpg", "c.jpg"),
            ("bad:name?.jpg", "bad_name_.jpg"),
            ("CON.jpg", "_CON.jpg"),
            ("", "file"),
            ("...", "file"),
        ],
    )
    def test_filenames_cannot_escape_or_break_providers(self, value, expected):
        assert sanitize_filename(value) == expected

    def test_path_traversal_is_stripped(self):
        assert sanitize_path("root/../../secret/a.jpg") == "root/secret/a.jpg"
        assert sanitize_path("//a///b//") == "a/b"

    def test_long_names_are_truncated_but_keep_extension(self):
        result = sanitize_filename("x" * 400 + ".jpg")
        assert result.endswith(".jpg")
        assert len(result) <= 130


class TestOwnerSegment:
    def test_per_person_uses_the_persons_slug(self):
        assert owner_segment(person(), None, FolderMode.PER_PERSON) == "dmitrii"

    def test_explicit_personal_folder_wins_over_the_slug(self):
        assert owner_segment(person(personal_folder="dad"), None, FolderMode.PER_PERSON) == "dad"

    def test_shared_collapses_everyone_into_one_folder(self):
        assert owner_segment(person(), None, FolderMode.SHARED) == ""

    def test_per_group_uses_the_group_folder(self):
        group = Group(id=1, name="Родители", folder="parents")
        assert owner_segment(person(), group, FolderMode.PER_GROUP) == "parents"

    def test_per_group_without_a_group_falls_back_to_personal(self):
        # Better a stray personal folder than silently mixing into a shared root.
        assert owner_segment(person(), None, FolderMode.PER_GROUP) == "dmitrii"

    def test_personal_override_beats_the_global_mode(self):
        subject = person(folder_mode_override=FolderMode.SHARED)
        assert owner_segment(subject, None, FolderMode.PER_PERSON) == ""

    def test_override_can_pull_one_person_into_a_group_folder(self):
        group = Group(id=1, name="Родители", folder="parents")
        subject = person(group_id=1, folder_mode_override=FolderMode.PER_GROUP)
        assert owner_segment(subject, group, FolderMode.PER_PERSON) == "parents"


class TestResolveDestination:
    taken = datetime(2026, 3, 14, 9, 26, 53)

    def resolve(self, mode, subject=None, group=None, **overrides):
        settings = Settings(folder_mode=mode, **overrides)
        return resolve_destination(
            person=subject or person(),
            group=group,
            settings=settings,
            root_folder="FotoHu",
            taken_at=self.taken,
            original_name="IMG_0042.JPG",
            compressed=False,
        )

    def test_per_person_layout(self):
        assert self.resolve(FolderMode.PER_PERSON) == (
            "FotoHu/dmitrii/2026/2026-03",
            "IMG_0042.JPG",
        )

    def test_shared_layout_drops_the_owner_segment(self):
        # {owner} expands to nothing and must not leave an empty path element.
        assert self.resolve(FolderMode.SHARED)[0] == "FotoHu/2026/2026-03"

    def test_group_layout(self):
        group = Group(id=1, name="Родители", folder="parents")
        subject = person(group_id=1)
        assert self.resolve(FolderMode.PER_GROUP, subject, group)[0] == (
            "FotoHu/parents/2026/2026-03"
        )

    def test_compressed_photos_land_in_their_own_subfolder(self):
        settings = Settings(folder_mode=FolderMode.PER_PERSON)
        directory, _ = resolve_destination(
            person=person(),
            group=None,
            settings=settings,
            root_folder="FotoHu",
            taken_at=self.taken,
            original_name="IMG_0042.JPG",
            compressed=True,
        )
        assert directory == "FotoHu/dmitrii/_compressed/2026/2026-03"

    def test_custom_templates_are_honoured(self):
        directory, filename = self.resolve(
            FolderMode.PER_PERSON,
            dir_template="{root}/{yyyy}",
            file_template="{yyyy-mm-dd}_{hhmmss}_{filename}",
        )
        assert directory == "FotoHu/2026"
        assert filename == "2026-03-14_092653_IMG_0042.JPG"

    def test_a_hostile_filename_cannot_escape_the_root(self):
        settings = Settings(folder_mode=FolderMode.PER_PERSON)
        directory, filename = resolve_destination(
            person=person(),
            group=None,
            settings=settings,
            root_folder="FotoHu",
            taken_at=self.taken,
            original_name="../../../../etc/shadow",
            compressed=False,
        )
        assert ".." not in directory and ".." not in filename
        assert filename == "shadow"


class TestTemplates:
    def test_unknown_placeholders_are_left_alone(self):
        assert render_template("{root}/{nope}", {"root": "a"}) == "a/{nope}"

    @pytest.mark.parametrize(
        ("attempt", "expected"),
        [(1, "a.jpg"), (2, "a (2).jpg"), (3, "a (3).jpg")],
    )
    def test_collision_suffixes(self, attempt, expected):
        assert dedupe_filename("a.jpg", attempt) == expected

    def test_collision_suffix_without_extension(self):
        assert dedupe_filename("README", 2) == "README (2)"
