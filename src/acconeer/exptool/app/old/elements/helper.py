from __future__ import annotations

import enum
import os
import sys
from argparse import SUPPRESS, ArgumentParser
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QCheckBox, QLineEdit, QPushButton


class LoadState(enum.Enum):
    UNLOADED = enum.auto()
    BUFFERED = enum.auto()
    LOADED = enum.auto()


class ErrorFormater:
    def __init__(self):
        pass

    def error_to_text(self, error):
        exc_type, exc_obj, exc_tb = sys.exc_info()
        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
        err_text = "File: {}<br>Line: {}<br>Error: {}".format(fname, exc_tb.tb_lineno, error)

        return err_text


class Count:
    def __init__(self, val=0):
        self.val = val

    def pre_incr(self):
        self.val += 1
        return self.val

    def post_incr(self):
        ret = self.val
        self.val += 1
        return ret

    def decr(self, val=1):
        self.val -= val

    def set_val(self, val):
        self.val = val


class ExptoolArgumentParser(ArgumentParser):
    def __init__(self):
        super().__init__()
        self.add_argument(
            "--purge-config",
            action="store_true",
            help="Remove Exptool-related files interactively.",
        )
        self.add_argument(
            "--no-config",
            action="store_false",
            dest="use_last_config",
            help="Runs Exptool without loading or saving gui configuration.",
        )
        self.add_argument(
            "--portable",
            action="store_true",
            help=SUPPRESS,  # makes option hidden
        )


class CalibrationUiState:
    """
    Presenter object that modifies the semi-passive view that is
    the calibration-related ui-elements
    """

    def __init__(
        self,
        load_btn: Optional[QPushButton] = None,
        save_btn: Optional[QPushButton] = None,
        clear_btn: Optional[QPushButton] = None,
        status_text: Optional[QLineEdit] = None,
        auto_apply_cb: Optional[QCheckBox] = None,
        apply_btn: Optional[QPushButton] = None,
    ):
        self._source: Optional[str] = None
        self._modified: bool = False
        self._auto_apply: bool = False
        self._scan_is_running: bool = False  # Will be updated from GUI

        self._load_btn = load_btn
        self._save_btn = save_btn
        self._clear_btn = clear_btn
        self._status_text = status_text
        self._auto_apply_cb = auto_apply_cb
        self._apply_btn = apply_btn
        self._update_ui_elements()

    def get_display_tooltip_text(self):
        """Produces a nice tooltip from the `self.source` field"""
        if self.source is None:
            return "No calibration loaded. Load from file or start a measurement to get one."

        if self.source == "Session":
            return "Calibration was created in this session and is not saved to a file."
        else:
            filename = self.source
            return f"Calibration is loaded from {filename}"

    def get_display_text(self):
        """
        What should be displayed in the "Calibration status" field. Should be one of
            1. <file_name>[*]
            2. Session[*]
            3. <empty string>, which makes the placeholder text show in case of a QLineEdit.
        Where the asterisk denotes if there are unsaved changes.
        """
        if self.source is None:
            return ""

        source_str = self.source

        if source_str != "Session":
            path = Path(source_str)

            file_str = path.name
            if path.parent != Path("."):
                # Truncated dir path. Whole path will be shown in tooltip.
                file_str = ".../" + file_str
            source_str = file_str

        mby_asterisk = "*" if self.modified else ""
        return f"{source_str}{mby_asterisk}"

    def set_scan_is_running(self, value: bool):
        self._scan_is_running = value
        self._update_ui_elements()

    def clear(self):
        self.source = None
        self.modified = False

    def save(self, save_destination):
        self.source = save_destination
        self.modified = False

    def load(self, source):
        self.source = source
        self.modified = False

    def _update_ui_elements(self):
        if self._load_btn:
            self._load_btn.setEnabled(self.load_button_enabled)

        if self._save_btn:
            self._save_btn.setEnabled(self.save_button_enabled)

        if self._clear_btn:
            self._clear_btn.setEnabled(self.clear_button_enabled)

        if self._status_text:
            self._status_text.setText(self.get_display_text())
            self._status_text.setToolTip(self.get_display_tooltip_text())
            if self.is_display_text_italic:
                self._status_text.setStyleSheet("QLineEdit { font: italic }")
            else:
                self._status_text.setStyleSheet("")

        if self._auto_apply_cb:
            self._auto_apply_cb.setChecked(self._auto_apply)

        if self._apply_btn:
            self._apply_btn.setEnabled(self.apply_button_enabled)

    @property
    def source(self):
        return self._source

    @source.setter
    def source(self, value):
        self._source = value
        self._update_ui_elements()

    @property
    def modified(self):
        return self._modified

    @modified.setter
    def modified(self, value):
        self._modified = value
        self._update_ui_elements()

    @property
    def is_display_text_italic(self):
        """It's pretty nice if the text is in italics if there are unsaved changes."""
        return self.modified

    @property
    def load_button_enabled(self):
        """
        The load button should be enabled if the current calibration is cleared or saved
        I.e. calibration has no unsaved changes state.
        """
        return not self.modified

    @property
    def save_button_enabled(self):
        """The save button should be enabled if there are unsaved changes."""
        return self.modified

    @property
    def _has_source(self):
        return self.source is not None

    clear_button_enabled = _has_source

    @property
    def apply_button_enabled(self):
        """The apply button should only work in a "LIVE" scenario."""
        return self._has_source and self._scan_is_running

    @property
    def auto_apply(self):
        return self._auto_apply

    @auto_apply.setter
    def auto_apply(self, auto_apply):
        self._auto_apply = auto_apply
        self._update_ui_elements()
