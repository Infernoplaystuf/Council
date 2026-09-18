"""
Specialists — the tab whose Tk form destroys the widget that is calling it.

The detail pane is destroyed and rebuilt on every selection AND on every
Enabled toggle, and the Enabled checkbox's own handler triggers one of those
rebuilds. Tk tolerates a widget destroying itself inside its own callback. Qt
does not — that is a use-after-free.

The Qt form is built once and repopulated, so the question cannot arise. These
tests hold that, and the four smaller defects around it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import specialists_ops as ops  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Spec:
    def __init__(self, id="sales", name="Sales Specialist", icon="💰",
                 description="money things", keywords=("sales", "revenue"),
                 overlay="think commercially", base="writer", enabled=True):
        self.id, self.name, self.icon = id, name, icon
        self.description = description
        self.domain_keywords = list(keywords)
        self.system_prompt_overlay = overlay
        self.base_personality = base
        self.enabled = enabled


# ============================================================
# The shared half
# ============================================================

def test_a_slug_is_stable_and_safe():
    """The id is the address — it is what the Council tab's pin resolves to
    and what the registry stores under."""
    assert ops.slugify("Sales Specialist!") == "sales_specialist"
    assert ops.slugify("  Q3 / Q4  ") == "q3_q4"
    assert ops.slugify("") == "specialist"
    assert len(ops.slugify("x" * 200)) <= 40


def test_keywords_keep_the_users_order():
    """Re-sorting would make the box appear to rewrite itself on save."""
    assert ops.parse_keywords(" zulu , alpha ,, mike ") == ["zulu", "alpha",
                                                            "mike"]
    assert ops.parse_keywords("") == []
    assert ops.format_keywords(["a", "b"]) == "a, b"


def test_a_specialist_with_no_keywords_is_refused():
    """They are the whole auto-summon mechanism; without them the specialist
    can only ever be reached by pinning it by hand."""
    problem = ops.check_specialist("Sales", [])
    assert "at least one domain keyword" in problem
    assert "summon" in problem


def test_a_specialist_with_no_name_is_refused():
    assert "Give the specialist a name" in ops.check_specialist("", ["x"])


def test_a_valid_specialist_has_no_complaint():
    assert ops.check_specialist("Sales", ["sales"]) is None


def test_the_base_personalities_are_the_tk_list_in_order():
    """A user picks by position."""
    assert ops.BASE_PERSONALITIES == ("writer", "sage", "strategist",
                                      "intern", "coder", "content")


def test_a_draft_round_trips_a_specialist():
    draft = ops.SpecialistDraft.of(Spec())
    assert draft.id == "sales"
    assert draft.keywords == ["sales", "revenue"]
    assert draft.base == "writer"
    assert draft.enabled is True


def test_a_specialist_missing_fields_still_drafts():
    class Bare:
        id = "x"
        name = "X"

    draft = ops.SpecialistDraft.of(Bare())
    assert draft.icon == ops.DEFAULT_ICON
    assert draft.base == ops.DEFAULT_BASE
    assert draft.keywords == []


def test_the_delete_prompt_says_what_is_not_lost():
    """A user who thinks their data is at stake will not press it."""
    text = ops.confirm_delete_text("Sales Specialist")
    assert "Sales Specialist" in text
    assert "NOT touched" in text
    assert "only a lens" in text


def test_the_management_label_shows_the_on_off_state():
    """Deliberately different from the pin label: this list is where a user
    turns specialists on and off, so the state belongs in the row."""
    assert "✓" in ops.list_label(Spec(enabled=True))
    assert "(off)" in ops.list_label(Spec(enabled=False))
    assert "(off)" not in ops.pin_label(Spec(enabled=False)), (
        "the chooser shows management state it has no use for")


def test_toggling_enabled_is_its_own_operation():
    """The Tk checkbox saves the WHOLE specialist as the form currently shows
    it, so toggling Enabled commits whatever half-typed edits are in the
    boxes."""
    import inspect
    source = inspect.getsource(ops.set_enabled)
    assert "domain_keywords" not in source
    assert "system_prompt_overlay" not in source


# ============================================================
# The Qt view
# ============================================================

pytest.importorskip("PySide6", reason="the Qt tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def tab(qapp, tmp_path):
    from council_qt.tabs.specialists import SpecialistsActions, SpecialistsTab

    saved = []

    class Actions(SpecialistsActions):
        def __init__(self):
            super().__init__(vault_dir=tmp_path)
            self.items = [Spec(), Spec(id="stock", name="Inventory",
                                       icon="📦", keywords=("stock",))]
            self.enabled_calls = []

        def load(self):
            return ops.SpecialistList(
                True, f"{len(self.items)} specialist(s).",
                labels=[ops.pin_label(s) for s in self.items],
                by_label={ops.pin_label(s): s.id for s in self.items},
                specialists=self.items)

        def save(self, draft):
            saved.append(draft)
            return ops.OpResult(True, f"Saved “{draft.name}”.")

        def set_enabled(self, specialist_id, enabled):
            self.enabled_calls.append((specialist_id, enabled))
            return ops.OpResult(True, "Enabled." if enabled else "Disabled.")

        def delete(self, specialist_id):
            return ops.OpResult(True, "Deleted.")

        def pool_counts(self):
            return 7, tmp_path / "data_in"

    widget = SpecialistsTab(actions=Actions())
    widget.saved = saved
    yield widget
    widget.deleteLater()


def test_the_form_is_built_once_and_never_destroyed(tab, qapp):
    """THE DEFECT. The Tk pane is destroyed and rebuilt on every selection and
    on every Enabled toggle, and the checkbox's own handler triggers one — so
    the widget currently executing destroys itself. Qt calls that a
    use-after-free."""
    first = tab.description
    tab.list.setCurrentRow(0)
    qapp.processEvents()
    tab.list.setCurrentRow(1)
    qapp.processEvents()
    assert tab.description is first, "the form was rebuilt on selection"
    assert first.parent() is not None, "the old form was destroyed"


def test_toggling_enabled_does_not_touch_the_form(tab, qapp):
    """The rebuild the Tk toggle triggers is what destroys the checkbox that
    is still running."""
    tab.list.setCurrentRow(0)
    qapp.processEvents()
    tab.description.setText("half-typed edit")
    tab.enabled.setChecked(False)
    qapp.processEvents()
    assert tab.description.text() == "half-typed edit", (
        "toggling Enabled rebuilt the form and lost the edit")


def test_toggling_enabled_writes_only_that(tab, qapp):
    tab.list.setCurrentRow(0)
    qapp.processEvents()
    tab.description.setText("not saved")
    tab.enabled.setChecked(False)
    qapp.processEvents()
    assert tab.actions.enabled_calls == [("sales", False)]
    assert tab.saved == [], "toggling Enabled committed the form"


def test_selecting_a_specialist_fills_the_form(tab, qapp):
    tab.list.setCurrentRow(0)
    qapp.processEvents()
    assert tab.description.text() == "money things"
    assert tab.keywords.text() == "sales, revenue"
    assert tab.overlay.toPlainText() == "think commercially"
    assert tab.base.currentText() == "writer"


def test_the_form_starts_disabled_until_something_is_selected(qapp, tmp_path):
    from council_qt.tabs.specialists import SpecialistsActions, SpecialistsTab

    class Empty(SpecialistsActions):
        def __init__(self):
            super().__init__(vault_dir=tmp_path)

        def load(self):
            return ops.SpecialistList(True, "none", specialists=[])

        def pool_counts(self):
            return 0, tmp_path

    widget = SpecialistsTab(actions=Empty())
    assert not widget.description.isEnabled()
    widget.deleteLater()


def test_unsaved_edits_are_detectable(tab, qapp):
    """The Tk form has no such notion: selecting another specialist destroys
    it and the edits with it, silently."""
    tab.list.setCurrentRow(0)
    qapp.processEvents()
    assert not tab.has_unsaved_edits()
    tab.description.setText("something new")
    assert tab.has_unsaved_edits()


def test_saving_sends_the_form_not_the_stored_values(tab, qapp):
    tab.list.setCurrentRow(0)
    qapp.processEvents()
    tab.keywords.setText("sales, refunds, q3")
    tab.on_save()
    assert tab.saved[-1].keywords == ["sales", "refunds", "q3"]
    assert tab.saved[-1].id == "sales", "the id was regenerated on save"


def test_the_pool_footer_says_the_data_is_shared(tab):
    """A specialist owns no data. Users assume otherwise, which is why the
    footer exists."""
    assert "shared pool" in tab.pool.text()
    assert "same vault" in tab.pool.text()
