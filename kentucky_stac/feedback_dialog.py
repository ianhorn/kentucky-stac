"""The Feedback dialog and Help link, matching the ArcGIS Pro add-ins' Feedback/Help pair.

Two panels share one dialog: the choice panel offers "Report an Issue on GitHub" (opens the browser,
needs an account, closes the dialog) or "Send Feedback Directly" (no account; swaps in a small
message form in place, posted to Formspree in the background on Send)."""

from __future__ import annotations

import configparser
import json
import os
import platform
from typing import Optional

from qgis.core import Qgis, QgsApplication, QgsBlockingNetworkRequest, QgsTask
from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .feedback import (
    DOCS_SITE_URL,
    FORMSPREE_ENDPOINT,
    REPO_URL,
    direct_payload,
    environment_summary,
    github_issue_url,
    summarize_error,
)

_NEUTRAL = "color: #555555;"
_ERROR = "color: #b22222;"
_SUCCESS = "color: #1e7e34;"


def plugin_version() -> str:
    parser = configparser.ConfigParser()
    try:
        parser.read(os.path.join(os.path.dirname(__file__), "metadata.txt"), encoding="utf-8")
        return parser.get("general", "version", fallback="unknown")
    except configparser.Error:
        return "unknown"


def current_environment() -> str:
    return environment_summary(plugin_version(), Qgis.version(), platform.platform())


def open_help(parent=None):
    if not QDesktopServices.openUrl(QUrl(DOCS_SITE_URL)):
        QMessageBox.warning(parent, "Kentucky STAC", f"Couldn't open the browser.\n\nThe docs are at {DOCS_SITE_URL}")


class SendFeedbackTask(QgsTask):
    """POST the feedback to Formspree. `callback(ok, error)` runs on the main thread unless cancelled."""

    def __init__(self, payload: dict, callback):
        super().__init__("Sending Kentucky STAC feedback")
        self._payload = payload
        self._callback = callback
        self.error: Optional[str] = None

    def run(self) -> bool:
        request = QNetworkRequest(QUrl(FORMSPREE_ENDPOINT))
        request.setRawHeader(b"Content-Type", b"application/json")
        request.setRawHeader(b"Accept", b"application/json")
        request.setRawHeader(b"Referer", DOCS_SITE_URL.encode("ascii"))
        blocking = QgsBlockingNetworkRequest()
        code = blocking.post(request, QByteArray(json.dumps(self._payload).encode("utf-8")), True)
        if int(getattr(code, "value", code)) == 0:
            return True
        reply = blocking.reply()
        body = bytes(reply.content()).decode("utf-8", "replace")
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        detail = summarize_error(body) if body.strip() else blocking.errorMessage()
        self.error = f"Formspree returned {status}: {detail}" if status else detail
        return False

    def finished(self, result: bool) -> None:
        if self.isCanceled():
            return
        self._callback(result, self.error)


class FeedbackDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Feedback")
        self.setMinimumWidth(420)
        self._task: Optional[SendFeedbackTask] = None

        heading = QLabel("<b>Have a bug, an idea, or a question about Kentucky STAC?</b>")
        heading.setWordWrap(True)

        # ---- choice panel
        self.github_button = self._choice_button(
            "Report an Issue on GitHub", "Opens your browser. Needs a free GitHub account; anyone can read it."
        )
        self.github_button.clicked.connect(self._open_github)
        self.direct_button = self._choice_button(
            "Send Feedback Directly", "No account needed. Goes straight to the developer, not publicly visible."
        )
        self.direct_button.clicked.connect(self._show_direct)
        self.choice_panel = QWidget()
        choice_layout = QVBoxLayout(self.choice_panel)
        choice_layout.setContentsMargins(0, 0, 0, 0)
        choice_layout.addWidget(self.github_button)
        choice_layout.addWidget(self.direct_button)

        # ---- direct-message panel
        self.message_edit = QPlainTextEdit()
        self.message_edit.setFixedHeight(110)
        self.email_edit = QLineEdit()
        attached = QLabel("The plugin, QGIS and operating system versions are attached automatically.")
        attached.setWordWrap(True)
        attached.setStyleSheet(_NEUTRAL)
        self.direct_panel = QWidget()
        direct_layout = QVBoxLayout(self.direct_panel)
        direct_layout.setContentsMargins(0, 0, 0, 0)
        direct_layout.addWidget(QLabel("Message"))
        direct_layout.addWidget(self.message_edit)
        direct_layout.addWidget(QLabel("Your email (optional -- only if you'd like a reply)"))
        direct_layout.addWidget(self.email_edit)
        direct_layout.addWidget(attached)
        self.direct_panel.setVisible(False)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setVisible(False)

        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self._show_choice)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._send)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.send_button)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(self.choice_panel)
        layout.addWidget(self.direct_panel)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        self._show_choice()

    @staticmethod
    def _choice_button(title: str, detail: str) -> QPushButton:
        button = QPushButton(f"{title}\n{detail}")
        button.setStyleSheet("QPushButton { text-align: left; padding: 8px 10px; }")
        return button

    # ---- panels -----------------------------------------------------------------------------

    def _show_choice(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.direct_panel.setVisible(False)
        self.choice_panel.setVisible(True)
        self.back_button.setVisible(False)
        self.send_button.setVisible(False)
        self.status.setVisible(False)
        self._set_sending(False)

    def _show_direct(self):
        self.choice_panel.setVisible(False)
        self.direct_panel.setVisible(True)
        self.back_button.setVisible(True)
        self.send_button.setVisible(True)
        self.send_button.setDefault(True)
        self.message_edit.setFocus()

    def _show_status(self, text: str, style: str):
        self.status.setText(text)
        self.status.setStyleSheet(style)
        self.status.setVisible(True)

    def _set_sending(self, sending: bool):
        for w in (self.send_button, self.back_button, self.message_edit, self.email_edit):
            w.setEnabled(not sending)

    # ---- actions ----------------------------------------------------------------------------

    def _open_github(self):
        if not QDesktopServices.openUrl(QUrl.fromEncoded(github_issue_url(current_environment()).encode("ascii"))):
            QMessageBox.warning(
                self, "Kentucky STAC", f"Couldn't open the browser.\n\nYou can file an issue directly at {REPO_URL}/issues."
            )
            return
        self.accept()

    def _send(self):
        message = self.message_edit.toPlainText().strip()
        if not message:
            self._show_status("Enter a message first.", _ERROR)
            return
        self._set_sending(True)
        self._show_status("Sending...", _NEUTRAL)
        payload = direct_payload(message, self.email_edit.text(), current_environment())
        self._task = SendFeedbackTask(payload, self._on_sent)
        QgsApplication.taskManager().addTask(self._task)

    def _on_sent(self, ok: bool, error: Optional[str]):
        self._task = None
        if ok:
            self.direct_panel.setVisible(False)
            self.back_button.setVisible(False)
            self.send_button.setVisible(False)
            self._show_status("Thanks -- your feedback was sent.", _SUCCESS)
        else:
            self._set_sending(False)
            self._show_status(f"Couldn't send that: {error}", _ERROR)

    def done(self, result: int):
        if self._task is not None:
            self._task.cancel()
            self._task = None
        super().done(result)
